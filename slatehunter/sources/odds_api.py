"""The Odds API (https://the-odds-api.com) player-prop collection.

Licensed, keyed API (free Starter plan: 500 credits/month at time of writing;
verify current terms on the-odds-api.com). Per the provider docs:
- /v4/sports/{sport}/events costs 0 credits.
- /v4/sports/{sport}/events/{id}/odds costs (markets x regions); when the
  `bookmakers` parameter is used, every group of up to 10 bookmakers counts
  as one region.
- Responses carry x-requests-remaining / x-requests-used / x-requests-last.
PrizePicks is exposed under the `us_dfs` region with bookmaker key `prizepicks`.
"""
import math

from ..util import player_key

BASE = "https://api.the-odds-api.com/v4/sports"
SPORT_KEYS = {"NBA": "basketball_nba", "NFL": "americanfootball_nfl", "CFB": "americanfootball_ncaaf"}

# normalized market -> provider market key
MARKETS = {
    "NBA": {"points": "player_points", "rebounds": "player_rebounds", "assists": "player_assists",
            "three_pointers": "player_threes", "pra": "player_points_rebounds_assists"},
    "NFL": {"passing_yards": "player_pass_yds", "rushing_yards": "player_rush_yds",
            "receiving_yards": "player_reception_yds", "receptions": "player_receptions"},
    "CFB": {"passing_yards": "player_pass_yds", "rushing_yards": "player_rush_yds",
            "receiving_yards": "player_reception_yds", "receptions": "player_receptions"},
}
DEFAULT_MARKETS = {"NBA": ["points", "rebounds", "assists"],
                   "NFL": ["passing_yards", "rushing_yards", "receiving_yards"],
                   "CFB": ["passing_yards", "rushing_yards", "receiving_yards"]}
DFS_BOOKS = {"prizepicks", "underdog", "betr_us_dfs", "pick6"}


def estimated_cost(n_markets, books):
    return n_markets * max(1, math.ceil(len(books) / 10))


def region_for(book_key):
    return "us_dfs" if book_key in DFS_BOOKS else "us"


def quota(headers):
    def as_int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    return as_int(headers.get("x-requests-remaining")), as_int(headers.get("x-requests-used"))


def events(http, api_key, sport, commence_from=None, commence_to=None):
    params = {"apiKey": api_key, "dateFormat": "iso"}
    if commence_from:
        params["commenceTimeFrom"] = commence_from
    if commence_to:
        params["commenceTimeTo"] = commence_to
    return http.get_json(f"{BASE}/{SPORT_KEYS[sport]}/events", params)


def event_odds(http, api_key, sport, event_id, provider_markets, books):
    params = {"apiKey": api_key, "markets": ",".join(provider_markets), "bookmakers": ",".join(books),
              "oddsFormat": "american", "dateFormat": "iso"}
    return http.get_json(f"{BASE}/{SPORT_KEYS[sport]}/events/{event_id}/odds", params)


def parse_event_odds(data, sport):
    """Flatten one event-odds response into observation dicts (one per outcome)."""
    reverse = {v: k for k, v in MARKETS.get(sport, {}).items()}
    rows = []
    for bm in (data or {}).get("bookmakers", []) or []:
        bkey = bm.get("key")
        for mk in bm.get("markets", []) or []:
            market = reverse.get(mk.get("key"))
            if not market:
                continue
            for oc in mk.get("outcomes", []) or []:
                name = oc.get("description") or ""
                side = str(oc.get("name") or "").strip().lower()
                if not name or side not in ("over", "under", "yes", "no"):
                    continue
                price = oc.get("price")
                try:
                    price = int(round(float(price))) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                point = oc.get("point")
                try:
                    point = float(point) if point is not None else None
                except (TypeError, ValueError):
                    point = None
                rows.append({"book_key": bkey, "book_title": bm.get("title"), "region": region_for(bkey),
                             "market": market, "source_market_key": mk.get("key"),
                             "player_name": name, "player_key": player_key(name), "side": side,
                             "line": point, "price_american": price,
                             "book_last_update": mk.get("last_update") or bm.get("last_update")})
    return rows
