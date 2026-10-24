"""Post-game grading: store box-score results, grade every tracked prop (bet or
not), and settle paper picks with closing-line value."""
from datetime import timedelta

from .collector import Run, SOURCE_ESPN
from .config import iso_utc, utc_now
from .sources import espn
from .util import no_vig_pair, profit_units


def _outcome(side, line, actual):
    if actual is None or line is None:
        return None
    if side in ("over", "yes"):
        return "win" if actual > line else "loss" if actual < line else "push"
    return "win" if actual < line else "loss" if actual > line else "push"


def store_results(conn, game, summary, now_iso):
    players = espn.parse_box_players(summary)
    n = 0
    for pk, p in players.items():
        for market in espn.markets_for(game["sport"]):
            value, reason = espn.stat_value(p, game["sport"], market)
            if value is None and not p["did_not_play"]:
                continue  # stat absent: do not write a fake zero
            conn.execute(
                "INSERT INTO results(game_id, sport, espn_athlete_id, player_name, player_key, team, market, actual_value, did_not_play, source, collected_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id, player_key, market) DO UPDATE SET "
                "actual_value=excluded.actual_value, did_not_play=excluded.did_not_play, collected_at_utc=excluded.collected_at_utc",
                (game["id"], game["sport"], p["athlete_id"], p["name"], pk, p["team"], market, value,
                 1 if p["did_not_play"] else 0, f"{SOURCE_ESPN}: {reason}", now_iso))
            n += 1
    return n, players


def _close_snapshot(conn, game, book, market, pkey, side):
    row = conn.execute("SELECT * FROM prop_snapshots WHERE game_id=? AND book_key=? AND market=? AND player_key=? AND side=? "
                       "AND collected_at_utc < ? ORDER BY collected_at_utc DESC, id DESC LIMIT 1",
                       (game["id"], book, market, pkey, side, game["start_time_utc"])).fetchone()
    return row


def _opposite_price(conn, snap):
    opp = {"over": "under", "under": "over", "yes": "no", "no": "yes"}[snap["side"]]
    row = conn.execute("SELECT price_american FROM prop_snapshots WHERE run_id=? AND book_key=? AND market=? AND player_key=? AND side=? AND line IS ?",
                       (snap["run_id"], snap["book_key"], snap["market"], snap["player_key"], opp, snap["line"])).fetchone()
    return row["price_american"] if row else None


def side_no_vig(conn, snap):
    if snap is None or snap["price_american"] is None:
        return None
    p, _ = no_vig_pair(snap["price_american"], _opposite_price(conn, snap))
    return p


def grade_props(conn, game, now_iso):
    keys = conn.execute("SELECT DISTINCT book_key, market, player_key, side FROM prop_snapshots WHERE game_id=?", (game["id"],)).fetchall()
    n = 0
    for k in keys:
        snaps = conn.execute("SELECT * FROM prop_snapshots WHERE game_id=? AND book_key=? AND market=? AND player_key=? AND side=? "
                             "ORDER BY collected_at_utc, id", (game["id"], k["book_key"], k["market"], k["player_key"], k["side"])).fetchall()
        open_s = snaps[0]
        close_s = _close_snapshot(conn, game, *k) or snaps[-1]
        res = conn.execute("SELECT * FROM results WHERE game_id=? AND player_key=? AND market=?",
                           (game["id"], k["player_key"], k["market"])).fetchone()
        if res is None:
            outcome, reason, actual = "ungraded", "player/stat not found in ESPN box score (inactive, name mismatch, or stat absent)", None
        elif res["did_not_play"]:
            outcome, reason, actual = "void", "did not play (ESPN box score)", None
        else:
            actual = res["actual_value"]
            outcome, reason = _outcome(k["side"], close_s["line"], actual), "graded at closing snapshot line"
        conn.execute(
            """INSERT INTO prop_grades(game_id, book_key, market, player_key, player_name, side, open_snapshot_id, close_snapshot_id,
                   open_line, close_line, open_price, close_price, actual_value, outcome, outcome_reason, graded_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id, book_key, market, player_key, side) DO UPDATE SET
                 open_snapshot_id=excluded.open_snapshot_id, close_snapshot_id=excluded.close_snapshot_id,
                 open_line=excluded.open_line, close_line=excluded.close_line, open_price=excluded.open_price,
                 close_price=excluded.close_price, actual_value=excluded.actual_value, outcome=excluded.outcome,
                 outcome_reason=excluded.outcome_reason, graded_at_utc=excluded.graded_at_utc""",
            (game["id"], k["book_key"], k["market"], k["player_key"], open_s["player_name"], k["side"], open_s["id"], close_s["id"],
             open_s["line"], close_s["line"], open_s["price_american"], close_s["price_american"], actual, outcome, reason, now_iso))
        n += 1
    return n


def grade_paper_picks(conn, game, now_iso):
    picks = conn.execute("SELECT p.*, s.game_id, s.book_key, s.market, s.player_key, s.run_id FROM paper_picks p "
                         "JOIN prop_snapshots s ON s.id = p.snapshot_id WHERE s.game_id=? AND p.status='open'", (game["id"],)).fetchall()
    n = 0
    for p in picks:
        res = conn.execute("SELECT * FROM results WHERE game_id=? AND player_key=? AND market=?",
                           (game["id"], p["player_key"], p["market"])).fetchone()
        if res is None:
            continue  # stays open; reported as ungraded
        if res["did_not_play"]:
            outcome = "void"
        else:
            outcome = _outcome(p["side"], p["line"], res["actual_value"])
        if outcome == "win":
            profit = profit_units(p["price_american"], p["stake_units"]) if p["price_american"] is not None else None
        elif outcome == "loss":
            profit = -p["stake_units"]
        else:
            profit = 0.0
        close = _close_snapshot(conn, game, p["book_key"], p["market"], p["player_key"], p["side"])
        clv_line = clv_prob = None
        if close is not None and close["line"] is not None and p["line"] is not None:
            clv_line = (close["line"] - p["line"]) if p["side"] in ("over", "yes") else (p["line"] - close["line"])
            if close["line"] == p["line"]:
                entry = conn.execute("SELECT * FROM prop_snapshots WHERE id=?", (p["snapshot_id"],)).fetchone()
                pe, pc = side_no_vig(conn, entry), side_no_vig(conn, close)
                if pe is not None and pc is not None:
                    clv_prob = round(pc - pe, 4)
        conn.execute("UPDATE paper_picks SET status='graded', outcome=?, profit_units=?, close_snapshot_id=?, clv_line=?, clv_prob=?, graded_at_utc=? WHERE id=?",
                     (outcome, profit, close["id"] if close else None, clv_line, clv_prob, now_iso, p["id"]))
        n += 1
    return n


def grade(conn, cfg, http, now=None, min_hours_after_start=2):
    """Grade games that started at least `min_hours_after_start` ago and have
    tracked props or open paper picks without a final grade."""
    now = now or utc_now()
    with Run(conn, "grade", now) as run:
        cutoff = iso_utc(now - timedelta(hours=min_hours_after_start))
        floor = iso_utc(now - timedelta(days=10))
        games = conn.execute(
            """SELECT g.* FROM games g WHERE g.start_time_utc <= ? AND g.start_time_utc >= ?
               AND EXISTS (SELECT 1 FROM prop_snapshots s WHERE s.game_id=g.id)
               AND (NOT EXISTS (SELECT 1 FROM prop_grades pg WHERE pg.game_id=g.id)
                    OR EXISTS (SELECT 1 FROM prop_grades pg WHERE pg.game_id=g.id AND pg.outcome='ungraded' AND pg.graded_at_utc < ?)
                    OR EXISTS (SELECT 1 FROM paper_picks p JOIN prop_snapshots s ON s.id=p.snapshot_id WHERE s.game_id=g.id AND p.status='open'))
               ORDER BY g.start_time_utc""", (cutoff, floor, iso_utc(now - timedelta(hours=6)))).fetchall()
        if not games:
            run.skip("no games ready to grade")
            return run
        for g in games:
            scope = f"{g['sport']} {g['espn_event_id']} {g['name']}"
            try:
                data, _ = http.get_json(espn.summary_url(g["sport"]), {"event": g["espn_event_id"]})
            except Exception as exc:
                run.error(SOURCE_ESPN, scope, exc)
                continue
            state, completed, detail = espn.summary_state(data)
            if not completed:
                run.log("missing", SOURCE_ESPN, scope, "NotFinal", f"game not final yet ({detail or state})")
                continue
            conn.execute("UPDATE games SET state='post', completed=1, status_detail=?, last_updated_utc=? WHERE id=?",
                         (detail, iso_utc(now), g["id"]))
            g = conn.execute("SELECT * FROM games WHERE id=?", (g["id"],)).fetchone()
            n_res, players = store_results(conn, g, data, iso_utc(now))
            if not players:
                run.log("missing", SOURCE_ESPN, scope, "NoBoxScore", "final game returned no player box score")
                continue
            run.items += n_res + grade_props(conn, g, iso_utc(now)) + grade_paper_picks(conn, g, iso_utc(now))
            conn.commit()
    return run
