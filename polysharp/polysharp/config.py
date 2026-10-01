"""All settings come from environment variables so Railway can manage them."""
import os
from dataclasses import dataclass, field


def _env(name, default, cast=str):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if cast is list:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return cast(raw)


@dataclass
class Config:
    # Telegram
    tg_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    tg_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))

    # Storage (mount a Railway volume at /data so state survives redeploys)
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "/data/polysharp.db"))

    # --- Morning suggestions (leaderboard scan -> shortlist you pick from) --
    # New names on purpose: older LB_*/MIN_* Railway variables are ignored.
    lb_slices: list = field(default_factory=lambda: _env("SUGGEST_SLICES", [
        "MONTH:SPORTS:PNL", "ALL:SPORTS:PNL", "WEEK:SPORTS:PNL",
        "MONTH:SPORTS:VOL", "ALL:SPORTS:VOL"], list))
    lb_depth: int = field(default_factory=lambda: _env("SUGGEST_DEPTH", 300, int))
    min_lb_vol: float = field(default_factory=lambda: _env("SUGGEST_MIN_LB_VOL", 250000, float))
    # Filters are computed on SPORTS positions only
    min_closed: int = field(default_factory=lambda: _env("SUGGEST_MIN_BETS", 100, int))
    min_staked: float = field(default_factory=lambda: _env("SUGGEST_MIN_STAKED", 500000, float))
    min_roi: float = field(default_factory=lambda: _env("SUGGEST_MIN_ROI", 0.02, float))
    min_win_rate: float = field(default_factory=lambda: _env("SUGGEST_MIN_WIN_RATE", 0.0, float))
    min_realized_pnl: float = field(default_factory=lambda: _env("SUGGEST_MIN_PNL", 25000, float))
    max_days_inactive: float = field(default_factory=lambda: _env("SUGGEST_MAX_DAYS_INACTIVE", 7, float))
    min_sports_share: float = field(default_factory=lambda: _env("SUGGEST_MIN_SPORTS_SHARE", 0.8, float))
    max_live_share: float = field(default_factory=lambda: _env("SUGGEST_MAX_LIVE_SHARE", 0.25, float))
    history_positions: int = field(default_factory=lambda: _env("HISTORY_POSITIONS", 500, int))
    suggest_count: int = field(default_factory=lambda: _env("SUGGEST_COUNT", 8, int))
    suggest_time: str = field(default_factory=lambda: _env("SUGGEST_TIME", "08:00"))
    tz: str = field(default_factory=lambda: _env("BOT_TZ", "America/Halifax"))
    suggest_cooldown_days: float = field(default_factory=lambda: _env("SUGGEST_COOLDOWN_DAYS", 3, float))

    # --- Alerting -----------------------------------------------------------
    min_alert_usd: float = field(default_factory=lambda: _env("MIN_ALERT_USD", 2000, float))
    alert_sells: bool = field(default_factory=lambda: _env("ALERT_SELLS", True, bool))
    # Fills on the same wallet/outcome/side within this window get merged into one alert
    bundle_seconds: float = field(default_factory=lambda: _env("BUNDLE_SECONDS", 20, float))
    # Ignore prices outside this band (near-certain outcomes are mostly redemption plays)
    min_price: float = field(default_factory=lambda: _env("MIN_PRICE", 0.03, float))
    max_price: float = field(default_factory=lambda: _env("MAX_PRICE", 0.97, float))
    # Separate 🔥 message when this many of your wallets hold the same side
    consensus_alert_wallets: int = field(default_factory=lambda: _env("CONSENSUS_ALERT_WALLETS", 3, int))
    # How far back to look for other wallets' trades in the same market (holdings re-checked live)
    crowd_days: float = field(default_factory=lambda: _env("CROWD_DAYS", 7, float))
    # Let in-game SELLs / hedge-buys through, but only on positions you were alerted on pre-game
    live_hedge_alerts: bool = field(default_factory=lambda: _env("LIVE_HEDGE_ALERTS", True, bool))
    # Conviction = this bundle >= X% taker, from a wallet that is normally <= Y% taker
    conviction_min_taker: float = field(default_factory=lambda: _env("CONVICTION_MIN_TAKER", 0.8, float))
    conviction_max_baseline: float = field(default_factory=lambda: _env("CONVICTION_MAX_BASELINE", 0.4, float))
    # If a WS fill isn't on /activity yet, wait this long once more before alerting
    fee_retry_seconds: float = field(default_factory=lambda: _env("FEE_RETRY_SECONDS", 6, float))

    # Alert-time filters: skip non-sports markets and in-game (live) fills
    sports_only_alerts: bool = field(default_factory=lambda: _env("SPORTS_ONLY_ALERTS", True, bool))
    pregame_only_alerts: bool = field(default_factory=lambda: _env("PREGAME_ONLY_ALERTS", True, bool))

    # --- Transport ----------------------------------------------------------
    use_websocket: bool = field(default_factory=lambda: _env("USE_WEBSOCKET", True, bool))
    ws_url: str = field(default_factory=lambda: _env("WS_URL", "wss://ws-live-data.polymarket.com"))
    poll_seconds: float = field(default_factory=lambda: _env("POLL_SECONDS", 15, float))
    # Railway egress fallback: set HTTPS_PROXY / HTTP_PROXY and both httpx and
    # websockets will route through it automatically.

    def validate(self):
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", self.tg_token),
                                  ("TELEGRAM_CHAT_ID", self.tg_chat_id)) if not v]
        if missing:
            raise SystemExit(f"Missing env vars: {', '.join(missing)}")
