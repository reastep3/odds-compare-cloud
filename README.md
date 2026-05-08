# Odds Compare — +EV Edge Finder

A Telegram bot **and** progressive web app ("Arbyx") that scan DraftKings and FanDuel lines against sharper books, flag positive-EV bets, size them with Kelly, capture closing lines for CLV, and pick optimal bets + parlays for sportsbook promos (including photo-extracted ones).

Runs on [Fly.io](https://fly.io) as a single always-on machine. The Telegram bot uses long-polling and the webapp is served from the same process via an `[http_service]` block — both share one asyncio loop.

**Keys required to run**: a Telegram bot token, a [The Odds API](https://the-odds-api.com/) key, and optionally an [Anthropic API](https://www.anthropic.com/api) key for the photo→promo feature.

---

## What it does

- **Scans 13 sports** for game lines (NBA, MLB, NHL, NFL, NCAAB, NCAAF, UFC, soccer — MLS + 9 European leagues, WNBA, AFL, Boxing, Cricket, Tennis) and player props for NBA / MLB / NHL.
- **Anchors fair probability on Pinnacle** when available (~2% vig); falls back to a consensus of ~13 books.
- **Captures closing lines** automatically — every +EV pick is re-queried 0–15 min before commence so each bet gets a CLV (closing-line value) measurement.
- **Outputs**:
  - **Summary** — edge counts, Pinnacle matches, by-book splits.
  - **Play These** — the filtered subset worth real money (calibrated against historical ROI per sport / market).
  - **Promo Picks** — best straight-boost pick per promo.
  - **Parlay Picks** — best N-leg parlay per parlay/SGP promo.
  - **Excel workbook** with every edge, all books, confidence tiers, CLV columns, and a dedicated Promos tab.

## Data sources

**Primary: [The Odds API](https://the-odds-api.com/)** (paid, requires `ODDS_API_KEY`). One call per sport returns DraftKings, FanDuel, Pinnacle, Bovada, BetMGM, ESPN Bet, Caesars, PointsBet, and several more — covers main lines and player props.

**Pinnacle direct guest API** (`PINNACLE_GUEST_KEY`) for game-line coverage. Pinnacle's prices weight 65% of the fair-probability estimate when available.

**Result grading** (for `/backtest`):
- ESPN scoreboard — most US team sports, major European soccer, IPL cricket via numeric league IDs
- TennisExplorer (scraped) — ATP / WTA singles
- BoxLive (scraped) — boxing
- Squiggle (free API) — AFL
- MLB Stats API — MLB box scores for `total_bases` / `strikeouts` / `hits` / etc.

**Optional: [Anthropic Claude API](https://www.anthropic.com/api)** — only for the photo→promo feature, ~$0.003 per image.

## Promo handling

Promos are parsed from a compact string syntax (filler words like "any", "event", "profit boost" are ignored).

```
dk 25% 3+ nhl +100 $10
dk 25% any ice hockey event, 3+ leg parlays, $10 max wager, +100 min
```

Supported modifiers:

- `25%` / `bonus $25` — boost percent or bonus-bet amount
- `3-leg` / `3+` — parlay (exactly N or N-or-more legs)
- `sgp` — same-game parlay
- `game:Braves-Phillies` — restrict to one matchup
- `+100` / `-200` — min odds (signed American)
- `$10` — max bet
- `2x`, `3x` — count: multiple identical copies of the promo
- Sport aliases: `hockey`/`nhl`, `basketball`/`nba`, `football`/`nfl`, `baseball`/`mlb`, `golf`/`pga`, `mma`/`ufc`, plus phrases like `ice hockey`, `premier league`, `college basketball`

### Photo → promo via Claude vision

Send a screenshot of a promo card at the promo step — [Claude Sonnet 4.6](https://docs.anthropic.com/claude/docs/models-overview) extracts the compact promo string automatically. Stepped/tiered boosts (e.g. "20% for 2 legs, 33% for 3, 50% for 4+") collapse to the highest tier.

## Bot commands

| Command | What it does |
|---|---|
| `/run` | Full scan — bankrolls + promos + Excel output. |
| `/run_skip` | Picks only — no bankrolls, no promos, no bet sizing. |
| `/run_single` | Run for a single book of your choosing. |
| `/run_promos` | Bankrolls + promos only. No Summary, no Excel. |
| `/run_live` | Live in-progress games only (for live promo use). |
| `/backtest` | Backtest archived picks against grading sources. |
| `/digest` | Weekly P&L summary. |
| `/books` | Set or change which sportsbooks you use. |
| `/kelly` | View / set Kelly fraction preference. |
| `/quickbet` | Standalone Kelly calculator (no scan). |
| `/status` | Check if a run is in progress. |
| `/cancel` | Cancel the current conversation. |
| `/add_user` | (Admin only) Authorize a new user by chat ID. |
| `/help` | Command list. |

## Webapp ("Arbyx")

Same backend; PWA delivered at `/` on the Fly host. Sign up with email + password, or sign in with the legacy admin password (`WEBAPP_PASSWORD`). Auth tokens are HMAC-signed and last 30 days.

API routes:

- `POST /api/login`, `POST /api/signup` — auth
- `POST /api/run`, `POST /api/run_live` — fire a scan, returns JSON
- `GET /api/archives`, `GET /api/backtest_archives` — list / download prior reports
- `POST /api/backtest` — run a custom date-range backtest
- `GET /api/health` — liveness check (used by Fly)

Installable as a home-screen PWA on iOS / Android.

## Closing-line capture (CLV)

Every +EV pick is appended to `pending_closes_<date>.json` when first seen. A worker fires every minute and re-queries the Odds API per-event endpoint 0–15 min before commence; the captured close + `clv_pct` attaches to the pick during backtest replay.

CLV converges on whether a model has a real edge much faster than win/loss does — variance washes out of CLV in dozens of bets vs. hundreds for W/L. When the worker can't match a pick at close (line moved past tolerance, market suspended, etc.), the reason is persisted to the pending_closes JSON under `last_no_match` so misses are auditable rather than inferred.

## How the math works

- **Fair probability**: Pinnacle-anchored = `0.65 × Pinnacle + 0.35 × consensus`. Without Pinnacle, `(median + sharpest) / 2`. Clamped to [5%, 95%].
- **EV %** = `fair_prob × decimal_odds − 1`.
- **Kelly sizing**: half-Kelly of your bankroll by default, clamped to the book's minimum bet. Adjustable per-user via `/kelly`.

**Play These qualifying rules** (calibrated against backtest cohorts; see docstrings on `_qualifies()` / `_qualifies_prop()` in `odds_compare.py` for the per-rule numbers):

- Game lines need ≥1.0% EV with Pinnacle anchor, otherwise ≥2.5%, plus a minimum of 4 reference books.
- H2H: MLB and MLS only.
- Spreads: any sport.
- Totals: MLB only AND Pinnacle-anchored.
- NBA game lines blocked entirely. European soccer (EPL, Bundesliga, Champions, Europa, LaLiga, Ligue 1, Serie A) blocked.
- Player props: per-market thresholds — `pts` ≥ 3.0%, `strikeouts` ≥ 2.0%, `total_bases` ≥ 1.5%. Other prop markets (rebounds, assists, hits, RBIs, etc.) excluded for negative ROI.
- Game must commence within 1 day.

**Profit boost**: `boosted_profit = profit × (1 + boost%/100)`. The promo picker ranks candidates by **boosted** EV, so long-shot bets that are -EV pre-boost but +EV post-boost can still win the pick — which a pre-boost-only search would miss.

**Parlay promos**: for each promo, the best parlay is found across every candidate game (for SGP) and every leg count (`N`, `N+1`, ..., `N+3`); only the single best by boosted EV is emitted.

## Project layout

```
odds_compare.py     — Fetch + edge-finding + promo picker + Excel output.
telegram_bot.py     — Bot handlers, promo parser, photo→promo flow, formatters.
webapp.py           — FastAPI webapp (PWA backend + REST API).
webapp_users.py     — User store (signup, password verify).
closing_lines.py    — Closing-line capture worker (CLV).
backtest.py         — Offline P&L report against grading sources.
bot_config.py       — Token + secret loader.
pull_latest.py      — Pull latest Excel off the Fly volume.
pull_range.py       — Bulk-pull archives.

static/             — PWA shell (index.html, app.js, style.css, sw.js, manifest, icons).

test_*.py           — pytest suite (closing-lines, math, users).

Dockerfile          — Python 3.13-slim image.
fly.toml            — Fly app config (one machine, 512 MB RAM, /data volume).
requirements.txt    — Runtime deps.
requirements-dev.txt — pytest + dev tools.
.env.example        — Sample env vars.
```

## Running locally

```bash
pip install -r requirements.txt

# Required
export TELEGRAM_BOT_TOKEN=your-bot-token            # from @BotFather
export TELEGRAM_CHAT_ID=your-chat-id                # from @userinfobot
export ODDS_API_KEY=your-odds-api-key               # from the-odds-api.com
export WEBAPP_PASSWORD=admin-password               # legacy /api/login

# Optional
export PINNACLE_GUEST_KEY=...                       # see odds_compare.py for how to grab
export ANTHROPIC_API_KEY=sk-ant-...                 # for photo→promo

python telegram_bot.py    # bot + webapp on http://localhost:8080
```

## Deploying to Fly

```bash
fly launch --no-deploy             # first time only
fly secrets set \
  TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_CHAT_ID=... \
  ODDS_API_KEY=... \
  WEBAPP_PASSWORD=... \
  PINNACLE_GUEST_KEY=... \
  ANTHROPIC_API_KEY=sk-ant-...     # optional
fly deploy
```

The `fly.toml` provisions a persistent `/data` volume for Excel archives, `prev_play_keys.json`, `users.json`, and `pending_closes_*.json`. The `[http_service]` block exposes the webapp at `https://<app>.fly.dev/`.

## Disclaimer

This is a personal tool. +EV betting over the long run has positive expected value, but short-term variance is real — no individual pick is guaranteed. Bet with money you can afford to lose, respect your jurisdiction's laws, and don't share your bot with anyone you wouldn't lend money to.
