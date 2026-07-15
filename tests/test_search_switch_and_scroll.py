from pathlib import Path

import pytest

from scrapers.boss import BossScraper, SearchValidationError
from utils.storage import JsonlStore


class Logger:
    def info(self, *args): pass
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass
    def exception(self, *args): pass


class InputLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self): return self
    def wait_for(self, **kwargs): return None
    def count(self): return 1
    def is_visible(self): return True
    def fill(self, value):
        self.page.fills.append(value)
        self.page.fill_locator_ids.append(id(self))


class Button:
    def __init__(self): self.clicks = 0
    def click(self): self.clicks += 1


class SearchPage:
    url = "https://www.zhipin.com/web/geek/jobs?query=旧关键词"

    def __init__(self):
        self.fills = []
        self.fill_locator_ids = []

    def locator(self, selector): return InputLocator(self)
    def is_closed(self): return False
    def bring_to_front(self): return None
    def wait_for_timeout(self, value): return None


def make_scraper(tmp_path, config=None):
    return BossScraper(
        Path(tmp_path), config or {}, JsonlStore(tmp_path / "jobs.jsonl"), Logger()
    )


def test_second_keyword_requeries_clears_and_fills_new_input(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    page = SearchPage()
    button = Button()
    monkeypatch.setattr(scraper, "_recover_boss_search_page", lambda current, step: current)
    monkeypatch.setattr(scraper, "_pause_if_abnormal", lambda page: None)
    monkeypatch.setattr(scraper, "_find_search_button", lambda page: ("button:has-text(搜索)", button))
    monkeypatch.setattr(
        scraper, "_wait_for_keyword_confirmation",
        lambda page, keyword, previous_job_ids, timeout_ms: {
            "confirmed": True, "url_query_matches": True, "input_matches": True,
            "result_keyword_matches": False, "has_results": True, "job_list_updated": True,
        },
    )
    monkeypatch.setattr("scrapers.boss.dismiss_overlays", lambda page: {
        "detected": False, "dismissed": False, "action": "none",
    })
    monkeypatch.setattr("scrapers.boss._result_job_ids", lambda page: ["old-job"])

    scraper._search_keyword(page, "交易系统运维")
    scraper._search_keyword(page, "合规审核")

    assert page.fills == ["", "交易系统运维", "", "合规审核"]
    assert len(set(page.fill_locator_ids)) == 4
    assert button.clicks == 2


class ScrollPage:
    url = "https://www.zhipin.com/web/geek/jobs?query=交易系统运维"

    def __init__(self): self.waits = []
    def is_closed(self): return False
    def wait_for_timeout(self, value): self.waits.append(value)


def job_items(start, end, *, query=""):
    suffix = f"?{query}" if query else ""
    return [
        {"href": f"https://www.zhipin.com/job_detail/job{index}.html{suffix}",
         "text": f"岗位{index}"}
        for index in range(start, end + 1)
    ]


def install_scroll_rounds(monkeypatch, scraper, rounds):
    state = {"index": 0, "scrolls": 0, "reads": 0}

    def read(page):
        state["reads"] += 1
        return rounds[min(state["index"], len(rounds) - 1)]

    def scroll(page):
        state["scrolls"] += 1
        state["index"] = min(state["index"] + 1, len(rounds) - 1)
        return {"mode": "container", "at_end": False, "no_more": False}

    monkeypatch.setattr(scraper, "_read_result_job_links", read)
    monkeypatch.setattr(scraper, "_scroll_job_results", scroll)
    monkeypatch.setattr(scraper, "_pause_if_abnormal", lambda page: None)
    monkeypatch.setattr("scrapers.boss.is_valid_search_results_page", lambda page, keyword: (True, ""))
    return state


def test_lazy_scroll_grows_from_ten_to_thirty_in_three_rounds(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    page = ScrollPage()
    state = install_scroll_rounds(monkeypatch, scraper, [
        job_items(1, 10), job_items(1, 20), job_items(1, 30),
    ])
    urls = scraper._collect_job_urls(page, 30, "交易系统运维")
    assert len(urls) == 30
    assert state["reads"] == 3
    assert state["scrolls"] == 2


def test_lazy_scroll_stops_after_three_rounds_without_new_urls(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    page = ScrollPage()
    state = install_scroll_rounds(monkeypatch, scraper, [job_items(1, 10)])
    urls = scraper._collect_job_urls(page, 30, "交易系统运维")
    assert len(urls) == 10
    assert state["reads"] == 4
    assert state["scrolls"] == 3


def test_lazy_scroll_stops_immediately_at_limit(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    page = ScrollPage()
    state = install_scroll_rounds(monkeypatch, scraper, [job_items(1, 10)])
    urls = scraper._collect_job_urls(page, 5, "交易系统运维")
    assert len(urls) == 5
    assert state["reads"] == 1
    assert state["scrolls"] == 0


def test_same_job_id_across_scroll_rounds_is_only_kept_once(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    page = ScrollPage()
    state = install_scroll_rounds(monkeypatch, scraper, [
        job_items(1, 2, query="from=first"),
        job_items(1, 2, query="from=second") + job_items(3, 3),
    ])
    urls = scraper._collect_job_urls(page, 3, "交易系统运维")
    assert len(urls) == 3
    assert [scraper._job_id(url) for url in urls] == ["job1", "job2", "job3"]
    assert state["reads"] == 2


def test_homepage_during_scroll_retries_search_before_reading_recommendations(
    tmp_path, monkeypatch,
):
    scraper = make_scraper(tmp_path)
    page = ScrollPage()
    checks = iter([(True, ""), (False, "返回首页")])
    reads = []
    monkeypatch.setattr(
        "scrapers.boss.is_valid_search_results_page", lambda page, keyword: next(checks)
    )
    monkeypatch.setattr(
        scraper, "_read_result_job_links",
        lambda page: reads.append("read") or job_items(1, 1),
    )
    monkeypatch.setattr(
        scraper, "_scroll_job_results",
        lambda page: {"mode": "window", "at_end": False, "no_more": False},
    )
    monkeypatch.setattr(scraper, "_pause_if_abnormal", lambda page: None)
    monkeypatch.setattr(
        scraper, "_search_keyword",
        lambda page, keyword: (_ for _ in ()).throw(SearchValidationError(keyword, "恢复失败")),
    )

    with pytest.raises(SearchValidationError, match="恢复失败"):
        scraper._collect_job_urls(page, 3, "交易系统运维")
    assert reads == ["read"]
