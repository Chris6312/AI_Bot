from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # App
    APP_ENV: str = "development"
    SECRET_KEY: str
    LOG_LEVEL: str = "INFO"
    
    # Tradier
    TRADIER_API_KEY: str
    TRADIER_ACCOUNT_ID: str
    TRADIER_BASE_URL: str = "https://sandbox.tradier.com/v1"
    
    # Discord
    DISCORD_BOT_TOKEN: str
    DISCORD_TRADING_CHANNEL_ID: int
    DISCORD_WEBHOOK_URL: str
    DISCORD_USER_ID: int
    
    # Database
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379"
    
    # Safety
    SAFETY_MAX_TRADES_PER_DAY: int = 3
    SAFETY_MAX_POSITION_SIZE_PCT: float = 0.25
    SAFETY_MAX_DAILY_LOSS: float = 500.00
    SAFETY_VIX_MAX: float = 35.0
    SAFETY_GRACE_PERIOD_SECONDS: int = 30
    SAFETY_ALLOW_OVERRIDE: bool = True
    SAFETY_REQUIRE_MARKET_HOURS: bool = True
    
    # Risk
    STOP_LOSS_PCT: float = 0.015
    PROFIT_TARGET_PCT: float = 0.025
    TRAILING_STOP_PCT: float = 0.03
    TIME_STOP_HOUR: int = 15
    TIME_STOP_MINUTE: int = 45
    
    class Config:
        env_file = ".env"

settings = Settings()
