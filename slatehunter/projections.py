"""Projection models. Each model has a version string stored with every row.

baseline_v1   (Oct 24 rows only) 0.65*mean(last 5) + 0.35*mean(all) over ESPN's whole
              game log. Bug: the log mixes in PRESEASON games and is not guaranteed
              newest-first. Kept only so old rows stay reproducible.
baseline_v1_1 same formula, regular-season/postseason games only, sorted newest first.
shrink_v2     baseline_v1_1 shrunk toward last season's per-game mean while the current
              sample is small:  w = n / (n + K),  proj = w*current + (1-w)*last_season.
              Falls back to baseline_v1_1 when no prior-season log is available.
None of these is calibrated: est_prob_over stays NULL. Do not present them as probabilities.
"""
import json
import statistics

from .config import iso_ct, iso_utc
from .espn_players import choose_metric, flatten_stat_labels, get_search_items
from .sources import espn
from .util import player_key

MODEL_VERSION = "baseline_v1_1"
PRIMARY_MODEL = "shrink_v2"      # used for candidate ranking
SHRINK_K = 8                     # games of weight given to last season
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


def _excluded_season_type(st):
    name = " ".join(str(st.get(k) or "") for k in ("displayName", "name", "abbreviation")).lower()
    return "preseason" in name or "pre season" in name or "all-star" in name or "all star" in name


def clean_gamelog(data):
    """Drop preseason/All-Star season types and sort game rows newest first.

    Returns (labels, rows, excluded_count). Rows without a parseable date keep
    their relative order after dated rows.
    """
    if not isinstance(data, dict):
        return [], [], 0
    seasons = data.get("seasonTypes")
    excluded = 0
    if isinstance(seasons, list):
        kept = []
        for st in seasons:
            if isinstance(st, dict) and _excluded_season_type(st):
                for cat in st.get("categories") or []:
                    excluded += len((cat or {}).get("events") or [])
                continue
            kept.append(st)
        data = {**data, "seasonTypes": kept}
    labels, rows = flatten_stat_labels(data)
    meta = data.get("events") if isinstance(data.get("events"), dict) else {}

    def when(row):
        raw = row.get("raw") or {}
        m = meta.get(str(raw.get("eventId") or raw.get("id") or ""), {}) if isinstance(meta, dict) else {}
        return str(m.get("gameDate") or m.get("date") or "")

    dated = [r for r in rows if when(r)]
    undated = [r for r in rows if not when(r)]
    dated.sort(key=when, reverse=True)
    return labels, dated + undated, excluded


def baseline_v1_1(values):
    return baseline_v1(values)


def shrink_v2(values, prior_mean, k=SHRINK_K):
    cur = baseline_v1(values)
    if prior_mean is None:
        return cur
    if cur is None:
        return round(prior_mean, 2)
    w = len(values) / (len(values) + k)
    return round(w * cur + (1 - w) * prior_mean, 2)


def season_for(sport, when):
    """ESPN season year of the season in progress on `when`, and the prior one."""
    if sport == "NBA":
        cur = when.year + 1 if when.month >= 8 else when.year
    else:
        cur = when.year if when.month >= 3 else when.year - 1
    return cur, cur - 1


def gamelog_values(data, sport, market, legacy=False):
    """Most-recent-first values. legacy=True reproduces baseline_v1's raw parsing."""
    if legacy:
        labels, rows = flatten_stat_labels(data)
    else:
        labels, rows, _ = clean_gamelog(data)
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
                      model_version=MODEL_VERSION, source="espn_gamelog", est_prob_over=None, now=None):
    cur = conn.execute(
        "INSERT INTO projections(created_at_utc, created_at_ct, model_version, sport, game_id, espn_athlete_id, "
        "player_name, player_key, market, projection, est_prob_over, reference_line, reference_snapshot_id, "
        "sample_size, inputs_json, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (iso_utc(now), iso_ct(now), model_version, sport, game_id, athlete_id, player_name, pkey, market, projection,
         est_prob_over, reference_line, reference_snapshot_id, sample_size, json.dumps(inputs, default=str), source))
    return cur.lastrowid


def gamelog_url(sport, aid):
    return GAMELOG_URL.format(path=espn.PATHS[sport], aid=aid)


def gamelog_params(season=None):
    return {"season": str(season)} if season else None
