import os, requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
TIMEOUT = 12
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

SPORTS = {
    "NFL": {"path": "football/nfl", "odds_key": "americanfootball_nfl"},
    "NBA": {"path": "basketball/nba", "odds_key": "basketball_nba"},
    "CFB": {"path": "football/college-football", "odds_key": "americanfootball_ncaaf"},
}

def fetch_json(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent":"SlateHunterMobile/3.0"})
    r.raise_for_status()
    return r.json()

def parse_games(sport, days):
    spec = SPORTS[sport]
    out, errors = [], []
    today = datetime.now().astimezone().date()
    for offset in range(days):
        day = today + timedelta(days=offset)
        date_arg = day.strftime("%Y%m%d")
        url = f"{ESPN}/{spec['path']}/scoreboard"
        try:
            data = fetch_json(url, {"dates": date_arg, "limit": 500})
            for event in data.get("events", []):
                comps = event.get("competitions", [{}])[0].get("competitors", [])
                teams = []
                for c in comps:
                    team = c.get("team", {})
                    teams.append(team.get("displayName") or team.get("name") or "Unknown")
                status = event.get("status", {}).get("type", {}).get("detail", "Status unknown")
                out.append({
                    "sport": sport,
                    "date": event.get("date"),
                    "name": event.get("name", "Game"),
                    "shortName": event.get("shortName", ""),
                    "teams": teams,
                    "status": status,
                    "eventId": event.get("id"),
                    "source": "ESPN public scoreboard",
                    "url": event.get("links", [{}])[0].get("href", "")
                })
        except Exception as exc:
            errors.append(f"{sport} {date_arg}: {type(exc).__name__}")
    # Deduplicate event IDs and keep chronological order
    unique = {g["eventId"]: g for g in out if g.get("eventId")}
    games = list(unique.values())
    games.sort(key=lambda g: g.get("date") or "")
    return games, errors

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "app": "Slate Hunter Mobile v3", "time": datetime.now(timezone.utc).isoformat()})

@app.post("/api/hunt")
def hunt():
    body = request.get_json(silent=True) or {}
    sports = [s for s in body.get("sports", ["NFL","NBA","CFB"]) if s in SPORTS]
    days = int(body.get("days", 7))
    days = max(1, min(days, 7))
    games, errors = [], []
    for sport in sports:
        found, errs = parse_games(sport, days)
        games.extend(found)
        errors.extend(errs)
    games.sort(key=lambda g: g.get("date") or "")
    # The app does not fabricate player props. The free public schedule feed is not a player-prop odds feed.
    return jsonify({
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "sports": sports,
        "gameCount": len(games),
        "games": games,
        "errors": errors,
        "limitations": [
            "This build automatically discovers schedules from ESPN's public scoreboard endpoints.",
            "It does not yet have a reliable free player-prop odds feed or an integrated research-model API.",
            "It will not invent prop lines or player projections. Candidates need current market lines and player-level data from sources that permit access."
        ],
        "nextResearchBrief": build_research_brief(sports, days, games)
    })

def build_research_brief(sports, days, games):
    today = datetime.now().astimezone().date().isoformat()
    lines = []
    for g in games[:100]:
        when = g.get("date", "time unavailable")
        teams = " vs ".join(g.get("teams") or [g.get("name","Game")])
        lines.append(f"- {g['sport']}: {teams}; scheduled {when}; status {g['status']}; source {g['source']}")
    slate = "\n".join(lines) if lines else "- No games returned by the public schedule feed."
    return f"""SLATE HUNTER v3 DEEP RESEARCH RUN
Run date (local): {today}
Window: next {days} day(s)
Sports: {", ".join(sports)}

SCHEDULE DISCOVERED AUTOMATICALLY (ESPN public scoreboard):
{slate}

Research task:
For upcoming games in this schedule, independently research the highest-value player prop opportunities across player yards, points, rebounds, assists, and other player markets. Search across accessible reputable market and stats sources, not PrizePicks only. Verify the current line, platform/book, update time, player/team/opponent, injuries/availability, role, recent logs, season baseline, matchup, and projection. Compare the projection with the posted line; distinguish market consensus from a book-specific line. Include source links. Rank only candidates with defensible evidence. If a current player-prop line or projection cannot be verified, mark WATCHLIST ONLY, not a confirmed edge. Do not invent data or probabilities. Provide 5-10 candidates only if evidence supports them; otherwise PASS. Explain risks and uncertainty. Free/public sources only; no paid APIs assumed. No betting guarantees.
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
