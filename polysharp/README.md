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

**Agree / oppose / hedge.** On every new pre-game buy the bot checks what your other wallets **currently hold** in that market. That's a live position lookup, so someone who already sold out doesn't count. It covers the whole game: alt spreads, moneyline and totals count too (🔀 marks a middle). Each account is counted once, with all its lines added together, and accounts under `/crowdmin` (default $250, env `CROWD_MIN_USD`) are ignored. A single position gets a full line; with more, amounts are added up across accounts and lines into one line per side, e.g. `🤝 $6,133 more on Georgia (UpTheBlues)` / `⚔️ $11,007 on Vanderbilt (Kch-Temp)`.
- `🤝 AGREES ×n` / `⚔️ OPPOSES ×n` in the header, with one line per wallet showing size and average price.
- `🔥 CONSENSUS` is sent as a separate message when `CONSENSUS_ALERT_WALLETS` (default 3) of your wallets are on the same side.
- `🛡️ HEDGE` fires when a wallet buys the other side of a market where that side is still its **bigger** position. It shows both positions and the net result either way.
- `⚖️ BOTH SIDES` fires when the new side now outweighs a meaningful position they already hold on the other side. It's scored like a normal buy, plus an "Also holds" line for the other side.
- Leftover dust on the other side (under 10% of the new side, or under $50; set with `HEDGE_DUST_PCT`) is ignored, so it's a plain NEW/ADD.
- `📉 TRIM` / `🚪 EXIT` fire when a wallet sells. They're marked `↩️` if it's a position you were alerted on.

**Conviction score.** Every pre-game buy is scored and tagged 🔥 HIGH / ⭐ MED / ▫️ LOW:
| | points |
|---|---|
| Position size vs their average bet: ≥1.5× / ≥3× ("volumed out") | +1 / +2 |
| Still buying: 2nd+ separate buy on this side in 24h | +1 |
| Each tracked wallet already on the same side (max 2) | +1 |
| Any tracked wallet on the other side | −2 |
| Paid fees (≥50% of the buy crossed the spread) | +1 |
| …and that's out of character for a normally passive wallet | +1 more |

HIGH is ≥4 and MED is ≥2 (`TIER_HIGH`, `TIER_MED`). `/tier high` shows only HIGH buys; sells, exits, hedges and 🔥 CONSENSUS always come through.

**Live.** In-game buys are never sent and never count toward agree/oppose. The one exception, toggled with `/livehedges on|off` (default off — no live posts at all), is an in-game sell or hedge on a position you were alerted on pre-game. Those are tagged `🔴 LIVE`, so you know when a sharp is getting off something you may have tailed.

```
TRADE ALERT! - HomeRunHazard

Market: [NFL] Spread: Browns (-3.5)
Outcome: Steelers +3.5
Side: BUY
Amount: 9,313.00 USDC (🚨 BUY TAKER 🚨)
Price: 0.71c(-245)
Size: 13,004.00 shares
Time: 2026-10-01 13:01
Starts: in 10h 42m

🎯 Conviction +3 (⭐ MED): 2.1× their usual bet · paid fees · no opposition
📦 Now holds 25,980 sh · avg 0.710 (-245) · cost $18,440
💸 TAKER 100% · paid $80.33 fees (0.87% of stake) · usually 26% taker
```
Tags in brackets on the Amount line: `🚨 BUY TAKER 🚨` (paid fees), `⚡ CONVICTION ⚡`, `🛡️ HEDGE 🛡️`, `⚖️ BOTH SIDES ⚖️`, `🤝 AGREES ×n`, `⚔️ OPPOSES ×n`, `🔴 LIVE 🔴`, `📉 TRIM` / `🚪 EXIT`. Passive limit-order buys get no tag, and their bottom line reads `🧱 MAKER`.

## Fee-based conviction (who paid to cross the spread)
Polymarket only charges **takers**: `fee = contracts × rate × p × (1−p)`, in USDC, while makers pay nothing. Each `/activity` row has `size` (contracts), `price` and `usdcSize` (USDC actually moved), so:

- **BUY:** `fee = usdcSize − size×price`. **SELL:** `fee = size×price − usdcSize`.
- If `fee / (size×p×(1−p))` comes out at the fee rate, it's a taker fill. If it's zero, it's a maker fill (resting limit order).

Checked on live rows: taker fills back out to exactly 0.030, and maker fills to exactly 0.

- Buys where they paid fees lead with **💸 PAID $y** in the header, ahead of everything else. Passive limit-order buys don't get the tag.
- Every alert shows `💸 TAKER x% · paid $y fees` or `🧱 MAKER`, next to that wallet's usual taker share (measured from its last 500 trades at selection).
- **⚡ CONVICTION** is added when a bundle is ≥80% taker from a wallet that is normally ≤40% taker. That's a patient limit-order trader suddenly paying up to get filled now.
- `/takeronly on` mutes everything except bundles where they paid to cross.
- Websocket fills don't carry `usdcSize`, so the bot looks them up on `/activity` by tx hash before alerting. That adds about 0–6s.
- Fee-free categories (geopolitics) give no signal.

## Telegram commands
**Everyone** (group members): `/top10 [name | sport | league]` (e.g. `/top10 football` or `/top10 nfl` covers all accounts, `/top10 alwaysfade` one account; inside a sport tab plain `/top10` shows that sport), `/wallets`, `/stats 0x…`, `/status`, `/help`

**Admin only:** `/topics [setup]`, `/bindtopic <sport>`, `/add 0x… [name]`, `/remove 0x…`, `/skip 0x… [days]`, `/suggest`, `/min 5000`, `/tier all|med|high`, `/takeronly on|off`, `/livehedges on|off`, `/crowdmin 250`, `/mute 2h`, `/unmute`

## Group chat setup
1. Add the bot to your group. To get the group's id, open the group in web.telegram.org/a; the number after `#` starts with `-100`.
2. Set `TELEGRAM_CHAT_ID` to where **bet alerts** go:
   - `-1001234567890` sends alerts to the group only, which is the usual setup.
   - `-1001234567890,123456789` sends them to the group and your private chat.
3. Set `ADMIN_USER_IDS` to your Telegram user id, which is the same number as your private chat id. Your private chat always accepts every command, even when it isn't an alert chat.
4. The morning shortlist and the startup message go only to admins, by private message. Non-admins who try an admin command in the group get "🔒 Only the bot admin can do that".

## Sport tabs (one group, Telegram Topics)
1. In the group, open **Edit → Topics** and turn it on.
2. Make Whaletail a group **admin** with the **Manage Topics** permission.
3. Restart the bot, or send `/topics setup`. It creates 🏈 Football, ⚾ Baseball, 🏀 Basketball, 🏒 Hockey, ⚽ Soccer, 🎾 Tennis, 🥊 Fighting and 🎯 Other.
4. Each bet posts **once, in its sport tab**, as the full detailed alert. Telegram's built-in **All** view shows every tab merged. `COMPACT_TABS=true` switches the tabs to a 3-line version (with `ALL_FEED=true`, the full alert is also copied silently into General).
   ```
   💸 PAID · $9,313 · HomeRunHazard        (or 🧱 SET for passive limit orders)
   Steelers +3.5 @ 0.710 (-245)
   📦 Holds $18,440 (25,980 sh)
   ```
5. If you'd rather make the tabs yourself, run `/bindtopic football` (or baseball, basketball, hockey, soccer, tennis, fighting, other) inside each tab.
6. Commands run inside a tab, like `/top10`, reply in that same tab.

## Settings (Railway variables, all optional)
| Variable | Default | |
|---|---|---|
| `ADMIN_USER_IDS` | private ids in TELEGRAM_CHAT_ID | who can run admin commands |
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
| `SPORT_TOPICS` / `ALL_FEED` | true / false | route alerts to sport tabs; `ALL_FEED=true` also copies them into General (Telegram's built-in "All" view already merges every tab) |
| `COMPACT_TABS` | false | 3-line alerts in sport tabs instead of the full one |
| `SILENT_GENERAL` | true | General copy posts without a notification, so only the sport tab pings |
| `SPORTS_ONLY_ALERTS` / `PREGAME_ONLY_ALERTS` | true / true | alert-time filters |
| `CONSENSUS_ALERT_WALLETS` | 3 | separate 🔥 message threshold |
| `LIVE_HEDGE_ALERTS` | false | default for /livehedges |
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
