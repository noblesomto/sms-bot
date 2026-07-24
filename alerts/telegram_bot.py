import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from config import settings
from db.database import SessionLocal
from db.models import Signal

logger = logging.getLogger(__name__)

ALERT_COOLDOWN_MINUTES = 30  # Rate-limit per pair+timeframe+direction combination


def _was_recently_alerted(pair: str, timeframe: str, direction: str) -> bool:
    """Block re-alerting the same pair/timeframe/direction within the cooldown window."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ALERT_COOLDOWN_MINUTES)
        return (
            db.query(Signal)
            .filter(
                Signal.pair == pair,
                Signal.timeframe == timeframe,
                Signal.direction == direction,
                Signal.alerted_at >= cutoff,
            )
            .first()
        ) is not None
    finally:
        db.close()


def _record_alert(signal_id: int) -> None:
    """Stamp alerted_at on the signal row so deduplication works."""
    db = SessionLocal()
    try:
        signal = db.query(Signal).filter(Signal.id == signal_id).first()
        if signal:
            signal.alerted_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


async def send_alert(
    pair: str,
    signal_data: dict,
    chart_image_path: Optional[str] = None,
) -> bool:
    """
    Deliver a Telegram alert with an optional chart image.

    Deduplication is per pair+timeframe+direction: the same setup on the same
    timeframe won't re-alert within ALERT_COOLDOWN_MINUTES.  A 15min signal
    can still alert even if a 5min signal on the same pair just fired.
    Returns True if the message was sent, False otherwise.
    """
    timeframe = signal_data.get("timeframe", "")
    direction = signal_data.get("direction", "")
    if _was_recently_alerted(pair, timeframe, direction):
        logger.info(
            f"[{datetime.now(timezone.utc)}] Skipping duplicate alert "
            f"for {pair}/{timeframe}/{direction}"
        )
        return False

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    chat_id = settings.TELEGRAM_CHAT_ID
    message = signal_data.get("message", "")

    try:
        if chart_image_path and Path(chart_image_path).exists():
            with open(chart_image_path, "rb") as img:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=img,
                    caption=message,
                    parse_mode="HTML",
                )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
            )

        signal_id = signal_data.get("signal_id")
        if signal_id:
            _record_alert(signal_id)

        logger.info(f"[{datetime.now(timezone.utc)}] Alert sent for {pair}")
        return True

    except TelegramError as e:
        logger.error(f"[{datetime.now(timezone.utc)}] Telegram error for {pair}: {e}")
        return False


async def send_tp_notification(message: str) -> bool:
    """Send a TP1/TP2 hit notification — no deduplication, always fires."""
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML",
        )
        logger.info(f"[{datetime.now(timezone.utc)}] TP notification sent")
        return True
    except TelegramError as e:
        logger.error(f"[{datetime.now(timezone.utc)}] Telegram TP notification error: {e}")
        return False


def send_alert_sync(
    pair: str,
    signal_data: dict,
    chart_image_path: Optional[str] = None,
) -> bool:
    """Synchronous wrapper around send_alert for use outside an async context."""
    return asyncio.run(send_alert(pair, signal_data, chart_image_path))


def test_telegram_bot():
    """Standalone test — sends a live ping if credentials are configured."""
    if not settings.TELEGRAM_BOT_TOKEN or settings.TELEGRAM_BOT_TOKEN == "your_token_here":
        print("Telegram not configured — skipping live test")
        return
    sent = send_alert_sync("TEST", {"message": "🟢 SMC Bot — connectivity test OK"})
    print(f"Test alert sent: {sent}")


if __name__ == "__main__":
    test_telegram_bot()
