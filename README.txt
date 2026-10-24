SLATE HUNTER v4 — CONSOLIDATED FREE-FIRST RELEASE

NEW IN 0.5 (feature/persistent-collector): persistent SQLite tracking.
- `python -m slatehunter ...` collector: games, prop snapshots (DraftKings,
  FanDuel, PrizePicks via The Odds API), injuries, baseline projections,
  post-game grading of every tracked prop, paper picks with CLV, daily reports,
  and CSV exports. Every run and every failure is logged in the database.
- Free always-on host: .github/workflows/collector.yml (database kept on the
  `data` branch). Dashboard: /tracker.
- Full architecture, setup, data-source limits and verification status:
  see PROJECT_HANDOFF.md. Tests: python -m unittest discover -s tests -v

WHAT THIS BUILD DOES
- Scans NFL, NBA and college football schedules from ESPN's public scoreboard endpoints.
- Separates upcoming games, games in progress, and completed games.
- Displays game start times in the phone/browser's local timezone.
- Includes player search using ESPN's public search endpoint.
- Attempts to retrieve player game logs and calculates a simple recent/season weighted baseline for selected markets.
- Lets you enter a posted line manually and compares it to that simple baseline.
- Generates a research brief with rules against inventing lines or data.
- The interactive research page does not require a database or paid API key.

IMPORTANT LIMITATIONS
- This is a research assistant, not a betting guarantee.
- ESPN's public endpoints are unofficial and can change shape or rate-limit requests.
- Player search and log parsing are best-effort; inspect the displayed sample, metric label and source before trusting any result.
- Player prop lines are NOT fetched automatically. Enter the line from the platform/book you are looking at. The free tier of many odds APIs does not include player props. A truly automated cross-book prop-line feed may require a paid provider or a separately configured data source.
- The projection is a simple heuristic (65% recent 5-game mean + 35% available sample mean), not a calibrated statistical model. It does not fully adjust for minutes, injuries, role, pace, matchup, weather, or lineup changes.
- No picks are guaranteed. Verify active status and current lines before making decisions.

DEPLOYMENT ON RENDER
1. Download and extract the ZIP on a computer.
2. Open your existing GitHub repository: GlizZoCkProps/slate-hunter-mobile.
3. Replace the repository files with the files from this ZIP, preserving templates/index.html inside the templates folder.
4. Commit changes to the main branch.
5. Render should redeploy the existing service automatically. Keep the Free plan; do not upgrade unless you choose to.
6. Test /api/health, then open the app and press HUNT PROPS.

REPOSITORY LAYOUT
app.py                      web app (research UI + read-only tracker)
slatehunter/                collector, database, grading, reports
templates/index.html        research UI
templates/tracker.html      tracker dashboard
tests/                      offline tests
.github/workflows/          scheduled collector
deploy/                     optional systemd unit
PROJECT_HANDOFF.md          start here

The research UI needs no API key. Automatic prop lines need ODDS_API_KEY.
