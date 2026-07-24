#!/usr/bin/env bash
# Fired by systemd (OnFailure=) the moment smc_bot.service enters the failed
# state — i.e. immediate crash detection, independent of the Python app
# itself (so it still works even if the app's own code is what's broken).
# Restart=on-failure in smc_bot.service handles the actual recovery; this
# script only notifies.
set -euo pipefail

APP_DIR="/root/smc_bot"
ENV_FILE="$APP_DIR/.env"
COOLDOWN_FILE="$APP_DIR/.failure_alert_last"
COOLDOWN_SEC=300   # avoid spamming Telegram if the service is crash-looping

if [ -f "$COOLDOWN_FILE" ]; then
    last=$(cat "$COOLDOWN_FILE")
    now=$(date +%s)
    if [ $(( now - last )) -lt "$COOLDOWN_SEC" ]; then
        exit 0
    fi
fi

TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
CHAT_ID=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)

MSG="SMC Bot ALERT
Service smc_bot.service entered the FAILED state on $(hostname) at $(date -u '+%Y-%m-%d %H:%M:%S UTC').
systemd will auto-restart it (Restart=on-failure).
Check: systemctl status smc_bot / journalctl -u smc_bot -n 50"

curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MSG}" > /dev/null || true

date +%s > "$COOLDOWN_FILE"
