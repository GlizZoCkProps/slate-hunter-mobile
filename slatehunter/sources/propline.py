"""PropLine (https://prop-line.com) player-prop source.

Free tier: 1,000 requests/day, no card (verify current terms on prop-line.com).
API (from the official SDK, propline@0.52.0):
  base     https://api.prop-line.com/v1
  auth     header  X-API-Key: <key>
  events   GET /sports/{sport}/events                      -> [event]   (no date filter)
  odds     GET /sports/{sport}/events/{id}/odds?markets=&bookmakers=   -> event + bookmakers[]
  quota    headers X-Daily-Limit / X-Daily-Used / X-Daily-Remaining / X-Daily-Reset (epoch s)
Response shape mirrors The Odds API (bookmakers[].markets[].outcomes[] with name,
description, price, point). Differences handled here:
  - PrizePicks/Underdog prices are synthetic (+100); stored as NULL, never as a price.
  - PrizePicks goblin/demon (alternate) lines are skipped; only standard lines kept.
  - Outcome timestamps are last_change_at / last_seen_at.
"""
from ..util import player_key
from .odds_api import MARKETS, DEFAULT_MARKETS, region_for

BASE = "https://api.prop-line.com/v1"
SPORT_KEYS = {"NBA": "basketball_nba", "NFL": "football_nfl", "CFB": "football_ncaaf"}
SYNTHETIC_PRICE_BOOKS = {"prizepicks", "underdog"}
SOURCE = "propline"


def _headers(api_key):
    return {"X-API-Key": api_key, "Accept": "application/json"}


def quota(headers):
    def as_int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    return as_int(headers.get("x-daily-remaining")), as_int(headers.get("x-daily-used"))


def events(http, api_key, sport):
    return http.get_json(f"{BASE}/sports/{SPORT_KEYS[sport]}/events", None, _headers(api_key))


def event_odds(http, api_key, sport, event_id, provider_markets, books):
    params = {"markets": ",".join(provider_markets), "bookmakers": ",".join(books)}
    return http.get_json(f"{BASE}/sports/{SPORT_KEYS[sport]}/events/{event_id}/odds", params, _headers(api_key))


def parse_event_odds(data, sport):
    reverse = {v: k for k, v in MARKETS.get(sport, {}).items()}
    rows, skipped_alt = [], 0
    for bm in (data or {}).get("bookmakers", []) or []:
        bkey = bm.get("key")
        for mk in bm.get("markets", []) or []:
            market = reverse.get(mk.get("key"))
            if not market or mk.get("suspended_at"):
                continue
            for oc in mk.get("outcomes", []) or []:
                name = oc.get("description") or ""
                side = str(oc.get("name") or "").strip().lower()
                if not name or side not in ("over", "under", "yes", "no"):
                    continue
                if oc.get("dfs_odds_type") not in (None, "", "standard"):
                    skipped_alt += 1
                    continue
                price = oc.get("price")
                if bkey in SYNTHETIC_PRICE_BOOKS:
                    price = None
                try:
                    price = int(round(float(price))) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    point = float(oc["point"]) if oc.get("point") is not None else None
                except (TypeError, ValueError):
                    point = None
                rows.append({"book_key": bkey, "book_title": bm.get("title"), "region": region_for(bkey),
                             "market": market, "source_market_key": mk.get("key"),
                             "player_name": name, "player_key": player_key(name), "side": side,
                             "line": point, "price_american": price,
                             "book_last_update": oc.get("last_change_at") or oc.get("last_seen_at")})
    return rows, skipped_alt
