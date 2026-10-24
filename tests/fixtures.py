"""Synthetic payloads shaped like ESPN / The Odds API responses.

These are NOT real lines or stats. They mirror the response structures the
parsers expect so the pipeline can be tested offline. Re-record real samples
when a live run is available (see PROJECT_HANDOFF.md, "Verification").
"""

NBA_SCOREBOARD = {"events": [
    {"id": "401700001", "name": "Golden State Warriors at Los Angeles Lakers", "date": "2026-10-25T02:30Z",
     "status": {"type": {"state": "pre", "completed": False, "shortDetail": "10/24 - 9:30 PM EDT"}},
     "competitions": [{"competitors": [
         {"homeAway": "home", "score": "0", "team": {"displayName": "Los Angeles Lakers"}},
         {"homeAway": "away", "score": "0", "team": {"displayName": "Golden State Warriors"}}]}]},
    {"id": "401700000", "name": "Boston Celtics at New York Knicks", "date": "2026-10-23T23:30Z",
     "status": {"type": {"state": "post", "completed": True, "shortDetail": "Final"}},
     "competitions": [{"competitors": [
         {"homeAway": "home", "score": "110", "team": {"displayName": "New York Knicks"}},
         {"homeAway": "away", "score": "104", "team": {"displayName": "Boston Celtics"}}]}]},
]}

ODDS_EVENTS_NBA = [
    {"id": "oddsev1", "sport_key": "basketball_nba", "commence_time": "2026-10-25T02:30:00Z",
     "home_team": "Los Angeles Lakers", "away_team": "Golden State Warriors"},
    {"id": "oddsev_unmatched", "sport_key": "basketball_nba", "commence_time": "2026-10-25T03:00:00Z",
     "home_team": "Portland Trail Blazers", "away_team": "Utah Jazz"},
]


def odds_event(lebron_line=24.5, over=-115, under=-105):
    return {"id": "oddsev1", "bookmakers": [
        {"key": "draftkings", "title": "DraftKings", "markets": [
            {"key": "player_points", "last_update": "2026-10-24T18:00:00Z", "outcomes": [
                {"name": "Over", "description": "LeBron James", "price": over, "point": lebron_line},
                {"name": "Under", "description": "LeBron James", "price": under, "point": lebron_line},
                {"name": "Over", "description": "Stephen Curry", "price": -110, "point": 27.5},
                {"name": "Under", "description": "Stephen Curry", "price": -110, "point": 27.5}]},
            {"key": "player_rebounds", "last_update": "2026-10-24T18:00:00Z", "outcomes": [
                {"name": "Over", "description": "LeBron James", "price": -120, "point": 7.5},
                {"name": "Under", "description": "LeBron James", "price": 100, "point": 7.5}]}]},
        {"key": "prizepicks", "title": "PrizePicks", "markets": [
            {"key": "player_points", "last_update": "2026-10-24T18:00:00Z", "outcomes": [
                {"name": "Over", "description": "LeBron James", "point": 25.0},
                {"name": "Under", "description": "LeBron James", "point": 25.0}]}]},
    ]}


QUOTA = {"x-requests-remaining": "480", "x-requests-used": "20", "x-requests-last": "3"}

NBA_SUMMARY_FINAL = {
    "header": {"competitions": [{"status": {"type": {"state": "post", "completed": True, "shortDetail": "Final"}}}]},
    "boxscore": {"players": [
        {"team": {"displayName": "Los Angeles Lakers"}, "statistics": [{
            "name": None,
            "keys": ["minutes", "fieldGoalsMade-fieldGoalsAttempted", "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
                     "rebounds", "assists", "points"],
            "labels": ["MIN", "FG", "3PT", "REB", "AST", "PTS"],
            "athletes": [
                {"athlete": {"id": "1966", "displayName": "LeBron James"}, "stats": ["35", "10-19", "2-6", "8", "9", "27"]},
                {"athlete": {"id": "9999", "displayName": "Bench Guy Jr."}, "stats": [], "didNotPlay": True},
            ]}]},
        {"team": {"displayName": "Golden State Warriors"}, "statistics": [{
            "keys": ["minutes", "fieldGoalsMade-fieldGoalsAttempted", "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
                     "rebounds", "assists", "points"],
            "labels": ["MIN", "FG", "3PT", "REB", "AST", "PTS"],
            "athletes": [{"athlete": {"id": "3975", "displayName": "Stephen Curry"}, "stats": ["34", "9-20", "5-12", "4", "6", "27"]}]}]},
    ]},
}

NBA_SUMMARY_PRE = {
    "header": {"competitions": [{"status": {"type": {"state": "pre", "completed": False, "shortDetail": "9:30 PM"}}}]},
    "injuries": [{"team": {"displayName": "Los Angeles Lakers"}, "injuries": [
        {"athlete": {"displayName": "Anthony Davis"}, "status": "Questionable", "details": {"type": "Knee", "detail": "Soreness"}}]}],
}

NFL_SUMMARY_FINAL = {
    "header": {"competitions": [{"status": {"type": {"state": "post", "completed": True}}}]},
    "boxscore": {"players": [{"team": {"displayName": "Kansas City Chiefs"}, "statistics": [
        {"name": "passing", "keys": ["completions/passingAttempts", "passingYards", "passingTouchdowns"], "labels": ["C/ATT", "YDS", "TD"],
         "athletes": [{"athlete": {"id": "3139477", "displayName": "Patrick Mahomes"}, "stats": ["25/36", "291", "2"]}]},
        {"name": "receiving", "keys": ["receptions", "receivingYards", "receivingTouchdowns"], "labels": ["REC", "YDS", "TD"],
         "athletes": [{"athlete": {"id": "15847", "displayName": "Travis Kelce"}, "stats": ["7", "68", "1"]}]},
    ]}]},
}

NBA_GAMELOG = {
    "labels": ["MIN", "REB", "AST", "PTS"],
    "events": {"e1": {"gameDate": "2026-10-22T23:00Z"}, "e2": {"gameDate": "2026-10-20T23:00Z"}},
    "seasonTypes": [{"categories": [{"events": [
        {"eventId": "e1", "stats": ["34", "7", "8", "26"]},
        {"eventId": "e2", "stats": ["36", "9", "10", "22"]}]}]}],
}
