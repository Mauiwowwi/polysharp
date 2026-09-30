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

    # --- Wallet selection ---------------------------------------------------
    # Leaderboard slices to pull candidates from, "PERIOD:CATEGORY" pairs.
    lb_slices: list = field(default_factory=lambda: _env(
        "LB_SLICES", ["MONTH:OVERALL", "ALL:OVERALL", "MONTH:SPORTS", "ALL:SPORTS"], list))
    lb_depth: int = field(default_factory=lambda: _env("LB_DEPTH", 100, int))  # per slice
    max_wallets: int = field(default_factory=lambda: _env("MAX_WALLETS", 40, int))
    refresh_hours: float = field(default_factory=lambda: _env("REFRESH_HOURS", 24, float))

    # Filters applied to each candidate's closed-position history
    min_closed: int = field(default_factory=lambda: _env("MIN_CLOSED", 40, int))
    min_win_rate: float = field(default_factory=lambda: _env("MIN_WIN_RATE", 0.55, float))
    min_roi: float = field(default_factory=lambda: _env("MIN_ROI", 0.04, float))
    min_realized_pnl: float = field(default_factory=lambda: _env("MIN_REALIZED_PNL", 25000, float))
    max_days_inactive: int = field(default_factory=lambda: _env("MAX_DAYS_INACTIVE", 14, int))
    history_positions: int = field(default_factory=lambda: _env("HISTORY_POSITIONS", 500, int))

    # --- Alerting -----------------------------------------------------------
    min_alert_usd: float = field(default_factory=lambda: _env("MIN_ALERT_USD", 2000, float))
    alert_sells: bool = field(default_factory=lambda: _env("ALERT_SELLS", True, bool))
    # Fills on the same wallet/outcome/side within this window get merged into one alert
    bundle_seconds: float = field(default_factory=lambda: _env("BUNDLE_SECONDS", 20, float))
    # Ignore prices outside this band (near-certain outcomes are mostly redemption plays)
    min_price: float = field(default_factory=lambda: _env("MIN_PRICE", 0.03, float))
    max_price: float = field(default_factory=lambda: _env("MAX_PRICE", 0.97, float))
    consensus_wallets: int = field(default_factory=lambda: _env("CONSENSUS_WALLETS", 2, int))
    # Conviction = this bundle >= X% taker, from a wallet that is normally <= Y% taker
    conviction_min_taker: float = field(default_factory=lambda: _env("CONVICTION_MIN_TAKER", 0.8, float))
    conviction_max_baseline: float = field(default_factory=lambda: _env("CONVICTION_MAX_BASELINE", 0.4, float))
    # If a WS fill isn't on /activity yet, wait this long once more before alerting
    fee_retry_seconds: float = field(default_factory=lambda: _env("FEE_RETRY_SECONDS", 6, float))
    consensus_hours: float = field(default_factory=lambda: _env("CONSENSUS_HOURS", 6, float))

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
