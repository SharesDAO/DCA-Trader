#!/usr/bin/env bash
# Start the configured trading bot and read-only dashboard in the background.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHON="$PROJECT_DIR/venv/bin/python"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
[[ -x "$PYTHON" ]] || { echo "Missing venv/bin/python" >&2; exit 1; }
[[ "$DASHBOARD_PORT" =~ ^[0-9]+$ ]] && (( DASHBOARD_PORT >= 1 && DASHBOARD_PORT <= 65535 )) || {
    echo "Invalid DASHBOARD_PORT" >&2; exit 1;
}
[[ "$DASHBOARD_HOST" == 0.0.0.0 || "$DASHBOARD_HOST" == 127.0.0.1 ]] || {
    echo "DASHBOARD_HOST must be 0.0.0.0 or 127.0.0.1" >&2; exit 1;
}
mkdir -p logs
# Serialize simultaneous invocations of this launcher.
exec 9>logs/start.lock
flock -n 9 || { echo "Another launcher is running" >&2; exit 1; }

find_process() {
    local pid
    while read -r pid; do
        if [[ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" == "$PROJECT_DIR" ]]; then
            echo "$pid"
        fi
    done < <(pgrep -f "^([^[:space:]]*/)?python[0-9.]* -m src\\.$1([[:space:]]|$)" || true)
}

"$PYTHON" -m src.main --check-config
bot_pid="$(find_process main)"
if [[ -n "$bot_pid" ]]; then
    echo "Bot already running: PID $bot_pid"
else
    setsid -f nohup "$PYTHON" -m src.main --log-level INFO >>logs/fgv-live.log 2>&1 < /dev/null 9>&-
    sleep 2
    bot_pid="$(find_process main)"
    [[ -n "$bot_pid" ]] || { echo "Bot failed; see logs/fgv-live.log" >&2; exit 1; }
    echo "Bot started: PID $bot_pid"
fi

dashboard_pid="$(find_process dashboard)"
if [[ -n "$dashboard_pid" ]]; then
    echo "Dashboard already running: PID $dashboard_pid (existing host/port unchanged)"
    exit 0
fi
if ss -H -ltn "sport = :$DASHBOARD_PORT" | rg -q .; then
    echo "Port $DASHBOARD_PORT is already occupied; dashboard not started" >&2
    exit 1
fi
setsid -f nohup "$PYTHON" -m src.dashboard --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" >>logs/fgv-dashboard.log 2>&1 < /dev/null 9>&-
for attempt in {1..10}; do
    if curl --noproxy '*' --silent --fail "http://127.0.0.1:$DASHBOARD_PORT/" >/dev/null; then
        echo "Dashboard ready: http://<server-ip>:$DASHBOARD_PORT (bind $DASHBOARD_HOST)"
        echo "Warning: dashboard has no authentication; restrict access to trusted networks."
        exit 0
    fi
    sleep 1
done
echo "Dashboard failed readiness check; see logs/fgv-dashboard.log" >&2
exit 1
