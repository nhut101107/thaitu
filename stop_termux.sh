#!/data/data/com.termux/files/usr/bin/bash
set -u

APP_DIR="${APP_DIR:-$HOME/nftoken-app}"
RUN_DIR="$APP_DIR/.run"

stop_pidfile() {
  local label="$1"
  local file="$2"
  if [ ! -f "$file" ]; then
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
    echo "$label: đã dừng"
  fi
  rm -f "$file"
}

for name in bot tunnel miniapp; do
  stop_pidfile "$name supervisor" "$RUN_DIR/$name.supervisor.pid"
done
for name in bot tunnel miniapp; do
  stop_pidfile "$name" "$RUN_DIR/$name.pid"
done

rm -f "$RUN_DIR/miniapp_url"
termux-wake-unlock >/dev/null 2>&1 || true
echo "NFToken Pro đã dừng."
