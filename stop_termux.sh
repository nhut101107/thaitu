#!/data/data/com.termux/files/usr/bin/bash
set -u

APP_DIR="${APP_DIR:-$HOME/nftoken-app}"
RUN_DIR="$APP_DIR/.run"

stop_one() {
  local name="$1"
  local file="$RUN_DIR/$name.pid"
  if [ ! -f "$file" ]; then
    echo "$name: không có PID"
    return
  fi
  local pid
  pid="$(cat "$file" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "$name: đã dừng"
  else
    echo "$name: không chạy"
  fi
  rm -f "$file"
}

stop_one tunnel
stop_one bot
stop_one miniapp
termux-wake-unlock >/dev/null 2>&1 || true
