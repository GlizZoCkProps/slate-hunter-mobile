"""Collector jobs. Each job opens a collection_runs row, logs every failure or
missing item to collection_errors, and closes the run with ok/partial/failed/skipped.

A failure in one sport/event never aborts the rest of the job.
"""
import re
import subprocess
import traceback
from datetime import timedelta

from . import __version__
from .config import iso_ct, iso_utc, parse_iso, utc_now, CENTRAL
from .sources import espn, odds_api
from .util import team_key

SOURCE_ESPN = "espn_public_api"
SOURCE_ODDS = "the_odds_api"


def _code_version():
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        sha = ""
    return f"{__version__}+{sha}" if sha else __version__


def redact(text):
    """Strip API keys from any error text before it is stored or printed."""
    return re.sub(r"(api[_-]?key=)[^&\s'\"]+", r"\1[REDACTED]", str(text), flags=re.I)


class Run:
    """Context manager around one collection_runs row."""

    def __init__(self, conn, job, now=None):
        self.conn, self.job, self.now = conn, job, now
        self.items = 0
        self.errors = 0
        self.missing = 0
        self.message = ""
        self.status = None
        self.quota_remaining = None
        self.quota_used = None

    def __enter__(self):
        now = self.now or utc_now()
        cur = self.conn.execute(
            "INSERT INTO collection_runs(job, started_at_utc, started_at_ct, status, code_version) VALUES (?,?,?,?,?)",
            (self.job, iso_utc(now), iso_ct(now), "running", _code_version()))
        self.id = cur.lastrowid
        self.conn.commit()
        return self

    def log(self, kind, source, scope, error_type, message):
        if kind == "error":
            self.errors += 1
        else:
            self.missing += 1
        self.conn.execute(
            "INSERT INTO collection_errors(run_id, at_utc, kind, source, scope, error_type, message) VALUES (?,?,?,?,?,?,?)",
            (self.id, iso_utc(self.now), kind, source, scope, error_type, redact(message)[:500]))

    def error(self, source, scope, exc):
        self.log("error", source, scope, type(exc).__name__, str(exc))

    def skip(self, message):
        self.status = "skipped"
        self.message = message

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.conn.rollback()
            self.log("error", "collector", self.job, exc_type.__name__, "".join(traceback.format_exception_only(exc_type, exc)))
            status = "failed"
        elif self.status:
            status = self.status
        elif self.errors and not self.items:
            status = "failed"
        elif self.errors:
            status = "partial"
        else:
            status = "ok"
        msg = self.message or f"{self.items} written, {self.errors} errors, {self.missing} missing"
        self.conn.execute(
            "UPDATE collection_runs SET finished_at_utc=?, status=?, items_written=?, error_count=?, message=?, "
            "quota_remaining=?, quota_used=? WHERE id=?",
            (iso_utc(), status, self.items, self.errors, msg, self.quota_remaining, self.quota_used, self.id))
        self.conn.commit()
        self.final_status = status
        # Job failures are recorded, never raised to the scheduler; Ctrl-C / SystemExit still propagate.
        return exc_type is None or issubclass(exc_type, Exception)


# ---------------------------------------------------------------- games

def upsert_game(conn, g, now_iso):
    start = parse_iso(g.get("start_time"))
    start_utc = iso_utc(start) if start else None
    start_ct = iso_ct(start) if start else None
    conn.execute(
        """INSERT INTO games(sport, espn_event_id, name, home_team, away_team, start_time_utc, start_time_ct, state,
                             status_detail, completed, home_score, away_score, source, source_url, first_seen_utc, last_updated_utc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(sport, espn_event_id) DO UPDATE SET
             name=excluded.name, home_team=excluded.home_team, away_team=excluded.away_team,
             start_time_utc=excluded.start_time_utc, start_time_ct=excluded.start_time_ct, state=excluded.state,
             status_detail=excluded.status_detail, completed=excluded.completed, home_score=excluded.home_score,
             away_score=excluded.away_score, source_url=excluded.source_url, last_updated_utc=excluded.last_updated_utc""",
        (g["sport"], g["espn_event_id"], g.get("name"), g.get("home_team"), g.get("away_team"), start_utc, start_ct,
         g.get("state"), g.get("status_detail"), 1 if g.get("completed") else 0, g.get("home_score"), g.get("away_score"),
         SOURCE_ESPN, g.get("source_url"), now_iso, now_iso))


def collect_games(conn, cfg, http, now=None):
    now = now or utc_now()
    with Run(conn, "collect_games", now) as run:
        today_ct = now.astimezone(CENTRAL).date()
        for sport in cfg.sports:
            if sport not in espn.PATHS:
                run.log("error", "config", sport, "UnknownSport", f"unsupported sport {sport}")
                continue
            # Include yesterday so late games get their final status.
            for offset in range(-1, cfg.days_ahead):
                day = today_ct + timedelta(days=offset)
                scope = f"{sport} {day.isoformat()}"
                params = {"dates": day.strftime("%Y%m%d"), "limit": 500}
                if sport == "CFB":
                    params["groups"] = "80"  # FBS (ESPN group id); omit to get only the default featured set
                try:
                    data, _ = http.get_json(espn.scoreboard_url(sport), params)
                    games = espn.parse_scoreboard(data, sport)
                except Exception as exc:
                    run.error(SOURCE_ESPN, scope, exc)
                    continue
                for g in games:
                    upsert_game(conn, g, iso_utc(now))
                    run.items += 1
        conn.commit()
    return run


# ---------------------------------------------------------------- injuries

def collect_injuries(conn, cfg, http, now=None):
    """Append injury rows only when a player's status/detail changed since the
    last observation for that game, so history is preserved without noise."""
    now = now or utc_now()
    with Run(conn, "collect_injuries", now) as run:
        horizon = iso_utc(now + timedelta(hours=cfg.props_lookahead_hours))
        games = conn.execute("SELECT * FROM games WHERE completed=0 AND start_time_utc>? AND start_time_utc<=? ORDER BY start_time_utc",
                             (iso_utc(now), horizon)).fetchall()
        if not games:
            run.skip("no upcoming games in lookahead window")
        for g in games:
            scope = f"{g['sport']} {g['espn_event_id']}"
            try:
                data, _ = http.get_json(espn.summary_url(g["sport"]), {"event": g["espn_event_id"]})
            except Exception as exc:
                run.error(SOURCE_ESPN, scope, exc)
                continue
            items = espn.parse_injuries(data, g["sport"])
            if not items:
                run.log("missing", SOURCE_ESPN, scope, "NoInjuryData", "summary returned no injury entries (feed may be empty or absent)")
            for it in items:
                last = conn.execute("SELECT status, detail FROM injury_reports WHERE game_id=? AND player_key=? ORDER BY id DESC LIMIT 1",
                                    (g["id"], it["player_key"])).fetchone()
                if last and last["status"] == it["status"] and last["detail"] == it["detail"]:
                    continue
                conn.execute("INSERT INTO injury_reports(run_id, collected_at_utc, sport, game_id, team, player_name, player_key, status, detail, source, source_url) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (run.id, iso_utc(now), g["sport"], g["id"], it["team"], it["player_name"], it["player_key"],
                              it["status"], it["detail"], SOURCE_ESPN, g["source_url"]))
                run.items += 1
        conn.commit()
    return run


# ---------------------------------------------------------------- props

def match_game(conn, sport, home, away, commence):
    start = parse_iso(commence)
    if not start:
        return None
    lo, hi = iso_utc(start - timedelta(hours=12)), iso_utc(start + timedelta(hours=12))
    cands = conn.execute("SELECT * FROM games WHERE sport=? AND start_time_utc BETWEEN ? AND ?", (sport, lo, hi)).fetchall()
    hk, ak = team_key(home), team_key(away)

    def same(a, b):
        return bool(a and b) and (a == b or a in b or b in a)

    for g in cands:
        if same(hk, team_key(g["home_team"])) and same(ak, team_key(g["away_team"])):
            return g
    for g in cands:  # neutral-site games can flip home/away between providers
        if same(hk, team_key(g["away_team"])) and same(ak, team_key(g["home_team"])):
            return g
    return None


def _last_quota(conn):
    row = conn.execute("SELECT quota_remaining FROM collection_runs WHERE job='collect_props' AND quota_remaining IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return row["quota_remaining"] if row else None


def _credits_today(conn, day_ct):
    row = conn.execute("SELECT COALESCE(SUM(COALESCE(reported_cost, estimated_cost)),0) c FROM odds_calls WHERE called_date_ct=? AND ok=1",
                       (day_ct,)).fetchone()
    return row["c"]


def _calls_for_event(conn, odds_event_id):
    return conn.execute("SELECT COUNT(*) n FROM odds_calls WHERE odds_event_id=? AND ok=1", (odds_event_id,)).fetchone()["n"]


def collect_props(conn, cfg, http, now=None):
    """Snapshot player props for upcoming games, soonest game first across ALL sports.

    Credit controls (each skip is logged in collection_errors as kind='missing'):
      SLATE_PROPS_PREGAME_WINDOW_MIN   only games starting within N minutes (0 = use lookahead)
      SLATE_PROPS_SNAPSHOTS_PER_EVENT  max paid calls per game (0 = unlimited)
      SLATE_ODDS_DAILY_CREDIT_BUDGET   max estimated credits per Central day (0 = none)
      SLATE_MAX_EVENTS_PER_RUN         max paid calls per run
      SLATE_ODDS_CREDIT_RESERVE        never go below this many remaining credits
    """
    now = now or utc_now()
    with Run(conn, "collect_props", now) as run:
        if not cfg.odds_api_key:
            run.log("missing", SOURCE_ODDS, "config", "NoApiKey", "ODDS_API_KEY not configured; no sportsbook/pick'em lines collected")
            run.skip("ODDS_API_KEY not configured")
            return run
        remaining = _last_quota(conn)
        books = cfg.books
        day_ct = now.astimezone(CENTRAL).date().isoformat()
        spent_today = _credits_today(conn, day_ct)
        if cfg.props_pregame_window_min > 0:
            horizon = now + timedelta(minutes=cfg.props_pregame_window_min)
        else:
            horizon = now + timedelta(hours=cfg.props_lookahead_hours)

        # 1. Gather candidate events from every sport (the events endpoint costs 0 credits).
        candidates = []
        for sport in cfg.sports:
            if sport not in odds_api.SPORT_KEYS:
                continue
            try:
                evs, _ = odds_api.events(http, cfg.odds_api_key, sport, commence_from=iso_utc(now), commence_to=iso_utc(horizon))
            except Exception as exc:
                run.error(SOURCE_ODDS, f"{sport} events", exc)
                continue
            for ev in evs or []:
                start = parse_iso(ev.get("commence_time"))
                if start and now < start <= horizon:
                    candidates.append((start, sport, ev))
        candidates.sort(key=lambda c: (c[0], c[1]))

        # 2. Spend credits on the soonest games first.
        events_done = 0
        for start, sport, ev in candidates:
            scope = f"{sport} {ev.get('away_team')} at {ev.get('home_team')} ({ev.get('id')})"
            markets = odds_api.DEFAULT_MARKETS[sport]
            provider = [odds_api.MARKETS[sport][m] for m in markets]
            cost = odds_api.estimated_cost(len(provider), books)
            if cfg.props_snapshots_per_event and _calls_for_event(conn, ev["id"]) >= cfg.props_snapshots_per_event:
                continue  # already has its snapshot(s); not a gap, so not logged
            if events_done >= cfg.max_events_per_run:
                run.log("missing", SOURCE_ODDS, scope, "RunCapReached", f"SLATE_MAX_EVENTS_PER_RUN={cfg.max_events_per_run} reached")
                continue
            if cfg.odds_daily_credit_budget and spent_today + cost > cfg.odds_daily_credit_budget:
                run.log("missing", SOURCE_ODDS, scope, "DailyBudget",
                        f"skipped: {spent_today} credits used today (CT {day_ct}), budget {cfg.odds_daily_credit_budget}, call ~{cost}")
                continue
            if remaining is not None and remaining - cost < cfg.odds_credit_reserve:
                run.log("missing", SOURCE_ODDS, scope, "CreditReserve",
                        f"skipped: {remaining} credits left, call costs ~{cost}, reserve {cfg.odds_credit_reserve}")
                continue
            game = match_game(conn, sport, ev.get("home_team"), ev.get("away_team"), ev.get("commence_time"))
            if game is None:
                run.log("missing", SOURCE_ODDS, scope, "NoEspnMatch", "no ESPN game matched; snapshots stored with game_id NULL")
            elif not game["odds_event_id"]:
                conn.execute("UPDATE games SET odds_event_id=? WHERE id=?", (ev.get("id"), game["id"]))
            try:
                data, headers = odds_api.event_odds(http, cfg.odds_api_key, sport, ev["id"], provider, books)
            except Exception as exc:
                run.error(SOURCE_ODDS, scope, exc)
                conn.execute("INSERT INTO odds_calls(run_id, called_at_utc, called_date_ct, sport, odds_event_id, commence_time_utc, estimated_cost, ok) "
                             "VALUES (?,?,?,?,?,?,?,0)", (run.id, iso_utc(now), day_ct, sport, ev["id"], iso_utc(start), cost))
                continue
            events_done += 1
            r, u = odds_api.quota(headers)
            if r is not None:
                remaining, run.quota_remaining, run.quota_used = r, r, u
            reported = odds_api.last_cost(headers)
            spent_today += reported if reported is not None else cost
            rows = odds_api.parse_event_odds(data, sport)
            conn.execute("INSERT INTO odds_calls(run_id, called_at_utc, called_date_ct, sport, odds_event_id, commence_time_utc, estimated_cost, reported_cost, rows_returned, ok) "
                         "VALUES (?,?,?,?,?,?,?,?,?,1)", (run.id, iso_utc(now), day_ct, sport, ev["id"], iso_utc(start), cost, reported, len(rows)))
            seen_books = {x["book_key"] for x in rows}
            for b in books:
                if b not in seen_books:
                    run.log("missing", SOURCE_ODDS, scope, "NoBookData", f"{b} returned no player props for {','.join(markets)}")
            ts_utc, ts_ct = iso_utc(now), iso_ct(now)
            for x in rows:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO prop_snapshots(run_id, collected_at_utc, collected_at_ct, source, book_key, book_title, region, sport, "
                    "game_id, odds_event_id, commence_time_utc, market, source_market_key, player_name, player_key, side, line, price_american, book_last_update_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run.id, ts_utc, ts_ct, SOURCE_ODDS, x["book_key"], x["book_title"], x["region"], sport,
                     game["id"] if game else None, ev["id"], iso_utc(start), x["market"], x["source_market_key"],
                     x["player_name"], x["player_key"], x["side"], x["line"], x["price_american"], x["book_last_update"]))
                run.items += cur.rowcount
            conn.commit()
        if not candidates:
            run.message = f"no games starting before {iso_utc(horizon)}"
        detect_moves(conn, run.id)
        conn.commit()
    return run


MOVE_NOTE = ("Observed change in this book's posted line/price between two of our snapshots. "
             "This is not evidence of who bet or why; no ticket or handle data is implied.")


def detect_moves(conn, run_id):
    """Write line_move / price_move signals for snapshots in run_id versus the
    previous run that observed the same (book, event, market, player, side).
    Skipped when either run has more than one line for that key (alt lines)."""
    keys = conn.execute("SELECT book_key, odds_event_id, market, player_key, side, COUNT(*) n FROM prop_snapshots WHERE run_id=? "
                        "GROUP BY 1,2,3,4,5", (run_id,)).fetchall()
    written = 0
    for k in keys:
        if k["n"] != 1:
            continue
        base = (k["book_key"], k["odds_event_id"], k["market"], k["player_key"], k["side"])
        cur = conn.execute("SELECT * FROM prop_snapshots WHERE run_id=? AND book_key=? AND odds_event_id=? AND market=? AND player_key=? AND side=?",
                           (run_id, *base)).fetchone()
        prev_run = conn.execute("SELECT MAX(run_id) r FROM prop_snapshots WHERE run_id<? AND book_key=? AND odds_event_id=? AND market=? AND player_key=? AND side=?",
                                (run_id, *base)).fetchone()["r"]
        if prev_run is None:
            continue
        prev = conn.execute("SELECT * FROM prop_snapshots WHERE run_id=? AND book_key=? AND odds_event_id=? AND market=? AND player_key=? AND side=?",
                            (prev_run, *base)).fetchall()
        if len(prev) != 1:
            continue
        prev = prev[0]
        if prev["line"] != cur["line"]:
            typ, a, b = "line_move", prev["line"], cur["line"]
        elif prev["price_american"] != cur["price_american"]:
            typ, a, b = "price_move", prev["price_american"], cur["price_american"]
        else:
            continue
        conn.execute("INSERT OR IGNORE INTO market_signals(observed_at_utc, sport, game_id, player_key, player_name, market, book_key, side, signal_type, "
                     "value_before, value_after, from_snapshot_id, to_snapshot_id, evidence_level, source, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cur["collected_at_utc"], cur["sport"], cur["game_id"], cur["player_key"], cur["player_name"], cur["market"], cur["book_key"],
                      cur["side"], typ, a, b, prev["id"], cur["id"], "observed_line_change", "slate_hunter_snapshots", MOVE_NOTE))
        written += 1
    return written


# ---------------------------------------------------------------- projections

def project(conn, cfg, http, now=None, max_players=60, refresh_hours=12):
    """baseline_v1_1 and shrink_v2 projections for players with snapshots in upcoming games.
    Uses ESPN regular-season game logs (current + prior season); one projection per
    (game, player, market, model) per refresh window."""
    from . import projections as pj
    now = now or utc_now()
    with Run(conn, "project", now) as run:
        todo = conn.execute(
            """SELECT s.sport, s.game_id, s.player_name, s.player_key, s.market, MAX(s.id) snap_id
               FROM prop_snapshots s JOIN games g ON g.id = s.game_id
               WHERE g.start_time_utc > ? AND g.completed = 0
               GROUP BY s.sport, s.game_id, s.player_key, s.market""", (iso_utc(now),)).fetchall()
        cutoff = iso_utc(now - timedelta(hours=refresh_hours))
        todo = [t for t in todo if not conn.execute(
            "SELECT 1 FROM projections WHERE game_id=? AND player_key=? AND market=? AND model_version=? AND created_at_utc>?",
            (t["game_id"], t["player_key"], t["market"], pj.PRIMARY_MODEL, cutoff)).fetchone()]
        if not todo:
            run.skip("nothing to project")
            return run
        logs = {}
        players_done = set()
        for t in todo:
            scope = f"{t['sport']} {t['player_name']} {t['market']}"
            pk = (t["sport"], t["player_key"])
            if pk not in players_done and len(players_done) >= max_players:
                run.log("missing", SOURCE_ESPN, scope, "RunCapReached", f"max {max_players} players per run")
                continue
            players_done.add(pk)
            try:
                aid, note = pj.resolve_athlete(conn, http, t["sport"], t["player_name"], t["player_key"], iso_utc(now))
            except Exception as exc:
                run.error(SOURCE_ESPN, scope, exc)
                continue
            if not aid:
                run.log("missing", SOURCE_ESPN, scope, "NoAthleteId", note)
                continue
            cur_season, prior_season = pj.season_for(t["sport"], now)
            for key, season in (("cur", None), ("prior", prior_season)):
                ck = (t["sport"], aid, key)
                if ck in logs:
                    continue
                try:
                    logs[ck] = http.get_json(pj.gamelog_url(t["sport"], aid), pj.gamelog_params(season))[0]
                except Exception as exc:
                    logs[ck] = None
                    if key == "cur":
                        run.error(SOURCE_ESPN, scope, exc)
                    else:
                        run.log("missing", SOURCE_ESPN, scope, "NoPriorSeason", f"season {season} game log unavailable: {type(exc).__name__}")
            data = logs[(t["sport"], aid, "cur")]
            if data is None:
                continue
            values, label = pj.gamelog_values(data, t["sport"], t["market"])
            _, _, excluded = pj.clean_gamelog(data)
            prior_values = []
            if logs[(t["sport"], aid, "prior")] is not None:
                prior_values, _ = pj.gamelog_values(logs[(t["sport"], aid, "prior")], t["sport"], t["market"])
            prior_mean = round(sum(prior_values) / len(prior_values), 3) if prior_values else None
            if not values and prior_mean is None:
                run.log("missing", SOURCE_ESPN, scope, "NoGamelogValues", f"no numeric values parsed ({label})")
                continue
            snap = conn.execute("SELECT line FROM prop_snapshots WHERE id=?", (t["snap_id"],)).fetchone()
            inputs = {"values_recent_first": values[:20], "metric_label": label, "preseason_games_excluded": excluded,
                      "prior_season": prior_season, "prior_season_games": len(prior_values), "prior_season_mean": prior_mean,
                      "shrink_k": pj.SHRINK_K, "gamelog": pj.gamelog_url(t["sport"], aid)}
            common = dict(sport=t["sport"], game_id=t["game_id"], player_name=t["player_name"], pkey=t["player_key"],
                          market=t["market"], inputs=inputs, athlete_id=aid,
                          reference_line=snap["line"] if snap else None, reference_snapshot_id=t["snap_id"], now=now)
            if values:
                pj.record_projection(conn, projection=pj.baseline_v1_1(values), sample_size=len(values),
                                     model_version="baseline_v1_1", **common)
                run.items += 1
            if prior_mean is None:
                run.log("missing", SOURCE_ESPN, scope, "NoPriorSeason", "no prior-season values; shrink_v2 equals baseline_v1_1")
            pj.record_projection(conn, projection=pj.shrink_v2(values, prior_mean), sample_size=len(values),
                                 model_version=pj.PRIMARY_MODEL, **common)
            run.items += 1
        conn.commit()
    return run
