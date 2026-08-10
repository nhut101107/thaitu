#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/nftoken-app}"
REPO_URL="${REPO_URL:-https://github.com/nhut101107/thaitu.git}"
BRANCH="${BRANCH:-codex/fix-bot-errors-and-upgrade-features}"

pkg update -y
pkg install -y python git cloudflared termux-api

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
  git config core.fileMode false
else
  cd "$APP_DIR"
  # Termux chmod có thể làm Git hiểu nhầm script là đã sửa chỉ vì executable bit.
  # Bỏ theo dõi file mode để lần cập nhật sau không bị chặn bởi lỗi local changes.
  git config core.fileMode false
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
  sed -i "s|BOT_DATABASE_PATH=/absolute/path/to/bot_database.db|BOT_DATABASE_PATH=$APP_DIR/bot_database.db|" .env
  echo
  echo "Đã tạo $APP_DIR/.env"
  echo "Hãy sửa TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID và thông tin VietQR trước khi chạy."
fi

echo
echo "Cài đặt Termux hoàn tất."
echo "1) nano $APP_DIR/.env"
echo "2) cd $APP_DIR"
echo "3) bash start_termux.sh"
