#!/bin/zsh

set -u

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
DESKTOP_HOME="${JOB_SCANNER_DESKTOP_HOME:-$HOME/Desktop/AI Job Intelligence Collector}"
LOG_DIR="${JOB_SCANNER_PID_DIR:-$DESKTOP_HOME/logs}"
CHROME_PROFILE="$HOME/.ai-job-collector-chrome"

stop_pid_file() {
  local PID_FILE="$1"
  [[ -f "$PID_FILE" ]] || return 0
  local PID="$(<"$PID_FILE")"
  if [[ "$PID" == <-> ]] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
}

stop_pid_file "$LOG_DIR/scanner.pid"
stop_pid_file "$LOG_DIR/streamlit.pid"

while IFS= read -r PID; do
  [[ -n "$PID" ]] || continue
  COMMAND_LINE="$(/bin/ps -p "$PID" -o command= 2>/dev/null)"
  if [[ "$COMMAND_LINE" == *"Google Chrome"* ]] && [[ "$COMMAND_LINE" == *"$CHROME_PROFILE"* ]]; then
    kill -TERM "$PID" 2>/dev/null || true
  fi
done < <(/usr/bin/pgrep -f "$CHROME_PROFILE" 2>/dev/null)

echo "AI Job Intelligence Collector stopped."
