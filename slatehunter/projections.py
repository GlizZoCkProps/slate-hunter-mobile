"""Projection models. Each model has a version string stored with every row.

baseline_v1 is the same heuristic the web app has always shown:
    0.65 * mean(last 5 games) + 0.35 * mean(all games in ESPN's current game log)
It is descriptive, NOT calibrated: est_prob_over is left NULL. Do not present
its output as a probability.
"""
import json
import statistics

from .config import iso_ct, iso_utc
from .espn_players import choose_metric, flatten_stat_labels, get_search_items
from .sources import espn
from .util import player_key

MODEL_VERSION = "baseline_v1"
SEARCH_URL = "https://site.web.api.espn.com/apis/search/v2"
GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/{path}/athletes/{aid}/gamelog"
SEARCH_SPORT = {"NBA": "basketball", "NFL": "football", "CFB": "football"}
COMPONENTS = {"pra": ("points", "rebounds", "assists")}


def baseline_v1(values):
    """values: most-recent-first list of per-game stat values."""
    if not values:
        return None
    recent = values[:5]
    return round(statistics.mean(recent) * 0.65 + statistics.mean(values) * 0.35, 2)


def gamelog_values(data, sport, market):
    labels, rows = flatten_stat_labels(data)
    if market in COMPONENTS:
        series = [choose_metric(sport, m, labels, rows)[0] for m in COMPONENTS[market]]
        n = min(len(s) for s in series) if series else 0
        if any(len(s) != n for s in series):
            return [], "component series lengths differ; not combined"
        return [sum(s[i] for s in series) for i in range(n)], "sum of components"
    values, label, _ = choose_metric(sport, market, labels, rows)
    return values, label


def resolve_athlete(conn, http, sport, name, pkey, now_utc_iso):
    row = conn.execute("SELECT espn_athlete_id, resolved_at_utc, note FROM player_ids WHERE sport=? AND player_key=?",
                       (sport, pkey)).fetchone()
    if row is not None and (row["espn_athlete_id"] or row["resolved_at_utc"][:10] == now_utc_iso[:10]):
        # Successful resolutions are cached permanently; failures are retried once per day.
        return row["espn_athlete_id"], f"cached ({row['note']})"
    # A prior box score is the most reliable source of the id.
    r = conn.execute("SELECT espn_athlete_id FROM results WHERE sport=? AND player_key=? AND espn_athlete_id<>'' "
                     "ORDER BY id DESC LIMIT 1", (sport, pkey)).fetchone()
    if r:
        aid, note, source = r["espn_athlete_id"], "from prior box score", "espn_box_score"
    else:
        data, _ = http.get_json(SEARCH_URL, {"query": name, "limit": 15, "sport": SEARCH_SPORT[sport]})
        exact = [x for x in get_search_items(data, sport) if player_key(x["name"]) == pkey]
        ids = {x["id"] for x in exact}
        if len(ids) == 1:
            aid, note = exact[0]["id"], "exact name match in ESPN search"
        elif not ids:
            aid, note = None, "no exact name match in ESPN search"
        else:
            aid, note = None, f"ambiguous: {len(ids)} ESPN athletes share this name"
        source = "espn_search"
    conn.execute("INSERT OR REPLACE INTO player_ids(sport, player_key, espn_athlete_id, name, resolved_at_utc, source, note) "
                 "VALUES (?,?,?,?,?,?,?)", (sport, pkey, aid, name, now_utc_iso, source, note))
    return aid, note


def record_projection(conn, *, sport, game_id, player_name, pkey, market, projection, sample_size,
                      inputs, athlete_id=None, reference_line=None, reference_snapshot_id=None,
                      model_version=MODEL_VERSION, source="espn_gamelog", est_prob_over=None):
    cur = conn.execute(
        "INSERT INTO projections(created_at_utc, created_at_ct, model_version, sport, game_id, espn_athlete_id, "
        "player_name, player_key, market, projection, est_prob_over, reference_line, reference_snapshot_id, "
        "sample_size, inputs_json, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (iso_utc(), iso_ct(), model_version, sport, game_id, athlete_id, player_name, pkey, market, projection,
         est_prob_over, reference_line, reference_snapshot_id, sample_size, json.dumps(inputs, default=str), source))
    return cur.lastrowid


def gamelog_url(sport, aid):
    return GAMELOG_URL.format(path=espn.PATHS[sport], aid=aid)
