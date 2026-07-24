#!/usr/bin/env python3
"""Health watchdog for smc_bot — run every few minutes via systemd timer.

Catches the failure mode systemd's own Restart=on-failure can't: the process
stays alive (uvicorn up, port open) but the scheduler stops producing scans
(a hung event loop, a stuck job, etc.). Deliberately standalone — does not
import any of the app's own modules, so a bug in the app can't take the
watchdog down with it.

Alerts once per outage (state file), then sends a single recovery message
when scanning resumes. Skips alerting during the weekend market closure,
when the bot intentionally stops scanning.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

APP_DIR = Path("/root/smc_bot")
load_dotenv(APP_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = APP_DIR / ".watchdog_state"
STALE_THRESHOLD_MIN = 20  # 4x the 5-min scan interval — tolerates one slow cycle


def is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5


def send_telegram(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"watchdog: failed to send Telegram alert: {e}", file=sys.stderr)


def read_state() -> str:
    return STATE_FILE.read_text().strip() if STATE_FILE.exists() else "OK"


def write_state(state: str) -> None:
    STATE_FILE.write_text(state)


def check() -> None:
    if is_weekend():
        return

    prev_state = read_state()
    now = datetime.now(timezone.utc)
    host = os.uname().nodename

    try:
        resp = requests.get("http://localhost:8000/health", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        last_scan_at = data.get("last_scan_at")
        if not last_scan_at:
            raise ValueError("no last_scan_at in /health response")

        last_scan = datetime.fromisoformat(last_scan_at)
        if last_scan.tzinfo is None:
            last_scan = last_scan.replace(tzinfo=timezone.utc)
        staleness_min = (now - last_scan).total_seconds() / 60

        if staleness_min > STALE_THRESHOLD_MIN:
            if prev_state != "DOWN":
                send_telegram(
                    f"SMC Bot ALERT\n"
                    f"No scan in {staleness_min:.0f} min (last: {last_scan_at}). "
                    f"Process is up but appears hung on {host}.\n"
                    f"Check: systemctl status smc_bot / bot.log"
                )
                write_state("DOWN")
        else:
            if prev_state == "DOWN":
                send_telegram(
                    f"SMC Bot RECOVERED\nScanning resumed normally on {host} "
                    f"(last scan: {last_scan_at})."
                )
            write_state("OK")

    except Exception as e:
        if prev_state != "DOWN":
            send_telegram(
                f"SMC Bot ALERT\n"
                f"/health unreachable on {host}: {type(e).__name__}: {e}\n"
                f"Check: systemctl status smc_bot"
            )
            write_state("DOWN")


if __name__ == "__main__":
    check()
