import pytest
from playwright.sync_api import Error as PlaywrightError
from pathlib import Path

from scrapers.boss import (
    BossScraper, absolute_job_url, connect_cdp, ensure_boss_search_page,
    ensure_separate_pages, get_boss_page,
)


class FakePage:
    def __init__(self, url, closed=False):
        self.url = url
        self.closed = closed
        self.close_calls = 0
        self.timeout = None
        self.front_calls = 0

    def is_closed(self):
        return self.closed

    def close(self):
        self.close_calls += 1
        self.closed = True

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    def bring_to_front(self):
        self.front_calls += 1


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


def test_get_boss_page_skips_about_blank():
    blank = FakePage("about:blank")
    boss = FakePage("https://www.zhipin.com/web/geek/jobs")
    assert get_boss_page(FakeContext([blank, boss])) is boss


def test_get_boss_page_accepts_shanghai_homepage():
    shanghai = FakePage("https://www.zhipin.com/shanghai/?seoRefer=index")
    assert get_boss_page(FakeContext([shanghai])) is shanghai


def test_get_boss_page_accepts_jobs_page():
    search = FakePage("https://www.zhipin.com/web/geek/jobs?query=python")
    assert get_boss_page(FakeContext([search])) is search


def test_socket_worker_asset_is_not_a_boss_page():
    worker = FakePage("https://www.zhipin.com/web/socket-worker/assets/index.js")
    shanghai = FakePage("https://www.zhipin.com/shanghai/")
    assert get_boss_page(FakeContext([shanghai, worker])) is shanghai
    assert ensure_boss_search_page(FakeContext([worker])) is None


def test_get_boss_page_accepts_subdomain_and_detail():
    detail = FakePage("https://m.zhipin.com/job_detail/abc.html")
    assert get_boss_page(FakeContext([detail])) is detail


def test_get_boss_page_rejects_blank_newtab_google_and_closed():
    pages = [
        FakePage("about:blank"), FakePage("chrome://newtab"),
        FakePage("https://www.google.com/_/chrome/newtab"),
        FakePage("https://www.zhipin.com/shanghai/", closed=True),
    ]
    assert get_boss_page(FakeContext(pages)) is None
    assert all(page.close_calls == 0 for page in pages)


def test_ensure_boss_page_ignores_streamlit_and_new_tab():
    boss = FakePage("https://www.zhipin.com/shanghai/")
    streamlit = FakePage("http://127.0.0.1:8501/")
    new_tab = FakePage("chrome://new-tab-page/")
    assert ensure_boss_search_page(FakeContext([boss, streamlit, new_tab])) is boss


def test_opening_streamlit_tab_does_not_replace_current_boss_page():
    boss = FakePage("https://www.zhipin.com/web/geek/jobs")
    context = FakeContext([boss])
    assert ensure_boss_search_page(context, boss) is boss
    context.pages.append(FakePage("http://localhost:8501/"))
    assert ensure_boss_search_page(context, boss) is boss


def test_closed_boss_page_can_rebind_to_new_boss_page():
    old = FakePage("https://www.zhipin.com/shanghai/", closed=True)
    replacement = FakePage("https://www.zhipin.com/web/geek/jobs")
    context = FakeContext([old, FakePage("chrome://new-tab-page/"), replacement])
    assert ensure_boss_search_page(context, old) is replacement


def test_ensure_boss_page_never_binds_chrome_new_tab():
    context = FakeContext([
        FakePage("chrome://new-tab-page/"), FakePage("chrome-untrusted://new-tab-page/"),
        FakePage("http://127.0.0.1:8501/"), FakePage("about:blank"),
    ])
    assert ensure_boss_search_page(context) is None


def test_search_and_detail_pages_must_be_separate():
    search = FakePage("https://www.zhipin.com/web/geek/jobs")
    detail = FakePage("about:blank")
    ensure_separate_pages(search, detail)
    with pytest.raises(RuntimeError, match="保护搜索页"):
        ensure_separate_pages(search, search)


def test_job_href_becomes_absolute_url():
    base = "https://www.zhipin.com/web/geek/jobs?query=python"
    assert absolute_job_url(base, "/job_detail/abc.html") == "https://www.zhipin.com/job_detail/abc.html"
    assert absolute_job_url(base, "https://www.zhipin.com/job_detail/xyz.html") == \
        "https://www.zhipin.com/job_detail/xyz.html"
    assert absolute_job_url(base, "/gongsi/123.html") == ""


def test_connect_cdp_is_mockable():
    context = FakeContext([])

    class Browser:
        contexts = [context]

    class Chromium:
        endpoint = ""

        def connect_over_cdp(self, endpoint):
            self.endpoint = endpoint
            return Browser()

    class Playwright:
        chromium = Chromium()

    browser, selected_context = connect_cdp(Playwright(), "http://127.0.0.1:9222")
    assert selected_context is context
    assert Playwright.chromium.endpoint == "http://127.0.0.1:9222"
    assert browser.contexts[0] is context


def test_connect_cdp_failure_has_clear_message():
    class Chromium:
        def connect_over_cdp(self, endpoint):
            raise PlaywrightError("connection refused")

    class Playwright:
        chromium = Chromium()

    with pytest.raises(RuntimeError, match="未发现9222端口Chrome"):
        connect_cdp(Playwright(), "http://127.0.0.1:9222")


def test_cdp_exit_closes_no_pages_or_browser():
    detail = FakePage("https://www.zhipin.com/job_detail/abc.html")
    search = FakePage("https://www.zhipin.com/web/geek/jobs")
    context = FakeContext([search, detail])

    class Browser:
        close_calls = 0
        def close(self): self.close_calls += 1

    class Playwright:
        stop_calls = 0
        def stop(self): self.stop_calls += 1

    browser = Browser()
    playwright = Playwright()
    scraper = BossScraper.__new__(BossScraper)
    scraper.detail_page = detail
    scraper.detail_page_owned = True
    scraper.context = context
    scraper.browser = browser
    scraper.playwright = playwright
    scraper.logger = type("Logger", (), {
        "warning": lambda *args: None, "info": lambda *args: None
    })()
    scraper.close()
    assert detail.close_calls == 1
    assert search.closed is False
    assert browser.close_calls == 0
    # worker只断开自己的Playwright客户端，不关闭CDP Chrome。
    assert playwright.stop_calls == 1


def test_scanner_creates_detail_page_only_in_dedicated_factory():
    source = (Path(__file__).parents[1] / "scrapers" / "boss.py").read_text(encoding="utf-8")
    assert source.count("self.context." + "new_page(") == 1
    assert "def _ensure_detail_page" in source


def test_browser_code_has_no_unsupported_launch_flags():
    source = (Path(__file__).parents[1] / "scrapers" / "boss.py").read_text(encoding="utf-8")
    assert "--no-" + "sandbox" not in source
    assert "launch_" + "persistent_context" not in source
    assert "chromium." + "launch(" not in source
