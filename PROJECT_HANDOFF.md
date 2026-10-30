# Slate Hunter — Project Handoff

Shared source of truth for Claude (implementation) and ChatGPT (research and analytics review).
Last updated: 2026-10-29 (v0.6: pregame credit plan).

## 1. What Slate Hunter is

A free-first research and tracking system for NBA, NFL and college-football player props.
It is a **research tool, not a betting service**. It never invents lines, injuries, projections, odds movement or betting splits, and it never calls a prop guaranteed.

There are two parts:

| Part | Entry point | Role | Writes to the DB? |
|---|---|---|---|
| Web app | `app.py` + `templates/` | Interactive research UI (`/`) and read-only tracker dashboard (`/tracker`) | **No** |
| Collector | `python -m slatehunter …` | Scheduled collection, grading, reports, exports | **Yes (the only writer)** |

## 2. Architecture

```
ESPN public API ──┐                          ┌─> reports/YYYY-MM-DD.md
The Odds API ─────┼─> slatehunter collector ─┤
(DK, FD, PrizePicks)   (scheduler.py)        ├─> SQLite: data/slate_hunter.db
                                             └─> CSV exports
GitHub Actions (every 30 min) runs the collector and force-pushes the DB to the `data` branch
Render web app downloads a read-only copy of that DB  ──>  /tracker dashboard
```

### Package layout

| File | Purpose |
|---|---|
| `slatehunter/config.py` | Env-var config and UTC/Central-time helpers |
| `slatehunter/db.py` | Schema (`SCHEMA_VERSION = 1`), connection, integrity check |
| `slatehunter/sources/espn.py` | Scoreboard, box score, injury parsing, and the box-score → market map |
| `slatehunter/sources/odds_api.py` | The Odds API events/odds, market map, credit-cost estimate |
| `slatehunter/espn_players.py` | ESPN search/game-log parsing, moved **verbatim** from `app.py` (shared) |
| `slatehunter/collector.py` | Jobs: `collect_games`, `collect_injuries`, `collect_props`, `project`, line-move detection |
| `slatehunter/grading.py` | Job `grade`: results, grading every tracked prop, paper-pick settlement and CLV |
| `slatehunter/scheduler.py` | Decides which jobs are due; `loop` mode for always-on hosts |
| `slatehunter/reports.py` | Health, metrics, candidate ranking, CSV export, daily report, paper picks, splits import |
| `slatehunter/webdb.py` | Read-only DB access for the web app (local file or `SLATE_DB_URL`) |
| `.github/workflows/collector.yml` | Free always-on host |
| `deploy/slate-hunter-collector.service` | Optional systemd unit for a VPS |
| `tests/` | Offline tests with synthetic fixtures (`python -m unittest discover -s tests -v`) |

### Database tables

All timestamps are ISO-8601 UTC (`…Z`). Most tables also keep a Central Time copy (`*_ct`). Sort and compare on the UTC columns.

| Table | Write pattern | Contents |
|---|---|---|
| `collection_runs` | append | One row per job run: status (`ok`/`partial`/`failed`/`skipped`), counts, Odds API quota, code version |
| `collection_errors` | append | Every failure (`kind='error'`) and every missing item (`kind='missing'`): no API key, book returned nothing, unmatched event, credit reserve hit, game not final… |
| `games` | upsert by (sport, ESPN id) | Schedule/status/score metadata (not a market observation) |
| `prop_snapshots` | **append-only** | One row per side × book × line × run: source, book, market, player, line, American price (NULL when the source has none, e.g. PrizePicks), collection time UTC+CT, provider last-update |
| `market_signals` | append | `line_move` / `price_move` between our own snapshots (`evidence_level='observed_line_change'`), and imported ticket/handle % (`reported_split`, source URL required) |
| `injury_reports` | append on change | ESPN game-summary injury entries; a new row only when status or detail changes |
| `projections` | append | `model_version`, projection, `est_prob_over` (NULL until a calibrated model exists), sample size, `inputs_json` for reproducibility |
| `odds_calls` | append | One row per paid Odds API call: event, Central date, estimated and provider-reported credit cost, rows returned (schema v2) |
| `player_ids` | upsert | Provider name → ESPN athlete id, with how it was resolved (or why it wasn't) |
| `results` | upsert per (game, player, market) | Actual stat from the ESPN box score; DNP flagged |
| `prop_grades` | upsert per (game, book, market, player, side) | Open/close snapshot, line and price, actual, outcome (`win`/`loss`/`push`/`void`/`ungraded` + reason). Covers **every tracked prop, bet or not** |
| `paper_picks` | insert, then settle once | Simulated pick tied to the **exact snapshot** (line and price actually available), outcome, profit, CLV (line and no-vig probability) |

Grading rules:

- A prop is graded at its **closing snapshot**: the last snapshot before scheduled start.
- DNP → `void`.
- Player missing from the box score → `ungraded` with a reason. It is never graded as 0.
- Football exception: a player who appears in other box-score categories but not this one (e.g. a QB with no rush attempts) is graded as 0, and the reason is recorded.

## 3. Data sources, limits and licensing

| Source | Used for | Cost / limits | Terms / notes |
|---|---|---|---|
| ESPN site API (`site.api.espn.com`, `site.web.api.espn.com`) | Schedules, scores, box scores, injuries, player search, game logs | Free, no key. **Unofficial and undocumented**; can change shape or rate-limit at any time. The client spaces requests ≥0.35 s and retries once. | Personal, low-volume research only. Subject to ESPN's terms of use. Do not redistribute bulk data. |
| The Odds API (`api.the-odds-api.com`, v4) | DraftKings, FanDuel (`us` region) and PrizePicks (`us_dfs` region) player props | Free Starter plan: 500 credits/month (verify on the-odds-api.com). `/events` is free. Event odds cost ≈ markets × regions, and with the `bookmakers` parameter each group of ≤10 books counts as 1 region. Defaults: 3 markets × 1 = **~3 credits per game per snapshot**. | Licensed API; key required (`ODDS_API_KEY`). Respect their ToS; don't share the key. |
| PrizePicks / DraftKings / FanDuel sites | **Not scraped.** | — | Collected only through the licensed Odds API. No private accounts, no bypassing access controls. |
| Betting splits (ticket/handle %) | Optional manual import (`import-splits`) | — | No free reliable feed is wired. Every row needs a source URL. A "sharp money" claim with no data is never stored as a signal. |

**Credit plan (v0.6, default in the GitHub workflow):** the free tier cannot snapshot every slate hourly (your first hourly-style run used 47 credits). The workflow therefore runs in *pregame mode*:

- Every 30-minute run checks for games starting within `SLATE_PROPS_PREGAME_WINDOW_MIN` = 90 minutes.
- Each game gets at most `SLATE_PROPS_SNAPSHOTS_PER_EVENT` = 1 paid snapshot, which lands about 60–90 min before its own start, so the closing line is close to the real close.
- Games are taken **soonest first across all sports**, at most `SLATE_MAX_EVENTS_PER_RUN` = 5 per run.
- Spending stops at `SLATE_ODDS_DAILY_CREDIT_BUDGET` = 15 estimated credits per Central-Time day (≈5 games/day, ≈465/month). The `SLATE_ODDS_CREDIT_RESERVE` guard still applies.
- Every paid call is recorded in `odds_calls` (estimated cost, provider-reported cost, rows returned). Every game skipped for cap, budget or reserve is logged in `collection_errors` (`RunCapReached`, `DailyBudget`, `CreditReserve`).
- Trade-off: with ~5 games/day, the rest of a big slate gets no lines. Raise the budget (paid plan) or add a free second source (PropLine, under evaluation) for full coverage.
- Simulated over 48 scheduled runs: each game was snapshotted exactly once, ~85 min before start (`tests/test_pipeline.py::CreditControlTests`).

## 4. Setup

### Local

```bash
pip install -r requirements.txt
export ODDS_API_KEY=...            # optional; without it prop collection is logged as skipped
python -m slatehunter init-db
python -m slatehunter run-scheduled   # runs whatever is due
python -m slatehunter status
python app.py                         # http://localhost:5000  and  /tracker
python -m unittest discover -s tests -v
```

### Always-on collector (free): GitHub Actions

1. Merge this branch to `main`.
2. Repo → Settings → Secrets and variables → Actions → **New repository secret** `ODDS_API_KEY`.
3. Optional **Variables**: `SLATE_SPORTS`, `SLATE_BOOKS`, `SLATE_PROPS_ACTIVE_INTERVAL_MIN`, `SLATE_PROPS_IDLE_INTERVAL_MIN`, `SLATE_ODDS_CREDIT_RESERVE`, `SLATE_MAX_EVENTS_PER_RUN`.
4. Actions tab → `collector` → **Run workflow** once (optionally force `collect_games`).
5. Confirm a `data` branch appears containing `slate_hunter.db` and `reports/`.

How each run works: restore the DB from `data` → run due jobs → write the report → guard → force-push a single-commit snapshot → upload a twice-daily backup artifact (kept 14 days).

The guard fails the run, so nothing is pushed, when `PRAGMA integrity_check` fails or when any append-only table has fewer rows than before the run. If the `data` branch exists but the DB can't be restored, the run aborts instead of starting fresh.

Caveats:

- The repo is public, so the `data` branch (including paper picks) is public.
- GitHub can delay scheduled runs, and it disables schedules after 60 days without repo activity.
- GitHub blocks files over 100 MB; the workflow warns at 40 MB.

### Alternative host: VPS or home server

Run `python -m slatehunter loop` under `deploy/slate-hunter-collector.service`, with env vars in `.env`. Point the web app's `SLATE_DB_PATH` at the same file.

### Web app on Render (free)

`render.yaml` sets `SLATE_DB_URL` to the raw `data`-branch DB, refreshed every 10 minutes. Render's free filesystem is ephemeral and can't attach disks, so it must never be the database host.

### Config reference (env vars)

`SLATE_DB_PATH` (default `data/slate_hunter.db`), `ODDS_API_KEY`, `SLATE_SPORTS` (NBA,NFL,CFB), `SLATE_BOOKS` (draftkings,fanduel,prizepicks), `SLATE_DAYS_AHEAD` (3), `SLATE_GAMES_INTERVAL_MIN` (180), `SLATE_PROPS_ACTIVE_INTERVAL_MIN` (60, used when a game starts within `SLATE_ACTIVE_WINDOW_HOURS`=6), `SLATE_PROPS_IDLE_INTERVAL_MIN` (360), `SLATE_PROPS_LOOKAHEAD_HOURS` (36), `SLATE_GRADE_INTERVAL_MIN` (60), `SLATE_ODDS_CREDIT_RESERVE` (25), `SLATE_MAX_EVENTS_PER_RUN` (15; workflow 5), `SLATE_PROPS_PREGAME_WINDOW_MIN` (0 = off; workflow 90), `SLATE_PROPS_SNAPSHOTS_PER_EVENT` (0 = unlimited; workflow 1), `SLATE_ODDS_DAILY_CREDIT_BUDGET` (0 = none; workflow 15), `SLATE_HTTP_TIMEOUT` (15). When the pregame window is on, the props interval settings are ignored. Web only: `SLATE_DB_URL`, `SLATE_DB_REFRESH_SEC`.

### CLI

`python -m slatehunter --help` lists every command:

- Jobs: `run-scheduled`, `loop`, `collect-games`, `collect-injuries`, `collect-props`, `project`, `grade`
- Reporting: `status`, `report`, `export`, `export-all`, `candidates`
- Paper picks: `snapshots --player`, `paper-pick`
- Other: `import-splits`, `integrity`

Paper-pick flow:

```bash
python -m slatehunter snapshots --player "LeBron James"     # find the snapshot id
python -m slatehunter paper-pick 1234 --stake 1 --why "baseline gap +2.1, no injury flag"
```

A pick is refused if its snapshot is over 3 h old, if the game has started, or if the snapshot isn't matched to an ESPN game.

## 5. Status: what is done and how it was verified

**Do not treat any item as operational in production until it is checked against a live run.**

| Feature | Status | Verification |
|---|---|---|
| Existing UI and endpoints (`/api/hunt`, `/api/player-search`, `/api/player-research`, `/api/live-game`, `/api/prop-line`) | Preserved; parsing helpers moved verbatim to `slatehunter/espn_players.py` | Import and route tests pass. Live behavior was not re-tested: the dev sandbox blocks ESPN and Odds API hosts. |
| SQLite schema, append-only snapshots, run and error logging | Done | Unit tests |
| ESPN schedule collection; game upsert with UTC + CT | Done | Tests on synthetic ESPN-shaped fixtures. **Not yet run live.** |
| Odds API prop snapshots (DK, FD, PrizePicks), quota capture, credit guard, per-book missing rows, unmatched-event logging | Done | Tests on synthetic fixtures. **Not yet run live**; confirm the `us_dfs`/PrizePicks response shape on the first keyed run. |
| Line/price move detection (no sharp-money inference) | Done | Unit tests |
| Injury history (append on change) | Done | Unit tests; ESPN injury shape needs a live check |
| baseline_v1 projections from ESPN game logs, with ESPN id resolution and caching | Done | Unit tests on synthetic fixtures |
| Grading every tracked prop; DNP/void and ungraded reasons; paper picks with profit and CLV | Done | Unit tests (NBA and NFL box-score shapes) |
| Metrics (hit rate, MAE/bias, ROI, CLV), candidate ranking, daily report, CSV export | Done | Unit tests + CLI smoke test |
| `/tracker` dashboard, `/api/tracker/*`, `/api/export/<table>.csv` | Done | Flask test client against local and HTTP-served DBs |
| GitHub Actions collector and `data`-branch persistence | Written | Git persistence steps simulated locally (3 runs against a bare remote; guard caught a deliberate row deletion). **The workflow has not run on GitHub yet.** |
| Bug fix: NFL "touchdowns" mapped to `player_pass_tds` | Not changed in the interactive UI (left for review); the collector does not collect TD props | — |

### Live verification checklist (first run after merge)

1. Run the `collector` workflow manually. In the Actions log, check that `collect_games` is `ok` and `games` > 0.
2. With `ODDS_API_KEY` set, check that `collect_props` writes `prop_snapshots`, `quota_remaining` is populated, and `collection_errors` shows which books returned nothing.
3. Spot-check 3 snapshot rows against the sportsbook app at roughly the same time.
4. The day after games: `grade` produces `prop_grades`. Spot-check 3 actual values against the ESPN box score.
5. CFB: confirm the scoreboard call with `groups=80` returns the full FBS slate, not only featured games.
6. Save one real ESPN scoreboard, summary and Odds API response into `tests/` as recorded fixtures, replacing the synthetic ones.

## 6. Outstanding work (in priority order)

1. Run the live verification above and fix any shape mismatches.
   - Done 2026-10-24: first live run OK (98 games, 1,812 snapshots, 0 unmatched). Fixed 2026-10-29: run cap now fills soonest-first across sports (CFB had been starved); projection and error timestamps now use the job clock.
2. Fix or remove the NFL/CFB `touchdowns` mapping in the interactive UI (`app.py` `ODDS_MARKETS`). It currently asks for passing TDs for every player.
3. The interactive "evidence signals" tally counts overlapping samples (L5, L10, season) as separate signals. Show them as one historical signal.
4. More markets (NBA threes/PRA are already parsed; football receptions; anytime TD as a yes/no market), each with grading tests.
5. A calibrated probability model (e.g. a distribution fit per player/market, validated out of sample) so `est_prob_over` can be filled and EV ranking replaces the descriptive gap. Until then, rankings are descriptive only.
6. Closing-line capture: an optional short pregame snapshot interval (credit permitting).
7. When the DB nears 40 MB: move the collector to a VPS, or switch to monthly DB files.

## 7. How ChatGPT can review the project independently

ChatGPT has no write access to the database, and its scheduled research tasks do not feed it. Review read-only from:

- **Code:** `https://github.com/GlizZoCkProps/slate-hunter-mobile` (branch `main`), starting with this file.
- **Data:** the `data` branch (`slate_hunter.db`, `reports/YYYY-MM-DD.md`), or the deployed dashboard's CSV endpoints: `/api/export/prop_snapshots.csv`, `/api/export/prop_grades.csv`, `/api/export/projections.csv`, `/api/export/paper_picks.csv`, `/api/export/collection_errors.csv` (each accepts `?since=2026-10-01T00:00:00Z`). The daily report is at `/api/tracker/report`.

Suggested review checks:

1. **Data quality:** share of runs `failed`/`partial` (from `collection_runs`), and the most common `collection_errors.error_type`.
2. **Line spot-checks:** compare a sample of `prop_snapshots` (book, player, line, price, `collected_at_ct`) with independent sources from the same time window.
3. **Grading audit:** re-derive 10 `prop_grades` outcomes from ESPN box scores.
4. **Model evaluation:** MAE/bias by market from `projections` × `results`, using only projections created before `games.start_time_utc`. Check hit rate and CLV on `paper_picks`, and sample sizes before drawing conclusions.
5. **Claims audit:** every `market_signals` row with `evidence_level='reported_split'` must have a working `source_url`. `observed_line_change` rows must never be described as sharp action.
6. Report findings as a GitHub issue, or as a markdown file in `reviews/` via a PR, so Claude can act on them.

## 8. Research standards (enforced in code where possible)

- **Never invent data.** Missing values are stored as NULL and logged in `collection_errors`.
- **Separate verified, estimated and unavailable data.** Verified means `prop_snapshots`/`results` from a named source. Estimated means `projections` with a `model_version`. Unavailable means `collection_errors` with kind `missing`.
- **No sharp-money claims without data.** Line moves are labeled `observed_line_change` with an explicit note.
- **Record the price actually available.** Paper picks reference an immutable snapshot ≤3 h old, taken pregame.
- **No guarantees.** Rankings are descriptive (`gap`) until a calibrated model exists.
