"""Metrics, candidate ranking, CSV export, daily report, paper picks, splits import."""
import csv
import io
import os
from datetime import timedelta

from .config import iso_ct, iso_utc, parse_iso, utc_now, CENTRAL
from .util import player_key

EXPORTABLE = ["games", "prop_snapshots", "projections", "market_signals", "injury_reports", "results",
              "prop_grades", "paper_picks", "collection_runs", "collection_errors", "player_ids", "odds_calls"]


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- health

def health(conn, now=None):
    now = now or utc_now()
    since = iso_utc(now - timedelta(hours=24))
    last_runs = rows(conn, """SELECT r.* FROM collection_runs r JOIN (SELECT job, MAX(id) id FROM collection_runs GROUP BY job) m ON m.id=r.id
                              ORDER BY r.job""")
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in EXPORTABLE}
    err24 = rows(conn, "SELECT kind, source, error_type, COUNT(*) n FROM collection_errors WHERE at_utc>=? GROUP BY 1,2,3 ORDER BY n DESC", (since,))
    q = conn.execute("SELECT quota_remaining, quota_used, started_at_utc FROM collection_runs WHERE quota_remaining IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    day = now.astimezone(CENTRAL).date().isoformat()
    spend = conn.execute("SELECT COUNT(*) calls, COALESCE(SUM(COALESCE(reported_cost, estimated_cost)),0) credits FROM odds_calls "
                         "WHERE called_date_ct=? AND ok=1", (day,)).fetchone()
    return {"generated_at_utc": iso_utc(now), "generated_at_ct": iso_ct(now), "last_runs": last_runs, "table_counts": counts,
            "errors_last_24h": err24, "odds_api_quota": dict(q) if q else None,
            "odds_credits_today_ct": {"date": day, "calls": spend["calls"], "credits": spend["credits"]}}


# ---------------------------------------------------------------- metrics

def grade_summary(conn, since_utc=None):
    where, params = "", ()
    if since_utc:
        where, params = "WHERE g.start_time_utc >= ?", (since_utc,)
    return rows(conn, f"""SELECT g.sport, pg.market, pg.side, COUNT(*) tracked,
              SUM(pg.outcome='win') wins, SUM(pg.outcome='loss') losses, SUM(pg.outcome='push') pushes,
              SUM(pg.outcome='void') voids, SUM(pg.outcome='ungraded') ungraded,
              ROUND(1.0*SUM(pg.outcome='win')/NULLIF(SUM(pg.outcome IN ('win','loss')),0),3) hit_rate
            FROM prop_grades pg JOIN games g ON g.id=pg.game_id {where}
            GROUP BY 1,2,3 ORDER BY 1,2,3""", params)


def projection_error(conn, since_utc=None):
    """Error of the latest projection made before start, per model/market."""
    where, params = "", ()
    if since_utc:
        where, params = "AND g.start_time_utc >= ?", (since_utc,)
    data = rows(conn, f"""SELECT p.model_version, p.sport, p.market, p.projection, r.actual_value
            FROM projections p JOIN games g ON g.id=p.game_id
            JOIN results r ON r.game_id=p.game_id AND r.player_key=p.player_key AND r.market=p.market
            WHERE r.did_not_play=0 AND r.actual_value IS NOT NULL AND p.projection IS NOT NULL
              AND p.created_at_utc < g.start_time_utc {where}
              AND p.id = (SELECT MAX(p2.id) FROM projections p2 WHERE p2.game_id=p.game_id AND p2.player_key=p.player_key
                          AND p2.market=p.market AND p2.model_version=p.model_version AND p2.created_at_utc < g.start_time_utc)""", params)
    groups = {}
    for d in data:
        groups.setdefault((d["model_version"], d["sport"], d["market"]), []).append(d["actual_value"] - d["projection"])
    out = []
    for (mv, sp, mk), errs in sorted(groups.items()):
        out.append({"model_version": mv, "sport": sp, "market": mk, "n": len(errs),
                    "mae": round(sum(abs(e) for e in errs) / len(errs), 2),
                    "bias_actual_minus_proj": round(sum(errs) / len(errs), 2)})
    return out


def paper_summary(conn):
    return rows(conn, """SELECT COALESCE(model_version,'manual') model_version, COUNT(*) picks,
              SUM(status='open') open, SUM(outcome='win') wins, SUM(outcome='loss') losses, SUM(outcome='push') pushes, SUM(outcome='void') voids,
              ROUND(SUM(CASE WHEN outcome IN ('win','loss','push') AND profit_units IS NOT NULL THEN stake_units END),2) staked,
              ROUND(SUM(profit_units),2) profit_units,
              ROUND(SUM(profit_units)/NULLIF(SUM(CASE WHEN outcome IN ('win','loss','push') AND profit_units IS NOT NULL THEN stake_units END),0),3) roi,
              ROUND(AVG(clv_line),2) avg_clv_line, ROUND(AVG(clv_prob),4) avg_clv_prob,
              SUM(profit_units IS NULL AND outcome='win') wins_without_price
            FROM paper_picks GROUP BY 1""")


def candidates(conn, now=None, limit=50):
    """Latest line per (book, player, market) for upcoming games next to the latest
    projection. `gap` = projection - line. Descriptive only: not a probability."""
    now = now or utc_now()
    data = rows(conn, """
        WITH latest AS (
          SELECT s.* FROM prop_snapshots s
          WHERE s.side='over' AND s.game_id IS NOT NULL AND s.id = (
            SELECT MAX(s2.id) FROM prop_snapshots s2 WHERE s2.game_id=s.game_id AND s2.book_key=s.book_key
              AND s2.market=s.market AND s2.player_key=s.player_key AND s2.side='over'))
        SELECT l.id snapshot_id, l.sport, g.name game, g.start_time_ct, l.book_key, l.player_name, l.market, l.line,
               l.price_american over_price, l.collected_at_ct,
               (SELECT u.price_american FROM prop_snapshots u WHERE u.run_id=l.run_id AND u.book_key=l.book_key AND u.market=l.market
                  AND u.player_key=l.player_key AND u.side='under' AND u.line IS l.line) under_price,
               p.projection, p.model_version, p.sample_size, p.id projection_id
        FROM latest l JOIN games g ON g.id=l.game_id
        LEFT JOIN projections p ON p.id = COALESCE(
          (SELECT MAX(id) FROM projections p3 WHERE p3.game_id=l.game_id AND p3.player_key=l.player_key AND p3.market=l.market AND p3.model_version='shrink_v2'),
          (SELECT MAX(id) FROM projections p3 WHERE p3.game_id=l.game_id AND p3.player_key=l.player_key AND p3.market=l.market))
        WHERE g.start_time_utc > ? AND g.completed=0""", (iso_utc(now),))
    for d in data:
        d["gap"] = round(d["projection"] - d["line"], 2) if d["projection"] is not None and d["line"] is not None else None
        d["gap_pct"] = round(d["gap"] / d["line"], 3) if d["gap"] is not None and d["line"] else None
        lat = [x for x in rows(conn, "SELECT status FROM injury_reports WHERE game_id=(SELECT game_id FROM prop_snapshots WHERE id=?) AND player_key=? ORDER BY id DESC LIMIT 1",
                               (d["snapshot_id"], player_key(d["player_name"])))]
        d["injury_status"] = lat[0]["status"] if lat else None
    data.sort(key=lambda d: (d["gap_pct"] is None, -abs(d["gap_pct"] or 0)))
    return data[:limit]


# ---------------------------------------------------------------- exports

def export_csv(conn, table, out=None, since_utc=None):
    if table not in EXPORTABLE:
        raise ValueError(f"unknown table {table}; choose from {', '.join(EXPORTABLE)}")
    time_col = {"prop_snapshots": "collected_at_utc", "collection_runs": "started_at_utc", "collection_errors": "at_utc",
                "projections": "created_at_utc", "market_signals": "observed_at_utc", "injury_reports": "collected_at_utc",
                "results": "collected_at_utc", "prop_grades": "graded_at_utc", "paper_picks": "created_at_utc",
                "games": "start_time_utc", "player_ids": "resolved_at_utc", "odds_calls": "called_at_utc"}[table]
    sql, params = f"SELECT * FROM {table}", ()
    if since_utc:
        sql, params = sql + f" WHERE {time_col} >= ?", (since_utc,)
    cur = conn.execute(sql + " ORDER BY rowid", params)
    buf = out or io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in cur.description])
    for r in cur:
        w.writerow(list(r))
    return buf


def daily_report(conn, now=None):
    now = now or utc_now()
    day = now.astimezone(CENTRAL).date().isoformat()
    h = health(conn, now)
    since = iso_utc(now - timedelta(days=30))
    L = [f"# Slate Hunter daily report — {day} (Central)", "",
         f"Generated {h['generated_at_ct']}. All figures come from the Slate Hunter database; nothing here is a guarantee.", "",
         "## Collector health", "", "| job | last run (UTC) | status | items | errors | message |", "|---|---|---|---|---|---|"]
    for r in h["last_runs"]:
        L.append(f"| {r['job']} | {r['started_at_utc']} | {r['status']} | {r['items_written']} | {r['error_count']} | {(r['message'] or '')[:80]} |")
    q = h["odds_api_quota"]
    t = h["odds_credits_today_ct"]
    L += ["", f"Odds API credits remaining: {q['quota_remaining'] if q else 'unknown (no keyed call yet)'}; "
              f"used today (CT {t['date']}): {t['credits']} over {t['calls']} calls", "",
          "Errors / missing data, last 24h:", ""]
    L += [f"- {e['kind']} · {e['source']} · {e['error_type']}: {e['n']}" for e in h["errors_last_24h"]] or ["- none"]
    L += ["", "## Table counts", ""] + [f"- {k}: {v}" for k, v in h["table_counts"].items()]
    L += ["", "## Grades, last 30 days (every tracked prop, graded at closing snapshot line)", "",
          "| sport | market | side | tracked | W | L | P | void | ungraded | hit rate |", "|---|---|---|---|---|---|---|---|---|---|"]
    for g in grade_summary(conn, since):
        L.append(f"| {g['sport']} | {g['market']} | {g['side']} | {g['tracked']} | {g['wins']} | {g['losses']} | {g['pushes']} | {g['voids']} | {g['ungraded']} | {g['hit_rate']} |")
    L += ["", "## Projection error, last 30 days", "", "| model | sport | market | n | MAE | bias (actual−proj) |", "|---|---|---|---|---|---|"]
    for p in projection_error(conn, since):
        L.append(f"| {p['model_version']} | {p['sport']} | {p['market']} | {p['n']} | {p['mae']} | {p['bias_actual_minus_proj']} |")
    L += ["", "## Paper picks", ""]
    for p in paper_summary(conn):
        L.append(f"- {p['model_version']}: {p['picks']} picks ({p['open']} open), {p['wins']}-{p['losses']}-{p['pushes']}, "
                 f"profit {p['profit_units']}u, ROI {p['roi']}, avg CLV line {p['avg_clv_line']}, avg CLV prob {p['avg_clv_prob']}")
    L += ["", "## Upcoming candidates (gap = shrink_v2 projection − line; descriptive, not a probability)", "",
          "| game (CT) | book | player | market | line | over/under | proj | gap | injury | snapshot |", "|---|---|---|---|---|---|---|---|---|---|"]
    for c in candidates(conn, now, limit=25):
        L.append(f"| {c['game']} {c['start_time_ct'][:16] if c['start_time_ct'] else ''} | {c['book_key']} | {c['player_name']} | {c['market']} | "
                 f"{c['line']} | {c['over_price']}/{c['under_price']} | {c['projection']} | {c['gap']} | {c['injury_status'] or ''} | {c['snapshot_id']} |")
    moves = rows(conn, "SELECT * FROM market_signals WHERE observed_at_utc>=? ORDER BY id DESC LIMIT 25", (iso_utc(now - timedelta(hours=24)),))
    L += ["", "## Line/price moves observed, last 24h", "",
          "Observed changes between our own snapshots. Not evidence of sharp money: no ticket/handle data unless a row says `reported_split` with a source URL.", ""]
    L += [f"- {m['player_name']} {m['market']} {m['side']} @ {m['book_key']}: {m['signal_type']} {m['value_before']} → {m['value_after']} ({m['evidence_level']})" for m in moves] or ["- none"]
    return day, "\n".join(L) + "\n"


def write_daily_report(conn, out_dir="reports", now=None):
    day, text = daily_report(conn, now)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{day}.md")
    with open(path, "w") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------- paper picks / splits

def create_paper_pick(conn, snapshot_id, stake=1.0, rationale="", projection_id=None, model_version=None, now=None):
    now = now or utc_now()
    snap = conn.execute("SELECT s.*, g.start_time_utc FROM prop_snapshots s LEFT JOIN games g ON g.id=s.game_id WHERE s.id=?", (snapshot_id,)).fetchone()
    if snap is None:
        raise ValueError(f"snapshot {snapshot_id} not found")
    if snap["game_id"] is None:
        raise ValueError("snapshot is not matched to an ESPN game; it could not be graded")
    start = parse_iso(snap["start_time_utc"])
    if start and start <= now:
        raise ValueError("game already started; paper picks must use a pregame snapshot")
    age = now - parse_iso(snap["collected_at_utc"])
    if age > timedelta(hours=3):
        raise ValueError(f"snapshot is {age} old; collect fresh lines first so the recorded price was actually available")
    cur = conn.execute("INSERT INTO paper_picks(created_at_utc, created_at_ct, snapshot_id, side, line, price_american, stake_units, projection_id, model_version, rationale) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (iso_utc(now), iso_ct(now), snapshot_id, snap["side"], snap["line"], snap["price_american"], stake,
                        projection_id, model_version, rationale))
    conn.commit()
    return cur.lastrowid


SPLIT_FIELDS = ["observed_at_utc", "sport", "player_name", "market", "book_key", "side", "signal_type", "value", "source", "source_url"]


def import_splits(conn, path):
    """Import ticket/handle percentages from a CSV with SPLIT_FIELDS columns.
    Rows without a source_url, or with an unsupported signal_type, are rejected."""
    ok, rejected = 0, []
    with open(path, newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            if r.get("signal_type") not in ("ticket_pct", "handle_pct"):
                rejected.append((i, "signal_type must be ticket_pct or handle_pct"))
                continue
            if not (r.get("source_url") or "").startswith("http"):
                rejected.append((i, "source_url required"))
                continue
            try:
                value = float(r["value"])
            except (TypeError, ValueError):
                rejected.append((i, "value must be numeric"))
                continue
            conn.execute("INSERT INTO market_signals(observed_at_utc, sport, player_key, player_name, market, book_key, side, signal_type, value_after, "
                         "evidence_level, source, source_url, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (r["observed_at_utc"], r["sport"], player_key(r["player_name"]), r["player_name"], r["market"], r.get("book_key"),
                          r.get("side"), r["signal_type"], value, "reported_split", r["source"], r["source_url"], r.get("notes")))
            ok += 1
    conn.commit()
    return ok, rejected
