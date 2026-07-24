#!/usr/bin/env bash
set -euo pipefail

echo "=== SMC Bot Setup ==="

# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 3. Bootstrap .env from example if not already present
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  .env created from .env.example — fill in your API keys before starting."
    echo ""
fi

# 4. Install and enable the systemd service
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/smc_bot.service"

# Patch the working directory path to match where this script lives
sed "s|/root/smc_bot|$SCRIPT_DIR|g" "$SERVICE_FILE" \
    | sudo tee /etc/systemd/system/smc_bot.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable smc_bot
sudo systemctl start smc_bot

echo ""
echo "=== Setup complete ==="
sudo systemctl status smc_bot --no-pager
