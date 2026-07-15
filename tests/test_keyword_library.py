from __future__ import annotations

from datetime import datetime, timezone

from utils.keyword_library import (
    KeywordLibrary,
    keywords_to_text,
    toggle_keyword_selection,
)
from utils.ui_runtime import build_ui_config


def fixed_now() -> datetime:
    return datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc)


def test_saved_keywords_survive_new_library_instance(tmp_path):
    path = tmp_path / "saved_keywords.json"
    KeywordLibrary(path, now=fixed_now).save_keywords(["交易系统运维", "合规风控"])
    assert [row["name"] for row in KeywordLibrary(path).load()] == [
        "交易系统运维", "合规风控",
    ]


def test_duplicate_keywords_are_saved_once_and_order_is_preserved(tmp_path):
    library = KeywordLibrary(tmp_path / "saved_keywords.json", now=fixed_now)
    rows = library.save_keywords("交易系统运维，合规风控;交易系统运维")
    assert [row["name"] for row in rows] == ["交易系统运维", "合规风控"]


def test_click_keyword_adds_to_current_selection():
    assert toggle_keyword_selection(["交易系统运维"], "合规风控") == [
        "交易系统运维", "合规风控",
    ]


def test_second_click_removes_selection_but_not_saved_keyword(tmp_path):
    library = KeywordLibrary(tmp_path / "saved_keywords.json", now=fixed_now)
    library.save_keywords(["交易系统运维"])
    assert toggle_keyword_selection(["交易系统运维"], "交易系统运维") == []
    assert [row["name"] for row in library.load()] == ["交易系统运维"]


def test_delete_saved_keyword_does_not_affect_others(tmp_path):
    library = KeywordLibrary(tmp_path / "saved_keywords.json", now=fixed_now)
    library.save_keywords(["交易系统运维", "合规风控", "证券系统测试"])
    rows = library.delete("合规风控")
    assert [row["name"] for row in rows] == ["交易系统运维", "证券系统测试"]


def test_scan_config_only_contains_left_selected_keywords(tmp_path):
    library = KeywordLibrary(tmp_path / "saved_keywords.json", now=fixed_now)
    library.save_keywords(["交易系统运维", "合规风控", "证券系统测试"])
    selected = toggle_keyword_selection([], "合规风控")
    config = build_ui_config(keywords_to_text(selected), 20, "上海", 6, 10)
    assert config["search_keywords"] == ["合规风控"]


def test_mark_used_updates_count_and_last_used_only_for_task_keywords(tmp_path):
    library = KeywordLibrary(tmp_path / "saved_keywords.json", now=fixed_now)
    library.save_keywords(["交易系统运维", "合规风控"])
    rows = library.mark_used(["交易系统运维"])
    by_name = {row["name"]: row for row in rows}
    assert by_name["交易系统运维"]["use_count"] == 1
    assert by_name["交易系统运维"]["last_used_at"]
    assert by_name["合规风控"]["use_count"] == 0
    assert by_name["合规风控"]["last_used_at"] == ""


def test_empty_library_is_created_and_loads_safely(tmp_path):
    path = tmp_path / "配置" / "saved_keywords.json"
    assert KeywordLibrary(path).load() == []
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip().startswith("{")


def test_corrupt_json_is_backed_up_before_empty_recovery(tmp_path):
    path = tmp_path / "saved_keywords.json"
    path.write_text("{broken", encoding="utf-8")
    library = KeywordLibrary(path, now=fixed_now)
    assert library.load() == []
    backups = list(tmp_path.glob("saved_keywords.corrupt_*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{broken"
    assert KeywordLibrary(path).load() == []


def test_sorting_prefers_pin_then_recent_usage_count_and_created_time(tmp_path):
    path = tmp_path / "saved_keywords.json"
    path.write_text(
        '''{
  "keywords": [
    {"name":"高频", "created_at":"2026-01-01T00:00:00", "last_used_at":"2026-07-14T08:00:00", "use_count":8},
    {"name":"最近", "created_at":"2026-01-02T00:00:00", "last_used_at":"2026-07-15T08:00:00", "use_count":1},
    {"name":"置顶", "created_at":"2025-01-01T00:00:00", "last_used_at":"", "use_count":0, "pinned":true},
    {"name":"新建", "created_at":"2026-07-15T09:00:00", "last_used_at":"", "use_count":0}
  ]
}\n''',
        encoding="utf-8",
    )
    rows = KeywordLibrary(path).sorted_keywords()
    assert [row["name"] for row in rows] == ["置顶", "最近", "高频", "新建"]


def test_library_keeps_at_most_one_hundred_keywords(tmp_path):
    library = KeywordLibrary(tmp_path / "saved_keywords.json")
    rows = library.save_keywords([f"关键词{index}" for index in range(120)])
    assert len(rows) == 100
    assert rows[0]["name"] == "关键词0"
    assert rows[-1]["name"] == "关键词99"


def test_pin_can_be_enabled_and_disabled(tmp_path):
    library = KeywordLibrary(tmp_path / "saved_keywords.json", now=fixed_now)
    library.save_keywords(["交易系统运维"])
    assert library.toggle_pin("交易系统运维")[0]["pinned"] is True
    assert library.toggle_pin("交易系统运维")[0]["pinned"] is False
