import json
from pathlib import Path

import pytest

from main import parse_args
from utils.runtime_options import apply_runtime_overrides, normalize_keywords, save_last_run_config


def test_keywords_support_all_separators_trim_and_deduplicate():
    raw = " 金融软件测试工程师，交易系统技术支持;证券系统运维\n交易系统技术支持；AI交易系统工程师 "
    assert normalize_keywords(raw) == [
        "金融软件测试工程师", "交易系统技术支持", "证券系统运维", "AI交易系统工程师"
    ]


@pytest.mark.parametrize("raw", ["", " ,，；\n ", "A", "测" * 51])
def test_invalid_keywords_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_keywords(raw)


def test_cli_overrides_config_and_allows_empty_city():
    config = {
        "search_keywords": ["旧关键词"], "jobs_per_keyword": 30, "city": "上海",
        "wait_seconds_min": 5, "wait_seconds_max": 10,
    }
    resolved = apply_runtime_overrides(
        config, keywords="新关键词,第二关键词", limit=7, city="", wait_min=6, wait_max=9,
    )
    assert resolved["search_keywords"] == ["新关键词", "第二关键词"]
    assert resolved["jobs_per_keyword"] == 7
    assert resolved["city"] == ""
    assert resolved["wait_seconds_min"] == 6
    assert resolved["wait_seconds_max"] == 9


def test_runtime_limits_and_wait_range_are_validated():
    base = {
        "search_keywords": ["测试关键词"], "jobs_per_keyword": 10, "city": "上海",
        "wait_seconds_min": 6, "wait_seconds_max": 10,
    }
    with pytest.raises(ValueError, match="between 1 and 50"):
        apply_runtime_overrides(base, limit=51)
    with pytest.raises(ValueError, match="cannot exceed"):
        apply_runtime_overrides(base, wait_min=11, wait_max=10)


def test_last_run_config_has_exact_runtime_fields(tmp_path):
    path = tmp_path / "last_run_config.json"
    payload = save_last_run_config(path, {
        "search_keywords": ["关键词一", "关键词二"], "jobs_per_keyword": 10,
        "city": "上海", "wait_seconds_min": 6.0, "wait_seconds_max": 10.0,
    })
    assert set(payload) == {
        "search_keywords", "jobs_per_keyword", "city", "wait_seconds_min",
        "wait_seconds_max", "save_mode", "platform", "created_at",
    }
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert payload["wait_seconds_min"] == 6
    assert payload["save_mode"] == "snapshot"


def test_parse_args_supports_plural_keywords_and_runtime_options():
    args = parse_args([
        "--keywords", "关键词一,关键词二", "--limit", "10", "--city", "上海",
        "--wait-min", "6", "--wait-max", "10", "--debug",
    ])
    assert args.keywords == "关键词一,关键词二"
    assert args.limit == 10 and args.city == "上海"
    assert args.wait_min == 6 and args.wait_max == 10 and args.debug


def test_desktop_launcher_uses_safe_array_and_no_eval():
    source = (Path(__file__).parents[1] / "scripts" / "start_job_collector.sh").read_text(encoding="utf-8")
    assert "eval " not in source
    assert "python -m streamlit run app.py" in source
    assert "python main.py" not in source
    assert "prompt_keywords" not in source
