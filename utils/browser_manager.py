from __future__ import annotations

import os
from pathlib import Path

from utils.desktop_service import get_cdp_pages, restart_dedicated_chrome


def ensure_dedicated_chrome_running() -> bool:
    """Ensure the dedicated Chrome instance is running on port 9222.

    Return True only when Chrome was restarted by this call.
    """
    if get_cdp_pages(timeout=1.0):
        return False
    pid_dir = Path(os.environ.get(
        "JOB_SCANNER_PID_DIR", Path.home() / "Desktop" / "AI Job Intelligence Collector" / "logs"
    )).expanduser()
    log_dir = Path(os.environ.get(
        "JOB_SCANNER_LAUNCHER_LOG_DIR", Path.home() / "Desktop" / "AI Job Intelligence Collector" / "logs"
    )).expanduser()
    restart_dedicated_chrome(pid_dir, log_dir)
    return True
