#!/bin/zsh

set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
DESKTOP_HOME="${JOB_SCANNER_DESKTOP_HOME:-$HOME/Desktop/AI Job Intelligence Collector}"
APP_PATH="$DESKTOP_HOME/AI Job Intelligence Collector.app"
TEMPLATE="$PROJECT_DIR/scripts/JobCollectorLauncher.applescript"
TEMP_SCRIPT="$(mktemp -t ai-job-collector.XXXXXX.applescript)"

/bin/mkdir -p "$DESKTOP_HOME/results" "$DESKTOP_HOME/logs" "$DESKTOP_HOME/config"
/bin/chmod +x "$PROJECT_DIR/scripts/start_job_collector.sh" "$PROJECT_DIR/scripts/stop_job_collector.sh"

LAUNCHER_PATH="$PROJECT_DIR/scripts/start_job_collector.sh"
/usr/bin/sed "s|__LAUNCHER_SCRIPT__|$LAUNCHER_PATH|g" "$TEMPLATE" > "$TEMP_SCRIPT"
/usr/bin/osacompile -o "$APP_PATH" "$TEMP_SCRIPT"
/bin/rm -f "$TEMP_SCRIPT"

echo "Installed: $APP_PATH"
