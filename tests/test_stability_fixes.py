import logging

from scrapers.boss import BossScraper
from utils.desktop_service import browser_status, is_boss_url
from utils.storage import JsonlStore


def valid_record(url="https://www.zhipin.com/job_detail/abc.html"):
    return {
        "title": "交易系统运维工程师", "company": "测试公司", "salary": "20-30K",
        "city": "上海", "district": "浦东新区", "experience": "3-5年", "education": "本科",
        "jd_text": "负责交易系统稳定运行、故障处置与日常巡检。" * 10,
        "url": url, "screenshot_path": "", "html_path": "", "search_keyword": "交易系统运维",
        "search_rank": 1,
    }


class ScreenshotPage:
    url = "https://www.zhipin.com/job_detail/abc.html"

    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []

    def evaluate(self, script):
        return None

    def wait_for_timeout(self, timeout):
        return None

    def screenshot(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise TimeoutError(f"截图超时{len(self.calls)}")
        from pathlib import Path
        Path(kwargs["path"]).write_bytes(b"png")

    def content(self):
        return "<html>detail</html>"


def make_scraper(tmp_path):
    store = JsonlStore(tmp_path / "jobs.jsonl")
    scraper = BossScraper(
        tmp_path, {"wait_seconds_min": 0, "wait_seconds_max": 0}, store,
        logging.getLogger(f"test-{tmp_path.name}"), data_dir=tmp_path,
    )
    return scraper, store


def test_full_page_screenshot_falls_back_to_viewport(tmp_path):
    scraper, _ = make_scraper(tmp_path)
    page = ScreenshotPage(failures=1)
    record = valid_record()
    scraper._save_valid_detail(page, record, 1, "交易系统运维")
    assert [call["full_page"] for call in page.calls] == [True, False]
    assert page.calls[0]["timeout"] == 20000
    assert page.calls[1]["timeout"] == 10000
    assert all(call["animations"] == "disabled" for call in page.calls)
    assert record["screenshot_status"] == "success"
    assert record["screenshot_error"] == ""


def test_two_screenshot_failures_keep_valid_job_in_jsonl(tmp_path, monkeypatch):
    scraper, store = make_scraper(tmp_path)
    page = ScreenshotPage(failures=2)
    page.goto = lambda *args, **kwargs: None
    monkeypatch.setattr(scraper, "_pause_if_abnormal", lambda value: None)
    monkeypatch.setattr(scraper, "_wait_for_detail_stable", lambda value: True)
    monkeypatch.setattr(scraper, "_extract_detail_record", lambda *args: (valid_record(), {}))
    monkeypatch.setattr("scrapers.boss.time.sleep", lambda seconds: None)

    assert scraper._capture_detail(page, valid_record()["url"], 1, "交易系统运维", "run-1") == "captured"
    rows = store.read_all()
    assert len(rows) == 1
    assert rows[0]["screenshot_status"] == "failed"
    assert rows[0]["screenshot_path"] == ""
    assert "视口截图失败" in rows[0]["screenshot_error"]
    assert scraper.failed_count == 0
    assert scraper.invalid_records == []


def test_first_blank_detail_then_second_success_only_counts_success(tmp_path, monkeypatch):
    scraper, store = make_scraper(tmp_path)
    page = ScreenshotPage()
    page.goto = lambda *args, **kwargs: None
    blank = {**valid_record(), "title": "", "salary": "", "jd_text": ""}
    records = iter([(blank, {}), (valid_record(), {})])
    monkeypatch.setattr(scraper, "_pause_if_abnormal", lambda value: None)
    monkeypatch.setattr(scraper, "_wait_for_detail_stable", lambda value: True)
    monkeypatch.setattr(scraper, "_extract_detail_record", lambda *args: next(records))
    monkeypatch.setattr(scraper, "_save_invalid_detail", lambda *args: None)
    monkeypatch.setattr("scrapers.boss.time.sleep", lambda seconds: None)

    assert scraper._capture_detail(page, valid_record()["url"], 1, "交易系统运维", "run-1") == "captured"
    assert len(store.read_all()) == 1
    assert scraper.captured_count == 1
    assert scraper.failed_count == 0
    assert scraper.invalid_records == []


def test_detail_readiness_wait_uses_500ms_polling_and_ten_second_cap(tmp_path):
    scraper, _ = make_scraper(tmp_path)

    class Page:
        url = "https://www.zhipin.com/job_detail/abc.html"
        def wait_for_function(self, expression, **kwargs):
            self.expression = expression
            self.kwargs = kwargs

    page = Page()
    assert scraper._wait_for_detail_stable(page)
    assert page.kwargs == {"timeout": 10000, "polling": 500}
    assert ".job-banner .name" in page.expression
    assert ".job-banner .salary" in page.expression
    assert "职位描述" in page.expression


def test_browser_status_excludes_socket_worker_and_prefers_search(monkeypatch):
    pages = [
        {"type": "shared_worker", "url": "https://www.zhipin.com/web/socket-worker/assets/a.js"},
        {"type": "page", "url": "https://www.zhipin.com/job_detail/a.html"},
        {"type": "page", "url": "https://www.zhipin.com/shanghai/"},
        {"type": "page", "url": "https://www.zhipin.com/web/geek/jobs?query=python"},
    ]
    monkeypatch.setattr("utils.desktop_service.get_cdp_pages", lambda: pages)
    status = browser_status()
    assert status["boss_url"] == "https://www.zhipin.com/web/geek/jobs?query=python"
    assert not is_boss_url("https://www.zhipin.com/web/socket-worker/assets/a.js")
