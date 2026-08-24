import json
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Conservative per-pair spread estimates in the bot's own pip conventions
# (see scheduler._calc_pips): forex 0.0001 = 1 pip, JPY 0.01 = 1 pip,
# metals $0.10 = 1 pip, indices 1 point = 1 pip. Deducted from gross PnL at
# every resolution so tracked expectancy approximates a real fill.
# Override via SPREADS_JSON='{"XAU/USD": 5.0, ...}' if your broker differs —
# these defaults are estimates, not gospel (2026-08-24 profitability roadmap,
# Phase 2 item 5).
_DEFAULT_SPREADS: dict = {
    "EUR/USD": 1.0, "GBP/USD": 1.2, "USD/CAD": 1.5, "AUD/USD": 1.2,
    "EUR/CAD": 1.8, "GBP/CAD": 2.2, "EUR/JPY": 1.2, "GBP/JPY": 2.0,
    "AUD/JPY": 1.5, "USD/JPY": 1.0,
    "XAU/USD": 4.0,   # ~$0.40 spread
    "XAG/USD": 3.0,   # ~$0.30 spread
    "NAS100": 1.5,    # index points
    "US30": 2.5,      # index points
}


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
    # SHORT-only evaluation mode (profitability roadmap Phase 1): live ledger
    # Jul 1–Aug 21 showed LONG at 3 wins / 26 trades / −920 pips while SHORT
    # carried +444 pips. Default true so this only changes behavior when a
    # deployment explicitly opts in.
    ENABLE_LONG: bool = os.getenv("ENABLE_LONG", "true").strip().lower() \
        not in ("false", "0", "no")
    # Kill-switch thresholds (Phase 2½): alert when trailing mean R over the
    # last N resolved signals ≤ KILL_SWITCH_R, with at least KILL_SWITCH_MIN_N
    # resolved signals on file to avoid small-sample false alarms.
    KILL_SWITCH_LOOKBACK: int = int(os.getenv("KILL_SWITCH_LOOKBACK", "30"))
    KILL_SWITCH_MIN_N: int = int(os.getenv("KILL_SWITCH_MIN_N", "20"))
    KILL_SWITCH_R: float = float(os.getenv("KILL_SWITCH_R", "-0.5"))


settings = Settings()


def get_spread_pips(pair: str) -> float:
    """Spread estimate for pair in bot-pip units; unknown pairs get 1.5."""
    return _DEFAULT_SPREADS.get(pair, 1.5)


def load_spread_overrides() -> None:
    """Apply SPREADS_JSON env overrides on top of the built-in defaults."""
    raw = os.getenv("SPREADS_JSON", "").strip()
    if not raw:
        return
    try:
        overrides = json.loads(raw)
        if isinstance(overrides, dict):
            _DEFAULT_SPREADS.update({str(k): float(v) for k, v in overrides.items()})
    except (ValueError, TypeError) as e:
        logger.warning(f"SPREADS_JSON ignored (invalid JSON): {e}")


load_spread_overrides()
