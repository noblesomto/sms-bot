import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
    TWELVE_DATA_API_KEY: str = os.getenv("TWELVE_DATA_API_KEY", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
    PAIRS: list = os.getenv("PAIRS", "XAUUSD,EURUSD,GBPUSD,USDJPY").split(",")
    TIMEFRAMES: list = os.getenv("TIMEFRAMES", "1h,4h,1day").split(",")
    HTF_TIMEFRAME: str = os.getenv("HTF_TIMEFRAME", "1day")
    LTF_TIMEFRAME: str = os.getenv("LTF_TIMEFRAME", "1h")
    MIN_CONFLUENCE_SCORE: int = int(os.getenv("MIN_CONFLUENCE_SCORE", "3"))
    DISPLACEMENT_ATR_MULT: float = float(os.getenv("DISPLACEMENT_ATR_MULT", "1.5"))
    DISPLACEMENT_WINDOW: int = int(os.getenv("DISPLACEMENT_WINDOW", "3"))
    DB_URL: str = os.getenv("DB_URL", "sqlite:///smc_bot.db")


settings = Settings()
