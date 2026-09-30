# PolySharp

Telegram alerts when proven-profitable Polymarket wallets take positions, in near real time.

## How it works

**1. Picking the winners (every 24h)**
- Pulls the top 100 of the Polymarket leaderboard for each slice in `LB_SLICES` (by default Month and All-time, Overall and Sports).
- Re-scores every candidate from its own settled history:
  - Up to 500 closed positions.
  - **Plus resolved-but-unredeemed losers** (`/positions?redeemable=true`, curPrice 0). These never show up as "closed", so the leaderboard and the naive win rate both look better than they are.
- Keeps wallets that pass all of `MIN_CLOSED`, `MIN_WIN_RATE`, `MIN_ROI`, `MIN_REALIZED_PNL` and `MAX_DAYS_INACTIVE`.
- Ranks them by `ROI × √n` and tracks the top `MAX_WALLETS`. Market makers drop out on ROI, and one-hit whales drop out on n.

**2. Watching them (real time)**
- **Websocket firehose** (`wss://ws-live-data.polymarket.com`, topic `activity/trades`): every Polymarket fill arrives with the trader's `proxyWallet`, and the bot filters for tracked wallets. Latency is about 1s.
- **REST backstop**: `/activity` is polled per wallet, every 15s when the websocket is down or every 60s when it's healthy. Fills from the two feeds are deduped by tx hash.
- Fills on the same wallet, outcome and side within `BUNDLE_SECONDS` are merged into one alert, so a sweep of the book reads as one trade.

**3. Alerts**
```
🟢 NEW BUY · $12,450
Will the Dodgers win the NLCS?
➡️ Yes @ 0.412  (30,219 sh, 4 fills)
👤 sharpguy · win 61% · ROI +8.4% · n=312 · #14 Month Sports
📦 Now holds 30,219 sh · avg 0.412 · cost $12,450
⏱ 3s after fill · via ws
⚔️ Opposed by: otherwhale (No $8,000)
```
- Tags: `NEW` / `ADD` for buys, `TRIM` / `EXIT` for sells.
- `🔥 CONSENSUS` fires when `CONSENSUS_WALLETS`+ tracked wallets buy the same outcome within `CONSENSUS_HOURS`.

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
`/status` `/wallets` `/stats 0x…` `/add 0x… [name]` `/remove 0x…` `/min 5000` `/takeronly on|off` `/mute 2h` `/unmute` `/refresh`

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
