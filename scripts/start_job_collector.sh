#!/bin/zsh

set -u
setopt NO_BG_NICE

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
DESKTOP_HOME="${JOB_SCANNER_DESKTOP_HOME:-$HOME/Desktop/AI Job Intelligence Collector}"
RESULTS_DIR="${JOB_SCANNER_OUTPUT_ROOT:-$DESKTOP_HOME/results}"
LOG_DIR="${JOB_SCANNER_PID_DIR:-$DESKTOP_HOME/logs}"
VENV_DIR="$PROJECT_DIR/.venv"
ACTIVATE_FILE="$VENV_DIR/bin/activate"
REQUIREMENTS_FILE="$PROJECT_DIR/requirements.txt"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE="$HOME/.ai-job-collector-chrome"
CDP_BASE="http://127.0.0.1:9222"
FRONTEND_URL="http://127.0.0.1:8501"
BOSS_URL="https://www.zhipin.com/shanghai/"
STREAMLIT_PID_FILE="$LOG_DIR/streamlit.pid"
CHROME_PID_FILE="$LOG_DIR/chrome.pid"
LAUNCH_LOCK="$LOG_DIR/.launcher.lock"

fail() {
  echo "Job collector failed to start: $1"
  echo "Full log: $LOG_DIR/launcher.log"
  exit 1
}

pid_alive() {
  [[ "$1" == <-> ]] && kill -0 "$1" 2>/dev/null
}

cdp_ready() {
  /usr/bin/curl -fsS --max-time 2 "$CDP_BASE/json/version" >/dev/null 2>&1
}

page_exists() {
  local NEEDLE="$1"
  /usr/bin/curl -fsS --max-time 2 "$CDP_BASE/json/list" 2>/dev/null | \
    python -c 'import json,sys; needle=sys.argv[1]; pages=json.load(sys.stdin); raise SystemExit(0 if any(needle in str(p.get("url", "")) for p in pages) else 1)' "$NEEDLE" \
    >/dev/null 2>&1
}

open_cdp_tab() {
  /usr/bin/curl -fsS --max-time 4 -X PUT --globoff "$CDP_BASE/json/new?$1" >/dev/null
}

activate_or_open_frontend() {
  local TARGET_ID
  TARGET_ID="$(/usr/bin/curl -fsS --max-time 2 "$CDP_BASE/json/list" 2>/dev/null | \
    python -c 'import json,sys; needle=sys.argv[1]; pages=json.load(sys.stdin); print(next((p.get("id", "") for p in pages if needle in str(p.get("url", ""))), ""))' "$FRONTEND_URL")"
  if [[ -n "$TARGET_ID" ]]; then
    /usr/bin/curl -fsS --max-time 3 -X PUT "$CDP_BASE/json/activate/$TARGET_ID" >/dev/null
  else
    open_cdp_tab "$FRONTEND_URL"
  fi
}

cd "$PROJECT_DIR" || exit 1
mkdir -p "$RESULTS_DIR" "$LOG_DIR" "$DESKTOP_HOME/config"
exec >>"$LOG_DIR/launcher.log" 2>&1

if ! mkdir "$LAUNCH_LOCK" 2>/dev/null; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') A launch is already in progress; duplicate request ignored."
  exit 0
fi
cleanup_lock() {
  rmdir "$LAUNCH_LOCK" 2>/dev/null || true
}
trap cleanup_lock EXIT INT TERM

echo "$(date '+%Y-%m-%d %H:%M:%S') Starting AI Job Intelligence Collector"

if [[ ! -f "$ACTIVATE_FILE" ]]; then
  command -v python3 >/dev/null 2>&1 || fail "Python 3.11+ was not found"
  python3 -m venv "$VENV_DIR" || fail "Could not create the virtual environment"
fi
source "$ACTIVATE_FILE" || fail "Could not activate the virtual environment"
python -m pip install --disable-pip-version-check -q -r "$REQUIREMENTS_FILE" || fail "Dependency installation failed"

export JOB_SCANNER_OUTPUT_ROOT="$RESULTS_DIR"
export JOB_SCANNER_PID_DIR="$LOG_DIR"
export JOB_SCANNER_APP_LOG="${JOB_SCANNER_APP_LOG:-$LOG_DIR/scanner.log}"
export JOB_SCANNER_DESKTOP_HOME="$DESKTOP_HOME"
export JOB_SCANNER_KEYWORD_LIBRARY="${JOB_SCANNER_KEYWORD_LIBRARY:-$DESKTOP_HOME/config/saved_keywords.json}"

PORT_PID="$(/usr/sbin/lsof -tiTCP:9222 -sTCP:LISTEN 2>/dev/null | head -n 1)"
if [[ -n "$PORT_PID" ]]; then
  PORT_COMMAND="$(/bin/ps -p "$PORT_PID" -o command= 2>/dev/null)"
  [[ "$PORT_COMMAND" == *"$CHROME_PROFILE"* ]] || fail "Port 9222 is occupied by another process"
  echo "$PORT_PID" > "$CHROME_PID_FILE"
else
  [[ -x "$CHROME_BIN" ]] || fail "Google Chrome was not found"
  nohup "$CHROME_BIN" \
    --remote-debugging-port=9222 \
    --user-data-dir="$CHROME_PROFILE" \
    --new-window "$BOSS_URL" \
    >>"$LOG_DIR/chrome.log" 2>&1 </dev/null &
  CHROME_PID=$!
  echo "$CHROME_PID" > "$CHROME_PID_FILE"
  disown
  for _ in {1..40}; do
    cdp_ready && break
    sleep 0.25
  done
  cdp_ready || fail "Dedicated Chrome CDP endpoint did not become ready"
fi

STREAMLIT_PID=""
if [[ -f "$STREAMLIT_PID_FILE" ]]; then
  STREAMLIT_PID="$(<"$STREAMLIT_PID_FILE")"
fi
if ! pid_alive "$STREAMLIT_PID"; then
  PORT_8501_PID="$(/usr/sbin/lsof -tiTCP:8501 -sTCP:LISTEN 2>/dev/null | head -n 1)"
  if [[ -n "$PORT_8501_PID" ]]; then
    PORT_8501_COMMAND="$(/bin/ps -p "$PORT_8501_PID" -o command= 2>/dev/null)"
    [[ "$PORT_8501_COMMAND" == *"streamlit run app.py"* ]] || fail "Port 8501 is occupied by another process"
    STREAMLIT_PID="$PORT_8501_PID"
  else
    nohup python -m streamlit run app.py \
      --server.address 127.0.0.1 \
      --server.port 8501 \
      --server.headless true \
      --browser.gatherUsageStats false \
      >>"$LOG_DIR/streamlit.log" 2>&1 </dev/null &
    STREAMLIT_PID=$!
    disown
  fi
  echo "$STREAMLIT_PID" > "$STREAMLIT_PID_FILE"
fi

FRONTEND_READY=0
for _ in {1..40}; do
  if /usr/bin/curl -fsS --max-time 2 "$FRONTEND_URL/_stcore/health" >/dev/null 2>&1; then
    FRONTEND_READY=1
    break
  fi
  pid_alive "$STREAMLIT_PID" || fail "Streamlit exited before becoming ready"
  sleep 0.25
done
[[ "$FRONTEND_READY" -eq 1 ]] || fail "Streamlit did not become ready within 10 seconds"

page_exists "zhipin.com" || open_cdp_tab "$BOSS_URL" || fail "Could not open the BOSS Zhipin page"
activate_or_open_frontend || fail "Could not open the Streamlit interface"

echo "$(date '+%Y-%m-%d %H:%M:%S') Startup completed"
