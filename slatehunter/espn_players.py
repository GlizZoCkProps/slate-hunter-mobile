"""ESPN player search / game-log parsing, moved verbatim from app.py so the
web app and the collector share one implementation."""
import re
from datetime import datetime


def get_search_items(data, sport=None):
    found = []
    # Current ESPN search groups players under results[].contents[]. Their id is
    # a UUID; use the numeric athlete ID embedded in uid or the player URL.
    expected_leagues = {"NBA": {"nba"}, "NFL": {"nfl"}, "CFB": {"college-football"}}
    uid_prefixes = {"NBA": "s:40~l:46~", "NFL": "s:20~l:28~", "CFB": "s:20~l:23~"}
    groups = data.get("results", []) if isinstance(data, dict) else []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict) or str(group.get("type", "")).lower() not in ("player", "athlete"):
                continue
            people = group.get("contents", [])
            if not isinstance(people, list):
                continue
            for person in people:
                if not isinstance(person, dict):
                    continue
                uid = str(person.get("uid") or "")
                link = person.get("link") or {}
                href = link.get("web", "") if isinstance(link, dict) else ""
                league = str(person.get("defaultLeagueSlug") or "").lower()
                if sport and league and league not in expected_leagues.get(sport, set()):
                    continue
                if sport and not league and uid and not uid.startswith(uid_prefixes.get(sport, "")):
                    continue
                numeric_id = re.search(r"~a:(\d+)", uid) or re.search(r"/id/(\d+)", href)
                idv = numeric_id.group(1) if numeric_id else person.get("id")
                name = person.get("displayName") or person.get("fullName") or person.get("name")
                if idv and str(idv).isdigit() and name:
                    image = person.get("image") or {}
                    found.append({"id": str(idv), "name": name, "team": person.get("subtitle") or "",
                                  "position": person.get("position", ""),
                                  "image": image.get("default", "") if isinstance(image, dict) else "",
                                  "url": href})

    # Preserve support for older ESPN layouts that expose athlete objects directly.
    if not found:
        containers = []
        if isinstance(data, dict):
            for key in ("results", "items", "sports", "athletes"):
                val = data.get(key)
                if isinstance(val, list):
                    containers.extend(val)
            if not containers:
                for value in data.values():
                    if isinstance(value, list):
                        containers.extend(value)
        def walk(obj):
            if isinstance(obj, dict):
                typ = str(obj.get("type") or obj.get("_type") or "").lower()
                athlete = obj.get("athlete") if isinstance(obj.get("athlete"), dict) else obj
                idv = athlete.get("id") or obj.get("id")
                name = athlete.get("displayName") or athlete.get("fullName") or athlete.get("name") or obj.get("displayName") or obj.get("name")
                if idv and str(idv).isdigit() and name and ("athlete" in typ or athlete is not obj or any(k in athlete for k in ("position", "team", "headshot"))):
                    team = athlete.get("team") or obj.get("team") or {}
                    team_name = team.get("displayName") or team.get("name") or "" if isinstance(team, dict) else str(team)
                    pos = athlete.get("position") or {}
                    if isinstance(pos, dict):
                        pos = pos.get("abbreviation") or pos.get("displayName") or ""
                    headshot = athlete.get("headshot") or {}
                    links = athlete.get("links") or []
                    found.append({"id": str(idv), "name": name, "team": team_name, "position": pos,
                                  "image": headshot.get("href", "") if isinstance(headshot, dict) else "",
                                  "url": links[0].get("href", "") if isinstance(links, list) and links and isinstance(links[0], dict) else ""})
                for value in obj.values():
                    if isinstance(value, (dict, list)):
                        walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)
        walk(containers or data)
    unique = {}
    for item in found:
        unique[item["id"]] = item
    return list(unique.values())[:15]


def flatten_stat_labels(data):
    """Return ESPN's stat labels and per-game rows across common gamelog shapes."""
    if not isinstance(data, dict): return [], []
    labels = []
    for key in ("displayNames", "names", "labels"):
        value = data.get(key)
        if isinstance(value, list) and value and all(isinstance(x, (str, int, float)) for x in value):
            labels = [str(x) for x in value]
            break

    rows = []
    event_metadata = data.get("events") if isinstance(data.get("events"), dict) else {}
    # Current ESPN common-v3 format: seasonTypes -> categories -> events.
    for season_type in data.get("seasonTypes") or []:
        if not isinstance(season_type, dict): continue
        for category in season_type.get("categories") or []:
            if not isinstance(category, dict): continue
            for event_row in category.get("events") or []:
                if not isinstance(event_row, dict) or not isinstance(event_row.get("stats"), list): continue
                event_id = str(event_row.get("eventId") or event_row.get("id") or "")
                metadata = event_metadata.get(event_id, {}) if isinstance(event_metadata, dict) else {}
                opponent = metadata.get("opponent") or {}
                if isinstance(opponent, dict): opponent = opponent.get("displayName") or opponent.get("name") or ""
                date = metadata.get("gameDate") or metadata.get("date") or ""
                if date:
                    try: date = datetime.fromisoformat(str(date).replace("Z", "+00:00")).strftime("%b %d")
                    except Exception: date = str(date)[:10]
                label = date or (f"Week {metadata['week']}" if metadata.get("week") else "Game")
                rows.append({"label": label, "values": event_row["stats"],
                             "raw": {**event_row, "opponent": opponent}})

    # Keep compatibility with older ESPN payloads while avoiding season totals
    # in the current format, which would otherwise get counted as extra games.
    if not rows:
        def walk(obj):
            if isinstance(obj, dict):
                vals = obj.get("stats") or obj.get("statistics") or obj.get("values")
                if isinstance(vals, list) and vals:
                    values = [v.get("displayValue", v.get("value", v.get("stat"))) if isinstance(v, dict) else v for v in vals]
                    rows.append({"label": obj.get("displayName") or obj.get("name") or obj.get("date") or "Game", "values": values, "raw": obj})
                for value in obj.values():
                    if isinstance(value, (dict, list)): walk(value)
            elif isinstance(obj, list):
                for value in obj: walk(value)
        walk(data)
    return labels, rows


def parse_numeric(v):
    if isinstance(v, (int, float)): return float(v)
    if v is None: return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "--", "N/A", "DNP"): return None
    # Convert made-attempted strings only for simple stat categories if relevant.
    try: return float(s)
    except Exception: return None


def choose_metric(sport, metric, labels, rows):
    aliases = {
        "points": ["points", "pts"], "rebounds": ["rebounds", "reb", "total rebounds"],
        "assists": ["assists", "ast"], "minutes": ["minutes", "min"],
        "passing_yards": ["passing yards", "pass yards", "passingYards"],
        "rushing_yards": ["rushing yards", "rush yards", "rushingYards"], "receiving_yards": ["receiving yards", "rec yards", "receivingYards"],
        "receptions": ["receptions", "rec"], "touchdowns": ["touchdowns", "td"],
        "three_pointers": ["3-point field goals made", "three pointers made", "3pm", "3pt made"],
        "steals": ["steals", "stl"], "blocks": ["blocks", "blk"], "turnovers": ["turnovers", "to"],
    }
    names = aliases.get(metric, [metric.replace("_", " ")])
    idx = None
    for i, label in enumerate(labels):
        low = label.lower()
        if any(a == low or a in low for a in names):
            idx = i; break
    values = []; game_rows = []
    for row in rows:
        vals = row.get("values", [])
        if idx is not None and idx < len(vals):
            val = parse_numeric(vals[idx])
            if val is not None:
                values.append(val)
                raw=row.get("raw", {})
                opponent=raw.get("opponent") or raw.get("opponentTeam") or raw.get("opponentDisplayName") or ""
                if isinstance(opponent,dict): opponent=opponent.get("displayName") or opponent.get("name") or ""
                game_rows.append({"value":val,"label":row.get("label") or raw.get("date") or "Game","opponent":str(opponent)})
        else:
            raw = row.get("raw", {})
            # Try keyed stats objects as a fallback.
            for key, val in raw.items():
                if any(a.replace(" ", "") in str(key).lower().replace(" ", "") for a in names):
                    num = parse_numeric(val)
                    if num is not None:
                        values.append(num)
                        opponent=raw.get("opponent") or raw.get("opponentTeam") or raw.get("opponentDisplayName") or ""
                        if isinstance(opponent,dict): opponent=opponent.get("displayName") or opponent.get("name") or ""
                        game_rows.append({"value":num,"label":row.get("label") or raw.get("date") or "Game","opponent":str(opponent)})
                        break
    return values, (labels[idx] if idx is not None else None), game_rows
