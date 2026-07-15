from __future__ import annotations

from pathlib import Path

from main import should_discard_empty_page_failure
from scrapers.boss import BossScraper
from utils.desktop_service import BOSS_URL, ensure_boss_page_health
from utils.run_paths import create_run_directory, discard_running_directory
from utils.storage import JsonlStore


class FakeLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self):
        return False


class FakePage:
    def __init__(self, url: str, *, closed: bool = False, bring_error: Exception | None = None):
        self.url = url
        self.closed = closed
        self.bring_error = bring_error
        self.front_calls = 0
        self.timeout = None

    def is_closed(self):
        return self.closed

    def bring_to_front(self):
        self.front_calls += 1
        if self.bring_error:
            raise self.bring_error

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    def locator(self, selector):
        return FakeLocator()


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)


class FakePlaywright:
    def __init__(self, context):
        self.context = context
        self.stopped = False
        self.chromium = self

    def connect_over_cdp(self, endpoint):
        return type("Browser", (), {"contexts": [self.context]})()

    def stop(self):
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


class Logger:
    def info(self, *args): pass
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass
    def exception(self, *args): pass


def test_closed_boss_tab_is_created_and_brought_to_front(monkeypatch):
    old = FakePage("https://www.zhipin.com/shanghai/", closed=True)
    context = FakeContext([old])
    playwright = FakePlaywright(context)

    def open_tab(url):
        assert url == BOSS_URL
        context.pages.append(FakePage(url))

    monkeypatch.setattr("utils.desktop_service.get_cdp_pages", lambda timeout=1: [
        {"type": "page", "url": "chrome://new-tab-page/"}
    ])
    health = ensure_boss_page_health(
        playwright_factory=lambda: FakeStarter(playwright), open_tab_fn=open_tab,
        sleep_fn=lambda _seconds: None,
    )
    assert health["ok"] is True
    assert health["created"] is True
    assert health["boss_url"] == BOSS_URL
    assert context.pages[-1].front_calls == 1
    assert playwright.stopped is True


def test_stale_page_object_rebinds_to_new_page(monkeypatch):
    stale = FakePage(
        "https://www.zhipin.com/shanghai/",
        bring_error=RuntimeError("TargetClosedError: Target page has been closed"),
    )
    context = FakeContext([stale])
    playwright = FakePlaywright(context)

    def open_tab(_url):
        context.pages.append(FakePage(BOSS_URL))

    monkeypatch.setattr("utils.desktop_service.get_cdp_pages", lambda timeout=1: [
        {"type": "page", "url": stale.url}
    ])
    health = ensure_boss_page_health(
        playwright_factory=lambda: FakeStarter(playwright), open_tab_fn=open_tab,
        sleep_fn=lambda _seconds: None,
    )
    assert health["ok"] is True
    assert health["created"] is True
    assert context.pages[-1].front_calls == 1


def test_login_page_reports_waiting_for_user(monkeypatch):
    login = FakePage("https://www.zhipin.com/web/user/?ka=header-login")
    context = FakeContext([login])
    playwright = FakePlaywright(context)
    monkeypatch.setattr("utils.desktop_service.get_cdp_pages", lambda timeout=1: [
        {"type": "page", "url": login.url}
    ])
    monkeypatch.setattr("utils.desktop_service._boss_page_needs_login", lambda page: True)
    health = ensure_boss_page_health(
        playwright_factory=lambda: FakeStarter(playwright), open_tab_fn=lambda url: None,
    )
    assert health["ok"] is False
    assert health["login_required"] is True
    assert health["state"] == "Login required"
    assert health["message"] == "Sign in on the BOSS Zhipin page, then continue"


def test_scraper_target_closed_enters_recovery_and_retries_keyword(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {"keyword_wait_seconds_min": 0, "keyword_wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), Logger(),
    )
    old = FakePage("https://www.zhipin.com/shanghai/")
    replacement = FakePage("https://www.zhipin.com/shanghai/")
    calls = 0

    def execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("TargetClosedError: Page.bring_to_front: Target page has been closed")
        return replacement

    monkeypatch.setattr(scraper, "_execute_keyword", execute)
    monkeypatch.setattr(scraper, "_reconnect_search_runtime", lambda: replacement)
    result = scraper._run_keywords(old, ["交易系统运维"], 1)
    assert calls == 2
    assert result["failed"] == 0


def test_scraper_recovery_creates_boss_page_when_old_page_closed(tmp_path, monkeypatch):
    scraper = BossScraper(
        Path(tmp_path), {}, JsonlStore(tmp_path / "jobs.jsonl"), Logger(),
    )
    old = FakePage(BOSS_URL, closed=True)
    context = FakeContext([old])
    scraper.context = context

    def open_tab(url):
        context.pages.append(FakePage(url))

    monkeypatch.setattr("scrapers.boss.open_cdp_tab", open_tab)
    selected = scraper._recover_boss_search_page(old, "测试恢复")
    assert selected is context.pages[-1]
    assert selected.front_calls == 1


def test_empty_page_loss_running_directories_are_discarded_without_incomplete_names(tmp_path):
    first = create_run_directory(tmp_path, direct=True)
    second = create_run_directory(tmp_path, direct=True)
    discard_running_directory(first, tmp_path)
    discard_running_directory(second, tmp_path)
    assert not first.exists()
    assert not second.exists()
    assert not list(tmp_path.glob("*未完成*"))
    assert not list(tmp_path.glob(".running_*"))


def test_only_empty_page_loss_is_discarded():
    assert should_discard_empty_page_failure(0, True, False) is True
    assert should_discard_empty_page_failure(0, False, True) is True
    assert should_discard_empty_page_failure(1, True, True) is False
    assert should_discard_empty_page_failure(0, False, False) is False
