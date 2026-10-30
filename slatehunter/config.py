"""Environment-driven configuration. Every value has a safe default."""
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _list(name, default):
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Config:
    db_path: str = field(default_factory=lambda: os.environ.get("SLATE_DB_PATH", "data/slate_hunter.db"))
    odds_api_key: str = field(default_factory=lambda: os.environ.get("ODDS_API_KEY", "").strip())
    sports: list = field(default_factory=lambda: [s.upper() for s in _list("SLATE_SPORTS", ["NBA", "NFL", "CFB"])])
    # Bookmaker keys as used by The Odds API.
    books: list = field(default_factory=lambda: _list("SLATE_BOOKS", ["draftkings", "fanduel", "prizepicks"]))
    days_ahead: int = field(default_factory=lambda: _int("SLATE_DAYS_AHEAD", 3))
    # Schedules (minutes). A job runs when this much time has passed since its last
    # successful run. Props use the "active" interval when any game starts within
    # SLATE_ACTIVE_WINDOW_HOURS, otherwise the idle interval.
    games_interval_min: int = field(default_factory=lambda: _int("SLATE_GAMES_INTERVAL_MIN", 180))
    props_active_interval_min: int = field(default_factory=lambda: _int("SLATE_PROPS_ACTIVE_INTERVAL_MIN", 60))
    props_idle_interval_min: int = field(default_factory=lambda: _int("SLATE_PROPS_IDLE_INTERVAL_MIN", 360))
    active_window_hours: int = field(default_factory=lambda: _int("SLATE_ACTIVE_WINDOW_HOURS", 6))
    props_lookahead_hours: int = field(default_factory=lambda: _int("SLATE_PROPS_LOOKAHEAD_HOURS", 36))
    grade_interval_min: int = field(default_factory=lambda: _int("SLATE_GRADE_INTERVAL_MIN", 60))
    # The Odds API credit guard: stop prop collection when remaining credits would
    # fall below this reserve. Free plan is 500 credits/month.
    odds_credit_reserve: int = field(default_factory=lambda: _int("SLATE_ODDS_CREDIT_RESERVE", 25))
    max_events_per_run: int = field(default_factory=lambda: _int("SLATE_MAX_EVENTS_PER_RUN", 15))
    # Pregame mode: when > 0, only collect props for games starting within this many
    # minutes, and the props job is due on every scheduler tick.
    props_pregame_window_min: int = field(default_factory=lambda: _int("SLATE_PROPS_PREGAME_WINDOW_MIN", 0))
    # Max paid snapshots per game (0 = unlimited). 1 = one near-close snapshot per game.
    props_snapshots_per_event: int = field(default_factory=lambda: _int("SLATE_PROPS_SNAPSHOTS_PER_EVENT", 0))
    # Max estimated Odds API credits per Central-Time day (0 = no daily cap).
    odds_daily_credit_budget: int = field(default_factory=lambda: _int("SLATE_ODDS_DAILY_CREDIT_BUDGET", 0))
    http_timeout: int = field(default_factory=lambda: _int("SLATE_HTTP_TIMEOUT", 15))


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_utc(dt=None):
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_ct(dt=None):
    dt = dt or utc_now()
    return dt.astimezone(CENTRAL).isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
