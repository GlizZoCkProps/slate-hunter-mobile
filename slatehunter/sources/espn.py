"""ESPN public site API: schedules, box scores and injuries.

These endpoints are unofficial and undocumented. ESPN can change or rate-limit
them at any time; parsing is defensive and every failure is logged by the caller.
Use is subject to ESPN's terms of use: personal, low-volume research only.
"""
import re

from ..util import player_key

SITE = "https://site.api.espn.com/apis/site/v2/sports"
PATHS = {"NBA": "basketball/nba", "NFL": "football/nfl", "CFB": "football/college-football"}
WEB_SLUG = {"NBA": "nba", "NFL": "nfl", "CFB": "college-football"}

# Normalized market -> how to read it from an ESPN box score.
# keys: ESPN stat keys (preferred); labels: (category, label) fallbacks.
BOX_MARKETS = {
    "NBA": {
        "points": {"keys": ["points"], "labels": [(None, "PTS")]},
        "rebounds": {"keys": ["rebounds"], "labels": [(None, "REB")]},
        "assists": {"keys": ["assists"], "labels": [(None, "AST")]},
        "three_pointers": {"keys": ["threePointFieldGoalsMade-threePointFieldGoalsAttempted"], "labels": [(None, "3PT")], "made_of_pair": True},
    },
    "FOOTBALL": {
        "passing_yards": {"keys": ["passingYards"], "labels": [("passing", "YDS")], "category": "passing"},
        "rushing_yards": {"keys": ["rushingYards"], "labels": [("rushing", "YDS")], "category": "rushing"},
        "receiving_yards": {"keys": ["receivingYards"], "labels": [("receiving", "YDS")], "category": "receiving"},
        "receptions": {"keys": ["receptions"], "labels": [("receiving", "REC")], "category": "receiving"},
    },
}
DERIVED = {"NBA": {"pra": ("points", "rebounds", "assists")}}


def markets_for(sport):
    base = BOX_MARKETS["NBA"] if sport == "NBA" else BOX_MARKETS["FOOTBALL"]
    return list(base) + list(DERIVED.get(sport, {}))


def scoreboard_url(sport):
    return f"{SITE}/{PATHS[sport]}/scoreboard"


def summary_url(sport):
    return f"{SITE}/{PATHS[sport]}/summary"


def game_page_url(sport, event_id):
    return f"https://www.espn.com/{WEB_SLUG[sport]}/game/_/gameId/{event_id}"


def _status(obj):
    typ = (obj or {}).get("type") or {}
    state = str(typ.get("state") or "").lower()
    completed = bool(typ.get("completed"))
    if completed:
        state = "post"
    detail = typ.get("shortDetail") or typ.get("detail") or ""
    return state or "pre", completed, detail


def parse_scoreboard(data, sport):
    games = []
    for event in (data or {}).get("events", []) or []:
        comp = (event.get("competitions") or [{}])[0]
        home = away = None
        for c in comp.get("competitors", []) or []:
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
        state, completed, detail = _status(event.get("status") or comp.get("status"))
        eid = str(event.get("id") or "")
        if not eid:
            continue

        def tname(c):
            t = (c or {}).get("team") or {}
            return t.get("displayName") or t.get("name")

        games.append({
            "sport": sport, "espn_event_id": eid, "name": event.get("name"),
            "home_team": tname(home), "away_team": tname(away),
            "start_time": event.get("date"), "state": state, "completed": completed,
            "status_detail": detail,
            "home_score": (home or {}).get("score"), "away_score": (away or {}).get("score"),
            "source_url": game_page_url(sport, eid),
        })
    return games


def _num(value):
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s in ("", "-", "--", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_box_players(summary):
    """Return {player_key: player} from a summary's boxscore.players section.

    player = {athlete_id, name, team, did_not_play, categories:set, by_key:{}, by_label:{(cat,label):v}}
    """
    players = {}
    box = (summary or {}).get("boxscore") or {}
    for group in box.get("players", []) or []:
        team = (group.get("team") or {}).get("displayName") or (group.get("team") or {}).get("name")
        for cat in group.get("statistics", []) or []:
            cat_name = str(cat.get("name") or "").lower() or None
            keys = cat.get("keys") or []
            labels = cat.get("labels") or cat.get("names") or []
            for entry in cat.get("athletes", []) or []:
                ath = entry.get("athlete") or {}
                name = ath.get("displayName") or ath.get("shortName")
                if not name:
                    continue
                pk = player_key(name)
                p = players.setdefault(pk, {"athlete_id": str(ath.get("id") or ""), "name": name, "team": team,
                                            "did_not_play": False, "played": False, "categories": set(),
                                            "by_key": {}, "by_label": {}})
                stats = entry.get("stats") or []
                if entry.get("didNotPlay"):
                    p["did_not_play"] = True
                if stats and not entry.get("didNotPlay"):
                    p["played"] = True
                    if cat_name:
                        p["categories"].add(cat_name)
                for i, v in enumerate(stats):
                    if i < len(keys):
                        p["by_key"].setdefault(str(keys[i]), v)
                    if i < len(labels):
                        p["by_label"].setdefault((cat_name, str(labels[i])), v)
                        p["by_label"].setdefault((None, str(labels[i])), v)
    for p in players.values():
        if p["played"]:
            p["did_not_play"] = False
    return players


def stat_value(player, sport, market):
    """Return (value, reason). value None means not gradeable from this box score."""
    if player.get("did_not_play"):
        return None, "did not play (ESPN box score)"
    derived = DERIVED.get(sport, {}).get(market)
    if derived:
        parts = [stat_value(player, sport, m) for m in derived]
        if any(v is None for v, _ in parts):
            return None, "component stat missing"
        return sum(v for v, _ in parts), "sum of components"
    spec = (BOX_MARKETS["NBA"] if sport == "NBA" else BOX_MARKETS["FOOTBALL"]).get(market)
    if not spec:
        return None, f"market {market} not supported for grading"
    raw = None
    for k in spec["keys"]:
        if k in player["by_key"]:
            raw = player["by_key"][k]
            break
    if raw is None:
        for lab in spec["labels"]:
            if lab in player["by_label"]:
                raw = player["by_label"][lab]
                break
    if raw is not None:
        if spec.get("made_of_pair"):
            m = re.match(r"\s*(\d+)\s*-\s*\d+", str(raw))
            return (float(m.group(1)), "box score") if m else (None, f"unparseable value {raw!r}")
        v = _num(raw)
        return (v, "box score") if v is not None else (None, f"unparseable value {raw!r}")
    if sport != "NBA" and player.get("played") and spec.get("category"):
        # Player recorded stats in other categories but none in this one
        # (e.g. a QB with no rush attempts): ESPN omits zero rows.
        return 0.0, f"played; no {spec['category']} entry in box score, graded as 0"
    return None, "stat not present in box score"


def parse_injuries(summary, sport):
    """Injury entries from a game summary. Handles the top-level `injuries` list
    and the older header.competitions[].competitors[].injuries shape."""
    out = []

    def add(team, item):
        ath = item.get("athlete") or {}
        name = ath.get("displayName") or ath.get("fullName")
        if not name:
            return
        details = item.get("details") or {}
        status = item.get("status")
        if not status and isinstance(item.get("type"), dict):
            status = item["type"].get("description") or item["type"].get("name")
        detail_text = ""
        if isinstance(details, dict):
            detail_text = " ".join(str(x) for x in (details.get("type"), details.get("detail"), details.get("returnDate")) if x)
        out.append({"team": team, "player_name": name, "player_key": player_key(name),
                    "status": str(status or ""),"detail": detail_text or str(item.get("longComment") or item.get("shortComment") or "")[:300]})

    for block in (summary or {}).get("injuries", []) or []:
        team = (block.get("team") or {}).get("displayName")
        for item in block.get("injuries", []) or []:
            add(team, item)
    if not out:
        comp = (((summary or {}).get("header") or {}).get("competitions") or [{}])[0]
        for c in comp.get("competitors", []) or []:
            team = (c.get("team") or {}).get("displayName")
            for item in c.get("injuries", []) or []:
                add(team, item)
    return out


def summary_state(summary):
    comp = (((summary or {}).get("header") or {}).get("competitions") or [{}])[0]
    return _status(comp.get("status"))
