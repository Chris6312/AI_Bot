from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    # App
    APP_ENV: str = 'development'
    SECRET_KEY: str = 'change-this-secret'
    LOG_LEVEL: str = 'INFO'
    ADMIN_API_TOKEN: str = ''

    # Tradier legacy fallback
    TRADIER_API_KEY: str = ''
    TRADIER_ACCOUNT_ID: str = ''
    TRADIER_BASE_URL: str = 'https://sandbox.tradier.com/v1'

    # Tradier explicit paper/live credentials
    TRADIER_PAPER_API_KEY: str = ''
    TRADIER_PAPER_ACCOUNT_ID: str = ''
    TRADIER_PAPER_BASE_URL: str = 'https://sandbox.tradier.com/v1'
    TRADIER_LIVE_API_KEY: str = ''
    TRADIER_LIVE_ACCOUNT_ID: str = ''
    TRADIER_LIVE_BASE_URL: str = 'https://api.tradier.com/v1'

    # Kraken CLI / future live credentials
    KRAKEN_CLI_PATH: str = 'kraken'
    KRAKEN_API_KEY: str = ''
    KRAKEN_API_SECRET: str = ''

    # Discord
    DISCORD_BOT_TOKEN: str = ''
    DISCORD_TRADING_CHANNEL_ID: int = 0
    DISCORD_USER_ID: int = 0
    DISCORD_ALLOWED_ROLE_IDS: str = ''
    DISCORD_DECISION_MAX_AGE_SECONDS: int = 900
    DISCORD_REQUIRE_DECISION_TIMESTAMP: bool = True
    # Removed: DISCORD_WEBHOOK_URL (no longer needed - AI doesn't post directly)

    # Database
    DATABASE_URL: str = 'sqlite:///./ai_bot.db'
    REDIS_URL: str = 'redis://localhost:6379'

    # ============================================
    # GLOBAL POSITION SIZING
    # ============================================

    # Primary sizing method (percentage or fixed)
    POSITION_SIZE_PCT: float = 0.10              # 10% default per position
    POSITION_SIZE_FIXED: Optional[float] = None  # None = use percentage, set dollar amount to override

    # Position limits
    MAX_POSITIONS_PER_DECISION: int = 3          # Max trades in single screening
    MIN_POSITION_USD: float = 1000.0             # Minimum position size
    MAX_POSITION_PCT: float = 0.25               # Maximum position size (safety cap)

    # Asset-specific overrides (optional - if not set, uses global POSITION_SIZE_PCT)
    STOCK_POSITION_SIZE_PCT: Optional[float] = None    # Override for stocks only
    STOCK_MIN_POSITION_USD: Optional[float] = None     # Override min for stocks

    CRYPTO_POSITION_SIZE_PCT: Optional[float] = None   # Override for crypto only
    CRYPTO_MIN_POSITION_USD: Optional[float] = None    # Override min for crypto

    # ============================================
    # SAFETY SETTINGS
    # ============================================

    SAFETY_MAX_TRADES_PER_DAY: int = 3
    SAFETY_MAX_POSITION_SIZE_PCT: float = 0.25
    SAFETY_MAX_DAILY_LOSS: float = 500.00
    SAFETY_VIX_MAX: float = 35.0
    SAFETY_GRACE_PERIOD_SECONDS: int = 30
    SAFETY_ALLOW_OVERRIDE: bool = True
    SAFETY_REQUIRE_MARKET_HOURS: bool = True
    ORDER_FILL_CONFIRM_RETRIES: int = 3
    ORDER_FILL_CONFIRM_DELAY_SECONDS: float = 1.0
    PRE_TRADE_STOCK_QUOTE_MAX_AGE_SECONDS: int = 30
    PRE_TRADE_CRYPTO_TICKER_MAX_AGE_SECONDS: int = 30
    PRE_TRADE_CRYPTO_MAX_CANDLE_GAP_FACTOR: float = 2.5
    WATCHLIST_MAX_AGE_SECONDS: int = 21600
    WATCHLIST_DEFAULT_EXPIRY_HOURS: int = 30
    WATCHLIST_MONITOR_ENABLED: bool = True
    WATCHLIST_MONITOR_POLL_SECONDS: int = 20
    WATCHLIST_MONITOR_BATCH_LIMIT: int = 25
    WATCHLIST_EXIT_WORKER_ENABLED: bool = True
    WATCHLIST_EXIT_WORKER_POLL_SECONDS: int = 20
    WATCHLIST_EXIT_WORKER_BATCH_LIMIT: int = 25
    TRADIER_REQUEST_TIMEOUT_SECONDS: float = 8.0
    TRADIER_POSITIONS_TIMEOUT_SECONDS: float = 4.0
    RUNTIME_VISIBILITY_PROBE_TTL_SECONDS: int = 20
    RUNTIME_VISIBILITY_GATE_HISTORY_LIMIT: int = 50

    # Risk
    STOP_LOSS_PCT: float = 0.015
    PROFIT_TARGET_PCT: float = 0.025
    TRAILING_STOP_PCT: float = 0.03
    TIME_STOP_HOUR: int = 15
    TIME_STOP_MINUTE: int = 45

    # Runtime / local tooling
    BOT_DEFAULT_STOCK_MODE: str = 'PAPER'
    BOT_DEFAULT_CRYPTO_MODE: str = 'PAPER'
    RUNTIME_STATE_FILE: str = './backend/.runtime/runtime_state.json'
    BACKUP_ROOT_DIR: str = './backups'

    @property
    def runtime_state_path(self) -> Path:
        return Path(self.RUNTIME_STATE_FILE).resolve()

    @property
    def backup_root_path(self) -> Path:
        return Path(self.BACKUP_ROOT_DIR).resolve()

    @property
    def discord_allowed_role_ids(self) -> set[int]:
        values: set[int] = set()
        for part in self.DISCORD_ALLOWED_ROLE_IDS.split(','):
            candidate = part.strip()
            if not candidate:
                continue
            try:
                values.add(int(candidate))
            except ValueError:
                continue
        return values

    @property
    def admin_api_ready(self) -> bool:
        return bool(self.ADMIN_API_TOKEN.strip())

    def paper_tradier_credentials(self) -> dict[str, str]:
        api_key = self.TRADIER_PAPER_API_KEY or self.TRADIER_API_KEY
        account_id = self.TRADIER_PAPER_ACCOUNT_ID or self.TRADIER_ACCOUNT_ID
        base_url = self.TRADIER_PAPER_BASE_URL or self.TRADIER_BASE_URL
        return {
            'api_key': api_key,
            'account_id': account_id,
            'base_url': base_url,
        }

    def live_tradier_credentials(self) -> dict[str, str]:
        api_key = self.TRADIER_LIVE_API_KEY
        account_id = self.TRADIER_LIVE_ACCOUNT_ID
        base_url = self.TRADIER_LIVE_BASE_URL

        if not api_key and self.TRADIER_API_KEY and 'sandbox' not in (self.TRADIER_BASE_URL or '').lower():
            api_key = self.TRADIER_API_KEY
            account_id = self.TRADIER_ACCOUNT_ID
            base_url = self.TRADIER_BASE_URL

        return {
            'api_key': api_key,
            'account_id': account_id,
            'base_url': base_url,
        }

    @property
    def tradier_paper_ready(self) -> bool:
        creds = self.paper_tradier_credentials()
        return bool(creds['api_key'] and creds['account_id'])

    @property
    def tradier_live_ready(self) -> bool:
        creds = self.live_tradier_credentials()
        return bool(creds['api_key'] and creds['account_id'])

    @property
    def kraken_live_ready(self) -> bool:
        return bool(self.KRAKEN_API_KEY and self.KRAKEN_API_SECRET)


settings = Settings()
