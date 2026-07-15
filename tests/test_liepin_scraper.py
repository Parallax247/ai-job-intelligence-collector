from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from main import create_scraper
from scrapers.boss import BossScraper
from scrapers.liepin import (
    LIEPIN_SELECTORS,
    LiepinScraper,
    get_liepin_page,
    inspect_liepin_search_state,
    is_liepin_page_url,
    is_valid_liepin_search_page,
    normalize_liepin_keyword,
    validate_liepin_search_results,
    validate_liepin_record,
)
from utils.storage import JsonlStore


class Logger:
    def info(self, *args): pass
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass
    def exception(self, *args): pass


class Page:
    def __init__(self, url: str, closed: bool = False):
        self.url = url
        self.closed = closed
        self.front = 0
        self.timeout = 0

    def is_closed(self): return self.closed
    def bring_to_front(self): self.front += 1
    def set_default_timeout(self, value): self.timeout = value


class Context:
    def __init__(self, pages):
        self.pages = list(pages)
        self.created = 0

    def new_page(self):
        self.created += 1
        page = Page("about:blank")
        self.pages.append(page)
        return page


def make_scraper(tmp_path: Path) -> LiepinScraper:
    return LiepinScraper(
        tmp_path, {"wait_seconds_min": 0, "wait_seconds_max": 0},
        JsonlStore(tmp_path / "jobs.jsonl"), Logger(), data_dir=tmp_path,
    )


def test_liepin_page_recognition_and_priority():
    home = Page("https://www.liepin.com/")
    search = Page("https://www.liepin.com/zhaopin/?key=python")
    asset = Page("https://www.liepin.com/assets/app.js")
    boss = Page("https://www.zhipin.com/shanghai/")
    assert is_liepin_page_url(home.url)
    assert is_liepin_page_url("https://m.liepin.com/job/123.shtml")
    assert not is_liepin_page_url(asset.url)
    assert not is_liepin_page_url("about:blank")
    assert get_liepin_page(Context([home, boss, asset, search])) is search


def test_liepin_page_rebind_ignores_closed_old_page():
    closed_search = Page("https://www.liepin.com/zhaopin/?key=old", closed=True)
    replacement = Page("https://www.liepin.com/zhaopin/?key=new")
    assert get_liepin_page(Context([closed_search, replacement]), closed_search) is replacement


def test_liepin_search_selector_candidates_are_isolated():
    assert 'input[placeholder*="职位"]' in LIEPIN_SELECTORS["search_input"]
    assert 'input[placeholder*="搜索"]' in LIEPIN_SELECTORS["search_input"]
    assert 'button:has-text("搜索")' in LIEPIN_SELECTORS["search_button"]
    assert 'span:has-text("搜索")' in LIEPIN_SELECTORS["search_button"]
    assert '[class*="search"] button' in LIEPIN_SELECTORS["search_button"]
    assert '[data-tlg-elem-id="c_pc_search_job_listcard"]' in LIEPIN_SELECTORS["job_card"]
    assert "liepin" not in inspect.getsource(BossScraper).lower()


def test_liepin_search_validation_requires_real_keyword_evidence(monkeypatch):
    page = Page("https://www.liepin.com/zhaopin/?key=%E4%BA%A4%E6%98%93%E7%B3%BB%E7%BB%9F%E8%BF%90%E7%BB%B4")
    page.title = lambda: "交易系统运维招聘_猎聘"
    monkeypatch.setattr("scrapers.liepin._search_input_value", lambda page: "交易系统运维")
    monkeypatch.setattr("scrapers.liepin._real_liepin_job_cards", lambda page: [{
        "url": "https://www.liepin.com/job/123456.shtml", "title": "交易助理",
        "salary": "12-18k", "company": "测试公司", "source": "c_pc_search_job_listcard",
        "text": "交易助理 12-18k 测试公司",
    }])
    state = inspect_liepin_search_state(page, "交易系统运维")
    assert state["confirmed"] is True
    assert validate_liepin_search_results(page, "交易系统运维") == (True, "")


def test_liepin_keyword_normalization_supports_full_width_and_spaces():
    assert normalize_liepin_keyword("  ＡＩ  交易系统  ") == "ai 交易系统"


def test_liepin_three_updated_cards_confirm_even_when_input_is_temporarily_old(monkeypatch):
    page = Page("https://www.liepin.com/zhaopin/")
    monkeypatch.setattr("scrapers.liepin._search_input_value", lambda page: "旧关键词")
    monkeypatch.setattr("scrapers.liepin._real_liepin_job_cards", lambda page: [
        {"url": f"https://www.liepin.com/job/new{index}.shtml", "title": f"岗位{index}",
         "salary": "12-18k", "source": "c_pc_search_job_listcard", "text": "岗位"}
        for index in range(3)
    ])
    valid, reason = validate_liepin_search_results(page, "交易系统运维", ("old1", "old2"))
    assert (valid, reason) == (True, "")


def test_liepin_home_search_popup_becomes_bound_search_page(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    home = Page("https://c.liepin.com/")
    result = Page("https://www.liepin.com/zhaopin/?key=test")
    context = Context([home])
    scraper.context = context

    class Input:
        def fill(self, value): pass

    class Button:
        def click(self, no_wait_after=False):
            context.pages.append(result)

    monkeypatch.setattr(scraper, "_first_visible", lambda page, key: (
        (LIEPIN_SELECTORS[key][0], Input()) if key == "search_input"
        else (LIEPIN_SELECTORS[key][0], Button())
    ))
    monkeypatch.setattr("scrapers.liepin._real_liepin_job_cards", lambda page: [])
    monkeypatch.setattr("scrapers.liepin.inspect_liepin_search_state", lambda page, *args: {
        "confirmed": page is result, "input_matches": page is result,
        "card_count": 3 if page is result else 0, "result_updated": True,
    })
    assert scraper.search_keyword(home, "test") is result


def test_liepin_home_recommendations_are_rejected(monkeypatch):
    page = Page("https://www.liepin.com/")
    page.title = lambda: "猎聘首页"
    monkeypatch.setattr("scrapers.liepin._search_input_value", lambda page: "交易系统运维")
    monkeypatch.setattr("scrapers.liepin._real_liepin_job_cards", lambda page: [])
    valid, reason = is_valid_liepin_search_page(page, "交易系统运维")
    assert valid is False
    assert "真实职位结果列表" in reason


def test_liepin_lazy_loading_deduplicates_and_stops_at_limit(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    page = Page("https://www.liepin.com/zhaopin/?key=test")
    page.round = 0
    page.wait_for_timeout = lambda milliseconds: None
    batches = [
        [f"https://www.liepin.com/job/{index}.shtml" for index in range(1, 11)],
        [f"https://www.liepin.com/job/{index}.shtml" for index in range(6, 21)],
        [f"https://www.liepin.com/job/{index}.shtml" for index in range(16, 31)],
    ]
    monkeypatch.setattr(
        "scrapers.liepin.validate_liepin_search_results", lambda *args: (True, "")
    )
    monkeypatch.setattr(
        "scrapers.liepin._visible_job_urls", lambda page: batches[min(page.round, 2)]
    )
    monkeypatch.setattr(scraper, "_scroll", lambda page: setattr(page, "round", page.round + 1))
    urls = scraper.collect_job_urls(page, 30, "test")
    assert len(urls) == 30
    assert len(set(urls)) == 30
    assert page.round == 2


def test_liepin_job_id_formats():
    assert LiepinScraper.job_id("https://www.liepin.com/job/1968723105.shtml") == "1968723105"
    assert LiepinScraper.job_id("https://www.liepin.com/a/abcDEF123/") == "abcDEF123"


def test_liepin_detail_field_extraction(tmp_path):
    scraper = make_scraper(tmp_path)

    class DetailPage(Page):
        def evaluate(self, script, selectors):
            return {
                "title": "交易系统运维工程师", "salary": "20-30k·14薪",
                "company": "某金融科技公司", "basic": "上海·浦东新区\n3-5年\n本科",
                "benefits": ["五险一金", "年终奖"],
                "jd": "职位描述\n岗位职责：\n" + "负责交易系统维护和故障处理。" * 8
                + "\n任职要求：\n" + "本科及以上学历，三年金融系统经验。" * 6,
                "recruiter": "张女士", "companyInfo": "金融科技\n100-499人\nB轮",
                "selectors": {"title": "h1", "salary": "[class*=salary]", "jd": "semantic"},
            }

    record = scraper.extract_job_detail(
        DetailPage("https://www.liepin.com/job/1968723105.shtml"),
        "https://www.liepin.com/job/1968723105.shtml", 1, "交易系统运维", "run",
    )
    assert record["platform"] == "liepin"
    assert record["title"] == "交易系统运维工程师"
    assert record["company"] == "某金融科技公司"
    assert record["salary"] == "20-30k·14薪"
    assert record["city"] == "上海"
    assert record["district"] == "浦东新区"
    assert len(record["jd_text"]) >= 100
    assert record["responsibilities"]
    assert record["requirements"]
    assert validate_liepin_record(record)


def test_closed_liepin_detail_page_is_recreated(tmp_path):
    scraper = make_scraper(tmp_path)
    search = Page("https://www.liepin.com/zhaopin/?key=test")
    old = Page("https://www.liepin.com/job/1.shtml", closed=True)
    scraper.context = Context([search, old])
    scraper.detail_page = old
    scraper.detail_page_owned = True
    replacement = scraper._ensure_detail_page(search)
    assert replacement is not old
    assert scraper.context.created == 1


def test_liepin_multi_keyword_switch_runs_every_keyword(tmp_path, monkeypatch):
    scraper = make_scraper(tmp_path)
    search = Page("https://www.liepin.com/")
    detail = Page("about:blank")
    context = Context([search])
    searched: list[str] = []
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr("scrapers.liepin.ensure_dedicated_chrome_running", lambda: False)
    monkeypatch.setattr(scraper, "_connect", lambda: setattr(scraper, "context", context) or context)
    monkeypatch.setattr(scraper, "bind_page", lambda: search)
    monkeypatch.setattr(
        scraper, "search_keyword", lambda page, keyword: searched.append(keyword) or page
    )
    monkeypatch.setattr(
        scraper, "collect_job_urls",
        lambda page, limit, keyword: [f"https://www.liepin.com/job/{keyword}-1.shtml"],
    )
    monkeypatch.setattr(scraper, "_ensure_detail_page", lambda page: detail)
    monkeypatch.setattr(scraper, "_ensure_runtime", lambda page: (page, detail))

    def capture(page, url, rank, keyword, run_id):
        captured.append((keyword, url))
        scraper.captured_count += 1
        return "captured"

    monkeypatch.setattr(scraper, "_capture", capture)
    result = scraper.run(["交易系统运维", "合规风控"], 1)
    assert searched == ["交易系统运维", "合规风控"]
    assert [row["status"] for row in scraper.keyword_summaries] == ["completed", "completed"]
    assert len(captured) == 2
    assert result["status"] == "completed"
    assert (tmp_path / "screenshots" / "liepin" / "交易系统运维").is_dir()
    assert (tmp_path / "screenshots" / "liepin" / "合规风控").is_dir()


def test_adapter_factory_keeps_boss_and_liepin_separate(tmp_path):
    store = JsonlStore(tmp_path / "jobs.jsonl")
    boss = create_scraper(
        "boss", root=tmp_path, config={}, store=store, logger=Logger(), debug=False,
        data_dir=tmp_path, historical_urls=set(),
    )
    liepin = create_scraper(
        "liepin", root=tmp_path, config={}, store=store, logger=Logger(), debug=False,
        data_dir=tmp_path, historical_urls=set(),
    )
    assert isinstance(boss, BossScraper)
    assert isinstance(liepin, LiepinScraper)
    assert type(boss) is not type(liepin)
