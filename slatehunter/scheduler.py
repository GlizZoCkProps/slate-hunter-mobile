"""Decides which jobs are due. Idempotent: safe to call every few minutes from
cron, GitHub Actions, or the built-in loop."""
import time
from datetime import timedelta

from . import collector, grading
from .config import iso_utc, parse_iso, utc_now


def last_success(conn, job):
    row = conn.execute("SELECT started_at_utc FROM collection_runs WHERE job=? AND status IN ('ok','partial','skipped') "
                       "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
    return parse_iso(row["started_at_utc"]) if row else None


def market_is_active(conn, cfg, now):
    row = conn.execute("SELECT 1 FROM games WHERE completed=0 AND start_time_utc>? AND start_time_utc<=? LIMIT 1",
                       (iso_utc(now), iso_utc(now + timedelta(hours=cfg.active_window_hours)))).fetchone()
    return row is not None


def due_jobs(conn, cfg, now=None):
    now = now or utc_now()
    active = market_is_active(conn, cfg, now)
    intervals = {
        "collect_games": cfg.games_interval_min,
        # Pregame mode: check every tick; the window/per-game/daily limits control spend.
        "collect_props": 1 if cfg.props_pregame_window_min > 0 else (cfg.props_active_interval_min if active else cfg.props_idle_interval_min),
        "collect_props_propline": cfg.propline_interval_min,
        "collect_injuries": cfg.props_active_interval_min if active else cfg.props_idle_interval_min,
        "project": cfg.props_idle_interval_min,
        "grade": cfg.grade_interval_min,
    }
    due = []
    for job, minutes in intervals.items():
        last = last_success(conn, job)
        # 2-minute tolerance so a 30-minute cron does not drift past a 60-minute interval.
        if last is None or now - last >= timedelta(minutes=minutes) - timedelta(minutes=2):
            due.append(job)
    return due, active


JOBS = {
    "collect_games": collector.collect_games,
    "collect_injuries": collector.collect_injuries,
    "collect_props": collector.collect_props,
    "collect_props_propline": collector.collect_props_propline,
    "project": collector.project,
    "grade": grading.grade,
}
ORDER = ["collect_games", "collect_injuries", "collect_props", "collect_props_propline", "project", "grade"]


def run_scheduled(conn, cfg, http, now=None, force=None):
    due, active = due_jobs(conn, cfg, now)
    if force:
        due = list(force)
    results = []
    for job in ORDER:
        if job in due:
            run = JOBS[job](conn, cfg, http, now=now)
            results.append((job, getattr(run, "final_status", "?"), run.items, run.errors, run.missing))
    return results, active


def loop(conn_factory, cfg, http, tick_seconds=120, log=print):
    """Always-on mode for a VPS / home server. Ctrl-C to stop."""
    while True:
        conn = conn_factory()
        try:
            results, active = run_scheduled(conn, cfg, http)
            for r in results:
                log(f"{iso_utc()} {r[0]}: {r[1]} items={r[2]} errors={r[3]} missing={r[4]} active={active}")
        except Exception as exc:  # never let the loop die
            log(f"{iso_utc()} scheduler error: {type(exc).__name__}: {collector.redact(exc)}")
        finally:
            conn.close()
        time.sleep(tick_seconds)
