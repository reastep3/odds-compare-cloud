# Odds Compare — +EV Edge Finder

A Telegram bot that scans DraftKings and FanDuel lines against sharper markets, flags positive-EV bets, sizes them with Kelly, and finds optimal picks + parlays for sportsbook promos (including photo-extracted ones).

Runs on [Fly.io](https://fly.io) as a small always-on worker. The bot itself uses Telegram long-polling — no public HTTP endpoint needed.

---

## What it does

- **Scans 8 sports** (NBA, MLB, NHL, NFL, NCAAB, NCAAF, Soccer, UFC) and MLB HR / PGA / player props in ~60–90s.
- **Anchors fair probability on Pinnacle** (the sharpest book, ~2% vig) when available; falls back to a consensus of ~13 books via Action Network and Bovada.
- **Outputs**:
  - **Summary** — counts of edges, Pinnacle matches, etc.
  - **Play These** — the picks worth real money (EV ≥ 2%, or ≥ 1% with Pinnacle anchor).
  - **Promo Picks** — best straight-boost pick per promo.
  - **Parlay Picks** — best N-leg parlay per parlay/SGP promo, with the broader candidate pool so long-shot legs can turn a -EV parlay into a +EV boosted one.
  - **Excel workbook** (for `/run`) with every edge, all books, confidence tiers, and a dedicated Promos tab.

## Data sources (zero paid APIs)

1. **Action Network** internal API — aggregates up to 13 US books.
2. **Pinnacle** public guest API — used as the primary fair-line anchor.
3. **Bovada** public JSON API — extra sharp consensus line.

If any source breaks (header rotation, etc.), the module-level docstring in `odds_compare.py` has troubleshooting notes.

## Promo handling

Promos are parsed from a compact string syntax (flexible — filler words like "any", "event", "profit boost" are ignored). Example promo strings that all parse the same:

```
dk 25% 3+ nhl +100 $10
dk 25% ice hockey 3+ leg parlay +100 min max:$10
dk 25% any ice hockey event, 3+ leg parlays, $10 max wager, +100 min
```

Supported modifiers:

- `25%` / `bonus $25` — boost percent or bonus-bet amount
- `3-leg` / `3+` — parlay (exactly N or N-or-more legs)
- `sgp` — same-game parlay
- `game:Braves-Phillies` — restrict to one matchup (auto-resolved after fetch)
- `+100` / `-200` — min odds (signed American); `min:+100` also works
- `$10` — max bet; `max:$10` also works
- `2x`, `3x` — count: you have multiple identical copies (produces N distinct picks)
- Sport aliases: `hockey`/`nhl`, `basketball`/`nba`, `football`/`nfl`, `baseball`/`mlb`, `golf`/`pga`, `mma`/`ufc`, plus phrases like `ice hockey`, `premier league`, `college basketball`

### Photo → promo via Claude vision

Send a screenshot of a promo card at the promo step — [Claude Sonnet 4.6](https://docs.anthropic.com/claude/docs/models-overview) extracts the compact promo string automatically. The bot then asks you to confirm DraftKings vs FanDuel (once, per photo) since the two books' new dark-theme promo cards can look nearly identical without visible brand text. Stepped/tiered boosts (e.g. "20% for 2 legs, 33% for 3, 50% for 4+") are collapsed to the highest tier.

Costs roughly $0.003 per screenshot on the Anthropic API (set a $5 spend limit in your [console settings](https://console.anthropic.com/settings/billing) for zero surprise).

## Bot commands

| Command | What it does |
|---|---|
| `/run` | Full odds compare — asks for DK + FD bankrolls + any promos, runs the scan, sends all output + Excel. |
| `/run_single` | Run for a single book of your choosing. |
| `/run_skip` | Just the picks — no bankrolls, no promos, no bet sizing. |
| `/run_promos` | Bankrolls + promos only. Skips the Summary and Play These messages; no Excel file. |
| `/run_live` | Live in-progress games only (for live promo use). |
| `/recent` | (During promo step) re-show the saved-promos keyboard. |
| `/backtest` | Backtest past picks from archived `ev_edges_*.xlsx` files against ESPN's scoreboard API. |
| `/books` | Set or change which sportsbooks you use. |
| `/add_user` | (Admin only) Authorize a new user by chat ID. |
| `/status` | Check if a run is in progress. |
| `/cancel` | Cancel the current conversation. |
| `/help` | Command list. |

## How the math works

- **Fair probability**: when Pinnacle has a line, `fair_prob = 0.65 × Pinnacle + 0.35 × AN_consensus`. Without Pinnacle, `(median_consensus + sharpest_line) / 2`. Clamped to [5%, 95%].
- **EV %** = `fair_prob × decimal_odds − 1`. An edge qualifies for Play These at EV ≥ 2%, or ≥ 1% if Pinnacle-anchored.
- **Kelly sizing**: half-Kelly (industry standard for +EV bettors) of your bankroll, rounded to the book's minimum bet.
- **Profit boost**: `boosted_profit = profit × (1 + boost%/100)`. The promo picker ranks candidates by **boosted** EV, so long-shot bets that are slightly -EV pre-boost but +EV post-boost can win the pick — which the prior version missed because it only searched the pre-boost +EV pool.
- **Parlay promos**: for each promo, the best parlay is found across every candidate game (for SGP) and every leg count (`N`, `N+1`, ..., `N+3`); only the single best by boosted EV is emitted, plus a placeholder line when nothing qualifies so you know the promo was considered.

## Project layout

```
telegram_bot.py     — Bot handlers, promo parser, photo→promo flow, formatters.
odds_compare.py     — Scraping, edge-finding, promo picker, parlay builder, Excel output.
backtest.py         — Offline P&L report against ESPN results.
bot_config.py       — Token + chat-id loader (reads env vars).
requirements.txt    — Python deps (anthropic, python-telegram-bot, openpyxl, etc.).
Dockerfile          — Python 3.13-slim image.
fly.toml            — Fly.io app config (one machine, 512 MB RAM, persistent volume).
pull-*.bat / .py    — Local helpers to pull Excel / backtests off the Fly volume.
```

## Running locally

```bash
pip install -r requirements.txt

# Minimum env vars
export TELEGRAM_BOT_TOKEN=your-bot-token           # from @BotFather
export TELEGRAM_CHAT_ID=your-chat-id               # from @userinfobot
export ANTHROPIC_API_KEY=sk-ant-...                # optional, only for photo→promo

python telegram_bot.py
```

Then message your bot on Telegram. `/help` lists everything.

## Deploying to Fly

```bash
fly launch --no-deploy     # first time only — creates the app
fly secrets set TELEGRAM_BOT_TOKEN=...  \
                TELEGRAM_CHAT_ID=...    \
                ANTHROPIC_API_KEY=sk-ant-...
fly deploy
```

The `fly.toml` provisions a persistent `/data` volume for Excel archives and state (`prev_play_keys.json`, `users.json`). Without an `[http_service]` block, the machine stays running as a worker and uses Telegram long-polling.

## Disclaimer

This is a personal tool. +EV betting over the long run has positive expected value, but short-term variance is real — no individual pick is guaranteed. Bet with money you can afford to lose, respect your jurisdiction's laws, and don't share your bot with anyone you wouldn't lend money to.
