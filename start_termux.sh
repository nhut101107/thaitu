#!/data/data/com.termux/files/usr/bin/bash
set -u

APP_DIR="${APP_DIR:-$HOME/nftoken-app}"
LOG_DIR="$APP_DIR/logs"
RUN_DIR="$APP_DIR/.run"
MINIAPP_PORT="${MINIAPP_PORT:-8080}"
TUNNEL_MODE="${TUNNEL_MODE:-quick}"
CLOUDFLARE_HOSTNAME="${CLOUDFLARE_HOSTNAME:-}"
CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"

mkdir -p "$LOG_DIR" "$RUN_DIR"
cd "$APP_DIR" || { echo "Không tìm thấy APP_DIR: $APP_DIR"; exit 1; }

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Thiếu lệnh '$1'. Hãy chạy: bash setup_termux.sh"
    exit 1
  }
}

need_cmd python
need_cmd cloudflared

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "Thiếu TELEGRAM_BOT_TOKEN trong .env"
  exit 1
fi

termux-wake-lock >/dev/null 2>&1 || true

stop_pidfile() {
  local name="$1"
  local file="$RUN_DIR/$name.pid"
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
  echo
  echo "Đang dừng NFToken Pro..."
  stop_pidfile tunnel
  stop_pidfile bot
  stop_pidfile miniapp
  termux-wake-unlock >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT

run_supervised() {
  local name="$1"
  shift
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

start_tunnel() {
  if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]; then
    run_supervised tunnel cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN"
  elif [ "$TUNNEL_MODE" = "named" ]; then
    echo "TUNNEL_MODE=named nhưng chưa có CLOUDFLARE_TUNNEL_TOKEN" >> "$LOG_DIR/tunnel.log"
    return 1
  else
    run_supervised tunnel cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$MINIAPP_PORT"
  fi
}

export MINIAPP_HOST="${MINIAPP_HOST:-127.0.0.1}"
export MINIAPP_PORT

run_supervised miniapp python miniapp_server.py &
SUP_MINIAPP=$!
run_supervised bot python code_goc.py &
SUP_BOT=$!
start_tunnel &
SUP_TUNNEL=$!

sleep 2

echo ""
echo "NFToken Pro đang chạy trên Termux"
echo "- Mini App local: http://127.0.0.1:$MINIAPP_PORT"
echo "- Log Mini App: $LOG_DIR/miniapp.log"
echo "- Log Bot:      $LOG_DIR/bot.log"
echo "- Log Tunnel:   $LOG_DIR/tunnel.log"
echo ""
echo "Nếu dùng Quick Tunnel, xem URL HTTPS bằng:"
echo "  grep -o 'https://[-a-z0-9.]*trycloudflare.com' '$LOG_DIR/tunnel.log' | tail -1"
echo ""
echo "Nhấn CTRL+C để dừng toàn bộ."

wait "$SUP_MINIAPP" "$SUP_BOT" "$SUP_TUNNEL"
