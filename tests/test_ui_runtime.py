import ast
import importlib
import io
import json
from pathlib import Path

import pytest

from utils.config import load_config
from utils.runtime_options import apply_runtime_overrides
from utils.ui_runtime import (
    ScanAlreadyRunningError,
    ScanProcessController,
    build_ui_config,
    compact_log_lines,
    recent_log_lines,
    read_recent_run_results,
    task_preview_lines,
    write_ui_config,
)
from utils.run_paths import write_run_config


def test_ui_two_lines_create_two_keywords():
    config = build_ui_config("交易系统运维\n审核岗", 20, "上海", 6, 10)
    assert config["search_keywords"] == ["交易系统运维", "审核岗"]
    assert task_preview_lines(config)[:3] == [
        "Keywords in this run: 2", "1. 交易系统运维", "2. 审核岗",
    ]


@pytest.mark.parametrize(
    "raw", ["交易系统运维,审核岗", "交易系统运维，审核岗", "交易系统运维;审核岗", "交易系统运维；审核岗"],
)
def test_ui_all_supported_separators_keep_both_keywords(raw):
    assert build_ui_config(raw, 10, "上海", 6, 10)["search_keywords"] == [
        "交易系统运维", "审核岗",
    ]


def test_json_keyword_list_is_not_reparsed_by_shell(tmp_path):
    path = tmp_path / "ui_run_config.json"
    expected = build_ui_config("交易系统运维\n审核岗", 20, "上海", 6, 10)
    write_ui_config(path, expected)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["search_keywords"] == ["交易系统运维", "审核岗"]
    loaded = apply_runtime_overrides(load_config(path))
    assert loaded["search_keywords"] == ["交易系统运维", "审核岗"]


class FakeProcess:
    pid = 43210
    stdout = None
    returncode = None

    def __init__(self):
        self.stdin = io.StringIO()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_controller_prevents_duplicate_and_passes_json_config(tmp_path, monkeypatch):
    project = tmp_path
    (project / "data").mkdir()
    config_path = project / "data" / "ui_run_config.json"
    write_ui_config(config_path, build_ui_config("关键词一\n关键词二", 2, "上海", 0, 0))
    commands = []

    def factory(command, **kwargs):
        commands.append(command)
        return FakeProcess()

    controller = ScanProcessController(project, popen_factory=factory)
    monkeypatch.setattr(controller, "_read_output", lambda process: None)
    controller.start(config_path, debug=True)
    with pytest.raises(ScanAlreadyRunningError):
        controller.start(config_path, debug=True)
    assert commands[0][commands[0].index("--config") + 1] == "data/ui_run_config.json"
    assert "--keywords" not in commands[0]


def test_traceback_is_compacted_for_frontend():
    lines = [
        "任务开始", "Traceback (most recent call last):", '  File "main.py", line 1',
        "RuntimeError: secret detail", "[UI] Scan process exited with code: 1",
    ]
    compact = compact_log_lines(lines)
    assert compact == ["任务开始", "[Full traceback written to app.log]", "[UI] Scan process exited with code: 1"]


def test_retry_success_is_not_counted_as_invalid(tmp_path):
    controller = ScanProcessController(tmp_path)
    controller.add_log_lines([
        "[1/1] 自动搜索关键词：交易系统运维",
        "详情尝试未通过验证：第1次，原因=字段为空",
        "有效岗位已写入JSONL：序号=1 URL=https://www.zhipin.com/job_detail/a.html",
    ])
    progress = controller.progress()
    assert progress["processed"] == 1
    assert progress["valid"] == 1
    assert progress["invalid"] == 0
    assert progress["success_rate"] == 1.0


def test_recent_log_lines_missing_file_returns_empty(tmp_path):
    assert recent_log_lines(tmp_path / "missing.log") == []


def test_recent_log_lines_empty_file_returns_empty(tmp_path):
    path = tmp_path / "empty.log"
    path.write_text("", encoding="utf-8")
    assert recent_log_lines(path) == []


def test_recent_log_lines_reads_last_ten(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(f"日志{i}" for i in range(15)), encoding="utf-8")
    assert recent_log_lines(str(path)) == [f"日志{i}" for i in range(5, 15)]


def test_recent_log_lines_ignores_blank_lines(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("第一条\n\n   \n第二条\n\n第三条\n", encoding="utf-8")
    assert recent_log_lines(path) == ["第一条", "第二条", "第三条"]


def test_recent_log_lines_limit_two_and_nonpositive_limit(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("第一条\n第二条\n第三条\n", encoding="utf-8")
    assert recent_log_lines(path, limit=2) == ["第二条", "第三条"]
    assert recent_log_lines(path, limit=0) == []
    assert recent_log_lines(path, limit=-1) == []


def test_all_app_ui_runtime_imports_exist():
    app_path = Path(__file__).parents[1] / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "utils.ui_runtime"
        for alias in node.names
    }
    runtime = importlib.import_module("utils.ui_runtime")
    assert imported_names
    assert not [name for name in imported_names if not hasattr(runtime, name)]


def test_recent_tasks_show_completion_time_keywords_and_counts(tmp_path):
    run_dir = tmp_path / "2026-07-13_19-10_交易系统运维+合规风控"
    run_dir.mkdir()
    write_run_config(run_dir / "run_config.json", {
        "search_keywords": ["交易系统运维", "合规风控"],
        "completed_at": "2026-07-13T19:10:59+08:00",
        "status": "completed",
    })
    (run_dir / "jobs.jsonl").write_text(
        json.dumps({"search_keyword": "交易系统运维"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "invalid_records.jsonl").write_text(
        json.dumps({"search_keyword": "合规风控"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    recent = read_recent_run_results(tmp_path)
    assert recent[0]["task_name"] == "2026-07-13 19:10 · 交易系统运维、合规风控"
    assert recent[0]["valid_count"] == 1
    assert recent[0]["invalid_count"] == 1


def test_recent_tasks_read_flat_layout_internal_files(tmp_path):
    run_dir = tmp_path / "2026-07-14_22-10_交易系统运维"
    internal = run_dir / "internal"
    internal.mkdir(parents=True)
    (run_dir / "screenshots").mkdir()
    write_run_config(internal / "run_config.json", {
        "search_keywords": ["交易系统运维"],
        "completed_at": "2026-07-14T22:10:59+08:00", "status": "completed",
    })
    (internal / "jobs.jsonl").write_text(
        json.dumps({"search_keyword": "交易系统运维"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (internal / "invalid_records.jsonl").write_text("", encoding="utf-8")
    (internal / "app.log").write_text("完成\n", encoding="utf-8")
    recent = read_recent_run_results(tmp_path)
    assert recent[0]["valid_count"] == 1
    assert recent[0]["log_path"] == internal / "app.log"
    assert recent[0]["screenshots_dir"] == run_dir / "screenshots"


def test_controller_reads_stable_run_id_from_running_directory_log(tmp_path):
    controller = ScanProcessController(tmp_path)
    controller.add_log_lines([
        "2026-07-13 19:10:00 | INFO | 本次任务目录：/tmp/结果/.running_20260713_191000"
    ])
    assert controller.active_run_id() == "20260713_191000"


def test_new_controller_active_run_id_is_none(tmp_path):
    assert ScanProcessController(tmp_path).active_run_id() is None


def test_active_run_id_survives_completion_and_stop(tmp_path, monkeypatch):
    project = tmp_path
    (project / "data").mkdir()
    config_path = project / "data" / "ui_run_config.json"
    write_ui_config(config_path, build_ui_config("交易系统运维", 1, "上海", 0, 0))
    processes: list[FakeProcess] = []

    def factory(command, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    controller = ScanProcessController(project, popen_factory=factory)
    monkeypatch.setattr(controller, "_read_output", lambda process: None)
    controller.start(config_path)
    controller.add_log_lines([
        "2026-07-13 19:10:00 | INFO | 本次任务目录：/tmp/结果/.running_20260713_191000"
    ])
    assert controller.active_run_id() == "20260713_191000"

    processes[0].returncode = 0
    assert controller.active_run_id() == "20260713_191000"

    second = ScanProcessController(project, pid_file=project / "data" / "second.pid",
                                   popen_factory=factory)
    monkeypatch.setattr(second, "_read_output", lambda process: None)
    second.start(config_path)
    second.add_log_lines([
        "2026-07-13 19:11:00 | INFO | 本次任务目录：/tmp/结果/.running_20260713_191100"
    ])
    assert second.stop() is True
    assert second.active_run_id() == "20260713_191100"


def test_app_controller_public_calls_exist():
    app_path = Path(__file__).parents[1] / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    controller_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "controller"
    }
    controller_calls.add("start")  # app通过get_controller().start调用。
    assert controller_calls == {
        "active_run_id", "continue_after_manual_action", "is_running", "logs", "start", "stop"
    }
    assert not [name for name in controller_calls if not hasattr(ScanProcessController, name)]
