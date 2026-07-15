from pathlib import Path

import pytest

from scrapers.boss import BossScraper, StopScanError
from utils.storage import JsonlStore


class _Logger:
    def info(self, *args): pass
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass
    def exception(self, *args): pass


class _SearchPage:
    url = "https://www.zhipin.com/shanghai/"
    def is_closed(self): return False
    def bring_to_front(self): pass


def test_two_keywords_two_jobs_loop(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path),
        {"keyword_wait_seconds_min": 0, "keyword_wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), _Logger(), debug=False,
    )
    searched = []
    captured = []
    search_page = _SearchPage()
    scraper.context = type("Context", (), {"pages": [search_page]})()
    monkeypatch.setattr(scraper, "_search_keyword", lambda page, keyword: searched.append(keyword))
    monkeypatch.setattr(scraper, "_apply_configured_filters", lambda page: None)
    urls = ["https://www.zhipin.com/job_detail/a.html", "https://www.zhipin.com/job_detail/b.html"]
    detail_page = object()
    monkeypatch.setattr("scrapers.boss.is_valid_search_results_page", lambda page, keyword: (True, ""))
    monkeypatch.setattr(scraper, "_collect_job_urls", lambda page, limit, keyword: urls[:limit])
    monkeypatch.setattr(scraper, "_ensure_detail_page", lambda page: detail_page)

    def fake_capture(page, url, rank, keyword, run_id):
        captured.append((page, url, rank, keyword, run_id))
        scraper.captured_count += 1
        return "captured"

    monkeypatch.setattr(scraper, "_capture_detail", fake_capture)
    result = scraper._run_keywords(search_page, ["关键词一", "关键词二"], 2)
    assert searched == ["关键词一", "关键词二"]
    assert all(x[0] is detail_page and x[1] in urls for x in captured)
    assert [(x[2], x[3]) for x in captured] == [(1, "关键词一"), (2, "关键词一"),
                                                  (1, "关键词二"), (2, "关键词二")]
    assert result["captured"] == 4
    assert result["status"] == "completed"
    assert [row["status"] for row in scraper.keyword_summaries] == ["completed", "completed"]
    assert [x["captured_count"] for x in scraper.keyword_summaries] == [2, 2]
    assert [x["city"] for x in scraper.keyword_summaries] == ["", ""]
    assert (tmp_path / "data" / "screenshots" / "关键词一").is_dir()
    assert (tmp_path / "data" / "screenshots" / "关键词二").is_dir()
    assert (tmp_path / "data" / "html" / "关键词一").is_dir()
    assert (tmp_path / "data" / "html" / "关键词二").is_dir()


def test_zero_results_does_not_create_detail_page(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {}, JsonlStore(tmp_path / "jobs.jsonl"), _Logger(), debug=True,
    )
    monkeypatch.setattr(scraper, "_search_keyword", lambda page, keyword: None)
    monkeypatch.setattr(scraper, "_apply_configured_filters", lambda page: None)
    monkeypatch.setattr("scrapers.boss.is_valid_search_results_page", lambda page, keyword: (True, ""))
    monkeypatch.setattr(scraper, "_collect_job_urls", lambda page, limit, keyword: [])
    monkeypatch.setattr(scraper, "_ensure_detail_page",
                        lambda page: pytest.fail("零URL时不得创建detail_page"))
    monkeypatch.setattr("scrapers.boss.inspect_results_dom", lambda page, output_dir: {})
    page = type("Page", (), {"bring_to_front": lambda self: None, "is_closed": lambda self: False,
                              "url": "https://www.zhipin.com/shanghai/"})()
    scraper.context = type("Context", (), {"pages": [page]})()
    result = scraper._run_keywords(page, ["测试关键词"], 1)
    assert result["captured"] == 0
    assert result["status"] == "completed"
    assert scraper.keyword_summaries[0]["status"] == "no_new_jobs"
    assert scraper.keyword_summaries[0]["failed_count"] == 0
    assert scraper.detail_page_created is False


def test_new_only_all_historical_is_not_keyword_failure(tmp_path, monkeypatch):
    url = "https://www.zhipin.com/job_detail/history123.html"
    scraper = BossScraper(
        Path(tmp_path),
        {"save_mode": "new_only", "keyword_wait_seconds_min": 0,
         "keyword_wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), _Logger(), historical_urls={url},
    )
    page = _SearchPage()
    scraper.context = type("Context", (), {"pages": [page]})()
    monkeypatch.setattr(scraper, "_search_keyword", lambda page, keyword: page)
    monkeypatch.setattr(scraper, "_apply_configured_filters", lambda page: None)
    monkeypatch.setattr("scrapers.boss.is_valid_search_results_page", lambda page, keyword: (True, ""))
    monkeypatch.setattr(scraper, "_collect_job_urls", lambda page, limit, keyword: [url])
    monkeypatch.setattr(
        scraper, "_ensure_detail_page",
        lambda page: pytest.fail("全部历史岗位被跳过时不得创建详情页"),
    )

    result = scraper._run_keywords(page, ["交易系统运维"], 1)

    summary = scraper.keyword_summaries[0]
    assert summary["status"] == "historical_skipped"
    assert summary["historical_skipped_count"] == 1
    assert summary["failed_count"] == 0
    assert result["status"] == "completed"


def test_snapshot_mode_recaptures_historical_job(tmp_path, monkeypatch):
    url = "https://www.zhipin.com/job_detail/history123.html"
    scraper = BossScraper(
        Path(tmp_path), {"save_mode": "snapshot"},
        JsonlStore(tmp_path / "jobs.jsonl"), _Logger(), historical_urls={url},
    )
    page = _SearchPage()
    scraper.context = type("Context", (), {"pages": [page]})()
    monkeypatch.setattr(scraper, "_search_keyword", lambda page, keyword: page)
    monkeypatch.setattr(scraper, "_apply_configured_filters", lambda page: None)
    monkeypatch.setattr("scrapers.boss.is_valid_search_results_page", lambda page, keyword: (True, ""))
    monkeypatch.setattr(scraper, "_collect_job_urls", lambda page, limit, keyword: [url])
    monkeypatch.setattr(scraper, "_ensure_detail_page", lambda page: object())
    captured = []

    def capture(page, job_url, rank, keyword, run_id):
        captured.append(job_url)
        scraper.captured_count += 1
        return "captured"

    monkeypatch.setattr(scraper, "_capture_detail", capture)
    result = scraper._run_keywords(page, ["交易系统运维"], 1)

    assert captured == [url]
    assert scraper.keyword_summaries[0]["status"] == "completed"
    assert result["status"] == "completed"


def test_same_task_duplicate_job_id_is_no_new_not_failed(tmp_path, monkeypatch):
    store = JsonlStore(tmp_path / "jobs.jsonl")
    store.append({
        "job_id": "same123", "url": "https://www.zhipin.com/job_detail/same123.html",
        "search_keyword": "关键词一", "matched_keywords": ["关键词一"],
        "title": "交易系统工程师", "salary": "20-30K", "jd_text": "岗位描述" * 30,
    })
    scraper = BossScraper(Path(tmp_path), {"save_mode": "snapshot"}, store, _Logger())
    page = _SearchPage()
    scraper.context = type("Context", (), {"pages": [page]})()
    monkeypatch.setattr(scraper, "_search_keyword", lambda page, keyword: page)
    monkeypatch.setattr(scraper, "_apply_configured_filters", lambda page: None)
    monkeypatch.setattr("scrapers.boss.is_valid_search_results_page", lambda page, keyword: (True, ""))
    monkeypatch.setattr(
        scraper, "_collect_job_urls",
        lambda page, limit, keyword: ["https://www.zhipin.com/job_detail/same123.html?ka=x"],
    )
    monkeypatch.setattr(
        scraper, "_ensure_detail_page",
        lambda page: pytest.fail("同任务重复job_id不得再次访问详情页"),
    )

    result = scraper._run_keywords(page, ["关键词二"], 1)

    assert scraper.keyword_summaries[0]["status"] == "no_new_jobs"
    assert scraper.keyword_summaries[0]["duplicate_count"] == 1
    assert scraper.keyword_summaries[0]["failed_count"] == 0
    assert result["status"] == "completed"
    assert store.read_all()[0]["matched_keywords"] == ["关键词一", "关键词二"]


def test_historical_skip_plus_completed_keyword_is_fully_completed(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {"keyword_wait_seconds_min": 0, "keyword_wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), _Logger(),
    )
    page = _SearchPage()

    def execute(search_page, keyword, keyword_index, keyword_total, limit, summary, run_id):
        if keyword == "交易系统运维":
            summary.update({"status": "historical_skipped", "historical_skipped_count": 5})
        else:
            summary.update({
                "status": "collecting_details", "processed_count": 5,
                "captured_count": 5, "valid_count": 5,
            })
            scraper.captured_count += 5
        return search_page

    monkeypatch.setattr(scraper, "_execute_keyword", execute)
    result = scraper._run_keywords(page, ["交易系统运维", "合规审核"], 5)

    assert [row["status"] for row in scraper.keyword_summaries] == [
        "historical_skipped", "completed",
    ]
    assert result["status"] == "completed"
    assert result["failed"] == 0


def test_search_failure_keeps_valid_bound_page(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {}, JsonlStore(tmp_path / "jobs.jsonl"), _Logger(), debug=True,
    )

    class FailingLocator:
        @property
        def first(self): return self
        def wait_for(self, **kwargs): raise RuntimeError("not found")

    class SearchPage:
        url = "https://www.zhipin.com/shanghai/?seoRefer=index"
        def is_closed(self): return False
        def bring_to_front(self): return None
        def locator(self, selector): return FailingLocator()

    search_page = SearchPage()
    scraper.context = type("Context", (), {"pages": [search_page]})()
    monkeypatch.setattr(scraper, "_pause_if_abnormal", lambda page: None)
    monkeypatch.setattr(scraper, "_save_search_input_debug", lambda page, keyword: None)
    monkeypatch.setattr("scrapers.boss.dismiss_overlays", lambda page: {
        "detected": False, "dismissed": False, "action": "none"
    })
    with pytest.raises(StopScanError, match="已保存debug现场并停止"):
        scraper._search_keyword(search_page, "测试")
