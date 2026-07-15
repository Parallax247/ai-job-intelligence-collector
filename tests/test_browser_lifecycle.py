from __future__ import annotations

import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest

import app
from scrapers.boss import BossScraper, RuntimeInfrastructureError
from utils.browser_manager import ensure_dedicated_chrome_running
from utils.run_paths import build_final_run_name
from utils.storage import JsonlStore


class Logger:
    def info(self, *args): pass
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass
    def exception(self, *args): pass


class Page:
    def __init__(self, url="https://www.zhipin.com/web/geek/jobs?query=测试", closed=False):
        self.url = url
        self.closed = closed
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    def close(self):
        self.close_calls += 1
        self.closed = True

    def bring_to_front(self):
        return None

    def set_default_timeout(self, _timeout):
        return None


class Context:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.created = 0
        self.close_calls = 0

    def new_page(self):
        self.created += 1
        page = Page("about:blank")
        self.pages.append(page)
        return page

    def close(self):
        self.close_calls += 1


def make_scraper(tmp_path: Path) -> BossScraper:
    return BossScraper(
        tmp_path, {"keyword_wait_seconds_min": 0, "keyword_wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), Logger(), data_dir=tmp_path,
    )


def summary():
    return {
        "status": "pending", "duplicate_count": 0, "historical_skipped_count": 0,
        "processed_count": 0, "captured_count": 0, "valid_count": 0,
        "failed_count": 0, "invalid_count": 0,
    }


def prepare_keyword(monkeypatch, scraper, search_page, urls):
    monkeypatch.setattr(scraper, "_recover_boss_search_page", lambda page, step: search_page)
    monkeypatch.setattr(scraper, "_search_keyword", lambda page, keyword: page)
    monkeypatch.setattr(scraper, "_apply_configured_filters", lambda page: None)
    monkeypatch.setattr("scrapers.boss.is_valid_search_results_page", lambda page, keyword: (True, ""))
    monkeypatch.setattr(scraper, "_collect_job_urls", lambda page, limit, keyword: list(urls))


def test_closed_detail_page_is_recreated_without_closing_search_page(tmp_path):
    scraper = make_scraper(tmp_path)
    search = Page()
    old_detail = Page("https://www.zhipin.com/job_detail/old.html", closed=True)
    context = Context([search, old_detail])
    scraper.context = context
    scraper.detail_page = old_detail
    scraper.detail_page_owned = True
    scraper.detail_page_created = True

    replacement = scraper._ensure_detail_page(search)

    assert replacement is not old_detail
    assert context.created == 1
    assert search.close_calls == 0


def test_target_closed_reconnects_and_retries_current_job(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    search, detail, replacement = Page(), Page("about:blank"), Page("about:blank")
    scraper.browser = object()
    scraper.playwright = object()
    prepare_keyword(monkeypatch, scraper, search, ["https://www.zhipin.com/job_detail/a.html"])
    monkeypatch.setattr(scraper, "_ensure_detail_page", lambda page: detail)
    monkeypatch.setattr(scraper, "ensure_runtime_pages", lambda page: (page, detail))
    reconnects = []
    monkeypatch.setattr(
        scraper, "_reconnect_runtime",
        lambda: (reconnects.append(True) or search, replacement, False),
    )
    calls = 0

    def capture(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeInfrastructureError("TargetClosedError")
        scraper.captured_count += 1
        return "captured"

    monkeypatch.setattr(scraper, "_capture_detail", capture)
    result = summary()
    scraper._execute_keyword(search, "测试", 1, 1, 1, result, "run")

    assert calls == 2
    assert reconnects == [True]
    assert result["valid_count"] == 1
    assert result["invalid_count"] == 0
    assert scraper.processed_job_count == 1


def test_target_closed_is_not_written_to_invalid_records(tmp_path):
    scraper = make_scraper(tmp_path)

    class ClosedPage(Page):
        def goto(self, *args, **kwargs):
            raise RuntimeError("TargetClosedError: Target page, context or browser has been closed")

    with pytest.raises(RuntimeInfrastructureError):
        scraper._capture_detail(
            ClosedPage("about:blank"),
            "https://www.zhipin.com/job_detail/a.html", 1, "测试", "run",
        )

    assert scraper.invalid_records == []
    assert scraper.failed_count == 0
    assert scraper.processed_job_count == 0


def test_closed_manual_input_channel_is_not_an_invalid_job(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    scraper.config["abnormal_markers"] = ["验证码"]

    class Locator:
        def inner_text(self, timeout): return "请完成验证码"

    page = Page("https://www.zhipin.com/job_detail/a.html")
    page.locator = lambda _selector: Locator()
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(EOFError()))

    with pytest.raises(RuntimeInfrastructureError):
        scraper._pause_if_abnormal(page)
    assert scraper.invalid_records == []


def test_two_consecutive_target_closed_pauses_then_resumes_same_url(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    search, detail = Page(), Page("about:blank")
    scraper.browser = object()
    scraper.playwright = object()
    prepare_keyword(monkeypatch, scraper, search, ["https://www.zhipin.com/job_detail/a.html"])
    monkeypatch.setattr(scraper, "_ensure_detail_page", lambda page: detail)
    monkeypatch.setattr(scraper, "ensure_runtime_pages", lambda page: (page, detail))
    monkeypatch.setattr(scraper, "_reconnect_runtime", lambda: (search, detail, False))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    calls = 0

    def capture(*args):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeInfrastructureError("TargetClosedError")
        scraper.captured_count += 1
        return "captured"

    monkeypatch.setattr(scraper, "_capture_detail", capture)
    result = summary()
    scraper._execute_keyword(search, "测试", 1, 1, 1, result, "run")

    assert calls == 3
    assert scraper.infrastructure_failed_count == 1
    assert scraper.browser_disconnect_count == 2
    assert result["invalid_count"] == 0
    assert scraper.pending_urls == []
    assert json.loads(scraper.task_state_path.read_text(encoding="utf-8"))["task_status"] == "running"


def test_16_of_30_disconnect_preserves_14_pending_and_no_invalid_growth(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    scraper.captured_count = 16
    scraper.processed_job_count = 16
    scraper.failed_count = 0
    scraper.pending_urls = [f"url-{index}" for index in range(17, 31)]
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        scraper._pause_and_reconnect("browser disconnected", keyword="测试", rank=17)

    state = json.loads(scraper.task_state_path.read_text(encoding="utf-8"))
    assert state["valid_count"] == 16
    assert state["invalid_count"] == 0
    assert state["pending_count"] == 14
    assert state["task_status"] == "paused_browser_lost"


def test_worker_close_only_owned_detail_and_disconnects_playwright(tmp_path):
    scraper = make_scraper(tmp_path)
    search, detail = Page(), Page("about:blank")
    context = Context([search, detail])

    class Browser:
        close_calls = 0
        def close(self): self.close_calls += 1

    class Playwright:
        stop_calls = 0
        def stop(self): self.stop_calls += 1

    browser, playwright = Browser(), Playwright()
    scraper.context, scraper.browser, scraper.playwright = context, browser, playwright
    scraper.detail_page, scraper.detail_page_owned, scraper.detail_page_created = detail, True, True
    scraper.close()

    assert detail.close_calls == 1
    assert search.close_calls == 0
    assert context.close_calls == 0
    assert browser.close_calls == 0
    assert playwright.stop_calls == 1


def test_browser_manager_does_not_restart_when_cdp_is_alive(monkeypatch):
    monkeypatch.setattr("utils.browser_manager.get_cdp_pages", lambda timeout=1: [{"type": "page"}])
    monkeypatch.setattr(
        "utils.browser_manager.restart_dedicated_chrome",
        lambda *args: pytest.fail("不得重复启动Chrome"),
    )
    assert ensure_dedicated_chrome_running() is False


def test_verify_page_is_waiting_for_login():
    verify = Page("https://www.zhipin.com/web/passport/zp/verify.html")
    verify.locator = lambda _selector: type("Locator", (), {
        "first": property(lambda self: self),
        "count": lambda self: 0,
        "is_visible": lambda self: False,
    })()
    assert BossScraper._boss_page_needs_login(verify) is True


def test_paused_directory_name_contains_progress():
    name = build_final_run_name(
        datetime(2026, 7, 14, 10, 20), ["交易系统运维"], completed=False,
        status="paused_browser_lost", processed_count=16,
    )
    assert name == "2026-07-14_10-20_paused_交易系统运维_16-items"


def test_completed_status_requires_empty_pending_list(tmp_path):
    scraper = make_scraper(tmp_path)
    scraper.task_status = "running"
    scraper.pending_urls = ["url-17"]
    scraper._write_task_state()
    state = json.loads(scraper.task_state_path.read_text(encoding="utf-8"))
    assert state["task_status"] != "completed"
    assert state["pending_count"] == 1


def test_streamlit_does_not_cache_playwright_objects_and_disables_destructive_actions():
    source = inspect.getsource(app)
    assert "st.session_state[\"search_page\"]" not in source
    assert "st.session_state[\"detail_page\"]" not in source
    assert 'key="restart_chrome",\n            disabled=controller.is_running()' in source
    assert 'key="stop_service",\n                disabled=controller.is_running()' in source
