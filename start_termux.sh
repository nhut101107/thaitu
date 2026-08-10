#!/data/data/com.termux/files/usr/bin/bash
set -u

APP_DIR="${APP_DIR:-$HOME/nftoken-app}"
LOG_DIR="$APP_DIR/logs"
RUN_DIR="$APP_DIR/.run"
MINIAPP_PORT="${MINIAPP_PORT:-8080}"
TUNNEL_MODE="${TUNNEL_MODE:-quick}"
CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"

mkdir -p "$LOG_DIR" "$RUN_DIR"
cd "$APP_DIR" || { echo "Không tìm thấy APP_DIR: $APP_DIR"; exit 1; }

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PYTHON="$APP_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "Không thấy Python trong .venv. Hãy chạy: bash setup_termux.sh"
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "Thiếu cloudflared. Hãy chạy: bash setup_termux.sh"
  exit 1
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ "${TELEGRAM_BOT_TOKEN:-}" = "replace_with_botfather_token" ]; then
  echo "Thiếu TELEGRAM_BOT_TOKEN thật trong .env"
  exit 1
fi

export MINIAPP_HOST="${MINIAPP_HOST:-127.0.0.1}"
export MINIAPP_PORT

termux-wake-lock >/dev/null 2>&1 || true

kill_pidfile() {
  local file="$1"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
}

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "Đang dừng NFToken Pro..."
  for name in bot tunnel miniapp; do
    kill_pidfile "$RUN_DIR/$name.supervisor.pid"
  done
  for name in bot tunnel miniapp; do
    kill_pidfile "$RUN_DIR/$name.pid"
  done
  rm -f "$RUN_DIR/miniapp_url"
  termux-wake-unlock >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT

run_supervised() {
  local name="$1"
  shift
  trap 'exit 0' INT TERM
  while true; do
    echo "[$(date '+%F %T')] start $name" >> "$LOG_DIR/supervisor.log"
    "$@" >> "$LOG_DIR/$name.log" 2>&1 &
    local child=$!
    echo "$child" > "$RUN_DIR/$name.pid"
    wait "$child"
    local code=$?
    rm -f "$RUN_DIR/$name.pid"
    echo "[$(date '+%F %T')] $name thoát mã $code; khởi động lại sau 3s" >> "$LOG_DIR/supervisor.log"
    sleep 3
  done
}

LAST_SUPERVISOR_PID=""
start_supervisor() {
  local name="$1"
  shift
  run_supervised "$name" "$@" </dev/null &
  LAST_SUPERVISOR_PID=$!
  echo "$LAST_SUPERVISOR_PID" > "$RUN_DIR/$name.supervisor.pid"
}

wait_for_quick_url() {
  local url=""
  local i
  for i in $(seq 1 30); do
    url="$(grep -o 'https://[-a-z0-9.]*trycloudflare.com' "$LOG_DIR/tunnel.log" 2>/dev/null | tail -1 || true)"
    if [ -n "$url" ]; then
      echo "$url"
      return 0
    fi
    sleep 1
  done
  return 1
}

: > "$LOG_DIR/tunnel.log"
rm -f "$RUN_DIR/miniapp_url"

echo "Đang khởi động Mini App..."
start_supervisor miniapp "$PYTHON" miniapp_server.py
SUP_MINIAPP="$LAST_SUPERVISOR_PID"
sleep 1

if [ "$TUNNEL_MODE" = "named" ]; then
  if [ -z "$CLOUDFLARE_TUNNEL_TOKEN" ]; then
    echo "TUNNEL_MODE=named nhưng chưa có CLOUDFLARE_TUNNEL_TOKEN trong .env"
    exit 1
  fi
  if [ -z "${TELEGRAM_MINIAPP_URL:-}" ] || [[ "${TELEGRAM_MINIAPP_URL:-}" != https://* ]]; then
    echo "Named Tunnel cần TELEGRAM_MINIAPP_URL=https://domain-cua-ban trong .env"
    exit 1
  fi
  echo "Đang khởi động Cloudflare Named Tunnel..."
  start_supervisor tunnel cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN"
  SUP_TUNNEL="$LAST_SUPERVISOR_PID"
  PUBLIC_URL="$TELEGRAM_MINIAPP_URL"
else
  echo "Đang khởi động Cloudflare Quick Tunnel..."
  start_supervisor tunnel cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$MINIAPP_PORT"
  SUP_TUNNEL="$LAST_SUPERVISOR_PID"
  echo "Đang lấy URL HTTPS từ Cloudflare Quick Tunnel..."
  if ! PUBLIC_URL="$(wait_for_quick_url)"; then
    echo "Không lấy được Quick Tunnel URL sau 30 giây. Log gần nhất:"
    tail -30 "$LOG_DIR/tunnel.log" 2>/dev/null || true
    exit 1
  fi
  export TELEGRAM_MINIAPP_URL="$PUBLIC_URL"
fi

echo "$PUBLIC_URL" > "$RUN_DIR/miniapp_url"

echo "Đang khởi động Telegram bot..."
start_supervisor bot "$PYTHON" code_goc.py
SUP_BOT="$LAST_SUPERVISOR_PID"

sleep 2

echo
echo "NFToken Pro đang chạy trên Termux"
echo "- Mini App local:  http://127.0.0.1:$MINIAPP_PORT"
echo "- Mini App public: $PUBLIC_URL"
echo "- Log Mini App:    $LOG_DIR/miniapp.log"
echo "- Log Bot:         $LOG_DIR/bot.log"
echo "- Log Tunnel:      $LOG_DIR/tunnel.log"
echo
echo "Kiểm tra trạng thái ở terminal khác: bash status_termux.sh"
echo "Nhấn CTRL+C để dừng toàn bộ."

wait "$SUP_MINIAPP" "$SUP_TUNNEL" "$SUP_BOT"
