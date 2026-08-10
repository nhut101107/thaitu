#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/nftoken-app}"
REPO_URL="${REPO_URL:-https://github.com/nhut101107/thaitu.git}"
BRANCH="${BRANCH:-codex/fix-bot-errors-and-upgrade-features}"

pkg update -y
pkg install -y python git cloudflared termux-api

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  cd "$APP_DIR"
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
fi

cd "$APP_DIR"
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Đã tạo $APP_DIR/.env"
  echo "Hãy sửa TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID và thông tin VietQR trước khi chạy."
fi

chmod +x start_termux.sh stop_termux.sh status_termux.sh 2>/dev/null || true

echo
echo "Cài đặt Termux hoàn tất."
echo "1) nano $APP_DIR/.env"
echo "2) cd $APP_DIR"
echo "3) bash start_termux.sh"
