"""Offline end-to-end tests. Run: python -m unittest discover -s tests -v"""
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixtures as fx  # noqa: E402
from slatehunter import collector, grading, reports, scheduler  # noqa: E402
from slatehunter.config import Config  # noqa: E402
from slatehunter.db import open_db  # noqa: E402
from slatehunter.http import FixtureClient, HttpError  # noqa: E402
from slatehunter.sources import espn  # noqa: E402
from slatehunter.util import no_vig_pair, player_key, profit_units  # noqa: E402

T0 = datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc)   # 12:00 CT, morning of game
T1 = datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc)   # later snapshot
T2 = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)    # 30 min before tip
T_AFTER = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)

SEARCH = {"results": [{"type": "player", "contents": [
    {"uid": "s:40~l:46~a:1966", "displayName": "LeBron James", "defaultLeagueSlug": "nba"}]}]}


def cfg_for(**kw):
    c = Config()
    c.db_path = ":memory:"
    c.sports = ["NBA"]
    c.books = ["draftkings", "fanduel", "prizepicks"]
    c.odds_api_key = "test-key"
    c.days_ahead = 1
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def client(odds_payload, summary=fx.NBA_SUMMARY_PRE, quota=fx.QUOTA):
    return FixtureClient([
        ("/scoreboard", fx.NBA_SCOREBOARD),
        ("/summary", summary),
        ("/events/oddsev1/odds", (odds_payload, quota)),
        ("/events/oddsev_unmatched/odds", ({"bookmakers": []}, quota)),
        ("/events", (fx.ODDS_EVENTS_NBA, {})),
        ("/search/v2", SEARCH),
        ("/gamelog", fx.NBA_GAMELOG),
    ])


class UtilTests(unittest.TestCase):
    def test_player_key(self):
        self.assertEqual(player_key("Jaren Jackson Jr."), player_key("Jaren Jackson"))
        self.assertEqual(player_key("Nikola Jokić"), "nikolajokic")
        self.assertEqual(player_key("D'Angelo Russell"), "dangelorussell")

    def test_redact(self):
        from slatehunter.collector import redact
        self.assertNotIn("secret", redact("GET /v4/x?apiKey=secret&markets=a"))

    def test_odds_math(self):
        a, b = no_vig_pair(-110, -110)
        self.assertAlmostEqual(a, 0.5)
        self.assertAlmostEqual(profit_units(-110), 100 / 110)
        self.assertAlmostEqual(profit_units(150), 1.5)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = open_db(":memory:")
        self.cfg = cfg_for()

    def test_full_cycle(self):
        c = self.conn
        # 1. games
        run = collector.collect_games(c, self.cfg, client(fx.odds_event()), now=T0)
        self.assertEqual(run.final_status, "ok")
        g = c.execute("SELECT * FROM games WHERE espn_event_id='401700001'").fetchone()
        self.assertEqual(g["start_time_utc"], "2026-10-25T02:30:00Z")
        self.assertTrue(g["start_time_ct"].startswith("2026-10-24T21:30:00-05:00"))

        # 2. props snapshot #1
        run = collector.collect_props(c, self.cfg, client(fx.odds_event(24.5, -115, -105)), now=T0)
        self.assertEqual(run.final_status, "ok")
        n1 = c.execute("SELECT COUNT(*) FROM prop_snapshots").fetchone()[0]
        self.assertEqual(n1, 8)  # DK: 4 pts + 2 reb; PP: 2 pts
        self.assertEqual(run.quota_remaining, 480)
        missing = {r["error_type"] for r in c.execute("SELECT error_type FROM collection_errors WHERE run_id=?", (run.id,))}
        self.assertIn("NoEspnMatch", missing)      # unmatched odds event is logged, not dropped
        self.assertIn("NoBookData", missing)       # fanduel returned nothing -> explicit missing row
        pp = c.execute("SELECT * FROM prop_snapshots WHERE book_key='prizepicks' AND side='over'").fetchone()
        self.assertIsNone(pp["price_american"])    # pick'em has no price; never invented
        self.assertEqual(pp["region"], "us_dfs")

        # 3. snapshot #2 with a moved line -> history kept + line_move signal
        collector.collect_props(c, self.cfg, client(fx.odds_event(25.5, -110, -110)), now=T1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM prop_snapshots").fetchone()[0], 16)
        moves = c.execute("SELECT * FROM market_signals WHERE signal_type='line_move' AND player_key='lebronjames'").fetchall()
        self.assertEqual(len(moves), 2)            # over and under at DK
        self.assertEqual((moves[0]["value_before"], moves[0]["value_after"]), (24.5, 25.5))
        self.assertEqual(moves[0]["evidence_level"], "observed_line_change")
        self.assertNotIn("sharp", (moves[0]["notes"] or "").lower().replace("not evidence", ""))

        # 4. injuries (append only on change)
        collector.collect_injuries(c, self.cfg, client(fx.odds_event()), now=T0)
        collector.collect_injuries(c, self.cfg, client(fx.odds_event()), now=T1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM injury_reports").fetchone()[0], 1)

        # 5. projections
        run = collector.project(c, self.cfg, client(fx.odds_event()), now=T1)
        p = c.execute("SELECT * FROM projections WHERE player_key='lebronjames' AND market='points' AND model_version='baseline_v1_1'").fetchone()
        v2 = c.execute("SELECT * FROM projections WHERE player_key='lebronjames' AND market='points' AND model_version='shrink_v2'").fetchone()
        self.assertAlmostEqual(v2["projection"], 24.0)   # fixture prior season mean is also 24
        self.assertAlmostEqual(p["projection"], 24.0)    # mean(26,22)=24 both terms
        self.assertIsNone(p["est_prob_over"])            # no calibrated probability claimed
        self.assertEqual(p["espn_athlete_id"], "1966")
        miss = {r["error_type"] for r in c.execute("SELECT error_type FROM collection_errors WHERE run_id=?", (run.id,))}
        self.assertIn("NoAthleteId", miss)               # Curry not in search fixture -> logged, no projection
        self.assertIsNone(c.execute("SELECT 1 FROM projections WHERE player_key='stephencurry'").fetchone())

        # 6. paper pick at the exact snapshot, then a closing snapshot
        snap = c.execute("SELECT id FROM prop_snapshots WHERE book_key='draftkings' AND player_key='lebronjames' AND market='points' "
                         "AND side='over' ORDER BY id DESC").fetchone()["id"]
        pick = reports.create_paper_pick(c, snap, stake=1.0, rationale="test", now=T1)
        collector.collect_props(c, self.cfg, client(fx.odds_event(26.5, -115, -105)), now=T2)

        cands = reports.candidates(c, now=T2)
        self.assertTrue(any(x["player_name"] == "LeBron James" and x["gap"] is not None for x in cands))

        # 7. grade after the game
        run = grading.grade(c, self.cfg, client(fx.odds_event(), summary=fx.NBA_SUMMARY_FINAL), now=T_AFTER)
        self.assertIn(run.final_status, ("ok", "partial"))
        grade = c.execute("SELECT * FROM prop_grades WHERE book_key='draftkings' AND player_key='lebronjames' AND market='points' AND side='over'").fetchone()
        self.assertEqual((grade["open_line"], grade["close_line"], grade["actual_value"], grade["outcome"]), (24.5, 26.5, 27.0, "win"))
        curry_under = c.execute("SELECT outcome FROM prop_grades WHERE player_key='stephencurry' AND side='under'").fetchone()
        self.assertEqual(curry_under["outcome"], "win")  # 27 < 27.5; never bet, still graded
        pr = c.execute("SELECT * FROM paper_picks WHERE id=?", (pick,)).fetchone()
        self.assertEqual(pr["outcome"], "win")
        self.assertAlmostEqual(pr["profit_units"], 100 / 110, places=4)
        self.assertEqual(pr["clv_line"], 1.0)          # took 25.5, closed 26.5 on an over

        # 8. reporting + export
        day, text = reports.daily_report(c, now=T_AFTER)
        self.assertIn("Collector health", text)
        buf = reports.export_csv(c, "prop_snapshots")
        self.assertEqual(len(buf.getvalue().strip().splitlines()), 1 + 24)
        err = {(e["model_version"], e["market"]): e for e in reports.projection_error(c)}
        self.assertEqual(err[("baseline_v1_1", "points")]["mae"], 3.0)   # |27 - 24|
        self.assertEqual(err[("shrink_v2", "points")]["mae"], 3.0)

    def test_failures_are_recorded(self):
        c = self.conn
        bad = FixtureClient([("/scoreboard", HttpError(503, "scoreboard"))])
        run = collector.collect_games(c, self.cfg, bad, now=T0)
        self.assertEqual(run.final_status, "failed")
        self.assertGreater(c.execute("SELECT COUNT(*) FROM collection_errors WHERE run_id=?", (run.id,)).fetchone()[0], 0)

    def test_no_api_key_is_explicit(self):
        run = collector.collect_props(self.conn, cfg_for(odds_api_key=""), client(fx.odds_event()), now=T0)
        self.assertEqual(run.final_status, "skipped")
        row = self.conn.execute("SELECT * FROM collection_errors WHERE run_id=?", (run.id,)).fetchone()
        self.assertEqual(row["error_type"], "NoApiKey")

    def test_credit_reserve_blocks_calls(self):
        c = self.conn
        collector.collect_games(c, self.cfg, client(fx.odds_event()), now=T0)
        low = {"x-requests-remaining": "26", "x-requests-used": "474"}
        collector.collect_props(c, self.cfg, client(fx.odds_event(), quota=low), now=T0)
        http = client(fx.odds_event(), quota=low)
        run = collector.collect_props(c, cfg_for(odds_credit_reserve=25), http, now=T1)
        self.assertFalse(any("/odds" in u for u, _ in http.calls))
        self.assertTrue(c.execute("SELECT 1 FROM collection_errors WHERE run_id=? AND error_type='CreditReserve'", (run.id,)).fetchone())

    def test_scheduler_intervals(self):
        c = self.conn
        due, active = scheduler.due_jobs(c, self.cfg, now=T0)
        self.assertIn("collect_games", due)
        scheduler.run_scheduled(c, self.cfg, client(fx.odds_event()), now=T0)
        due, active = scheduler.due_jobs(c, self.cfg, now=T0)
        self.assertNotIn("collect_games", due)

    def test_paper_pick_rejects_stale_or_started(self):
        c = self.conn
        collector.collect_games(c, self.cfg, client(fx.odds_event()), now=T0)
        collector.collect_props(c, self.cfg, client(fx.odds_event()), now=T0)
        sid = c.execute("SELECT id FROM prop_snapshots WHERE game_id IS NOT NULL LIMIT 1").fetchone()["id"]
        with self.assertRaises(ValueError):
            reports.create_paper_pick(c, sid, now=T2)        # 9.5h old snapshot
        with self.assertRaises(ValueError):
            reports.create_paper_pick(c, sid, now=T_AFTER)   # game started

    def test_splits_require_source(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(",".join(reports.SPLIT_FIELDS) + "\n")
            f.write("2026-10-24T18:00:00Z,NBA,LeBron James,points,draftkings,over,ticket_pct,62,Example,https://example.com/x\n")
            f.write("2026-10-24T18:00:00Z,NBA,LeBron James,points,draftkings,over,ticket_pct,62,Rumor,\n")
            f.write("2026-10-24T18:00:00Z,NBA,LeBron James,points,draftkings,over,sharp_money,1,Tweet,https://x.com\n")
        ok, rejected = reports.import_splits(self.conn, f.name)
        os.unlink(f.name)
        self.assertEqual(ok, 1)
        self.assertEqual(len(rejected), 2)


class CreditControlTests(unittest.TestCase):
    """Soonest-first across sports, pregame window, one snapshot per game, daily budget."""

    CFB_EVENTS = [{"id": "cfbev1", "commence_time": "2026-10-24T23:00:00Z", "home_team": "Texas Longhorns", "away_team": "Georgia Bulldogs"}]

    def http(self):
        cfb_odds = {"id": "cfbev1", "bookmakers": [{"key": "draftkings", "title": "DraftKings", "markets": [
            {"key": "player_pass_yds", "outcomes": [
                {"name": "Over", "description": "Arch Manning", "price": -115, "point": 240.5},
                {"name": "Under", "description": "Arch Manning", "price": -105, "point": 240.5}]}]}]}
        quota = {"x-requests-remaining": "450", "x-requests-used": "50", "x-requests-last": "3"}
        return FixtureClient([
            ("/scoreboard", fx.NBA_SCOREBOARD),
            ("/events/oddsev1/odds", (fx.odds_event(), quota)),
            ("/events/oddsev_unmatched/odds", ({"bookmakers": []}, quota)),
            ("/events/cfbev1/odds", (cfb_odds, quota)),
            ("basketball_nba/events", (fx.ODDS_EVENTS_NBA, {})),
            ("americanfootball_ncaaf/events", (self.CFB_EVENTS, {})),
        ])

    def setUp(self):
        self.conn = open_db(":memory:")
        collector.collect_games(self.conn, cfg_for(), self.http(), now=T0)

    def paid_calls(self, http):
        return [u for u, _ in http.calls if u.endswith("/odds")]

    def test_soonest_first_across_sports(self):
        cfg = cfg_for(sports=["NBA", "CFB"], max_events_per_run=1)
        h = self.http()
        run = collector.collect_props(self.conn, cfg, h, now=T0)
        self.assertEqual(len(self.paid_calls(h)), 1)
        self.assertIn("cfbev1", self.paid_calls(h)[0])   # 23:00Z CFB beats 02:30Z NBA even though NBA is listed first
        capped = self.conn.execute("SELECT COUNT(*) FROM collection_errors WHERE run_id=? AND error_type='RunCapReached'", (run.id,)).fetchone()[0]
        self.assertEqual(capped, 2)

    def test_pregame_window(self):
        cfg = cfg_for(props_pregame_window_min=90)
        h = self.http()
        collector.collect_props(self.conn, cfg, h, now=T0)          # tip is 9.5h away
        self.assertEqual(self.paid_calls(h), [])
        h = self.http()
        collector.collect_props(self.conn, cfg, h, now=datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc))  # 75 min before tip
        self.assertEqual(len(self.paid_calls(h)), 1)
        self.assertIn("oddsev1", self.paid_calls(h)[0])
        due, _ = scheduler.due_jobs(self.conn, cfg, now=datetime(2026, 10, 25, 1, 45, tzinfo=timezone.utc))
        self.assertIn("collect_props", due)                        # checked every tick in pregame mode

    def test_one_snapshot_per_game(self):
        cfg = cfg_for(props_pregame_window_min=180, props_snapshots_per_event=1)
        near = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
        h1, h2 = self.http(), self.http()
        collector.collect_props(self.conn, cfg, h1, now=near)
        collector.collect_props(self.conn, cfg, h2, now=near.replace(minute=59))
        self.assertEqual(len(self.paid_calls(h1)), 2)   # both NBA games in window
        self.assertEqual(self.paid_calls(h2), [])       # each already has its snapshot; no credits spent
        row = self.conn.execute("SELECT COUNT(*), SUM(reported_cost) FROM odds_calls").fetchone()
        self.assertEqual(tuple(row), (2, 6))

    def test_daily_budget(self):
        cfg = cfg_for(sports=["NBA", "CFB"], odds_daily_credit_budget=6)
        h = self.http()
        run = collector.collect_props(self.conn, cfg, h, now=T0)
        self.assertEqual(len(self.paid_calls(h)), 2)    # 3 + 3 credits, third game would exceed 6
        self.assertTrue(self.conn.execute("SELECT 1 FROM collection_errors WHERE run_id=? AND error_type='DailyBudget'", (run.id,)).fetchone())
        h = self.http()
        collector.collect_props(self.conn, cfg, h, now=T0.replace(hour=20))
        self.assertEqual(self.paid_calls(h), [])        # same Central day: budget already spent
        self.assertEqual(reports.health(self.conn, now=T0)["odds_credits_today_ct"]["credits"], 6)


class ProjectionModelTests(unittest.TestCase):
    LOG = {
        "labels": ["PTS"],
        "events": {"r1": {"gameDate": "2026-10-22T23:00Z"}, "r2": {"gameDate": "2026-10-26T23:00Z"},
                   "p1": {"gameDate": "2026-10-10T23:00Z"}, "p2": {"gameDate": "2026-10-12T23:00Z"}},
        "seasonTypes": [
            {"displayName": "2026-27 Regular Season", "categories": [{"events": [
                {"eventId": "r1", "stats": ["20"]}, {"eventId": "r2", "stats": ["30"]}]}]},   # oldest first on purpose
            {"displayName": "2026-27 Preseason", "categories": [{"events": [
                {"eventId": "p1", "stats": ["6"]}, {"eventId": "p2", "stats": ["8"]}]}]},
        ],
    }

    def test_preseason_excluded_and_sorted(self):
        from slatehunter import projections as pj
        values, _ = pj.gamelog_values(self.LOG, "NBA", "points")
        self.assertEqual(values, [30.0, 20.0])                 # newest first, no preseason
        legacy, _ = pj.gamelog_values(self.LOG, "NBA", "points", legacy=True)
        self.assertEqual(sorted(legacy), [6.0, 8.0, 20.0, 30.0])  # what baseline_v1 used to see
        self.assertEqual(pj.clean_gamelog(self.LOG)[2], 2)

    def test_shrink_v2(self):
        from slatehunter import projections as pj
        # current baseline 25 (n=2), prior 30, K=8 -> 0.2*25 + 0.8*30 = 29
        self.assertEqual(pj.shrink_v2([30.0, 20.0], 30.0), 29.0)
        self.assertEqual(pj.shrink_v2([30.0, 20.0], None), 25.0)
        self.assertEqual(pj.shrink_v2([], 30.0), 30.0)
        # weight on current grows with sample: n=24 -> 0.75 current
        self.assertAlmostEqual(pj.shrink_v2([20.0] * 24, 40.0), 25.0)

    def test_season_for(self):
        from slatehunter import projections as pj
        self.assertEqual(pj.season_for("NBA", T0), (2027, 2026))
        self.assertEqual(pj.season_for("NFL", T0), (2026, 2025))
        self.assertEqual(pj.season_for("NFL", datetime(2027, 1, 10, tzinfo=timezone.utc)), (2026, 2025))


class EspnParsingTests(unittest.TestCase):
    def test_nba_box(self):
        players = espn.parse_box_players(fx.NBA_SUMMARY_FINAL)
        lj = players["lebronjames"]
        self.assertEqual(espn.stat_value(lj, "NBA", "points")[0], 27.0)
        self.assertEqual(espn.stat_value(lj, "NBA", "three_pointers")[0], 2.0)
        self.assertEqual(espn.stat_value(lj, "NBA", "pra")[0], 44.0)
        self.assertIsNone(espn.stat_value(players["benchguy"], "NBA", "points")[0])

    def test_nfl_box(self):
        players = espn.parse_box_players(fx.NFL_SUMMARY_FINAL)
        self.assertEqual(espn.stat_value(players["patrickmahomes"], "NFL", "passing_yards")[0], 291.0)
        self.assertEqual(espn.stat_value(players["traviskelce"], "NFL", "receiving_yards")[0], 68.0)
        self.assertEqual(espn.stat_value(players["traviskelce"], "NFL", "receptions")[0], 7.0)
        v, reason = espn.stat_value(players["patrickmahomes"], "NFL", "rushing_yards")
        self.assertEqual(v, 0.0)
        self.assertIn("graded as 0", reason)

    def test_injuries(self):
        inj = espn.parse_injuries(fx.NBA_SUMMARY_PRE, "NBA")
        self.assertEqual(inj[0]["status"], "Questionable")


class WebAppTests(unittest.TestCase):
    """Existing endpoints still import and respond; tracker endpoints read the DB."""

    def test_app(self):
        tmp = tempfile.mkdtemp()
        os.environ["SLATE_DB_PATH"] = os.path.join(tmp, "t.db")
        conn = open_db(os.environ["SLATE_DB_PATH"])
        collector.collect_games(conn, cfg_for(), client(fx.odds_event()), now=T0)
        conn.close()
        import app as webapp
        c = webapp.app.test_client()
        self.assertEqual(c.get("/api/health").status_code, 200)
        self.assertEqual(c.get("/").status_code, 200)
        self.assertEqual(c.post("/api/player-search", json={"query": "a"}).status_code, 400)
        s = c.get("/api/tracker/status").get_json()
        self.assertEqual(s["table_counts"]["games"], 2)
        r = c.get("/api/export/games.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("espn_event_id", r.get_data(as_text=True))
        self.assertEqual(c.get("/api/export/sqlite_master.csv").status_code, 404)
        self.assertEqual(c.get("/tracker").status_code, 200)


if __name__ == "__main__":
    unittest.main()
