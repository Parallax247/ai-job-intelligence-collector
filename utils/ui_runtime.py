from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from utils.runtime_options import apply_runtime_overrides, normalize_keywords
from utils.run_paths import (
    find_run_directory, latest_run_directory, list_run_directories, run_file_path,
)


class ScanAlreadyRunningError(RuntimeError):
    pass


def build_ui_config(keyword_text: str, jobs_per_keyword: int, city: str,
                    wait_min: float, wait_max: float,
                    save_mode: str = "snapshot", platform: str = "boss") -> dict[str, Any]:
    base = {
        "platform": platform,
        "search_keywords": normalize_keywords(keyword_text),
        "jobs_per_keyword": jobs_per_keyword,
        "city": city,
        "wait_seconds_min": wait_min,
        "wait_seconds_max": wait_max,
        "save_mode": save_mode,
    }
    return apply_runtime_overrides(base)


def config_from_saved(payload: dict[str, Any]) -> dict[str, Any]:
    return build_ui_config(
        "\n".join(str(item) for item in payload.get("search_keywords", [])),
        payload.get("jobs_per_keyword", 10),
        str(payload.get("city", "")),
        payload.get("wait_seconds_min", 6),
        payload.get("wait_seconds_max", 10),
        str(payload.get("save_mode", "snapshot")),
        str(payload.get("platform", "boss")),
    )


def write_ui_config(path: Path, config: dict[str, Any]) -> None:
    payload = {
        "platform": str(config.get("platform", "boss")),
        "search_keywords": list(config["search_keywords"]),
        "jobs_per_keyword": int(config["jobs_per_keyword"]),
        "city": str(config.get("city", "")),
        "wait_seconds_min": config["wait_seconds_min"],
        "wait_seconds_max": config["wait_seconds_max"],
        "save_mode": str(config.get("save_mode", "snapshot")),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def task_preview_lines(config: dict[str, Any]) -> list[str]:
    keywords = config["search_keywords"]
    lines = [f"Keywords in this run: {len(keywords)}"]
    lines.extend(f"{index}. {keyword}" for index, keyword in enumerate(keywords, 1))
    lines.append("")
    lines.append(f"Target jobs per keyword: {config['jobs_per_keyword']}")
    return lines


def compact_log_lines(lines: list[str]) -> list[str]:
    """Hide multiline tracebacks in the UI while retaining them in each run log."""
    compact: list[str] = []
    hiding_traceback = False
    for line in lines:
        if line.startswith("Traceback (most recent call last):"):
            hiding_traceback = True
            compact.append("[Full traceback written to app.log]")
            continue
        if hiding_traceback:
            if re.match(r"^\d{4}-\d{2}-\d{2}", line) or line.startswith("[UI]"):
                hiding_traceback = False
            else:
                continue
        compact.append(line)
    return compact


def recent_log_lines(log_path: str | Path, limit: int = 10) -> list[str]:
    """
    Return the last non-empty log lines safely.
    Missing, empty, or unreadable files return an empty list.
    Accepts either str or Path.
    """
    if limit <= 0:
        return []

    path = Path(log_path).expanduser()
    block_size = 8192
    max_tail_bytes = 2 * 1024 * 1024
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            if position <= 0:
                return []

            payload = b""
            while position > 0 and len(payload) < max_tail_bytes:
                read_size = min(block_size, position, max_tail_bytes - len(payload))
                position -= read_size
                handle.seek(position)
                payload = handle.read(read_size) + payload
                lines = [
                    line.strip()
                    for line in payload.decode("utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
                # Read one extra line so a block-boundary fragment is never returned.
                if len(lines) > limit:
                    break

            if position > 0:
                newline = payload.find(b"\n")
                if newline >= 0:
                    payload = payload[newline + 1:]
            lines = [
                line.strip()
                for line in payload.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            return lines[-limit:]
    except (OSError, ValueError):
        return []


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


class ScanProcessController:
    """Single-scan process controller shared by Streamlit; does not manage Chrome."""

    def __init__(self, project_root: Path, pid_file: Path | None = None,
                 popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.project_root = Path(project_root)
        pid_root = os.environ.get("JOB_SCANNER_PID_DIR", "").strip()
        default_pid = Path(pid_root).expanduser() / "scanner.pid" if pid_root else \
            self.project_root / "data" / "ui_scan.pid"
        self.pid_file = pid_file or default_pid
        self.popen_factory = popen_factory
        self.process: subprocess.Popen | None = None
        self._logs: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._last_run_id: str | None = None

    @staticmethod
    def _run_id_from_line(line: str) -> str | None:
        match = re.search(r"/\.running_([^/\s]+)", str(line))
        return match.group(1) if match else None

    def _remember_run_ids(self, lines: list[str]) -> None:
        for line in lines:
            run_id = self._run_id_from_line(line)
            if run_id:
                self._last_run_id = run_id

    def _external_pid(self) -> int | None:
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return None
        if _pid_is_alive(pid):
            return pid
        self.pid_file.unlink(missing_ok=True)
        return None

    def is_running(self) -> bool:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return True
            return self._external_pid() is not None

    def add_log_lines(self, lines: list[str]) -> None:
        with self._lock:
            self._logs.extend(lines)
            self._remember_run_ids(lines)

    def start(self, config_path: Path, debug: bool = False,
              preview_lines: list[str] | None = None) -> int:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                raise ScanAlreadyRunningError("A scan is already running.")
            external_pid = self._external_pid()
            if external_pid is not None:
                raise ScanAlreadyRunningError(f"A scan is already running (PID {external_pid}).")
            self._logs = list(preview_lines or [])
            self._last_run_id = None
            resolved_config = Path(config_path).resolve()
            try:
                config_argument = str(resolved_config.relative_to(self.project_root.resolve()))
            except ValueError:
                config_argument = str(resolved_config)
            command = [sys.executable, "-u", "main.py", "--config", config_argument]
            if debug:
                command.append("--debug")
            self.process = self.popen_factory(
                command,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text(str(self.process.pid), encoding="utf-8")
            # main.py keeps the manual-confirmation flow; the UI submits one initial Enter.
            try:
                if self.process.stdin is not None:
                    self.process.stdin.write("\n")
                    self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            threading.Thread(target=self._read_output, args=(self.process,), daemon=True).start()
            return self.process.pid

    def _read_output(self, process: subprocess.Popen) -> None:
        if process.stdout is not None:
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                self._queue.put(line.rstrip("\r\n"))
        process.wait()
        self._queue.put(f"[UI] Scan process exited with code: {process.returncode}")
        try:
            saved_pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            saved_pid = -1
        if saved_pid == process.pid:
            self.pid_file.unlink(missing_ok=True)

    def logs(self) -> list[str]:
        with self._lock:
            queued: list[str] = []
            while True:
                try:
                    queued.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if queued:
                self._logs.extend(queued)
                self._remember_run_ids(queued)
            return list(self._logs)

    def stop(self) -> bool:
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                external_pid = self._external_pid()
                if external_pid is None:
                    return False
                command = subprocess.run(
                    ["/bin/ps", "-p", str(external_pid), "-o", "command="],
                    capture_output=True, text=True,
                ).stdout
                if "main.py" not in command:
                    raise RuntimeError(f"PID {external_pid} is not a job-scanner process; stop refused")
                os.kill(external_pid, signal.SIGTERM)
                deadline = time.time() + 5
                while time.time() < deadline and _pid_is_alive(external_pid):
                    time.sleep(0.1)
                if _pid_is_alive(external_pid):
                    os.kill(external_pid, signal.SIGKILL)
                self.pid_file.unlink(missing_ok=True)
                self._logs.append("[UI] Current scan stopped; dedicated Chrome remains open.")
                return True
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.pid_file.unlink(missing_ok=True)
            self._logs.append("[UI] Current scan stopped; dedicated Chrome remains open.")
            return True

    def continue_after_manual_action(self) -> bool:
        """Submit one Enter after the user completes a captcha or login step."""
        with self._lock:
            if self.process is None or self.process.poll() is not None or self.process.stdin is None:
                return False
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
                self._logs.append("[UI] Continue signal sent.")
                return True
            except (BrokenPipeError, OSError):
                return False

    def progress(self) -> dict[str, Any]:
        lines = self.logs()
        current_keyword = "Not started"
        keyword_index = keyword_total = 0
        valid = invalid = screenshot_failed = 0
        for line in lines:
            match = re.search(r"\[(\d+)/(\d+)\]\s*自动搜索关键词：(.+)$", line)
            if match:
                keyword_index, keyword_total = int(match.group(1)), int(match.group(2))
                current_keyword = match.group(3).strip()
            if "有效岗位已写入JSONL" in line:
                valid += 1
            if "最终失败岗位：" in line:
                invalid += 1
            if "岗位截图失败：" in line:
                screenshot_failed += 1
        processed = valid + invalid
        return {
            "current_keyword": current_keyword,
            "keyword_index": keyword_index,
            "keyword_total": keyword_total,
            "keyword_progress": f"{keyword_index}/{keyword_total}" if keyword_total else "0/0",
            "processed": processed,
            "valid": valid,
            "invalid": invalid,
            "screenshot_failed": screenshot_failed,
            "success_rate": valid / processed if processed else 0.0,
            # Backward-compatible aliases.
            "completed": processed,
            "success": valid,
            "failed": invalid,
            "running": self.is_running(),
        }

    def active_run_id(self) -> str | None:
        """
        Return the active or most recently started run ID.
        Return None without creating directories or changing process state when idle.
        """
        with self._lock:
            if self._last_run_id:
                return self._last_run_id
            for line in reversed(self._logs):
                run_id = self._run_id_from_line(line)
                if run_id:
                    return run_id
            process_was_started = self.process is not None
            try:
                external_pid = int(self.pid_file.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError, OSError):
                external_pid = 0
            attached_to_running_process = _pid_is_alive(external_pid)

        if not (process_was_started or attached_to_running_process):
            return None
        output_root = Path(
            os.environ.get("JOB_SCANNER_OUTPUT_ROOT", self.project_root / "data")
        ).expanduser()
        run_dir = latest_run_directory(output_root)
        if run_dir is None:
            return None
        try:
            payload = json.loads(run_file_path(run_dir, "run_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        run_id = str(payload.get("run_id", "") or "").strip() if isinstance(payload, dict) else ""
        return run_id or None


def _read_run_result(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    jobs = _read_jsonl(run_file_path(run_dir, "jobs.jsonl"))
    invalid = _read_jsonl(run_file_path(run_dir, "invalid_records.jsonl"))
    try:
        config = json.loads(run_file_path(run_dir, "run_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    try:
        task_state = json.loads(run_file_path(run_dir, "task_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        task_state = {}
    if not isinstance(task_state, dict):
        task_state = {}
    keywords = [
        str(value).strip() for value in config.get("search_keywords", [])
        if str(value).strip()
    ]
    completed_at = str(config.get("completed_at", "") or "")
    display_time = completed_at[:16].replace("T", " ") if completed_at else "Running"
    keyword_text = "、".join(keywords) if keywords else "Unnamed run"
    per_keyword = Counter(str(row.get("search_keyword", "")) for row in jobs)
    per_keyword.pop("", None)
    return {
        "run_dir": run_dir,
        "run_id": str(config.get("run_id", "") or run_dir.name),
        "status": str(config.get("status", "") or ""),
        "completed_at": completed_at,
        "keywords": keywords,
        "keyword_summary": keyword_text,
        "task_name": f"{display_time} · {keyword_text}",
        "valid_count": len(jobs),
        "invalid_count": len(invalid),
        "task_state": task_state,
        "infrastructure_failed_count": int(
            task_state.get("infrastructure_failed_count", 0) or 0
        ),
        "browser_disconnect_count": int(task_state.get("browser_disconnect_count", 0) or 0),
        "pending_count": int(task_state.get("pending_count", 0) or 0),
        "screenshot_failed_count": sum(
            1 for row in jobs if str(row.get("screenshot_status", "")).lower() == "failed"
        ),
        "excel_path": run_dir / "jobs.xlsx",
        "screenshots_dir": run_dir / "screenshots",
        "log_path": run_file_path(run_dir, "app.log"),
        "per_keyword": dict(per_keyword),
        "invalid_items": [
            {
                "Rank": row.get("search_rank", ""),
                "URL": row.get("url", ""),
                "Failure reason": row.get("invalid_reason", ""),
            }
            for row in invalid
        ],
    }


def read_run_results(data_root: Path, run_id: str = "") -> dict[str, Any] | None:
    run_dir = find_run_directory(data_root, run_id) if run_id else None
    if run_dir is None:
        run_dir = latest_run_directory(data_root)
    return _read_run_result(run_dir) if run_dir is not None else None


def read_recent_run_results(data_root: Path, limit: int = 5) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    results = [_read_run_result(path) for path in list_run_directories(data_root)]
    results.sort(
        key=lambda item: (item["completed_at"], Path(item["run_dir"]).stat().st_mtime),
        reverse=True,
    )
    return results[:limit]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
