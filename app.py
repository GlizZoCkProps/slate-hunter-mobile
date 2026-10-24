import os, re, statistics, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
TIMEOUT = 14
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports"
ESPN_SEARCH = "https://site.web.api.espn.com/apis/search/v2"

SPORTS = {
    "NFL": {"path": "football/nfl", "web": "football/nfl", "search": "football", "label": "NFL"},
    "NBA": {"path": "basketball/nba", "web": "basketball/nba", "search": "basketball", "label": "NBA"},
    "CFB": {"path": "football/college-football", "web": "football/college-football", "search": "football", "label": "College Football"},
}

session = requests.Session()
session.headers.update({"User-Agent": "SlateHunter/4.0 (free-first research tool)", "Accept": "application/json"})
_cache = {}


def fetch_json(url, params=None, cache_seconds=90):
    key = (url, tuple(sorted((params or {}).items())))
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < cache_seconds:
        return cached[1]
    response = session.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    _cache[key] = (now, data)
    if len(_cache) > 500:
        for old_key, (stamp, _) in list(_cache.items())[:150]:
            if now - stamp > 300:
                _cache.pop(old_key, None)
    return data


def event_status(event):
    status_obj = event.get("status") or {}
    typ = status_obj.get("type") or {}
    state = str(typ.get("state") or "").lower()
    completed = bool(typ.get("completed")) or state in ("post", "final")
    if not state:
        name = str(typ.get("name") or "").lower()
        if "final" in name or "post" in name:
            state, completed = "post", True
        elif "progress" in name or "in_progress" in name:
            state = "in"
        else:
            state = "pre"
    if completed:
        state = "post"
    detail = typ.get("shortDetail") or typ.get("detail") or status_obj.get("displayClock") or ("Final" if completed else "Scheduled")
    return state, completed, detail


def parse_games(sport, days, local_today=None):
    spec = SPORTS[sport]
    out, errors = [], []
    local_today = local_today or datetime.now().astimezone().date()
    for offset in range(days):
        day = local_today + timedelta(days=offset)
        try:
            data = fetch_json(f"{ESPN_SITE}/{spec['path']}/scoreboard", {"dates": day.strftime("%Y%m%d"), "limit": 500})
            for event in data.get("events", []):
                comps = event.get("competitions") or [{}]
                competitors = comps[0].get("competitors", []) if comps else []
                teams = []
                for competitor in competitors:
                    team = competitor.get("team") or {}
                    teams.append(team.get("displayName") or team.get("name") or "Unknown")
                state, completed, status = event_status(event)
                date_value = event.get("date")
                try:
                    start_dt = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
                except Exception:
                    start_dt = None
                now_utc = datetime.now(timezone.utc)
                if completed or state == "post":
                    bucket = "final"
                elif state == "in" or (start_dt and start_dt <= now_utc):
                    bucket = "live"
                else:
                    bucket = "upcoming"
                links = event.get("links") or []
                source_url = next((l.get("href") for l in links if l.get("rel") and "summary" in l.get("rel")), None) or (links[0].get("href") if links else "")
                out.append({
                    "sport": sport, "date": date_value, "name": event.get("name", "Game"),
                    "shortName": event.get("shortName", ""), "teams": teams,
                    "status": status, "state": state, "bucket": bucket,
                    "eventId": str(event.get("id", "")), "source": "ESPN public scoreboard", "url": source_url
                })
        except Exception as exc:
            errors.append(f"{sport} {day.isoformat()}: {type(exc).__name__}")
    unique = {g["eventId"]: g for g in out if g.get("eventId")}
    games = list(unique.values())
    games.sort(key=lambda g: g.get("date") or "")
    return games, errors


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
    """Return stat labels and numeric row values from ESPN's variably-shaped gamelog payload."""
    labels, rows = [], []
    if not isinstance(data, dict): return labels, rows
    # ESPN common v3 gamelog commonly has labels in names/labels and events in events/categories.
    for key in ("labels", "names", "displayNames", "statistics"):
        val = data.get(key)
        if isinstance(val, list) and val and all(isinstance(x, (str, int, float)) for x in val):
            labels = [str(x) for x in val]
            break
    for container_key in ("events", "seasonTypes", "splits", "categories", "statistics"):
        val = data.get(container_key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    stat_values = item.get("stats") or item.get("statistics") or item.get("values")
                    if isinstance(stat_values, list):
                        vals = []
                        for x in stat_values:
                            if isinstance(x, dict): vals.append(x.get("displayValue", x.get("value", x.get("stat"))))
                            else: vals.append(x)
                        if vals: rows.append({"label": item.get("displayName") or item.get("name") or item.get("date") or "Game", "values": vals, "raw": item})
    # Some gamelog responses nest events under items or content.
    if not rows:
        def walk(obj):
            if isinstance(obj, dict):
                vals = obj.get("stats") or obj.get("statistics")
                if isinstance(vals, list):
                    values = [v.get("displayValue", v.get("value")) if isinstance(v, dict) else v for v in vals]
                    if values: rows.append({"label": obj.get("displayName") or obj.get("name") or obj.get("date") or "Game", "values": values, "raw": obj})
                for v in obj.values():
                    if isinstance(v, (dict, list)): walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        walk(data)
    if not labels:
        # Common gamelog includes names nested in seasonTypes.categories.
        cats = data.get("categories")
        if isinstance(cats, list): labels = [str(c.get("displayName") or c.get("name") or c.get("abbreviation") or "Stat") for c in cats if isinstance(c, dict)]
    return labels, rows


def athlete_profile(sport, athlete_id):
    spec = SPORTS[sport]
    urls = [
        f"{ESPN_WEB}/{spec['web']}/athletes/{athlete_id}/gamelog",
        f"https://site.api.espn.com/apis/site/v2/sports/{spec['path']}/athletes/{athlete_id}/gamelog",
    ]
    errors = []
    for url in urls:
        try:
            return fetch_json(url, cache_seconds=60), url, errors
        except Exception as exc:
            errors.append(type(exc).__name__)
    return {}, "", errors


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
        "passing_yards": ["passing yards", "pass yards", "yds"],
        "rushing_yards": ["rushing yards", "rush yards"], "receiving_yards": ["receiving yards", "rec yards"],
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
    values = []
    for row in rows:
        vals = row.get("values", [])
        if idx is not None and idx < len(vals):
            val = parse_numeric(vals[idx])
            if val is not None: values.append(val)
        else:
            raw = row.get("raw", {})
            # Try keyed stats objects as a fallback.
            for key, val in raw.items():
                if any(a.replace(" ", "") in str(key).lower().replace(" ", "") for a in names):
                    num = parse_numeric(val)
                    if num is not None: values.append(num); break
    return values, (labels[idx] if idx is not None else None)


@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "app": "Slate Hunter v4", "time": datetime.now(timezone.utc).isoformat()})

@app.post("/api/hunt")
def hunt():
    body = request.get_json(silent=True) or {}
    sports = [s for s in body.get("sports", ["NFL", "NBA", "CFB"]) if s in SPORTS]
    days = max(1, min(int(body.get("days", 7)), 7))
    requested_start = str(body.get("startDate", "")).strip()
    try:
        local_start = datetime.strptime(requested_start, "%Y-%m-%d").date() if requested_start else datetime.now().astimezone().date()
    except ValueError:
        local_start = datetime.now().astimezone().date()
    games, errors = [], []
    for sport in sports:
        found, errs = parse_games(sport, days, local_start)
        games.extend(found); errors.extend(errs)
    games.sort(key=lambda g: g.get("date") or "")
    upcoming = [g for g in games if g["bucket"] == "upcoming"]
    live = [g for g in games if g["bucket"] == "live"]
    final = [g for g in games if g["bucket"] == "final"]
    brief = build_research_brief(sports, days, upcoming, live, local_start)
    return jsonify({"generatedAt": datetime.now(timezone.utc).isoformat(), "days": days, "startDate": local_start.isoformat(), "sports": sports,
                    "gameCount": len(games), "upcomingCount": len(upcoming), "liveCount": len(live), "finalCount": len(final),
                    "upcoming": upcoming, "live": live, "final": final, "errors": errors, "nextResearchBrief": brief})


def build_research_brief(sports, days, upcoming, live, local_today=None):
    today = (local_today or datetime.now().astimezone().date()).isoformat()
    def lines_for(games):
        out = []
        for g in games[:100]:
            teams = " vs ".join(g.get("teams") or [g.get("name", "Game")])
            out.append(f"- {g['sport']}: {teams}; scheduled {g.get('date')}; status {g.get('status')}; source {g.get('url') or 'ESPN public scoreboard'}")
        return "\n".join(out) or "- None returned."
    return f"""SLATE HUNTER v4 RESEARCH BRIEF\nRun date (local): {today}\nWindow: next {days} day(s)\nSports: {', '.join(sports)}\n\nUPCOMING GAMES:\n{lines_for(upcoming)}\n\nLIVE GAMES (analyze only if user explicitly wants live props):\n{lines_for(live)}\n\nRESEARCH RULES:\n1. Use current, credible sources for the current prop line, player status, recent logs, season baseline, role, opponent matchup, and lineup context.\n2. Compare the posted line with a defensible projection; list source links and retrieval time.\n3. Do not infer a sportsbook line from a projection. Mark unavailable lines WATCHLIST ONLY.\n4. Never invent stats, injuries, probabilities, or a claimed edge. If evidence is thin, PASS.\n5. Markets of interest: yards, points, rebounds, assists, receptions, touchdowns, and other player props.\n6. Free/public sources only; disclose limitations and uncertainty.\n"""


@app.post("/api/player-search")
def player_search():
    body = request.get_json(silent=True) or {}
    query = re.sub(r"\s+", " ", str(body.get("query", ""))).strip()
    sport = str(body.get("sport", "NBA")).upper()
    if sport not in SPORTS: sport = "NBA"
    if len(query) < 2:
        return jsonify({"results": [], "error": "Enter at least two characters."}), 400
    try:
        data = fetch_json(ESPN_SEARCH, {"query": query, "limit": 15, "sport": SPORTS[sport]["search"]}, cache_seconds=60)
        results = get_search_items(data, sport)
        # Search results are not always athlete-only. Keep name matches and deduplicate.
        qlow = query.lower()
        results.sort(key=lambda x: (0 if x["name"].lower().startswith(qlow) else 1, x["name"].lower()))
        return jsonify({"results": results[:12], "sport": sport, "source": "ESPN public search"})
    except Exception as exc:
        return jsonify({"results": [], "error": f"Player search unavailable ({type(exc).__name__}). Try again later."}), 502


@app.post("/api/player-research")
def player_research():
    body = request.get_json(silent=True) or {}
    sport = str(body.get("sport", "NBA")).upper()
    athlete_id = re.sub(r"[^0-9]", "", str(body.get("athleteId", "")))
    metric = str(body.get("metric", "points")).lower()
    if sport not in SPORTS or not athlete_id:
        return jsonify({"error": "Choose a sport and player first."}), 400
    allowed = {"points", "rebounds", "assists", "minutes", "passing_yards", "rushing_yards", "receiving_yards", "receptions", "touchdowns", "three_pointers", "steals", "blocks", "turnovers"}
    if metric not in allowed: metric = "points"
    line = parse_numeric(body.get("line"))
    data, source_url, errors = athlete_profile(sport, athlete_id)
    labels, rows = flatten_stat_labels(data)
    values, label = choose_metric(sport, metric, labels, rows)
    recent = values[:5]
    season = values
    avg5 = round(statistics.mean(recent), 2) if recent else None
    avg_all = round(statistics.mean(season), 2) if season else None
    median5 = round(statistics.median(recent), 2) if recent else None
    weighted = round((statistics.mean(recent) * .65 + statistics.mean(season) * .35), 2) if recent and season else (avg5 if avg5 is not None else avg_all)
    hit_rate = round(sum(1 for v in recent if line is not None and v > line) / len(recent) * 100) if recent and line is not None else None
    if line is None or weighted is None:
        lean = "INSUFFICIENT DATA"
        gap = None
    else:
        gap = round(weighted - line, 2)
        # Conservative label only; this is not a calibrated probability or guarantee.
        if abs(gap) < max(1.0, abs(line) * .07): lean = "NO CLEAR EDGE"
        elif gap > 0: lean = "OVER LEAN"
        else: lean = "UNDER LEAN"
    return jsonify({"sport": sport, "athleteId": athlete_id, "metric": metric, "metricLabel": label or metric.replace("_", " ").title(),
                    "line": line, "recentValues": recent, "sampleSize": len(season), "recentAverage": avg5,
                    "sampleAverage": avg_all, "recentMedian": median5, "projection": weighted, "difference": gap,
                    "recentOverRate": hit_rate, "lean": lean, "dataSource": source_url or "ESPN public athlete game log",
                    "sourceLink": f"https://www.espn.com/{'nba' if sport == 'NBA' else 'nfl' if sport == 'NFL' else 'college-football'}/player/_/id/{athlete_id}",
                    "warnings": ["ESPN's public gamelog structure can change; check the sample and labels before relying on it.",
                                 "Projection is a simple recent/season blend, not a calibrated model or betting probability.",
                                 "The line is entered by you; this tool does not claim to fetch live player-prop odds."] + (["No usable numeric game-log values were parsed for this metric."] if not values else [])})


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow:\n", 200, {"Content-Type": "text/plain"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
