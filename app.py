from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import streamlit as st

from utils.desktop_service import (
    BOSS_URL,
    LIEPIN_URL,
    browser_status,
    ensure_boss_page_health,
    ensure_cdp_tab,
    ensure_platform_page_health,
    is_boss_url,
    is_liepin_url,
    platform_browser_status,
    restart_dedicated_chrome,
)
from utils.ui_runtime import (
    ScanAlreadyRunningError,
    ScanProcessController,
    build_ui_config,
    compact_log_lines,
    config_from_saved,
    recent_log_lines,
    read_recent_run_results,
    read_run_results,
    task_preview_lines,
    write_ui_config,
)
from utils.run_paths import run_file_path
from utils.keyword_library import (
    KeywordLibrary,
    keyword_key,
    keywords_to_text,
    selected_keywords,
    toggle_keyword_selection,
)


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DESKTOP_HOME = Path(os.environ.get(
    "JOB_SCANNER_DESKTOP_HOME", Path.home() / "Desktop" / "AI Job Intelligence Collector"
)).expanduser()
OUTPUT_ROOT = Path(os.environ.get("JOB_SCANNER_OUTPUT_ROOT", DATA_DIR)).expanduser()
PID_DIR = Path(os.environ.get("JOB_SCANNER_PID_DIR", DATA_DIR)).expanduser()
LOG_DIR = DESKTOP_HOME / "logs"
UI_CONFIG = DATA_DIR / "ui_run_config.json"
LAST_CONFIG = DATA_DIR / "last_run_config.json"
KEYWORD_LIBRARY_PATH = Path(os.environ.get(
    "JOB_SCANNER_KEYWORD_LIBRARY",
    DESKTOP_HOME / "config" / "saved_keywords.json",
)).expanduser()


CONTROLLER_INTERFACE_VERSION = "active-run-id-v2"


@st.cache_resource
def get_controller(interface_version: str = CONTROLLER_INTERFACE_VERSION) -> ScanProcessController:
    del interface_version
    return ScanProcessController(ROOT)


def record_ui_error(action: str, exc: Exception) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "ui-errors.log").open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{action}]\n{traceback.format_exc()}\n")
    st.error(f"{action} failed: {exc}. Full details were written to the log.")


def _save_boss_health_state(health: dict[str, Any]) -> None:
    st.session_state["boss_binding_state"] = str(health.get("state", "Page unavailable"))
    st.session_state["boss_binding_url"] = str(health.get("boss_url", ""))


def platform_from_label(label: str) -> str:
    if str(label).lower().startswith("liepin"):
        return "liepin"
    if str(label).startswith("LinkedIn"):
        return "linkedin"
    return "boss"


def platform_display_name(platform: str) -> str:
    return {"boss": "BOSS Zhipin", "liepin": "Liepin", "linkedin": "LinkedIn"}.get(platform, platform)


def state_display_label(value: str) -> str:
    return str(value)


def _save_platform_health_state(platform: str, health: dict[str, Any]) -> None:
    st.session_state[f"{platform}_binding_state"] = str(health.get("state", "Page unavailable"))
    st.session_state[f"{platform}_binding_url"] = str(
        health.get("page_url", health.get("boss_url", ""))
    )


def _start_scan_process(config: dict[str, Any], debug: bool) -> None:
    write_ui_config(UI_CONFIG, config)
    controller.start(
        UI_CONFIG, debug=debug, preview_lines=task_preview_lines(config)
    )
    try:
        KeywordLibrary(KEYWORD_LIBRARY_PATH).mark_used(config.get("search_keywords", []))
    except (OSError, ValueError):
        # A keyword-statistics failure must not stop an already-started scan.
        pass
    st.session_state.pop("pending_platform_config", None)
    st.session_state.pop("pending_platform_debug", None)
    st.success(f"Scan started. Results will be saved to: {OUTPUT_ROOT}")


def launch_config(config: dict[str, Any], debug: bool) -> bool:
    platform = str(config.get("platform", "boss"))
    st.session_state[f"{platform}_binding_state"] = "Reconnecting"
    health = ensure_platform_page_health(platform, create_if_missing=True)
    _save_platform_health_state(platform, health)
    if health.get("login_required"):
        st.session_state["pending_platform_config"] = dict(config)
        st.session_state["pending_platform_debug"] = bool(debug)
        st.warning(f"Sign in on the {platform_display_name(platform)} page, then click Continue.")
        return False
    if not health.get("ok"):
        st.warning(str(health.get("message") or "The platform page is closed or unavailable, so the scan cannot start."))
        return False
    _start_scan_process(config, debug)
    return True


def auto_prepare_boss_tab() -> dict[str, Any]:
    """Create missing CDP tabs before rendering; perform full page health checks on launch."""
    status = browser_status()
    if not status["chrome_running"]:
        st.session_state["boss_binding_state"] = "Page unavailable"
        return status
    if not status["boss_found"]:
        st.session_state["boss_binding_state"] = "Reconnecting"
        try:
            ensure_cdp_tab(BOSS_URL, is_boss_url)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = browser_status()
                if status["boss_found"]:
                    st.session_state["boss_binding_state"] = "Connected"
                    st.session_state["boss_binding_url"] = status["boss_url"]
                    return status
                time.sleep(0.1)
        except Exception:
            pass
        st.session_state["boss_binding_state"] = "Page unavailable"
        return status
    if st.session_state.get("boss_binding_state") == "Page unavailable":
        st.session_state["boss_binding_state"] = "Reconnecting"
        health = ensure_boss_page_health(create_if_missing=True)
        _save_boss_health_state(health)
        return browser_status()
    if "boss_binding_state" not in st.session_state:
        st.session_state["boss_binding_state"] = "Connected"
    st.session_state["boss_binding_url"] = status["boss_url"]
    return status


def auto_prepare_platform_tab(platform: str) -> dict[str, Any]:
    if platform == "boss":
        status = auto_prepare_boss_tab()
        return {
            **status, "platform": "boss", "platform_found": status.get("boss_found", False),
            "page_url": status.get("boss_url", ""),
            "platform_state": boss_state_label(status),
        }
    status = platform_browser_status(platform)
    if not status["chrome_running"]:
        st.session_state[f"{platform}_binding_state"] = "Page unavailable"
        return status
    if not status["platform_found"]:
        try:
            ensure_cdp_tab(LIEPIN_URL, is_liepin_url)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = platform_browser_status(platform)
                if status["platform_found"]:
                    break
                time.sleep(0.1)
        except Exception:
            pass
    st.session_state[f"{platform}_binding_state"] = status.get("platform_state", "Page unavailable")
    st.session_state[f"{platform}_binding_url"] = status.get("page_url", "")
    return status


def boss_state_label(status: dict[str, Any]) -> str:
    state = str(st.session_state.get("boss_binding_state", ""))
    if not status.get("chrome_running"):
        return "Page unavailable"
    if status.get("login_required"):
        return "Login required"
    if state in {"Login required", "Reconnecting", "Page unavailable"}:
        return state
    return "Connected" if status.get("boss_found") else "Page unavailable"


def platform_state_label(platform: str, status: dict[str, Any]) -> str:
    if platform == "boss":
        return boss_state_label(status)
    state = str(st.session_state.get(f"{platform}_binding_state", ""))
    if not status.get("chrome_running"):
        return "Page unavailable"
    if status.get("login_required"):
        return "Login required"
    if state in {"Login required", "Reconnecting", "Page unavailable"}:
        return state
    return "Connected" if status.get("platform_found") else "Page unavailable"


def open_local_path(path: Path) -> None:
    if not path.exists():
        st.warning(f"Path does not exist yet: {path}")
        return
    subprocess.Popen(["/usr/bin/open", str(path)])


def display_local_path(path: Path) -> str:
    value = str(Path(path).expanduser())
    home = str(Path.home())
    return f"~{value[len(home):]}" if value.startswith(f"{home}/") else value


def keyword_widget_key(prefix: str, keyword: str) -> str:
    digest = hashlib.sha1(keyword_key(keyword).encode("utf-8")).hexdigest()[:12]
    return f"keyword_{prefix}_{digest}"


def _set_task_keywords(values: list[str]) -> None:
    st.session_state["task_keywords_text"] = keywords_to_text(values)


def _toggle_task_keyword(keyword: str) -> None:
    current = str(st.session_state.get("task_keywords_text", ""))
    _set_task_keywords(toggle_keyword_selection(current, keyword))


def _clear_task_keywords() -> None:
    st.session_state["task_keywords_text"] = ""


def _save_current_keywords() -> None:
    try:
        values = selected_keywords(str(st.session_state.get("task_keywords_text", "")))
        if not values:
            st.session_state["keyword_library_notice"] = "No keywords are selected."
            return
        KeywordLibrary(KEYWORD_LIBRARY_PATH).save_keywords(values)
        st.session_state["keyword_library_notice"] = f"Saved {len(values)} keyword(s)."
    except (OSError, ValueError) as exc:
        st.session_state["keyword_library_notice"] = f"Failed to save keywords: {exc}"


def _create_saved_keyword() -> None:
    value = str(st.session_state.get("new_saved_keyword", "")).strip()
    try:
        if not value:
            st.session_state["keyword_library_notice"] = "Enter a keyword first."
            return
        KeywordLibrary(KEYWORD_LIBRARY_PATH).save_keywords([value])
        st.session_state["new_saved_keyword"] = ""
        st.session_state["keyword_create_open"] = False
        st.session_state["keyword_library_notice"] = f"Created keyword: {value}"
    except (OSError, ValueError) as exc:
        st.session_state["keyword_library_notice"] = f"Failed to create keyword: {exc}"


def _delete_saved_keyword(keyword: str) -> None:
    try:
        KeywordLibrary(KEYWORD_LIBRARY_PATH).delete(keyword)
        st.session_state["keyword_library_notice"] = f"Removed from keyword library: {keyword}"
    except OSError as exc:
        st.session_state["keyword_library_notice"] = f"Failed to delete keyword: {exc}"


def _toggle_saved_keyword_pin(keyword: str) -> None:
    try:
        KeywordLibrary(KEYWORD_LIBRARY_PATH).toggle_pin(keyword)
    except OSError as exc:
        st.session_state["keyword_library_notice"] = f"Failed to update pin status: {exc}"


def _toggle_keyword_create_panel() -> None:
    st.session_state["keyword_create_open"] = not bool(
        st.session_state.get("keyword_create_open", False)
    )


def _toggle_keyword_manage_panel() -> None:
    st.session_state["keyword_manage_open"] = not bool(
        st.session_state.get("keyword_manage_open", False)
    )


def _toggle_show_all_keywords() -> None:
    st.session_state["show_all_saved_keywords"] = not bool(
        st.session_state.get("show_all_saved_keywords", False)
    )


def render_keyword_library(current_keywords: list[str]) -> None:
    library = KeywordLibrary(KEYWORD_LIBRARY_PATH)
    records = library.sorted_keywords()
    selected_keys = {keyword_key(value) for value in current_keywords}
    create_open = bool(st.session_state.get("keyword_create_open", False))
    manage_open = bool(st.session_state.get("keyword_manage_open", False))
    show_all = bool(st.session_state.get("show_all_saved_keywords", False))

    title_col, count_col, create_col, manage_col = st.columns(
        [3.2, 1.15, 1.25, 1.1], vertical_alignment="center",
    )
    title_col.markdown('<div class="keyword-panel-title">Saved keywords</div>', unsafe_allow_html=True)
    count_col.markdown(
        f'<div class="keyword-saved-count">{len(records)} saved</div>',
        unsafe_allow_html=True,
    )
    create_col.button(
        "＋ New", key="toggle_keyword_create", type="tertiary",
        on_click=_toggle_keyword_create_panel,
    )
    manage_col.button(
        "Done" if manage_open else "Manage", key="toggle_keyword_manage", type="tertiary",
        on_click=_toggle_keyword_manage_panel,
    )

    if create_open:
        new_input, new_button = st.columns([4, 1], vertical_alignment="bottom")
        new_input.text_input(
            "New keyword", placeholder="Enter a keyword", key="new_saved_keyword",
            label_visibility="collapsed",
        )
        new_button.button(
            "Save", key="create_saved_keyword", width="stretch",
            on_click=_create_saved_keyword,
        )

    notice = str(st.session_state.pop("keyword_library_notice", "") or "")
    if notice:
        st.caption(notice)

    visible_records = records if show_all or len(records) <= 20 else records[:20]
    with st.container(key="saved_keyword_body"):
        if not records:
            st.markdown(
                '<div class="keyword-empty-state"><strong>No saved keywords</strong>'
                '<span>Enter keywords on the left, then click “Save current keywords”.</span></div>',
                unsafe_allow_html=True,
            )
            # Keep an accessible fallback message for automated UI checks.
            with st.container(key="keyword_empty_compat"):
                st.caption("No saved keywords yet. Save the current list to reuse it later.")
        else:
            with st.container(
                horizontal=True, horizontal_alignment="left", gap="small",
                key="saved_keyword_chips",
            ):
                for record in visible_records:
                    name = str(record["name"])
                    selected = keyword_key(name) in selected_keys
                    st.button(
                        f"{'✓ ' if selected else ''}{name}",
                        key=keyword_widget_key("toggle", name),
                        type="primary" if selected else "secondary",
                        help=(
                            f"Used {int(record.get('use_count', 0) or 0)} time(s); "
                            "click to toggle this keyword"
                        ),
                        on_click=_toggle_task_keyword, args=(name,),
                    )
            if len(records) > 20:
                st.button(
                    "Collapse" if show_all else f"Show all ({len(records)})",
                    key="toggle_show_all_keywords", type="tertiary",
                    on_click=_toggle_show_all_keywords,
                )

        if manage_open and records:
            st.caption("Manage saved keywords: use the dot to pin and × to delete.")
            for record in visible_records:
                name = str(record["name"])
                label_col, pin_col, delete_col = st.columns(
                    [7, 1, 1], vertical_alignment="center",
                )
                label_col.markdown(f"<span>{escape(name)}</span>", unsafe_allow_html=True)
                pin_col.button(
                    "●" if record.get("pinned") else "○",
                    key=keyword_widget_key("pin", name),
                    help="Unpin" if record.get("pinned") else "Pin",
                    on_click=_toggle_saved_keyword_pin, args=(name,),
                )
                delete_col.button(
                    "×", key=keyword_widget_key("delete", name), help="Delete",
                    on_click=_delete_saved_keyword, args=(name,),
                )
        elif records:
            # Compatibility controls are hidden by CSS in the default interface.
            with st.container(key="keyword_hidden_controls"):
                for record in records:
                    name = str(record["name"])
                    st.button(
                        "×", key=keyword_widget_key("delete", name), help="Delete",
                        on_click=_delete_saved_keyword, args=(name,),
                    )

    st.button(
        "Save current keywords", key="save_current_keywords", width="stretch",
        on_click=_save_current_keywords,
    )


def schedule_service_stop(close_chrome: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = (LOG_DIR / "service-control.log").open("a", encoding="utf-8")
    command = [
        sys.executable, "-m", "utils.desktop_service", "stop-service",
        "--pid-dir", str(PID_DIR), "--delay", "1.5",
    ]
    if close_chrome:
        command.append("--close-chrome")
    subprocess.Popen(
        command, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def short_boss_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "zhipin.com").removeprefix("www.")
    path = parsed.path or "/"
    return f"{host}{path}"


def result_breakdown(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if result is None:
        return []
    run_dir = Path(result["run_dir"])
    config = load_json(run_file_path(run_dir, "run_config.json"))
    keywords = [str(value) for value in config.get("search_keywords", []) if str(value).strip()]
    target = int(config.get("jobs_per_keyword", 0) or 0)
    valid = Counter(
        str(row.get("search_keyword", ""))
        for row in load_jsonl(run_file_path(run_dir, "jobs.jsonl"))
    )
    invalid = Counter(
        str(row.get("search_keyword", ""))
        for row in load_jsonl(run_file_path(run_dir, "invalid_records.jsonl"))
    )
    if not keywords:
        keywords = list(dict.fromkeys([*valid.keys(), *invalid.keys()]))
    return [
        {"Keyword": keyword, "Target": target, "Valid": valid[keyword], "Invalid": invalid[keyword]}
        for keyword in keywords if keyword
    ]


def task_display_state(lines: list[str], keywords: list[str], target: int,
                       running: bool, result: dict[str, Any] | None) -> dict[str, Any]:
    stats = {keyword: {"valid": 0, "invalid": 0} for keyword in keywords}
    keyword_states: dict[str, dict[str, Any]] = {
        keyword: {"status": "pending", "error": "", "processed": 0}
        for keyword in keywords
    }
    current_keyword = ""
    current_index = 0
    current_rank = 0
    title = ""
    stage = "Waiting to start"
    failure = ""
    stopped = False
    finished = False
    screenshot_failed = 0

    for line in lines:
        match = re.search(r"\[(\d+)/(\d+)\]\s*自动搜索关键词：(.+)$", line)
        if match:
            current_index = int(match.group(1))
            current_keyword = match.group(3).strip()
            stats.setdefault(current_keyword, {"valid": 0, "invalid": 0})
            keyword_states.setdefault(
                current_keyword, {"status": "pending", "error": "", "processed": 0}
            )["status"] = "searching"
            stage = "Searching jobs"
        detail_match = re.search(r"详情页第\s*\d+\s*次加载\s*\[(\d+)\]", line)
        if detail_match:
            current_rank = int(detail_match.group(1))
            stage = "Loading job details"
        field_match = re.search(r"详情字段：title=(.*?)\s+salary=", line)
        if field_match:
            title = field_match.group(1).strip()
            stage = "Extracting fields"
        if "有效岗位已写入JSONL" in line:
            if current_keyword:
                stats.setdefault(current_keyword, {"valid": 0, "invalid": 0})["valid"] += 1
            stage = "Saving screenshot"
        if "已保存真实详情页截图" in line:
            stage = "Job saved"
        if "岗位截图失败：" in line:
            screenshot_failed += 1
            stage = "Screenshot failed"
            failure = line.split("原因=", 1)[-1].strip()[:100]
        if "最终失败岗位：" in line:
            if current_keyword:
                stats.setdefault(current_keyword, {"valid": 0, "invalid": 0})["invalid"] += 1
            failure = line.split("原因=", 1)[-1].strip()[:100]
        search_failure = re.search(r"关键词搜索失败：(.+?)\s+原因=(.+?)\s+processed=(\d+)", line)
        if search_failure:
            failed_keyword = search_failure.group(1).strip()
            reason = search_failure.group(2).strip()
            keyword_states.setdefault(
                failed_keyword, {"status": "pending", "error": "", "processed": 0}
            ).update({"status": "failed", "error": reason, "processed": 0})
            failure = reason[:100]
            stage = "Search failed"
        keyword_result = re.search(
            r"关键词执行结果：keyword=(.+?)\s+status=(\w+)\s+processed=(\d+)\s+"
            r"valid=(\d+)\s+invalid=(\d+)\s+error=(.*)$", line
        )
        if keyword_result:
            result_keyword = keyword_result.group(1).strip()
            result_status = keyword_result.group(2).strip()
            processed_value = int(keyword_result.group(3))
            stats[result_keyword] = {
                "valid": int(keyword_result.group(4)),
                "invalid": int(keyword_result.group(5)),
            }
            keyword_states[result_keyword] = {
                "status": result_status,
                "processed": processed_value,
                "error": keyword_result.group(6).strip(),
            }
        if "[UI] Current scan stopped" in line:
            stopped = True
        if "[UI] Scan process exited" in line:
            finished = True

    if result is not None and not running:
        for row in result_breakdown(result):
            stats.setdefault(row["Keyword"], {"valid": 0, "invalid": 0})
            stats[row["Keyword"]] = {"valid": row["Valid"], "invalid": row["Invalid"]}

    latest_job: dict[str, Any] = {}
    if result is not None:
        jobs = load_jsonl(run_file_path(Path(result["run_dir"]), "jobs.jsonl"))
        latest_job = jobs[-1] if jobs else {}
    title = title or str(latest_job.get("title", ""))
    company = str(latest_job.get("company", ""))

    rows = []
    for index, keyword in enumerate(keywords, 1):
        values = stats.get(keyword, {"valid": 0, "invalid": 0})
        state_info = keyword_states.get(
            keyword, {"status": "pending", "error": "", "processed": 0}
        )
        processed = max(
            int(state_info.get("processed", 0)), values["valid"] + values["invalid"]
        )
        explicit_status = str(state_info.get("status", "pending"))
        if explicit_status in {"failed", "search_failed", "page_lost"}:
            error_text = str(state_info.get("error", ""))
            if explicit_status == "page_lost":
                state, icon = "Page unavailable", "×"
            else:
                state, icon = ("Search failed" if "搜索" in error_text or "结果页" in error_text
                               or explicit_status == "search_failed" else "Failed"), "×"
        elif explicit_status == "completed":
            state, icon = "Completed", "✓"
        elif explicit_status in {"historical_skipped", "no_new_jobs"}:
            state, icon = "Checked — no new jobs", "✓"
        elif explicit_status == "no_results":
            state, icon = "Search complete — no jobs", "✓"
        elif explicit_status == "stopped":
            state, icon = "Stopped", "■"
        elif explicit_status == "partial_failed":
            state, icon = "Partially failed", "◆"
        elif stopped and index == current_index:
            state, icon = "Stopped", "■"
        elif running and index == current_index:
            state, icon = "Collecting", "●"
        elif values["invalid"] and (processed >= target or index < current_index or finished):
            state, icon = "Partially failed", "◆"
        elif processed >= target or index < current_index or (finished and processed):
            state, icon = "Completed", "✓"
        else:
            state, icon = "Waiting", "○"
        rows.append({
            "keyword": keyword, "state": state, "icon": icon,
            "processed": processed, "valid": values["valid"], "invalid": values["invalid"],
            "error": str(state_info.get("error", "")),
            "progress": 1.0 if explicit_status in {"historical_skipped", "no_new_jobs", "no_results"} else (
                min(processed / target, 1.0) if target else 0.0
            ),
        })

    valid_total = sum(value["valid"] for value in stats.values())
    invalid_total = sum(value["invalid"] for value in stats.values())
    processed_total = valid_total + invalid_total
    keyword_completed = sum(
        1 for value in keyword_states.values()
        if value.get("status") in {"completed", "historical_skipped", "no_new_jobs", "no_results"}
    )
    keyword_failed = sum(
        1 for value in keyword_states.values()
        if value.get("status") in {"failed", "search_failed", "page_lost"}
    )
    result_status = str(result.get("status", "")) if result is not None else ""
    task_state = dict(result.get("task_state", {})) if result is not None else {}
    runtime_status = str(task_state.get("task_status", ""))
    keyword_stopped = sum(
        1 for value in keyword_states.values() if value.get("status") == "stopped"
    )
    if runtime_status == "waiting_for_login":
        task_status = "Login required"
        stage = "Waiting for BOSS Zhipin login"
        failure = str(task_state.get("error_message", "") or failure)[:100]
    elif runtime_status == "reconnecting_browser":
        task_status = "Reconnecting"
        stage = "Reconnecting dedicated Chrome"
    elif runtime_status == "paused_browser_lost" or result_status == "paused_browser_lost":
        task_status = "Paused / partially completed"
        stage = "Browser connection lost"
        failure = str(task_state.get("error_message", "") or failure)[:100]
    elif result_status == "stopped" or keyword_stopped:
        task_status = "Stopped"
    elif result_status == "partial_failed" or (keyword_completed and keyword_failed):
        task_status = "Partially completed"
    elif result_status == "failed" or (keyword_failed and not keyword_completed):
        task_status = "Failed"
    elif result_status == "completed" or (keyword_states and keyword_completed == len(keyword_states)):
        task_status = "Completed"
    else:
        task_status = "Running" if running else "Idle"
    return {
        "rows": rows,
        "current_keyword": current_keyword or "—",
        "keyword_index": current_index,
        "current_rank": current_rank,
        "title": title or "Waiting for job details",
        "company": company or "—",
        "stage": stage,
        "failure": failure,
        "processed": processed_total,
        "valid": valid_total,
        "invalid": invalid_total,
        "screenshot_failed": screenshot_failed,
        "success_rate": valid_total / processed_total if processed_total else 0.0,
        "keyword_completed": keyword_completed,
        "keyword_failed": keyword_failed,
        "infrastructure_failed": int(task_state.get("infrastructure_failed_count", 0) or 0),
        "browser_disconnects": int(task_state.get("browser_disconnect_count", 0) or 0),
        "pending": int(task_state.get("pending_count", 0) or 0),
        "runtime_status": runtime_status,
        "task_status": task_status,
    }


def simplify_log_line(line: str) -> str:
    timestamp = ""
    timestamp_match = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", line)
    if timestamp_match:
        timestamp = timestamp_match.group(1)
    message = re.sub(
        r"^\d{4}-\d{2}-\d{2}\s+[\d:,]+\s*\|\s*\w+\s*\|\s*", "", line
    ).strip()
    if any(marker in message for marker in (
        "search_page", "当前标签页总数", "标签页 ", "实际命中选择器", "对象id", "search_id=",
        "已关闭脚本自己创建的detail_page",
    )):
        return ""
    detail = re.search(r"详情页第\s*\d+\s*次加载\s*\[(\d+)\]", message)
    if detail:
        message = f"Loading job {detail.group(1)} details"
    elif message.startswith("详情字段："):
        title = re.search(r"title=(.*?)\s+salary=", message)
        message = f"Job fields extracted: {title.group(1).strip()}" if title and title.group(1).strip() \
            else "Job fields extracted"
    elif "有效岗位已写入JSONL" in message:
        rank = re.search(r"序号=(\d+)", message)
        message = f"Job {rank.group(1)} data saved" if rank else "Job data saved"
    elif "已保存真实详情页截图" in message:
        message = "Screenshot saved"
    elif "最终失败岗位：" in message:
        rank = re.search(r"序号=(\d+)", message)
        reason = message.split("原因=", 1)[-1].strip()
        message = f"Job {rank.group(1)} failed: {reason}" if rank else f"Job failed: {reason}"
    elif "岗位截图失败：" in message:
        message = "Screenshot failed; job data was retained"
    elif "关键词搜索失败：" in message:
        failed = re.search(r"关键词搜索失败：(.+?)\s+原因=(.+?)\s+processed=", message)
        message = (
            f"{failed.group(1).strip()} search failed: {failed.group(2).strip()}"
            if failed else "Keyword search failed"
        )
    elif "跳过已采集URL" in message:
        message = "Duplicate job skipped"
    elif "关键词无新增岗位：" in message:
        no_new = re.search(r"keyword=(.+?)\s+status=", message)
        message = f"{no_new.group(1).strip()} checked — no new jobs" if no_new else "Checked — no new jobs"
    elif "详情采集完成，低频等待" in message:
        message = "Job processed"
    elif "已导出 Excel" in message:
        counts = re.search(r"有效(\d+)条，无效(\d+)条", message)
        message = f"Task complete: {counts.group(1)} valid, {counts.group(2)} invalid" \
            if counts else "Excel output generated"
    message = re.sub(r"https?://\S+", "zhipin.com/…", message)
    message = re.sub(r"/(?:Users|private|tmp)/\S+", "local-results", message)
    message = re.sub(r"\bPID\s*\d+\b", "", message, flags=re.IGNORECASE)
    message = re.sub(r"\s+", " ", message).strip()
    return f"{timestamp}  {message}".strip()[:140]


def visible_recent_logs(lines: list[str], limit: int = 7) -> list[str]:
    simplified = [
        value for line in compact_log_lines(lines) if (value := simplify_log_line(line))
    ]
    return simplified[-limit:]


st.set_page_config(page_title="AI Job Intelligence Collector", layout="wide")
st.markdown(
    """
    <style>
    :root { color-scheme: light !important; }
    html, body, [class*="css"], .stApp {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC",
        "Helvetica Neue", sans-serif;
    }
    html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
      background: #f7f8fa !important;
      color: #17181a !important;
      color-scheme: light !important;
    }
    header[data-testid="stHeader"],
    [data-testid="stHeader"],
    [data-testid="stToolbar"],
    [data-testid="stToolbarActions"],
    [data-testid="stDeployButton"],
    [data-testid="stAppDeployButton"],
    [data-testid="stBaseButton-header"],
    [data-testid="stMainMenu"],
    [data-testid="stMainMenuButton"],
    [data-testid="stStatusWidget"],
    [data-testid="stDecoration"],
    [data-testid="stFooter"],
    [data-testid="stBottom"],
    [data-testid="stBottomBlockContainer"],
    #MainMenu,
    footer {
      display: none !important;
      visibility: hidden !important;
      width: 0 !important;
      height: 0 !important;
      min-width: 0 !important;
      min-height: 0 !important;
      margin: 0 !important;
      padding: 0 !important;
      overflow: hidden !important;
      pointer-events: none !important;
    }
    [data-testid="stAppViewContainer"] > .main { padding-top: 0; }
    .block-container {
      max-width: 1500px;
      padding-top: 24px;
      padding-bottom: 32px;
    }
    h1 { font-size: 1.9rem !important; letter-spacing: -0.035em; margin-bottom: 0 !important; }
    h2, h3 { letter-spacing: -0.02em; }
    div[data-testid="stVerticalBlockBorderWrapper"] {
      background: #ffffff; border: 1px solid #e4e6ea; border-radius: 10px;
      box-shadow: none; padding: 0.15rem;
    }
    div[data-testid="stMetric"] {
      background: #fff; border: 1px solid #e7e8eb; border-radius: 9px;
      padding: 0.65rem 0.75rem;
    }
    div[data-testid="stMetricLabel"] { font-size: 0.76rem; color: #74777d; }
    div[data-testid="stMetricValue"] { font-size: 1.45rem; line-height: 1.25; }
    div[data-baseweb="input"],
    div[data-baseweb="base-input"],
    div[data-baseweb="select"] > div,
    div[data-testid="stTextArea"] textarea,
    div[data-testid="stNumberInput"] button {
      background-color: #f1f3f5 !important;
      color: #17181a !important;
      border-color: #dfe2e6 !important;
    }
    div[data-baseweb="input"] input,
    div[data-baseweb="base-input"] input,
    div[data-testid="stTextArea"] textarea,
    div[data-baseweb="select"] * {
      color: #17181a !important;
      -webkit-text-fill-color: #17181a !important;
    }
    div[data-baseweb="popover"],
    div[data-baseweb="menu"],
    [role="listbox"] {
      background-color: #ffffff !important;
      color: #17181a !important;
    }
    div[data-testid="stHorizontalBlock"] { align-items:flex-start; }
    div[data-testid="column"] { min-width:0; overflow:visible; }
    .status-row {
      display:flex;
      width:100%;
      max-width:100%;
      box-sizing:border-box;
      justify-content:flex-end;
      align-items:center;
      gap:7px;
      flex-wrap:wrap;
      padding-top:16px;
    }
    .status-pill {
      display:inline-flex; flex:0 0 auto; max-width:100%; align-items:center; gap:6px;
      padding:5px 9px; border-radius:999px; white-space:nowrap;
      border:1px solid #e1e3e7; background:#fff; color:#4a4d52; font-size:12px; font-weight:600;
    }
    .status-pill::before { content:""; width:7px; height:7px; border-radius:50%; background:#a2a6ad; }
    .status-pill.ok::before { background:#26a269; }
    .status-pill.busy::before { background:#e9a23b; }
    .boss-link {
      width:100%; max-width:100%; box-sizing:border-box; text-align:right; overflow-wrap:anywhere;
      color:#73767c; font-size:12px; margin-top:7px;
    }
    .boss-link a { color:#60646c; text-decoration:none; }
    .boss-link a:hover { text-decoration:underline; }
    .section-kicker { font-size:12px; color:#8a8d92; font-weight:650; text-transform:uppercase;
      letter-spacing:.08em; margin-bottom:2px; }
    .config-summary { color:#666a70; font-size:13px; padding:3px 0 5px; }
    .keyword-panel-title {
      color:#202226; font-size:17px; line-height:32px; font-weight:650;
    }
    .keyword-saved-count {
      color:#777b82; font-size:12px; line-height:32px; text-align:right; white-space:nowrap;
    }
    .st-key-keyword_left_panel,
    .st-key-keyword_right_panel {
      background:#fff; border:1px solid #e1e4e8; border-radius:10px;
      padding:16px !important; box-sizing:border-box;
    }
    .st-key-keyword_left_body,
    .st-key-saved_keyword_body { min-height:194px; }
    .keyword-empty-state {
      min-height:160px; display:flex; flex-direction:column; align-items:center;
      justify-content:center; gap:7px; text-align:center; color:#777b82; font-size:13px;
    }
    .keyword-empty-state strong { color:#4f5359; font-size:14px; font-weight:600; }
    .st-key-saved_keyword_chips {
      display:flex !important; flex-wrap:wrap !important; align-items:center !important;
      align-content:flex-start !important; gap:8px !important; width:100%;
    }
    .st-key-saved_keyword_chips [data-testid="stButton"] {
      flex:0 0 auto !important; width:auto !important; margin:0 !important;
    }
    .st-key-saved_keyword_chips [data-testid="stButton"] > button {
      display:inline-flex !important; align-items:center !important; width:auto !important;
      min-height:34px !important; padding:6px 10px !important; margin:0 !important;
      border-radius:8px !important; border:1px solid #dfe3e8 !important;
      background:#fff !important; color:#25282d !important; font-size:14px !important;
      line-height:20px !important; font-weight:500 !important; box-shadow:none !important;
    }
    .st-key-saved_keyword_chips [data-testid="stButton"] > button[kind="primary"] {
      border-color:#dc3f46 !important; background:#fff1f2 !important;
      color:#27292d !important;
    }
    .st-key-keyword_hidden_controls { display:none !important; }
    .st-key-keyword_empty_compat { display:none !important; }
    .st-key-toggle_keyword_create button,
    .st-key-toggle_keyword_manage button,
    .st-key-toggle_show_all_keywords button {
      min-height:30px !important; padding:4px 7px !important; font-size:12px !important;
      white-space:nowrap;
    }
    .task-row { border-bottom:1px solid #eceef1; padding:9px 2px 10px; }
    .task-row:last-child { border-bottom:0; }
    .task-head { display:flex; justify-content:space-between; align-items:center; gap:10px; }
    .task-name { font-weight:650; font-size:14px; color:#202226; }
    .task-state { font-size:12px; color:#6f7379; white-space:nowrap; }
    .task-meta { color:#777b82; font-size:12px; margin-top:4px; }
    .mini-track { height:4px; background:#eceef1; border-radius:999px; overflow:hidden; margin-top:7px; }
    .mini-fill { height:100%; background:#5c67d8; border-radius:999px; }
    .current-job { padding:3px 1px; }
    .current-job .eyebrow { color:#85898f; font-size:12px; margin-bottom:4px; }
    .current-job .job-title { color:#202226; font-size:17px; line-height:1.35; font-weight:650; }
    .current-job .company { color:#686c73; font-size:13px; margin:4px 0 12px; }
    .stage-chip { display:inline-flex; border-radius:7px; padding:5px 8px; background:#f0f1f4;
      color:#555961; font-size:12px; font-weight:600; }
    .compact-result { font-size:14px; color:#53575e; }
    .compact-result strong { display:block; color:#202226; font-size:18px; margin-bottom:3px; }
    .st-key-task_keywords_text textarea {
      min-height:150px !important; max-height:150px !important; resize:none;
    }
    div.stButton > button[kind="primary"] { background:#dc3f46; border-color:#dc3f46; font-weight:650; }
    div.stButton > button[kind="primary"]:hover { background:#c9343b; border-color:#c9343b; }
    div[data-testid="stExpander"] { border:1px solid #e4e6ea; border-radius:9px; background:#fff; }
    div[data-testid="stCode"] { font-size:12px; }
    @media (max-width: 900px) {
      .status-row { justify-content:flex-end; }
      .boss-link { text-align:right; }
      .block-container { padding-left:1rem; padding-right:1rem; }
      div[data-testid="stHorizontalBlock"]:has(.st-key-keyword_left_panel):has(.st-key-keyword_right_panel) {
        flex-wrap:wrap !important;
      }
      div[data-testid="stHorizontalBlock"]:has(.st-key-keyword_left_panel):has(.st-key-keyword_right_panel)
        > div[data-testid="column"] {
        flex:1 1 100% !important; width:100% !important; min-width:100% !important;
      }
      .st-key-keyword_left_panel,
      .st-key-keyword_right_panel { min-height:auto; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

controller = get_controller(CONTROLLER_INTERFACE_VERSION)


@st.fragment(run_every=2.0)
def top_bar(platform: str) -> None:
    status = platform_browser_status(platform)
    binding_label = platform_state_label(platform, status)
    platform_name = platform_display_name(platform)
    running = controller.is_running()
    latest = read_run_results(OUTPUT_ROOT)
    latest_status = str(latest.get("status", "")) if latest is not None else ""
    task_label = "Running" if running else (
        "Partially completed" if latest_status == "partial_failed" else
        ("Stopped" if latest_status == "stopped" else
         ("Failed" if latest_status == "failed" else ("Completed" if latest is not None else "Idle")))
    )
    header_left, header_right = st.columns([3, 2], vertical_alignment="top")
    with header_left:
        st.title("AI Job Intelligence Collector")
        st.caption("Collect job details, full-page screenshots, and structured Excel output")
    with header_right:
        chrome_class = "ok" if status["chrome_running"] else ""
        boss_class = "ok" if binding_label == "Connected" else ("busy" if binding_label in {"Login required", "Reconnecting"} else "")
        task_class = "busy" if running else ("ok" if latest is not None else "")
        st.markdown(
            f'<div class="status-row">'
            f'<span class="status-pill {chrome_class}">Chrome: {"Connected" if status["chrome_running"] else "Disconnected"}</span>'
            f'<span class="status-pill {boss_class}">{platform_name}: {state_display_label(binding_label)}</span>'
            f'<span class="status-pill {task_class}">Current task: {task_label}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if status["page_url"]:
            label = escape(short_boss_url(status["page_url"]))
            url = escape(status["page_url"], quote=True)
            st.markdown(
                f'<div class="boss-link">{platform_name} page: <a href="{url}" target="_blank">{label}</a></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(f'<div class="boss-link">{platform_name} page: not connected</div>', unsafe_allow_html=True)


selected_platform = platform_from_label(
    str(st.session_state.get("platform_select", "BOSS Zhipin (available)"))
)
prepared_platform_status = auto_prepare_platform_tab(selected_platform)
top_bar(selected_platform)

st.markdown('<div class="section-kicker">Collection settings</div>', unsafe_allow_html=True)
with st.container(border=True):
    config1, config2, config3, config4 = st.columns([2.1, 1.25, 1.45, 1.35], vertical_alignment="bottom")
    with config1:
        platform_label = st.selectbox(
            "Platform", ["BOSS Zhipin (available)", "Liepin (available)", "LinkedIn (not implemented)"],
            key="platform_select",
        )
    with config2:
        jobs_per_keyword = st.number_input("Jobs per keyword", 1, 50, 10, 1)
    with config3:
        city = st.text_input("City", value="Shanghai", placeholder="Optional")

    wait_min = float(st.session_state.get("advanced_wait_min", 6.0))
    wait_max = float(st.session_state.get("advanced_wait_max", 10.0))
    debug = bool(st.session_state.get("advanced_debug", False))
    save_mode = str(st.session_state.get("advanced_save_mode", "snapshot"))
    platform = platform_from_label(platform_label)
    platform_available = platform in {"boss", "liepin"}
    prepared_platform_label = platform_state_label(platform, prepared_platform_status)

    left_keywords, right_keywords = st.columns([1, 1], gap="medium", vertical_alignment="top")
    with left_keywords:
        with st.container(border=False, key="keyword_left_panel"):
            st.markdown(
                '<div class="keyword-panel-title">Keywords for this run</div>',
                unsafe_allow_html=True,
            )
            with st.container(key="keyword_left_body"):
                keyword_text = st.text_area(
                    "Keywords for this run",
                    value="",
                    height=150,
                    key="task_keywords_text",
                    label_visibility="collapsed",
                    help="Enter one keyword per line. Commas, semicolons, and line breaks are supported.",
                )
                try:
                    current_keywords = selected_keywords(keyword_text)
                    current_keyword_error = ""
                except ValueError as exc:
                    current_keywords = []
                    current_keyword_error = str(exc)
                st.caption(f"{len(current_keywords)} keyword(s) selected")
            repeat_clicked = st.button(
                "Repeat last run", width="stretch", key="repeat_task",
                disabled=prepared_platform_label != "Connected",
            )
    with right_keywords:
        with st.container(border=False, key="keyword_right_panel"):
            render_keyword_library(current_keywords)
    try:
        preview_config = build_ui_config(
            keyword_text, jobs_per_keyword, city, wait_min, wait_max, save_mode, platform
        )
        preview_error = ""
    except ValueError as exc:
        preview_config = None
        preview_error = str(exc)
    with config4:
        start_clicked = st.button(
            "Start collection", type="primary", width="stretch",
            disabled=not platform_available or prepared_platform_label != "Connected",
            key="start_scan",
        )

    keyword_count = len(preview_config["search_keywords"]) if preview_config else 0
    if current_keyword_error:
        st.error(current_keyword_error)
    elif preview_error:
        st.error(preview_error)
    elif not platform_available:
        st.info("This platform adapter is not implemented yet.")
    else:
        max_jobs = keyword_count * int(preview_config["jobs_per_keyword"])
        city_label = preview_config["city"] or "Any city"
        st.markdown(
            f'<div class="config-summary">{keyword_count} keyword(s) · {preview_config["jobs_per_keyword"]} per keyword'
            f' · up to {max_jobs} jobs · {escape(city_label)} · {platform_display_name(platform)}</div>',
            unsafe_allow_html=True,
        )
    st.caption(f"Completed runs are named by completion time and keywords, then saved to {display_local_path(OUTPUT_ROOT)}")

    if start_clicked:
        if preview_config is None:
            st.error(preview_error)
        else:
            try:
                launch_config(preview_config, debug)
            except ScanAlreadyRunningError as exc:
                st.warning(str(exc))
            except Exception as exc:
                record_ui_error("Start collection", exc)
    if repeat_clicked:
        try:
            saved = json.loads(LAST_CONFIG.read_text(encoding="utf-8"))
            launch_config(config_from_saved(saved), debug)
        except FileNotFoundError:
            st.warning("No previous run configuration is available.")
        except ScanAlreadyRunningError as exc:
            st.warning(str(exc))
        except Exception as exc:
            record_ui_error("Repeat last run", exc)

    if prepared_platform_label == "Login required" or st.session_state.get("pending_platform_config"):
        st.warning(f"Sign in on the {platform_display_name(platform)} page, then click Continue.")
        if st.button("Continue after manual action", key="continue_boss_login"):
            try:
                st.session_state[f"{platform}_binding_state"] = "Reconnecting"
                health = ensure_platform_page_health(platform, create_if_missing=True)
                _save_platform_health_state(platform, health)
                if health.get("ok"):
                    pending = st.session_state.get("pending_platform_config")
                    if pending:
                        _start_scan_process(
                            pending, bool(st.session_state.get("pending_platform_debug", False))
                        )
                    else:
                        st.success(f"{platform_display_name(platform)} page reconnected. Collection can start.")
                    st.rerun()
                else:
                    st.warning(str(health.get("message") or "The platform page is still unavailable."))
            except ScanAlreadyRunningError as exc:
                st.warning(str(exc))
            except Exception as exc:
                record_ui_error("Reconnect after login", exc)


@st.fragment(run_every=1.0)
def live_workspace() -> None:
    running = controller.is_running()
    current_platform = platform_from_label(
        str(st.session_state.get("platform_select", "BOSS Zhipin (available)"))
    )
    live_browser = platform_browser_status(current_platform)
    controller_lines = controller.logs()
    try:
        active_run_id = controller.active_run_id()
    except Exception:
        active_run_id = None
    result = read_run_results(OUTPUT_ROOT, run_id=active_run_id)
    file_lines = recent_log_lines(result["log_path"], limit=500) if result is not None else []
    activity_lines = list(file_lines or controller_lines)
    if file_lines:
        activity_lines.extend(line for line in controller_lines if line.startswith("[UI]"))
    active_config = load_json(UI_CONFIG)
    keywords = [
        str(value) for value in active_config.get("search_keywords", []) if str(value).strip()
    ]
    if not keywords and preview_config is not None:
        keywords = list(preview_config["search_keywords"])
    target = int(active_config.get("jobs_per_keyword", jobs_per_keyword) or jobs_per_keyword)
    task_keywords = keywords if running or active_run_id else []
    task_result = result if active_run_id else None
    task = task_display_state(activity_lines, task_keywords, target, running, task_result)

    if running and not live_browser.get("platform_found"):
        st.warning(f"{platform_display_name(current_platform)} page unavailable. The run will pause without marking remaining jobs invalid.")
    elif running and live_browser.get("login_required"):
        st.warning(f"{platform_display_name(current_platform)} login required. Sign in in the dedicated Chrome window, then continue.")

    st.markdown('<div class="section-kicker">Current run</div>', unsafe_allow_html=True)
    theoretical_total = len(task_keywords) * target
    metric_columns = st.columns(11)
    metric_values = [
        ("Current keyword", task["current_keyword"]),
        ("Keywords completed", f'{task["keyword_completed"]}/{len(task_keywords)}' if task_keywords else "0/0"),
        ("Keywords failed", task["keyword_failed"]),
        ("Jobs processed", f'{task["processed"]}/{theoretical_total}' if theoretical_total else "0/0"),
        ("Valid jobs", task["valid"]),
        ("Invalid jobs", task["invalid"]),
        ("Infrastructure errors", task["infrastructure_failed"]),
        ("Pending", task["pending"]),
        ("Screenshot failures", task["screenshot_failed"]),
        ("Success rate", f'{task["success_rate"]:.1%}'),
        ("Run status", task["task_status"]),
    ]
    for column, (label, value) in zip(metric_columns, metric_values):
        column.metric(label, value)

    if running:
        left, right = st.columns([1.9, 1], gap="medium")
        with left:
            with st.container(border=True):
                st.markdown("##### Keyword queue")
                for row in task["rows"]:
                    width = int(row["progress"] * 100)
                    st.markdown(
                        f'<div class="task-row"><div class="task-head">'
                        f'<span class="task-name">{escape(row["keyword"])}</span>'
                        f'<span class="task-state">{row["icon"]} {row["state"]}</span></div>'
                        f'<div class="task-meta">Progress {row["processed"]}/{target} · '
                        f'Valid {row["valid"]} · Failed {row["invalid"]}</div>'
                        f'<div class="mini-track"><div class="mini-fill" style="width:{width}%"></div></div></div>',
                        unsafe_allow_html=True,
                    )
                    if row.get("error"):
                        st.caption(f'Reason: {row["error"][:120]}')
        with right:
            with st.container(border=True):
                st.markdown("##### Current job")
                st.markdown(
                    f'<div class="current-job"><div class="eyebrow">'
                    f'{escape(task["current_keyword"])} · item {task["current_rank"] or "—"}</div>'
                    f'<div class="job-title">{escape(task["title"])}</div>'
                    f'<div class="company">{escape(task["company"])}</div>'
                    f'<span class="stage-chip">Stage: {escape(task["stage"])}</span></div>',
                    unsafe_allow_html=True,
                )
                if task["failure"]:
                    st.warning(task["failure"])
                action_stop, action_continue = st.columns(2)
                stop_label = (
                    "Stop and keep current results"
                    if task["runtime_status"] == "paused_browser_lost" else "Stop scan"
                )
                if action_stop.button(stop_label, width="stretch", key="stop_current_scan"):
                    st.success("Current scan stopped.") if controller.stop() else st.info("No scan is running.")
                continue_label = (
                    "Reconnect and continue"
                    if task["runtime_status"] == "paused_browser_lost" else "Continue after manual action"
                )
                if action_continue.button(continue_label, width="stretch",
                                          key="continue_scan"):
                    st.success("Continue signal sent.") if controller.continue_after_manual_action() else \
                        st.info("No scan is waiting for manual continuation.")
    else:
        with st.container(border=True):
            state_text = "Most recent run completed" if active_run_id and result is not None else "No active run"
            st.caption(state_text)

    st.markdown("##### Recent activity")
    brief_source = recent_log_lines(result["log_path"], limit=40) if result is not None else controller_lines
    brief_lines = visible_recent_logs(brief_source, limit=7)
    st.code("\n".join(brief_lines) if brief_lines else "Waiting for run activity…", language="text")

    st.markdown('<div class="section-kicker">Latest results</div>', unsafe_allow_html=True)
    if result is None:
        with st.container(border=True):
            st.caption("Excel output, screenshots, and keyword statistics will appear here after the first run.")
    else:
        with st.container(border=True):
            summary, excel_col, shots_col, folder_col = st.columns(
                [2.2, 1, 1, 1.15], vertical_alignment="center"
            )
            with summary:
                result_title = (
                    "Run paused" if result.get("status") == "paused_browser_lost"
                    else
                    "Run partially completed" if result.get("status") == "partial_failed"
                    else ("Run stopped" if result.get("status") == "stopped"
                          else ("Run failed" if result.get("status") == "failed" else "Run completed"))
                )
                st.markdown(
                    f'<div class="compact-result"><strong>{result_title}</strong>'
                    f'{result["valid_count"]} valid · {result["invalid_count"]} invalid · '
                    f'{result["screenshot_failed_count"]} screenshot failures</div>',
                    unsafe_allow_html=True,
                )
                st.caption(f'Run name: {result["task_name"]}')
                st.caption(f'Saved to: {display_local_path(result["run_dir"])}')
            if excel_col.button("Open Excel", width="stretch", key="open_excel"):
                open_local_path(result["excel_path"])
            if shots_col.button("Open screenshots", width="stretch", key="open_screenshots"):
                open_local_path(result["screenshots_dir"])
            if folder_col.button("Open run folder", width="stretch", key="open_run_dir"):
                open_local_path(result["run_dir"])
            breakdown = result_breakdown(result)
            if breakdown:
                st.dataframe(breakdown, hide_index=True, width="stretch")
            if result["invalid_items"]:
                with st.expander("View failed jobs"):
                    st.dataframe(result["invalid_items"], hide_index=True, width="stretch")

        recent_results = read_recent_run_results(OUTPUT_ROOT, limit=5)
        if recent_results:
            st.markdown("##### Recent runs")
            for index, recent in enumerate(recent_results):
                recent_left, recent_counts, recent_open = st.columns(
                    [3.2, 1.5, 1], vertical_alignment="center"
                )
                with recent_left:
                    st.markdown(f'**{escape(recent["task_name"])}**')
                    st.caption(display_local_path(recent["run_dir"]))
                recent_counts.caption(
                    f'Valid {recent["valid_count"]} · Invalid {recent["invalid_count"]}'
                )
                if recent_open.button(
                    "Open folder", width="stretch", key=f"open_recent_run_{index}"
                ):
                    open_local_path(recent["run_dir"])


live_workspace()

with st.expander("Advanced settings and full logs", expanded=False):
    settings_tab, log_tab = st.tabs(["Advanced settings", "Full run logs"])
    with settings_tab:
        wait1, wait2, mode_col, debug_col = st.columns(4)
        with wait1:
            st.number_input(
                "Minimum wait (seconds)", min_value=0.0, value=6.0, step=0.5,
                key="advanced_wait_min",
            )
        with wait2:
            st.number_input(
                "Maximum wait (seconds)", min_value=0.0, value=10.0, step=0.5,
                key="advanced_wait_max",
            )
        with mode_col:
            st.selectbox(
                "Save mode", ["snapshot", "new_only"], index=0,
                format_func=lambda value: "Save this snapshot" if value == "snapshot" else "Save new jobs only",
                key="advanced_save_mode",
            )
        with debug_col:
            st.checkbox("Debug mode", value=False, key="advanced_debug")

        advanced_platform = platform_from_label(
            str(st.session_state.get("platform_select", "BOSS Zhipin (available)"))
        )
        advanced_status = platform_browser_status(advanced_platform)
        st.caption(f"Connected URL: {advanced_status['page_url'] or 'Not connected'}")
        with st.container(border=True):
            st.caption("Browser state")
            st.text("\n".join(advanced_status.get("pages", [])) or "No available pages")

        browser1, browser_home, browser2, browser3 = st.columns(4)
        if browser1.button("Reconnect platform page", width="stretch", key="rebind_boss"):
            try:
                st.session_state[f"{advanced_platform}_binding_state"] = "Reconnecting"
                health = ensure_platform_page_health(advanced_platform, create_if_missing=True)
                _save_platform_health_state(advanced_platform, health)
                if health.get("ok"):
                    controller.continue_after_manual_action()
                    st.success("Platform page confirmed and the scanner was asked to reconnect.")
                elif health.get("login_required"):
                    st.warning(f"Sign in on the {platform_display_name(advanced_platform)} page, then click Continue.")
                else:
                    st.warning(str(health.get("message") or "Failed to reconnect the platform page"))
                st.rerun()
            except Exception as exc:
                record_ui_error("Reconnect platform page", exc)
        if browser_home.button("Open platform home", width="stretch", key="open_platform_home"):
            try:
                home_url = BOSS_URL if advanced_platform == "boss" else LIEPIN_URL
                matcher = is_boss_url if advanced_platform == "boss" else is_liepin_url
                ensure_cdp_tab(home_url, matcher)
                st.success(f"Opened {platform_display_name(advanced_platform)} in the dedicated Chrome window.")
            except Exception as exc:
                record_ui_error("Open platform home", exc)
        if browser2.button(
            "Restart dedicated Chrome", width="stretch", key="restart_chrome",
            disabled=controller.is_running(),
        ):
            try:
                restart_dedicated_chrome(PID_DIR, LOG_DIR)
                st.success("Dedicated Chrome restarted and the platform/UI tabs were reopened.")
            except Exception as exc:
                record_ui_error("Restart dedicated Chrome", exc)
        with browser3:
            close_chrome = st.checkbox("Also close dedicated Chrome", value=False, key="close_chrome")
            if st.button(
                "Stop collector service", width="stretch", key="stop_service",
                disabled=controller.is_running(),
            ):
                try:
                    controller.stop()
                    schedule_service_stop(close_chrome)
                    st.success("Stop request submitted. This page will disconnect when the service exits.")
                except Exception as exc:
                    record_ui_error("Stop collector service", exc)
    with log_tab:
        latest = read_run_results(OUTPUT_ROOT)
        full_lines = controller.logs()
        st.caption(f"Full error details: {latest['log_path'] if latest else LOG_DIR}")
        st.code(
            "\n".join(compact_log_lines(full_lines)) if full_lines else "No logs for this session.",
            language="text",
        )
