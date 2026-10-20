SLATE HUNTER v3 — DEPLOYABLE STARTER

WHAT WORKS
- Mobile-first web app.
- One-tap scan request to backend.
- Fetches upcoming NFL, NBA, and college football schedules from ESPN's public scoreboard endpoints for today, 3 days, or 7 days.
- Builds a grounded research brief using the returned slate.
- No tracker/database.

IMPORTANT LIMITATION
This is the first deployable backend step, not yet a fully autonomous player-prop projection engine. ESPN schedule endpoints do not provide player props. Free odds API tiers commonly exclude player props; The Odds API documentation currently says its free tier is limited to NFL/NBA/MLB moneyline (h2h) odds, while player-prop edge data is a paid Business feature. Do not describe this starter as already finding verified player props or projections.

DEPLOY FOR FREE WITH RENDER
1. Create/sign into GitHub and create a new repository named slate-hunter-mobile.
2. Upload the files in this folder to the repository root, keeping templates/index.html in the templates folder.
3. Create/sign into Render.
4. Choose New > Web Service and connect the repository.
5. Settings:
   - Runtime: Python
   - Build command: pip install -r requirements.txt
   - Start command: gunicorn app:app
   - Instance type: Free
6. Deploy. Render gives you an HTTPS URL; open it on your Galaxy S26 and add it to your home screen from Chrome's menu.

Render free services sleep after 15 minutes idle and can take about a minute to wake up. Free host storage is ephemeral, but this app does not rely on local persistent storage.

NEXT ENGINE MILESTONES
1. Add a permitted player-stat source for season/recent logs and usage.
2. Add a reliable player-prop line source covering relevant sports/books.
3. Add projection methodology, probability calibration, and cross-book line comparison.
4. Only call a prop a value edge when a current line and evidence-backed projection both exist.

Do not paste API keys into frontend HTML or commit them to GitHub. If/when a provider key is needed, add it as a Render environment variable.
