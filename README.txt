SLATE HUNTER v4 — CONSOLIDATED FREE-FIRST RELEASE

WHAT THIS BUILD DOES
- Scans NFL, NBA and college football schedules from ESPN's public scoreboard endpoints.
- Separates upcoming games, games in progress, and completed games.
- Displays game start times in the phone/browser's local timezone.
- Includes player search using ESPN's public search endpoint.
- Attempts to retrieve player game logs and calculates a simple recent/season weighted baseline for selected markets.
- Lets you enter a posted line manually and compares it to that simple baseline.
- Generates a research brief with rules against inventing lines or data.
- Does not require a database or paid API key.

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
app.py
requirements.txt
render.yaml
README.txt
templates/index.html

NO API KEYS REQUIRED.
