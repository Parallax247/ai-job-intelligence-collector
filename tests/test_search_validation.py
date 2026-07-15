from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from scrapers.boss import (
    BossScraper,
    SearchValidationError,
    is_valid_search_results_page,
)
from utils.run_paths import build_final_run_name
from utils.storage import JsonlStore


class Locator:
    def __init__(self, *, visible=False, value=""):
        self.visible = visible
        self.value = value

    @property
    def first(self): return self
    def count(self): return 1 if self.visible else 0
    def nth(self, index): return self
    def is_visible(self): return self.visible
    def input_value(self): return self.value


class SearchPage:
    def __init__(self, url, keyword="", *, results=False, featured=False):
        self.url = url
        self.keyword = keyword
        self.results = results
        self.featured = featured

    def is_closed(self): return False
    def bring_to_front(self): return None

    def locator(self, selector):
        if selector.startswith("input"):
            return Locator(visible=True, value=self.keyword)
        if selector in {
            ".job-list-box", ".job-list-container", ".search-job-result",
            'ul[class*="job-list"]', '[class*="search-job-result"]',
        }:
            return Locator(visible=self.results)
        if "精选职位" in selector or "推荐职位" in selector or "recommend-job" in selector:
            return Locator(visible=self.featured)
        return Locator()


class Logger:
    def info(self, *args): pass
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass
    def exception(self, *args): pass


def test_shanghai_homepage_is_never_a_search_results_page():
    page = SearchPage("https://www.zhipin.com/shanghai/", "交易系统运维", featured=True)
    valid, reason = is_valid_search_results_page(page, "交易系统运维")
    assert valid is False
    assert "首页推荐岗位" in reason


def test_featured_jobs_without_results_container_are_blocked():
    page = SearchPage(
        "https://www.zhipin.com/web/geek/jobs?query=交易系统运维",
        "交易系统运维", results=False, featured=True,
    )
    valid, reason = is_valid_search_results_page(page, "交易系统运维")
    assert valid is False
    assert "搜索结果列表" in reason or "首页" in reason


def test_jobs_url_with_matching_query_input_and_container_is_valid():
    page = SearchPage(
        "https://www.zhipin.com/web/geek/jobs?query=交易系统运维",
        "交易系统运维", results=True,
    )
    assert is_valid_search_results_page(page, "交易系统运维") == (True, "")


def test_matching_url_and_result_list_tolerate_transient_old_input_value():
    page = SearchPage(
        "https://www.zhipin.com/web/geek/jobs?query=交易系统运维",
        "物流运营", results=True,
    )
    valid, reason = is_valid_search_results_page(page, "交易系统运维")
    assert (valid, reason) == (True, "")


def test_transient_empty_input_then_current_keyword_does_not_fail(monkeypatch):
    states = [
        {"confirmed": False, "input_value": "", "confirmation_count": 1},
        {"confirmed": True, "input_value": "交易系统运维", "confirmation_count": 3},
    ]

    def inspect(page, keyword, previous_job_ids):
        return states.pop(0)

    monkeypatch.setattr("scrapers.boss.inspect_search_results_state", inspect)
    page = type("Page", (), {"wait_for_timeout": lambda self, value: None})()
    state = BossScraper._wait_for_keyword_confirmation(
        page, "交易系统运维", {"old-job"}, timeout_ms=15000
    )
    assert state["confirmed"] is True
    assert state["input_value"] == "交易系统运维"


def test_existing_shanghai_query_skips_broad_city_filter_click(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {"city": "上海"}, JsonlStore(tmp_path / "jobs.jsonl"), Logger()
    )
    page = SearchPage(
        "https://www.zhipin.com/web/geek/jobs?query=交易系统运维&city=101020100",
        "交易系统运维", results=True,
    )
    monkeypatch.setattr(
        scraper, "_first_visible",
        lambda *args, **kwargs: pytest.fail("结果URL已限定上海时不得重复点击宽泛城市选项"),
    )
    scraper._apply_configured_filters(page)


def test_search_failure_prevents_collecting_homepage_recommendations(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {}, JsonlStore(tmp_path / "jobs.jsonl"), Logger()
    )
    homepage = SearchPage(
        "https://www.zhipin.com/shanghai/?seoRefer=index",
        "交易系统运维", featured=True,
    )
    scraper.context = type("Context", (), {"pages": [homepage]})()
    monkeypatch.setattr(scraper, "_search_keyword", lambda page, keyword: homepage)
    monkeypatch.setattr(scraper, "_apply_configured_filters", lambda page: None)
    monkeypatch.setattr(
        scraper, "_collect_job_urls",
        lambda *args: pytest.fail("搜索失败时不得读取拼多多、特斯拉等首页推荐卡"),
    )
    summary = {"status": "searching"}
    with pytest.raises(SearchValidationError, match="首页推荐岗位"):
        scraper._execute_keyword(homepage, "交易系统运维", 1, 1, 3, summary, "run")


def test_first_keyword_search_failure_does_not_stop_second_keyword(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {"keyword_wait_seconds_min": 0, "keyword_wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), Logger(),
    )
    page = SearchPage(
        "https://www.zhipin.com/web/geek/jobs?query=合规风控", "合规风控", results=True
    )
    calls = []

    def execute(search_page, keyword, keyword_index, keyword_total, limit, summary, run_id):
        calls.append(keyword)
        if keyword == "交易系统运维":
            raise SearchValidationError(keyword, "未进入关键词搜索结果页，已阻止采集首页推荐岗位")
        summary.update({"processed_count": 1, "valid_count": 1, "captured_count": 1})
        scraper.captured_count += 1
        return search_page

    monkeypatch.setattr(scraper, "_execute_keyword", execute)
    result = scraper._run_keywords(page, ["交易系统运维", "合规风控"], 1)
    assert calls == ["交易系统运维", "合规风控"]
    assert [row["status"] for row in scraper.keyword_summaries] == ["failed", "completed"]
    assert result["status"] == "partial_failed"
    assert result["captured"] == 1


def test_first_keyword_success_second_search_failure_is_partial(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {"keyword_wait_seconds_min": 0, "keyword_wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), Logger(),
    )
    page = SearchPage(
        "https://www.zhipin.com/web/geek/jobs?query=交易系统运维",
        "交易系统运维", results=True,
    )

    def execute(search_page, keyword, keyword_index, keyword_total, limit, summary, run_id):
        if keyword == "合规风控":
            raise SearchValidationError(keyword, "未进入关键词搜索结果页，已阻止采集首页推荐岗位")
        summary.update({
            "processed_count": 3, "valid_count": 2, "invalid_count": 1,
            "captured_count": 2, "failed_count": 1,
        })
        scraper.captured_count += 2
        return search_page

    monkeypatch.setattr(scraper, "_execute_keyword", execute)
    result = scraper._run_keywords(page, ["交易系统运维", "合规风控"], 3)
    summaries = scraper.keyword_summaries
    assert sum(row["processed_count"] for row in summaries) == 3
    assert summaries[0]["status"] == "completed"
    assert summaries[1]["status"] == "failed"
    assert summaries[1]["processed_count"] == 0
    assert result["status"] == "partial_failed"
    assert result["captured"] == 2


def test_partial_failed_directory_name_contains_partial_completion():
    name = build_final_run_name(
        datetime(2026, 7, 13, 20, 49), ["交易系统运维", "合规风控"],
        completed=False, status="partial_failed",
    )
    assert name == "2026-07-13_20-49_partial_交易系统运维+合规风控"
