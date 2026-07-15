#!/bin/zsh
# Backward-compatible alias.
SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/start_job_collector.sh"
