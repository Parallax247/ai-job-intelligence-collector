from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


RUN_SUBDIRECTORIES = ("screenshots", "html", "debug")
TECHNICAL_FILENAMES = (
    "jobs.jsonl", "invalid_records.jsonl", "run_config.json", "task_state.json", "app.log",
)
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_run_component(value: str, max_length: int = 100) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "unnamed-run")[:max_length].rstrip(" .")


def keyword_summary(keywords: list[str], max_length: int | None = None) -> str:
    cleaned = [sanitize_run_component(keyword, 50) for keyword in keywords if str(keyword).strip()]
    if not cleaned:
        return "unnamed-run"
    displayed = cleaned[:3]
    extra_marker = ""
    if len(cleaned) > 3:
        extra_marker = f"+{len(cleaned) - 3}-more"
    result = "+".join(displayed) + extra_marker
    if max_length is None or len(result) <= max_length:
        return result
    if extra_marker and max_length > len(extra_marker) + len(displayed) - 1:
        available = max_length - len(extra_marker)
        while len("+".join(displayed)) > available:
            longest = max(range(len(displayed)), key=lambda index: len(displayed[index]))
            displayed[longest] = displayed[longest][:-1]
        return "+".join(displayed) + extra_marker
    return result[:max_length].rstrip(" .+")


def build_final_run_name(completed_at: datetime, keywords: list[str], *,
                         completed: bool = True, status: str | None = None,
                         processed_count: int | None = None,
                         max_length: int = 100) -> str:
    timestamp = completed_at.strftime("%Y-%m-%d_%H-%M")
    if status == "partial_failed":
        status_prefix = "partial_"
    elif status == "paused_browser_lost":
        status_prefix = "paused_"
    else:
        status_prefix = "" if completed else "incomplete_"
    prefix = f"{timestamp}_{status_prefix}"
    count_suffix = (
        f"_{max(int(processed_count or 0), 0)}-items" if status == "paused_browser_lost" else ""
    )
    summary = keyword_summary(
        keywords, max_length=max_length - len(prefix) - len(count_suffix)
    )
    name = sanitize_run_component(f"{prefix}{summary}{count_suffix}", max_length)
    return name[:max_length].rstrip(" .")


def unique_run_destination(parent: Path, name: str, max_length: int = 100) -> Path:
    parent = Path(parent)
    candidate = parent / name[:max_length]
    suffix = 2
    while candidate.exists() or candidate.is_symlink():
        marker = f"_{suffix}"
        candidate = parent / f"{name[:max_length - len(marker)].rstrip(' ._')}{marker}"
        suffix += 1
    return candidate


def create_run_directory(data_root: Path, when: datetime | None = None, *,
                         direct: bool = False, update_latest: bool = True) -> Path:
    """Create a temporary .running directory; finalize its name when the run ends."""
    data_root = Path(data_root)
    runs_root = data_root if direct else data_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    run_dir = unique_run_destination(runs_root, f".running_{stamp}")
    run_dir.mkdir()
    for name in RUN_SUBDIRECTORIES:
        (run_dir / name).mkdir()
    if update_latest:
        update_latest_link(data_root, run_dir)
    return run_dir


def finalize_run_directory(run_dir: Path, data_root: Path, keywords: list[str],
                           completed_at: datetime, *, completed: bool,
                           status: str | None = None,
                           processed_count: int | None = None) -> Path:
    """Atomically rename a .running directory using completion time and keywords."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")
    final_name = build_final_run_name(
        completed_at, keywords, completed=completed, status=status,
        processed_count=processed_count,
    )
    destination = unique_run_destination(run_dir.parent, final_name)
    os.replace(run_dir, destination)
    update_latest_link(Path(data_root), destination)
    return destination


def discard_running_directory(run_dir: Path, data_root: Path) -> None:
    """Delete an empty .running run and restore latest to the newest completed run."""
    run_dir = Path(run_dir)
    if not run_dir.name.startswith(".running_"):
        raise ValueError(f"Refusing to delete a non-running directory: {run_dir}")
    data_root = Path(data_root)
    latest = data_root / "latest"
    latest_points_here = (
        latest.is_symlink()
        and latest.resolve(strict=False) == run_dir.resolve(strict=False)
    )
    if latest_points_here:
        latest.unlink(missing_ok=True)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    completed = list_run_directories(data_root)
    if completed:
        update_latest_link(data_root, completed[0])


def rewrite_artifact_paths(records: list[dict[str, Any]], old_run_dir: Path,
                           final_run_dir: Path, project_root: Path) -> list[dict[str, Any]]:
    """Rewrite screenshot and HTML paths relative to the finalized run directory."""
    old_root = Path(old_run_dir).resolve(strict=False)
    final_root = Path(final_run_dir).resolve(strict=False)
    project_root = Path(project_root).resolve(strict=False)
    for record in records:
        for field in ("screenshot_path", "html_path"):
            value = str(record.get(field, "") or "").strip()
            if not value:
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                if path.parts and path.parts[0] in (*RUN_SUBDIRECTORIES, "internal"):
                    record[field] = path.as_posix()
                    continue
                path = project_root / path
            resolved = path.resolve(strict=False)
            relative: Path | None = None
            for root in (old_root, final_root):
                try:
                    relative = resolved.relative_to(root)
                    break
                except ValueError:
                    continue
            if relative is not None:
                record[field] = relative.as_posix()
    return records


def run_file_path(run_dir: Path, filename: str) -> Path:
    """Resolve files from either an active run root or finalized internal directory."""
    run_dir = Path(run_dir)
    internal = run_dir / "internal" / filename
    root = run_dir / filename
    return internal if internal.exists() or not root.exists() else root


def _artifact_component(value: Any, max_length: int) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", str(value or ""))
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    return (cleaned or "unknown")[:max_length].rstrip(" ._")


def build_artifact_stem(record: dict[str, Any], fallback_index: int = 1,
                        max_length: int = 170) -> str:
    """Build a stable artifact name from platform, keyword, and job ID."""
    try:
        rank = max(1, int(record.get("search_rank", fallback_index) or fallback_index))
    except (TypeError, ValueError):
        rank = fallback_index
    platform = {"boss": "BOSS", "liepin": "Liepin"}.get(
        str(record.get("platform", "")).lower(), str(record.get("platform", "platform"))
    )
    keyword = _artifact_component(record.get("search_keyword", ""), 35)
    company = _artifact_component(record.get("company", ""), 42)
    title = _artifact_component(record.get("title", ""), 55)
    job_id = _artifact_component(record.get("job_id", ""), 8)[:8]
    # Asterisks are controlled separators; field values were sanitized above.
    stem = f"{rank:04d}_{_artifact_component(platform, 12)}*{keyword}*{company}_{title}_{job_id}"
    return stem[:max_length].rstrip(" ._")


def _unique_file(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not generate a unique filename: {path}")


def flatten_task_artifacts(run_dir: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten screenshots and move HTML files into internal/html."""
    run_dir = Path(run_dir)
    screenshot_root = run_dir / "screenshots"
    html_root = run_dir / "internal" / "html"
    screenshot_root.mkdir(parents=True, exist_ok=True)
    html_root.mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(records, 1):
        stem = build_artifact_stem(record, index)
        for field, destination_root, suffix in (
            ("screenshot_path", screenshot_root, ".png"),
            ("html_path", html_root, ".html"),
        ):
            raw = str(record.get(field, "") or "").strip()
            if not raw:
                continue
            source = Path(raw).expanduser()
            if not source.is_absolute():
                source = run_dir / source
            destination = destination_root / f"{stem}{source.suffix or suffix}"
            if source.resolve(strict=False) != destination.resolve(strict=False):
                destination = _unique_file(destination)
                if source.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
            record[field] = str(destination)
    for root in (screenshot_root, run_dir / "html"):
        if not root.exists():
            continue
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts), reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    return records


def internalize_run_directory(run_dir: Path) -> Path:
    """Move technical run files that users rarely need into internal/."""
    run_dir = Path(run_dir)
    internal = run_dir / "internal"
    internal.mkdir(parents=True, exist_ok=True)
    for filename in TECHNICAL_FILENAMES:
        source = run_dir / filename
        destination = internal / filename
        if source.exists() and source != destination:
            if destination.exists():
                destination.unlink()
            shutil.move(str(source), str(destination))
    for folder_name in ("html", "debug"):
        source = run_dir / folder_name
        destination = internal / folder_name
        if not source.exists() or source == destination:
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.mkdir(parents=True, exist_ok=True)
        for child in list(source.iterdir()):
            target = _unique_file(destination / child.name) if (destination / child.name).exists() else destination / child.name
            shutil.move(str(child), str(target))
        source.rmdir()
    (internal / "html").mkdir(parents=True, exist_ok=True)
    (internal / "debug").mkdir(parents=True, exist_ok=True)
    return internal


def nest_platform_artifacts(run_dir: Path, platform: str,
                            records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add platform subdirectories during finalization without changing adapter logic."""
    run_dir = Path(run_dir)
    platform = str(platform or "boss").strip().lower()
    moves: dict[Path, Path] = {}
    for folder_name in ("screenshots", "html"):
        root = run_dir / folder_name
        root.mkdir(parents=True, exist_ok=True)
        platform_root = root / platform
        platform_root.mkdir(parents=True, exist_ok=True)
        for child in list(root.iterdir()):
            if child == platform_root:
                continue
            destination = platform_root / child.name
            if destination.exists() and child.is_dir():
                for nested in child.iterdir():
                    shutil.move(str(nested), str(destination / nested.name))
                child.rmdir()
            else:
                shutil.move(str(child), str(destination))
            moves[child.resolve(strict=False)] = destination.resolve(strict=False)
    for record in records:
        for field in ("screenshot_path", "html_path"):
            value = str(record.get(field, "") or "").strip()
            if not value:
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = run_dir / path
            resolved = path.resolve(strict=False)
            for source, destination in moves.items():
                try:
                    suffix = resolved.relative_to(source)
                except ValueError:
                    continue
                record[field] = str(destination / suffix)
                break
    return records


def update_latest_link(data_root: Path, run_dir: Path) -> Path:
    data_root = Path(data_root)
    latest = data_root / "latest"
    temporary = data_root / ".latest.tmp"
    if latest.exists() and not latest.is_symlink():
        raise RuntimeError(f"Cannot update data/latest because the path exists and is not a symlink: {latest}")
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    relative_target = os.path.relpath(run_dir, data_root)
    temporary.symlink_to(relative_target, target_is_directory=True)
    os.replace(temporary, latest)
    return latest


def write_run_config(path: Path, config: dict[str, Any]) -> None:
    payload = {key: value for key, value in config.items() if not str(key).startswith("_")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_run_config(path: Path, **values: Any) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(values)
    write_run_config(path, payload)


def list_run_directories(data_root: Path, *, include_running: bool = False) -> list[Path]:
    data_root = Path(data_root)
    roots = [data_root, data_root / "runs"]
    directories: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if not path.is_dir() or path.name == "latest":
                continue
            if path.name.startswith(".running_") and not include_running:
                continue
            if run_file_path(path, "run_config.json").exists() or run_file_path(path, "jobs.jsonl").exists():
                directories.append(path.resolve())
    unique = list(dict.fromkeys(directories))
    return sorted(unique, key=lambda path: path.stat().st_mtime, reverse=True)


def find_run_directory(data_root: Path, run_id: str) -> Path | None:
    target = str(run_id or "").strip()
    if not target:
        return None
    for path in list_run_directories(data_root, include_running=True):
        try:
            payload = json.loads(run_file_path(path, "run_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, dict) and str(payload.get("run_id", "")) == target:
            return path
        if path.name.removeprefix(".running_") == target:
            return path
    return None


def latest_run_directory(data_root: Path) -> Path | None:
    latest = Path(data_root) / "latest"
    if latest.exists():
        return latest.resolve()
    completed = list_run_directories(data_root)
    if completed:
        return completed[0]
    running = list_run_directories(data_root, include_running=True)
    return running[0] if running else None
