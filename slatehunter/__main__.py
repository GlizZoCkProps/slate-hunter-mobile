"""Command line: python -m slatehunter <command> [options]

Commands
  init-db                       create/upgrade the SQLite schema
  run-scheduled [--force JOB]   run whichever jobs are due (call from cron / Actions)
  loop [--tick SECONDS]         always-on mode: run-scheduled forever
  collect-games | collect-injuries | collect-props | collect-props-propline | project | grade   run one job now
  status                        collector health as JSON
  report [--out DIR]            write today's markdown report (Central date)
  export TABLE [--out FILE] [--since ISO]   CSV export of one table
  export-all [--out DIR]        CSV export of every table
  candidates                    upcoming lines next to projections (JSON)
  snapshots --player NAME       recent snapshots for a player (to pick a snapshot id)
  paper-pick SNAPSHOT_ID [--stake U] [--why TEXT]   record a simulated pick at that exact line/price
  import-splits FILE.csv        import sourced ticket/handle % rows
  integrity [--baseline FILE] [--write-baseline FILE]  integrity + append-only row-count guard
"""
import argparse
import json
import os
import sys

from . import collector, grading, reports, scheduler
from .config import Config
from .db import integrity_ok, open_db
from .http import HttpClient

APPEND_ONLY = ["prop_snapshots", "odds_calls", "projections", "market_signals", "injury_reports", "collection_runs", "collection_errors", "results", "prop_grades", "paper_picks"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="slatehunter", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--db")
    ap.add_argument("--force", action="append")
    ap.add_argument("--tick", type=int, default=120)
    ap.add_argument("--out")
    ap.add_argument("--since")
    ap.add_argument("--player")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--why", default="")
    ap.add_argument("--baseline")
    ap.add_argument("--write-baseline")
    a = ap.parse_args(argv)

    cfg = Config()
    if a.db:
        cfg.db_path = a.db
    conn = open_db(cfg.db_path)
    http = HttpClient(timeout=cfg.http_timeout)
    cmd = a.command
    out = lambda obj: print(json.dumps(obj, indent=2, default=str))

    if cmd == "init-db":
        print(f"schema ready at {cfg.db_path}")
    elif cmd == "run-scheduled":
        results, active = scheduler.run_scheduled(conn, cfg, http, force=a.force)
        out({"market_active": active, "jobs": [dict(zip(["job", "status", "items", "errors", "missing"], r)) for r in results]})
    elif cmd == "loop":
        conn.close()
        scheduler.loop(lambda: open_db(cfg.db_path), cfg, http, tick_seconds=a.tick)
    elif cmd in ("collect-games", "collect-injuries", "collect-props", "collect-props-propline", "project", "grade"):
        fn = {"collect-games": collector.collect_games, "collect-injuries": collector.collect_injuries,
              "collect-props": collector.collect_props, "collect-props-propline": collector.collect_props_propline,
              "project": collector.project, "grade": grading.grade}[cmd]
        run = fn(conn, cfg, http)
        out({"job": run.job, "status": run.final_status, "items": run.items, "errors": run.errors, "missing": run.missing, "message": run.message})
        return 0 if run.final_status != "failed" else 1
    elif cmd == "status":
        out(reports.health(conn))
    elif cmd == "report":
        print(reports.write_daily_report(conn, a.out or "reports"))
    elif cmd == "export":
        if not a.args:
            ap.error("export needs a table name")
        if a.out:
            with open(a.out, "w", newline="") as f:
                reports.export_csv(conn, a.args[0], f, a.since)
            print(a.out)
        else:
            sys.stdout.write(reports.export_csv(conn, a.args[0], since_utc=a.since).getvalue())
    elif cmd == "export-all":
        d = a.out or "exports"
        os.makedirs(d, exist_ok=True)
        for t in reports.EXPORTABLE:
            with open(os.path.join(d, f"{t}.csv"), "w", newline="") as f:
                reports.export_csv(conn, t, f, a.since)
        print(d)
    elif cmd == "candidates":
        out(reports.candidates(conn))
    elif cmd == "snapshots":
        from .util import player_key
        out(reports.rows(conn, "SELECT id, collected_at_ct, book_key, sport, market, player_name, side, line, price_american, game_id "
                               "FROM prop_snapshots WHERE player_key=? ORDER BY id DESC LIMIT 40", (player_key(a.player or ""),)))
    elif cmd == "paper-pick":
        if not a.args:
            ap.error("paper-pick needs a snapshot id")
        pid = reports.create_paper_pick(conn, int(a.args[0]), stake=a.stake, rationale=a.why)
        print(f"paper pick {pid} recorded")
    elif cmd == "import-splits":
        ok, rejected = reports.import_splits(conn, a.args[0])
        out({"imported": ok, "rejected": rejected})
    elif cmd == "integrity":
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in APPEND_ONLY}
        problems = [] if integrity_ok(conn) else ["PRAGMA integrity_check failed"]
        if a.baseline and os.path.exists(a.baseline):
            base = json.load(open(a.baseline))
            problems += [f"{t} shrank {base[t]} -> {counts.get(t)}" for t in base if counts.get(t, 0) < base[t]]
        if a.write_baseline:
            json.dump(counts, open(a.write_baseline, "w"))
        out({"ok": not problems, "problems": problems, "counts": counts})
        return 0 if not problems else 2
    else:
        ap.error(f"unknown command {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
