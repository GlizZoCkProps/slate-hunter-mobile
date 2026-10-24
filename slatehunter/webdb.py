"""Read-only database access for the web dashboard.

Local mode: reads SLATE_DB_PATH directly.
Remote mode (Render free tier, whose disk is ephemeral): set SLATE_DB_URL to a
raw URL of the collector's database (e.g. the `data` branch on GitHub). The file
is downloaded to a temp path and refreshed at most every SLATE_DB_REFRESH_SEC.
The web app never writes to the database: the collector is the only writer.
"""
import os
import sqlite3
import tempfile
import threading
import time

import requests

_lock = threading.Lock()
_state = {"fetched": 0.0, "error": None, "source": None}


def _remote_path():
    return os.path.join(tempfile.gettempdir(), "slate_hunter_remote.db")


def db_location():
    url = os.environ.get("SLATE_DB_URL", "").strip()
    if not url:
        return os.environ.get("SLATE_DB_PATH", "data/slate_hunter.db"), "local file"
    path = _remote_path()
    ttl = int(os.environ.get("SLATE_DB_REFRESH_SEC", "600"))
    with _lock:
        if time.time() - _state["fetched"] > ttl or not os.path.exists(path):
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                if not r.content.startswith(b"SQLite format 3"):
                    raise ValueError("downloaded file is not a SQLite database")
                tmp = path + ".part"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, path)
                _state.update(fetched=time.time(), error=None)
            except Exception as exc:
                _state["error"] = f"{type(exc).__name__}: {exc}"[:200]
                _state["fetched"] = time.time() - ttl + 60  # retry in a minute
    return path, "remote copy" + (f" (last refresh failed: {_state['error']})" if _state["error"] else "")


def connect_ro():
    path, source = db_location()
    if not os.path.exists(path):
        return None, source + " — database file not found"
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn, source
