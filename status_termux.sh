#!/data/data/com.termux/files/usr/bin/bash
set -u

APP_DIR="${APP_DIR:-$HOME/nftoken-app}"
RUN_DIR="$APP_DIR/.run"
LOG_DIR="$APP_DIR/logs"

status_one() {
  local name="$1"
  local file="$RUN_DIR/$name.pid"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$name: RUNNING (pid $pid)"
      return
    fi
  fi
  echo "$name: STOPPED"
}

status_one miniapp
status_one bot
status_one tunnel

if [ -f "$RUN_DIR/miniapp_url" ]; then
  url="$(cat "$RUN_DIR/miniapp_url" 2>/dev/null || true)"
  if [ -n "$url" ]; then
    echo "Mini App URL: $url"
  fi
elif [ -f "$LOG_DIR/tunnel.log" ]; then
  url="$(grep -o 'https://[-a-z0-9.]*trycloudflare.com' "$LOG_DIR/tunnel.log" | tail -1 || true)"
  if [ -n "$url" ]; then
    echo "Quick Tunnel URL: $url"
  fi
fi
