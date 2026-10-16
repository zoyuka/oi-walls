# OI Walls — free GEX-style levels, auto-published daily

Every weekday at 8:15am ET, GitHub runs `make_site.py` in the cloud:
pulls option chains (Yahoo), finds top open-interest call/put walls for
SPX / SPY / QQQ, renders charts, and publishes a mobile dashboard.

## Setup (one time, ~5 min, all doable from a browser)
1. Create a GitHub account (free) and a new repository, e.g. `oi-walls`.
2. Upload everything in this folder (drag & drop on github.com works,
   including the `.github` folder — use "Add file > Upload files").
3. Repo **Settings > Pages** > Source: "Deploy from a branch" >
   Branch: `main`, folder: `/docs` > Save.
4. Repo **Actions** tab > enable workflows > run "Build OI Walls dashboard"
   once manually to test.
5. Your dashboard is live at:
   `https://YOURUSERNAME.github.io/oi-walls/`
   Bookmark it on your phone home screen.

## Daily use
- Open the URL each morning — charts, wall tables, and ToS study text.
- Tap "ToS study text" to copy fresh strikes into thinkorswim
  (or update the 12 inputs in your saved study).
- Re-run anytime from the GitHub mobile app: Actions > Run workflow.

## Notes
- Cron is set for EDT (12:15 UTC). In winter (EST) edit
  `.github/workflows/daily.yml` to `15 13 * * 1-5`.
- OI updates once daily (OCC, pre-market). Intraday volume streams
  live inside the ToS study labels (white label = vol > OI).
