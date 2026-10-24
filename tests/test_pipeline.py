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
        p = c.execute("SELECT * FROM projections WHERE player_key='lebronjames' AND market='points'").fetchone()
        self.assertEqual(p["model_version"], "baseline_v1")
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
        err = reports.projection_error(c)
        self.assertEqual(err[0]["mae"], 3.0)           # |27 - 24|

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
