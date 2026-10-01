# PolySharp

Telegram alerts when proven-profitable Polymarket wallets take positions, in near real time.

## How it works

**Your feed is manual.** Alerts only fire for wallets you `/add`. Remove them with `/remove`. The address is on each Polymarket profile.

**Morning shortlist (08:00 America/Halifax by default).** The bot scans the Polymarket **sports** leaderboards (Week/Month/All-time, by P&L and by volume, top 300 each) and sends you a ranked list of wallets worth a look, each with a tap-to-copy `/add` command.

Wallets are judged on their **whole track record**, using Polymarket's own numbers:
- **Predictions** (markets traded) ≥200 and **average bet** (all-time volume ÷ predictions) ≥$1,000.
- **Sports P&L** for 1W / 1M / all-time, which must be positive this month, plus **overall P&L** ≥$25K all-time.
- ≥$250K sports volume this month, and margin (P&L ÷ volume) ≥0.2%.
- **Win rate**, measured on their last 1,000 settled bets plus unredeemed losers from that *same* window. The message shows how many days that covers. It's displayed but not filtered by default, because underdog bettors win under 50% and still profit; set `SUGGEST_MIN_WIN_RATE` to filter on it.

Style checks come from their last 500 fills:
- ≥80% of buying in sports markets, which drops politics, war and crypto accounts.
- ≤25% of sports buying placed **after the game started**, which drops live traders.
- A bet within the last 7 days.

Ranked by last-month sports P&L. (Earlier versions rebuilt ROI from position samples. That broke on high-frequency accounts, whose latest 500 settled bets can cover a single day, while their unredeemed losers go back months.)

The same message ends with a health check on **your list**, flagging anyone who went cold, turned live-heavy or drifted out of sports.

**Sports vs. live detection** uses Polymarket's market data. Sports markets carry a `sports_fees` fee type, and game markets carry a `gameStartTime`. A fill more than 2 minutes after the start time counts as live. If the lookup misses, the bot falls back to the league prefix in the market URL (`mlb-`, `nfl-`, `nba-`…).

**Alerts** fire in real time (websocket firehose plus a REST backstop).

- By default the bot skips non-sports markets and in-game fills (`SPORTS_ONLY_ALERTS`, `PREGAME_ONLY_ALERTS`).
- Game alerts show the league and the time to start.
- Fills on the same wallet, outcome and side within `BUNDLE_SECONDS` merge into one alert.

**Agree / oppose / hedge.** On every new pre-game buy the bot checks what your other wallets **currently hold** in that market. That's a live position lookup, so someone who already sold out doesn't count.
- `🤝 AGREES ×n` / `⚔️ OPPOSES ×n` in the header, with one line per wallet showing size and average price.
- `🔥 CONSENSUS` is sent as a separate message when `CONSENSUS_ALERT_WALLETS` (default 3) of your wallets are on the same side.
- `🛡️ HEDGE` fires when a wallet buys the other side of a market it already holds. It shows both positions and the net result either way.
- `📉 TRIM` / `🚪 EXIT` fire when a wallet sells. They're marked `↩️` if it's a position you were alerted on.

**Conviction score.** Every pre-game buy is scored and tagged 🔥 HIGH / ⭐ MED / ▫️ LOW:
| | points |
|---|---|
| Position size vs their average bet: ≥1.5× / ≥3× ("volumed out") | +1 / +2 |
| Still buying: 2nd+ separate buy on this side in 24h | +1 |
| Each tracked wallet already on the same side (max 2) | +1 |
| Any tracked wallet on the other side | −2 |
| Paid to cross the spread, out of character | +1 |

HIGH is ≥4 and MED is ≥2 (`TIER_HIGH`, `TIER_MED`). `/tier high` shows only HIGH buys; sells, exits, hedges and 🔥 CONSENSUS always come through.

**Live.** In-game buys are never sent and never count toward agree/oppose. The one exception, toggled with `/livehedges on|off` (default on), is an in-game sell or hedge on a position you were alerted on pre-game. Those are tagged `🔴 LIVE`, so you know when a sharp is getting off something you may have tailed.

```
⚡ CONVICTION 🟢 NEW BUY · $12,636
[MLB] Dodgers vs. Padres · ⏳ starts in 3h 10m
➡️ Dodgers @ 0.620  (20,000 sh)
👤 177-letsgo · ROI +4.4% · $3.7M staked · n=68 · win 87%
💸 TAKER 100% · paid $235.60 fees (1.90% of stake) · usually 22% taker
```

## Fee-based conviction (who paid to cross the spread)
Polymarket only charges **takers**: `fee = contracts × rate × p × (1−p)`, in USDC, while makers pay nothing. Each `/activity` row has `size` (contracts), `price` and `usdcSize` (USDC actually moved), so:

- **BUY:** `fee = usdcSize − size×price`. **SELL:** `fee = size×price − usdcSize`.
- If `fee / (size×p×(1−p))` comes out at the fee rate, it's a taker fill. If it's zero, it's a maker fill (resting limit order).

Checked on live rows: taker fills back out to exactly 0.030, and maker fills to exactly 0.

- Every alert shows `💸 TAKER x% · paid $y fees` or `🧱 MAKER`, next to that wallet's usual taker share (measured from its last 500 trades at selection).
- **⚡ CONVICTION** is added when a bundle is ≥80% taker from a wallet that is normally ≤40% taker. That's a patient limit-order trader suddenly paying up to get filled now.
- `/takeronly on` mutes everything except bundles where they paid to cross.
- Websocket fills don't carry `usdcSize`, so the bot looks them up on `/activity` by tx hash before alerting. That adds about 0–6s.
- Fee-free categories (geopolitics) give no signal.

## Telegram commands
- **Feed:** `/add 0x… [name]`, `/remove 0x…`, `/wallets`, `/stats 0x…`
- **Shortlist:** `/suggest` (runs now), `/skip 0x… [days]`
- **Alerts:** `/min 5000`, `/takeronly on|off`, `/tier all|med|high`, `/livehedges on|off`, `/mute 2h`, `/unmute`, `/status`

## Settings (Railway variables, all optional)
| Variable | Default | |
|---|---|---|
| `SUGGEST_TIME` / `BOT_TZ` | `08:00` / `America/Halifax` | when the shortlist arrives |
| `SUGGEST_COUNT` | 8 | max wallets per shortlist |
| `SUGGEST_MIN_MONTH_VOL` | 250000 | sports volume this month |
| `SUGGEST_MIN_MONTH_PNL` | 0 | sports P&L this month |
| `SUGGEST_MIN_PNL` | 25000 | all-time sports P&L |
| `SUGGEST_MIN_MARGIN` | 0.002 | all-time P&L ÷ volume |
| `SUGGEST_MIN_PREDICTIONS` | 200 | markets traded |
| `SUGGEST_MIN_AVG_BET` | 1000 | all-time volume ÷ predictions |
| `SUGGEST_MIN_WIN_RATE` | 0 (off) | win rate filter |
| `SUGGEST_MAX_LIVE_SHARE` | 0.25 | max share of sports $ bet in-game |
| `SUGGEST_MIN_SPORTS_SHARE` | 0.8 | min share of $ in sports |
| `SUGGEST_MAX_DAYS_INACTIVE` | 7 | days since last bet |
| `SUGGEST_COOLDOWN_DAYS` | 3 | don't re-suggest the same wallet sooner |
| `SPORTS_ONLY_ALERTS` / `PREGAME_ONLY_ALERTS` | true / true | alert-time filters |
| `CONSENSUS_ALERT_WALLETS` | 3 | separate 🔥 message threshold |
| `LIVE_HEDGE_ALERTS` | true | default for /livehedges |
| `MIN_ALERT_USD` | 2000 | smallest bundle that alerts (or use `/min`) |

## Deploy on Railway
1. Push this folder to a new GitHub repo and create a Railway service from it. It builds from the Dockerfile.
2. Add a **Volume** mounted at `/data`, so the SQLite state survives redeploys.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Everything else in `.env.example` has sensible defaults.
4. Deploy. The first Telegram message is an **egress check** that shows ✅/❌ for the Data API and the websocket.

### If Railway blocks Polymarket (as it did for whale-tracker)
Set `HTTPS_PROXY=http://user:pass@host:port` to any residential or datacenter HTTP proxy and redeploy. Both the REST client and the websocket route through it automatically. If only the websocket fails, the bot still works on REST polling alone; you can also set `USE_WEBSOCKET=false`.

## Run locally
```
pip install -r requirements.txt
set TELEGRAM_BOT_TOKEN=...   (Windows)   /  export ... (Mac/Linux)
set TELEGRAM_CHAT_ID=...
set DB_PATH=polysharp.db
python -m polysharp.main
```

## Tests
`pip install pytest pytest-asyncio && pytest --asyncio-mode=auto tests`
