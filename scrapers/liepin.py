from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, sync_playwright

from scrapers.base import BaseScraper
from utils.browser_manager import ensure_dedicated_chrome_running
from utils.desktop_service import get_cdp_pages, open_cdp_tab
from utils.storage import JsonlStore, safe_directory_name, safe_filename


LIEPIN_HOME_URL = "https://www.liepin.com/"
SALARY_PATTERN = re.compile(r"\d{1,3}\s*-\s*\d{1,3}[kK](?:[·・]\d+薪)?|薪资面议")
EXPERIENCE_PATTERN = re.compile(r"经验不限|应届|\d+(?:\s*-\s*\d+)?年(?:以上|以内)?")
EDUCATION_PATTERN = re.compile(r"学历不限|初中|高中|中专|大专|本科|硕士|博士")
COMPANY_SIZE_PATTERN = re.compile(r"\d+\s*-\s*\d+人|\d+人以上")
FINANCING_PATTERN = re.compile(
    r"融资未公开|未融资|天使轮|A轮|B轮|C轮|D轮及以上|已上市|不需要融资"
)


LIEPIN_SELECTORS: dict[str, list[str]] = {
    "search_input": [
        'input[placeholder*="职位"]', 'input[placeholder*="搜索"]', 'input[type="text"]',
    ],
    "search_button": [
        'span[class*="search-btn"]:has-text("搜索")',
        'button:has-text("搜索")', 'a:has-text("搜索")',
        'span:has-text("搜索")',
        '[class*="search"] button', '[class*="search-btn"]',
    ],
    "result_container": [
        '[class*="job-list"]', '[class*="job-card"]', '[class*="job-item"]',
        'main:has(a[href*="/job/"])', 'main:has(a[href*="/a/"])',
    ],
    "job_card": [
        '[data-tlg-elem-id="c_pc_search_job_listcard"]',
        '.job-list-box .job-card-pc-container',
        '[class*="job-card"]', '[class*="job-list"] li', '[class*="job-item"]',
        'a[href*="/job/"]', 'a[href*="/a/"]',
    ],
    "scroll_container": [
        '[class*="job-list"]', '[class*="list-content"]', '[class*="search-content"]',
    ],
}


DETAIL_SELECTORS: dict[str, list[str]] = {
    "title": [
        '.job-apply-container .job-title', "h1",
        '[class*="job-title"]', '[class*="job-name"]',
    ],
    "salary": [
        '.job-apply-container .salary', '[class*="salary"]', '[class*="job-salary"]',
    ],
    "company": [
        '.company-info-container .company-card .name',
        '[class*="company-name"]', '[class*="company-info"] a',
        'a[href*="/company/"]', 'a[href*="/comp/"]',
    ],
    "basic": [
        '.job-apply-container .job-properties',
        '[class*="job-properties"]', '[class*="job-attr"]', '[class*="job-info"]',
        '[class*="job-tags"]',
    ],
    "benefits": [
        '.job-apply-container-left > .labels span',
        '[class*="benefit"] span', '[class*="welfare"] span', '[class*="job-label"]',
    ],
    "jd": [
        '.job-intro-container',
        '[class*="job-description"]', '[class*="job-desc"]', '[class*="job-intro"]',
        '[class*="job-detail-content"]', '[class*="job-detail"]',
    ],
    "recruiter": [
        '.recruiter-container', '[class*="recruiter"]', '[class*="hunter"]',
        '[class*="contact"]',
    ],
    "company_info": [
        '.company-info-container .company-other',
        '[class*="company-info"]', '[class*="company-detail"]',
    ],
}


class LiepinPageUnavailableError(RuntimeError):
    pass


class LiepinSearchValidationError(RuntimeError):
    pass


class LiepinNoResultsError(RuntimeError):
    pass


class LiepinInfrastructureError(RuntimeError):
    pass


def is_liepin_page_url(url: str) -> bool:
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    lowered = (url or "").lower()
    return (
        (hostname == "liepin.com" or hostname.endswith(".liepin.com"))
        and not any(value in lowered for value in (
            "assets", "service_worker", "chrome://", "about:blank", "localhost", "127.0.0.1",
        ))
        and not parsed.path.lower().endswith(".js")
    )


def liepin_page_priority(url: str) -> int:
    lowered = (url or "").lower()
    path = urlparse(url or "").path.lower()
    if any(value in lowered for value in ("key=", "keyword=", "dq=", "/zhaopin/")):
        return 0
    if any(value in path for value in ("/job/", "/jobs/", "/search/")):
        return 1
    return 2


def get_liepin_page(context: BrowserContext, current_page: Page | None = None) -> Page | None:
    pages: list[Page] = []
    if current_page is not None:
        pages.append(current_page)
    try:
        pages.extend(reversed(list(context.pages)))
    except Exception:
        return None
    valid: list[Page] = []
    seen: set[int] = set()
    for page in pages:
        if id(page) in seen:
            continue
        seen.add(id(page))
        try:
            if not page.is_closed() and is_liepin_page_url(page.url):
                valid.append(page)
        except Exception:
            continue
    return min(valid, key=lambda item: liepin_page_priority(item.url), default=None)


def _visible(page: Page, selectors: list[str]) -> bool:
    for selector in selectors:
        try:
            locators = page.locator(selector)
            for index in range(min(locators.count(), 20)):
                if locators.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def _search_input_value(page: Page) -> str:
    for selector in LIEPIN_SELECTORS["search_input"]:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return str(locator.input_value() or "").strip()
        except Exception:
            continue
    return ""


def normalize_liepin_keyword(value: str) -> str:
    """统一全半角、大小写和空白，避免输入法或页面格式造成假不匹配。"""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _real_liepin_job_cards(page: Page) -> list[dict[str, Any]]:
    """返回猎聘搜索结果卡片；明确排除首页推荐、二维码和筛选区域。"""
    try:
        values = page.evaluate(
            r"""() => {
              const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
              const salary=/\d{1,3}\s*-\s*\d{1,3}[kK](?:[·・]\d+薪)?|薪资面议/;
              const experience=/经验不限|应届|\d+(?:\s*-\s*\d+)?年(?:以上|以内)?/;
              const education=/学历不限|初中|高中|中专|大专|本科|硕士|博士/;
              const selectors=[
                '[data-tlg-elem-id="c_pc_search_job_listcard"]',
                '.job-list-box .job-card-pc-container',
                '[class*="job-list"] li', '[class*="job-item"]'
              ];
              const candidates=[];
              for(const selector of selectors) for(const element of document.querySelectorAll(selector)){
                if(visible(element)&&!candidates.includes(element))candidates.push(element);
              }
              const output=[];
              const seen=new Set();
              for(const card of candidates){
                const marker=card.getAttribute('data-tlg-elem-id')||'';
                const text=(card.innerText||'').replace(/\u00a0/g,' ').trim();
                if(/home|recommend/i.test(marker)||/求职期望相似的职位|推荐职位/.test(text))continue;
                const anchor=[...card.querySelectorAll('a[href*="/job/"],a[href*="/a/"]')]
                  .find(visible);
                if(!anchor)continue;
                const url=anchor.href||anchor.getAttribute('href')||'';
                if(!/liepin\.com\/(?:job|a)\//i.test(url)||seen.has(url))continue;
                const titleElement=card.querySelector('[title^="招聘"]')||
                  anchor.querySelector('[class*="title"],.ellipsis-1');
                let title=(titleElement?.getAttribute('title')||titleElement?.innerText||'').trim();
                title=title.replace(/^招聘/, '').trim();
                const salaryValue=text.match(salary)?.[0]||'';
                const location=text.match(/【\s*([^】]+?)\s*】/)?.[1]?.replace(/\s+/g,'')||'';
                const companyElement=card.querySelector(
                  '[data-nick="job-detail-company-info"] .ellipsis-1, [class*="company"] .ellipsis-1'
                );
                const company=(companyElement?.innerText||'').trim();
                const experienceEducation=[text.match(experience)?.[0]||'',text.match(education)?.[0]||'']
                  .filter(Boolean).join(' / ');
                const recruiterElement=card.querySelector('.recruiter-info-box,[class*="recruiter"]');
                const recruiter=(recruiterElement?.innerText||'').trim();
                const features=[title,salaryValue,company,location,experienceEducation,recruiter]
                  .filter(Boolean).length;
                if(features<3)continue;
                seen.add(url);
                output.push({url,title,salary:salaryValue,company,location,
                  experience_education:experienceEducation,recruiter,features,text:text.slice(0,500),
                  source:marker||card.className||''});
              }
              return output;
            }"""
        )
    except Exception:
        return []
    return [dict(value) for value in (values or []) if isinstance(value, dict)]


def _card_signature(cards: list[dict[str, Any]]) -> tuple[str, ...]:
    signatures: list[str] = []
    for card in cards:
        url = str(card.get("url", ""))
        path = urlparse(url).path
        job_match = re.search(r"/(?:job|a)/([^/?]+)", path)
        signatures.append(job_match.group(1) if job_match else f"{card.get('title','')}|{url}")
    return tuple(dict.fromkeys(signatures))


def _visible_job_urls(page: Page) -> list[str]:
    result: list[str] = []
    for card in _real_liepin_job_cards(page):
        full = urljoin(page.url, str(card.get("url", "")))
        if is_liepin_page_url(full) and re.search(r"/(?:job|a)/", urlparse(full).path):
            result.append(full)
    return list(dict.fromkeys(result))


def inspect_liepin_search_state(page: Page, keyword: str,
                                previous_signature: Any = None) -> dict[str, Any]:
    expected = normalize_liepin_keyword(keyword)
    input_value = _search_input_value(page)
    input_matches = normalize_liepin_keyword(input_value) == expected
    cards = _real_liepin_job_cards(page)
    signature = _card_signature(cards)
    if isinstance(previous_signature, (set, list, tuple)):
        previous = tuple(str(value) for value in previous_signature)
    elif previous_signature:
        previous = (str(previous_signature),)
    else:
        previous = ()
    updated = bool(previous and set(signature) != set(previous))
    try:
        parsed = urlparse(page.url)
        route_is_search = (
            parsed.hostname in {"www.liepin.com", "liepin.com"}
            and any(value in parsed.path.lower() for value in ("/zhaopin", "/search", "/jobs"))
        ) or any("search_job_listcard" in str(card.get("source", "")) for card in cards)
    except Exception:
        route_is_search = False
    has_salary_and_title = any(card.get("salary") and card.get("title") for card in cards)
    combination_a = input_matches and len(cards) >= 1
    combination_b = len(cards) >= 3 and updated
    combination_c = input_matches and route_is_search and has_salary_and_title
    confirmed = combination_a or combination_b or combination_c
    return {
        "input_value": input_value, "input_matches": input_matches,
        "cards": cards, "card_count": len(cards), "urls": [card["url"] for card in cards],
        "signature": signature, "result_updated": updated,
        "route_is_search": route_is_search, "has_salary_and_title": has_salary_and_title,
        "combination_a": combination_a, "combination_b": combination_b,
        "combination_c": combination_c, "confirmed": confirmed,
    }


def validate_liepin_search_results(page: Page, keyword: str,
                                   previous_signature: Any = None) -> tuple[bool, str]:
    try:
        if not is_liepin_page_url(page.url):
            return False, "当前不是猎聘普通页面"
    except Exception as exc:
        return False, f"猎聘页面URL不可读：{exc}"
    state = inspect_liepin_search_state(page, keyword, previous_signature)
    if state["confirmed"]:
        return True, ""
    if not state["input_matches"]:
        return False, "搜索框关键词与当前关键词不一致"
    if not state["card_count"]:
        return False, "未检测到猎聘真实职位结果列表"
    return False, "搜索结果尚未确认为当前关键词"


def is_valid_liepin_search_page(page: Page, keyword: str,
                                previous_urls: Any = None) -> tuple[bool, str]:
    """兼容旧调用名称；新逻辑统一由validate_liepin_search_results实现。"""
    return validate_liepin_search_results(page, keyword, previous_urls)


def split_jd_sections(text: str) -> tuple[str, str]:
    text = str(text or "").strip()
    responsibilities = requirements = ""
    responsibility = re.search(r"(?:岗位职责|工作职责|职位职责|工作内容)[：:]?\s*", text)
    requirement = re.search(r"(?:任职要求|职位要求|岗位要求)[：:]?\s*", text)
    if responsibility:
        end = requirement.start() if requirement and requirement.start() > responsibility.end() else len(text)
        responsibilities = text[responsibility.end():end].strip()
    if requirement:
        requirements = text[requirement.end():].strip()
    return responsibilities, requirements


def validate_liepin_record(record: dict[str, Any]) -> bool:
    return bool(
        str(record.get("title", "")).strip()
        and str(record.get("company", "")).strip()
        and str(record.get("salary", "")).strip()
        and len(str(record.get("jd_text", "")).strip()) >= 100
        and is_liepin_page_url(str(record.get("url", "")))
        and re.search(r"/(?:job|a)/", urlparse(str(record.get("url", ""))).path)
    )


def _infrastructure_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(value in text for value in (
        "targetclosed", "target page, context or browser has been closed",
        "browser disconnected", "context closed", "page closed", "connection closed",
    ))


class LiepinScraper(BaseScraper):
    """猎聘MVP适配器；选择器、搜索验证和详情解析均与BOSS完全分离。"""

    def __init__(self, root: Path | None = None, config: dict[str, Any] | None = None,
                 store: JsonlStore | None = None, logger=None, debug: bool = False,
                 data_dir: Path | None = None, historical_urls: set[str] | None = None):
        self.root = Path(root or Path.cwd())
        self.config = dict(config or {})
        self.data_dir = Path(data_dir or self.root / "data")
        self.store = store or JsonlStore(self.data_dir / "jobs.jsonl", logger)
        self.logger = logger
        self.debug = debug
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.detail_page: Page | None = None
        self.detail_page_owned = False
        self.captured_count = self.skipped_count = self.failed_count = 0
        self.screenshot_failed_count = self.processed_job_count = 0
        self.infrastructure_failed_count = self.browser_disconnect_count = 0
        self.page_recovery_waiting = False
        self.task_status = "pending"
        self.pending_urls: list[str] = []
        self.collected_urls: list[str] = []
        self.completed_job_ids: list[str] = []
        self.current_index = 0
        self.keyword_summaries: list[dict[str, Any]] = []
        self.invalid_store = JsonlStore(self.data_dir / "invalid_records.jsonl", logger)
        self.invalid_records = self.invalid_store.read_all()
        self.seen_urls, _ = self.store.load_keys()
        self.seen_job_ids = {
            str(row.get("job_id", "")) or self.job_id(str(row.get("url", "")))
            for row in self.store.read_all()
        }
        self.historical_urls = set(historical_urls or set())
        self._previous_urls: set[str] = set()
        self._previous_signature: tuple[str, ...] = ()

    @property
    def task_state_path(self) -> Path:
        return self.data_dir / "task_state.json"

    def _write_task_state(self, **values: Any) -> None:
        payload = {
            "platform": "liepin", "collected_urls": list(dict.fromkeys(self.collected_urls)),
            "completed_job_ids": list(dict.fromkeys(self.completed_job_ids)),
            "pending_urls": list(self.pending_urls), "current_index": self.current_index,
            "valid_count": self.captured_count, "invalid_count": self.failed_count,
            "infrastructure_failed_count": self.infrastructure_failed_count,
            "browser_disconnect_count": self.browser_disconnect_count,
            "pending_count": len(self.pending_urls), "task_status": self.task_status,
        }
        payload.update(values)
        self.task_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.task_state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.task_state_path)

    def _log(self, level: str, message: str, *args: Any) -> None:
        if self.logger is not None:
            getattr(self.logger, level)(message, *args)

    def _connect(self) -> BrowserContext:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(
            str(self.config.get("cdp_url", "http://127.0.0.1:9222"))
        )
        if not self.browser.contexts:
            raise LiepinPageUnavailableError("CDP Chrome没有可用context")
        self.context = self.browser.contexts[0]
        return self.context

    def bind_page(self) -> Page:
        context = self.context or self._connect()
        page = get_liepin_page(context)
        if page is None:
            open_cdp_tab(LIEPIN_HOME_URL)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                page = get_liepin_page(context)
                if page is not None:
                    break
                time.sleep(0.25)
        if page is None:
            raise LiepinPageUnavailableError("未找到猎聘页面")
        page.bring_to_front()
        page.set_default_timeout(int(self.config.get("timeout_ms", 10000)))
        if self._needs_login(page):
            self.page_recovery_waiting = True
            input("请在猎聘页面完成登录后点击继续：")
            self.page_recovery_waiting = False
        self._log("info", "已绑定猎聘页面：%s", page.url)
        return page

    @staticmethod
    def _needs_login(page: Page) -> bool:
        try:
            if any(value in urlparse(page.url).path.lower() for value in ("login", "passport")):
                return True
            for selector in ('input[placeholder*="手机号"]', 'button:has-text("登录")'):
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return True
        except Exception:
            return True
        return False

    def _first_visible(self, page: Page, key: str) -> tuple[str, Locator | None]:
        for selector in LIEPIN_SELECTORS[key]:
            try:
                locators = page.locator(selector)
                for index in range(min(locators.count(), 30)):
                    locator = locators.nth(index)
                    if locator.is_visible():
                        return selector, locator
            except Exception:
                continue
        return "", None

    def search_keyword(self, page: Page, keyword: str) -> Page:
        if page.is_closed() or not is_liepin_page_url(page.url):
            page = self._reconnect_search_page()
        previous_cards = _real_liepin_job_cards(page)
        self._previous_signature = _card_signature(previous_cards)
        self._previous_urls = {str(card.get("url", "")) for card in previous_cards}
        input_selector, search_input = self._first_visible(page, "search_input")
        if search_input is None:
            self._save_debug(page, keyword, "search_input_missing")
            raise LiepinSearchValidationError("未找到猎聘搜索框")
        search_input.fill("")
        _, search_input = self._first_visible(page, "search_input")
        assert search_input is not None
        search_input.fill(keyword)
        button_selector, button = self._first_visible(page, "search_button")
        if button is None:
            self._save_debug(page, keyword, "search_button_missing")
            raise LiepinSearchValidationError("未找到猎聘真实搜索按钮")
        self._log("info", "猎聘搜索框选择器：%s", input_selector)
        self._log("info", "猎聘搜索按钮选择器：%s", button_selector)
        self._log("info", "猎聘搜索提交：%s", keyword)
        before_pages: set[int] = set()
        if self.context is not None:
            try:
                before_pages = {id(candidate) for candidate in self.context.pages}
            except Exception:
                before_pages = set()
        button.click(no_wait_after=True)
        deadline = time.monotonic() + 15
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if self.context is not None:
                try:
                    new_pages = [
                        candidate for candidate in self.context.pages
                        if id(candidate) not in before_pages and not candidate.is_closed()
                        and is_liepin_page_url(candidate.url)
                    ]
                except Exception:
                    new_pages = []
                search_candidates = sorted(
                    new_pages, key=lambda candidate: liepin_page_priority(candidate.url)
                )
                if search_candidates:
                    page = search_candidates[0]
                    page.set_default_timeout(int(self.config.get("timeout_ms", 10000)))
            state = inspect_liepin_search_state(page, keyword, self._previous_signature)
            if state["confirmed"]:
                self._log_search_state(state, confirmed=True)
                return page
            page.wait_for_timeout(500)
        valid, reason = validate_liepin_search_results(
            page, keyword, self._previous_signature
        )
        del valid
        self._log_search_state(state, confirmed=False)
        self._save_search_validation_debug(page, keyword, state, reason)
        if state.get("input_matches") and state.get("route_is_search") and not state.get("card_count"):
            raise LiepinNoResultsError("搜索成功但没有岗位")
        raise LiepinSearchValidationError(reason)

    def validate_search_results(self, page: Page, keyword: str) -> tuple[bool, str]:
        return validate_liepin_search_results(page, keyword, self._previous_signature)

    def collect_job_urls(self, page: Page, limit: int, keyword: str = "") -> list[str]:
        unique: list[str] = []
        unique_keys: set[str] = set()
        stagnant = 0
        for round_number in range(1, 31):
            valid, reason = validate_liepin_search_results(
                page, keyword, self._previous_signature
            )
            if not valid:
                raise LiepinSearchValidationError(
                    f"滚动前搜索状态丢失，已阻止推荐职位：{reason}"
                )
            before = len(unique)
            for url in _visible_job_urls(page):
                normalized = self.store.normalize_url(url)
                key = self.job_id(url) or normalized
                if key not in unique_keys:
                    unique_keys.add(key)
                    unique.append(url)
                    if len(unique) >= limit:
                        break
            added = len(unique) - before
            self._log(
                "info", "猎聘滚动轮次：%d 当前唯一岗位：%d 本轮新增：%d 目标：%d",
                round_number, len(unique), added, limit,
            )
            if len(unique) >= limit:
                return unique[:limit]
            stagnant = stagnant + 1 if added == 0 else 0
            if stagnant >= 3:
                break
            self._scroll(page)
            page.wait_for_timeout(random.randint(1000, 2000))
        return unique[:limit]

    def _scroll(self, page: Page) -> None:
        try:
            page.evaluate(
                r"""selectors => {
                  for (const selector of selectors) for (const el of document.querySelectorAll(selector)) {
                    const s=getComputedStyle(el), r=el.getBoundingClientRect();
                    if(r.width>200&&r.height>200&&/(auto|scroll)/.test(s.overflowY)&&el.scrollHeight>el.clientHeight){
                      el.scrollBy(0,el.clientHeight*.7); return 'container';
                    }} window.scrollBy(0,window.innerHeight*.8); return 'window';
                }""",
                LIEPIN_SELECTORS["scroll_container"],
            )
        except Exception:
            page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")

    def _ensure_detail_page(self, search_page: Page) -> Page:
        try:
            if self.detail_page is not None and not self.detail_page.is_closed():
                return self.detail_page
        except Exception:
            pass
        if self.context is None:
            raise LiepinInfrastructureError("context closed")
        self.detail_page = self.context.new_page()
        if self.detail_page is search_page:
            raise LiepinInfrastructureError("detail_page不得复用search_page")
        self.detail_page_owned = True
        self.detail_page.set_default_timeout(int(self.config.get("timeout_ms", 10000)))
        return self.detail_page

    def _ensure_runtime(self, search_page: Page) -> tuple[Page, Page]:
        if not get_cdp_pages(timeout=1.0):
            raise LiepinInfrastructureError("9222 Chrome disconnected")
        if self.browser is None or not self.browser.is_connected():
            raise LiepinInfrastructureError("browser disconnected")
        try:
            list(self.context.pages if self.context else [])
            if search_page.is_closed() or not is_liepin_page_url(search_page.url):
                raise LiepinInfrastructureError("search page closed")
        except LiepinInfrastructureError:
            raise
        except Exception as exc:
            raise LiepinInfrastructureError(f"context closed: {exc}") from exc
        return search_page, self._ensure_detail_page(search_page)

    def _disconnect(self) -> None:
        self.detail_page = None
        self.detail_page_owned = False
        self.context = None
        self.browser = None
        playwright, self.playwright = self.playwright, None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def _reconnect_search_page(self) -> Page:
        self._disconnect()
        ensure_dedicated_chrome_running()
        self._connect()
        return self.bind_page()

    def _reconnect_runtime(self) -> tuple[Page, Page]:
        page = self._reconnect_search_page()
        return page, self._ensure_detail_page(page)

    def extract_job_detail(self, page: Page, job_url: str, rank: int, keyword: str,
                           keyword_run_id: str) -> dict[str, Any]:
        return self._extract_record(page, job_url, rank, keyword, keyword_run_id)

    def _extract_record(self, page: Page, job_url: str, rank: int, keyword: str,
                        keyword_run_id: str) -> dict[str, Any]:
        data = page.evaluate(
            r"""selectors => {
              const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();
                return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0};
              const clean=v=>(v||'').replace(/\u00a0/g,' ').trim();
              const first=sels=>{for(const s of sels)for(const e of document.querySelectorAll(s)){
                const t=clean(e.innerText);if(vis(e)&&t)return {text:t,selector:s}}return{text:'',selector:''}};
              const all=sels=>{const out=[];for(const s of sels)for(const e of document.querySelectorAll(s)){
                const t=clean(e.innerText);if(vis(e)&&t&&!out.includes(t))out.push(t)}return out};
              const longest=sels=>{let b={text:'',selector:''};for(const s of sels)for(const e of document.querySelectorAll(s)){
                const t=clean(e.innerText);if(vis(e)&&t.length>b.text.length)b={text:t,selector:s}}return b};
              const semantic=()=>{const labels=['职位描述','岗位职责','任职要求','工作内容'];
                for(const e of document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p')){
                  if(!vis(e)||!labels.some(x=>clean(e.innerText)===x))continue;const p=e.parentElement;
                  if(p&&clean(p.innerText).length>=100)return {text:clean(p.innerText),selector:'semantic'};}
                return {text:'',selector:''}};
              const title=first(selectors.title),salary=first(selectors.salary),company=first(selectors.company);
              const basic=first(selectors.basic),benefits=all(selectors.benefits),recruiter=first(selectors.recruiter);
              const companyInfo=first(selectors.company_info);let jd=semantic();if(!jd.text)jd=longest(selectors.jd);
              return {title:title.text,salary:salary.text,company:company.text,basic:basic.text,
                benefits,jd:jd.text,recruiter:recruiter.text,companyInfo:companyInfo.text,
                selectors:{title:title.selector,salary:salary.selector,company:company.selector,jd:jd.selector}};
            }""",
            DETAIL_SELECTORS,
        )
        title = str(data.get("title", "")).splitlines()[0].strip()
        salary_match = SALARY_PATTERN.search(str(data.get("salary", "")))
        salary = salary_match.group(0) if salary_match else str(data.get("salary", "")).strip()
        basic = str(data.get("basic", ""))
        city, district, experience, education = self._parse_basic(basic)
        jd_text = str(data.get("jd", "")).strip()
        responsibilities, requirements = split_jd_sections(jd_text)
        company_info = str(data.get("companyInfo", ""))
        return {
            "job_id": self.job_id(job_url), "platform": "liepin",
            "search_keyword": keyword, "search_rank": rank, "title": title,
            "company": str(data.get("company", "")).splitlines()[0].strip(), "salary": salary,
            "city": city, "district": district, "experience": experience,
            "education": education, "benefits": data.get("benefits", []),
            "responsibilities": responsibilities, "requirements": requirements,
            "jd_text": jd_text, "recruiter": str(data.get("recruiter", "")).strip(),
            "company_size": (COMPANY_SIZE_PATTERN.search(company_info) or [""])[0],
            "company_industry": self._industry(company_info),
            "financing_stage": (FINANCING_PATTERN.search(company_info) or [""])[0],
            "url": job_url, "screenshot_path": "", "html_path": "",
            "screenshot_status": "", "screenshot_error": "",
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "keyword_run_id": keyword_run_id, "matched_keywords": [keyword],
        }

    @staticmethod
    def _parse_basic(text: str) -> tuple[str, str, str, str]:
        raw = str(text or "").strip()
        experience_match = EXPERIENCE_PATTERN.search(raw)
        education_match = EDUCATION_PATTERN.search(raw)
        experience = experience_match.group(0) if experience_match else ""
        education = education_match.group(0) if education_match else ""
        boundaries = [
            match.start() for match in (experience_match, education_match) if match is not None
        ]
        location = raw[:min(boundaries)] if boundaries else re.split(r"[\n|]", raw, 1)[0]
        location = location.strip(" \t\r\n|·-")
        parts = [x for x in re.split(r"[·\s-]+", location) if x]
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "", experience, education)

    @staticmethod
    def _industry(company_info: str) -> str:
        match = re.search(r"企业行业[：:]\s*([^\n]+)", str(company_info))
        if match:
            return match.group(1).strip()
        for line in str(company_info).splitlines():
            value = line.strip()
            if value and not COMPANY_SIZE_PATTERN.search(value) and not FINANCING_PATTERN.search(value):
                return value
        return ""

    def validate_record(self, record: dict[str, Any]) -> bool:
        return validate_liepin_record(record)

    def save_screenshot(self, page: Page, record: dict[str, Any], rank: int, keyword: str) -> None:
        folder = self.data_dir / "screenshots" / "liepin" / safe_directory_name(keyword)
        html_folder = self.data_dir / "html" / "liepin" / safe_directory_name(keyword)
        folder.mkdir(parents=True, exist_ok=True)
        html_folder.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(f"{rank:04d}_{record.get('company','')}_{record.get('title','')}")
        screenshot = folder / f"{stem}.png"
        html = html_folder / f"{stem}.html"
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(screenshot), full_page=True, timeout=20000, animations="disabled")
        html.write_text(page.content(), encoding="utf-8")
        record["screenshot_path"] = str(screenshot)
        record["html_path"] = str(html)
        record["screenshot_status"] = "success"
        record["screenshot_error"] = ""

    def _capture(self, page: Page, url: str, rank: int, keyword: str, run_id: str) -> str:
        last_record: dict[str, Any] = {"url": url, "search_keyword": keyword, "search_rank": rank}
        errors: list[str] = []
        for attempt in (1, 2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_function(
                    "() => (document.body?.innerText||'').length>200 && "
                    "(/职位描述|岗位职责|任职要求|工作内容/.test(document.body.innerText))",
                    timeout=10000, polling=500,
                )
                record = self._extract_record(page, url, rank, keyword, run_id)
                last_record = record
                if self.validate_record(record):
                    record["screenshot_status"] = "pending"
                    self.store.append(record)
                    self.captured_count += 1
                    self.seen_urls.add(self.store.normalize_url(url))
                    self.seen_job_ids.add(str(record.get("job_id", "")))
                    try:
                        self.save_screenshot(page, record, rank, keyword)
                    except Exception as exc:
                        record["screenshot_status"] = "failed"
                        record["screenshot_error"] = str(exc)
                        self.screenshot_failed_count += 1
                    self.store.replace_by_url(record)
                    time.sleep(random.uniform(
                        float(self.config.get("wait_seconds_min", 5)),
                        float(self.config.get("wait_seconds_max", 10)),
                    ))
                    self._log(
                        "info", "猎聘有效岗位已写入JSONL：序号=%d title=%s URL=%s",
                        rank, record["title"], url,
                    )
                    return "captured"
                errors = ["猎聘详情字段验证失败"]
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if _infrastructure_error(exc):
                    raise LiepinInfrastructureError(str(exc)) from exc
                errors = [str(exc)]
                self._log("warning", "猎聘详情第%d次失败：%s", attempt, exc)
            if attempt == 1:
                continue
        invalid = dict(last_record)
        invalid.update({
            "platform": "liepin", "invalid_reason": "; ".join(errors),
            "invalid_source": "liepin_detail", "invalidated_at": datetime.now().astimezone().isoformat(),
        })
        self.invalid_store.append(invalid)
        self.invalid_records.append(invalid)
        self.failed_count += 1
        return "failed"

    def run(self, keywords: list[str], limit: int) -> dict[str, Any]:
        input("请先在9222专用Chrome中打开并登录猎聘，然后按Enter继续。")
        ensure_dedicated_chrome_running()
        self._connect()
        search_page = self.bind_page()
        self.task_status = "running"
        self._write_task_state(task_status=self.task_status)
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for keyword in keywords:
            (self.data_dir / "screenshots" / "liepin" / safe_directory_name(keyword)).mkdir(parents=True, exist_ok=True)
            (self.data_dir / "html" / "liepin" / safe_directory_name(keyword)).mkdir(parents=True, exist_ok=True)
        try:
            for keyword_index, keyword in enumerate(keywords, 1):
                summary = {
                    "search_keyword": keyword, "target_count": limit,
                    "city": str(self.config.get("city", "")), "processed_count": 0,
                    "valid_count": 0, "invalid_count": 0, "captured_count": 0,
                    "duplicate_count": 0, "historical_skipped_count": 0,
                    "failed_count": 0, "screenshot_failed_count": 0,
                    "status": "searching", "error_message": "",
                    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "finished_at": "",
                }
                self.keyword_summaries.append(summary)
                self._log("info", "[%d/%d] 自动搜索关键词：%s", keyword_index, len(keywords), keyword)
                try:
                    search_page = get_liepin_page(self.context, search_page) or self._reconnect_search_page()
                    search_page = self.search_keyword(search_page, keyword)
                    summary["status"] = "collecting_urls"
                    urls = self.collect_job_urls(search_page, limit, keyword)
                    self.collected_urls.extend(url for url in urls if url not in self.collected_urls)
                    eligible: list[str] = []
                    scheduled_ids: set[str] = set()
                    for url in urls:
                        normalized, job_id = self.store.normalize_url(url), self.job_id(url)
                        key = job_id or normalized
                        historical = normalized in self.historical_urls
                        if (
                            normalized in self.seen_urls or job_id in self.seen_job_ids
                            or key in scheduled_ids
                        ):
                            summary["duplicate_count"] += 1
                            self.skipped_count += 1
                        elif str(self.config.get("save_mode", "snapshot")) == "new_only" and historical:
                            summary["historical_skipped_count"] += 1
                            self.skipped_count += 1
                        else:
                            scheduled_ids.add(key)
                            eligible.append(url)
                    if not eligible:
                        summary["status"] = (
                            "historical_skipped"
                            if summary["historical_skipped_count"] else "no_new_jobs"
                        )
                        continue
                    summary["status"] = "collecting_details"
                    self.pending_urls = list(eligible)
                    self._write_task_state(
                        current_keyword=keyword, current_rank=0, task_status="running"
                    )
                    detail_page = self._ensure_detail_page(search_page)
                    for rank, url in enumerate(eligible, 1):
                        self.current_index = rank
                        self.pending_urls = eligible[rank - 1:]
                        self._write_task_state(
                            current_keyword=keyword, current_rank=rank, task_status="running"
                        )
                        infra_failures = 0
                        while True:
                            try:
                                search_page, detail_page = self._ensure_runtime(search_page)
                                status = self._capture(
                                    detail_page, url, rank, keyword,
                                    f"{run_stamp}_{keyword_index:03d}",
                                )
                                break
                            except LiepinInfrastructureError as exc:
                                infra_failures += 1
                                self.browser_disconnect_count += 1
                                if infra_failures == 1:
                                    search_page, detail_page = self._reconnect_runtime()
                                    continue
                                self.infrastructure_failed_count += 1
                                self.task_status = "paused_browser_lost"
                                self.page_recovery_waiting = True
                                self._write_task_state(
                                    current_keyword=keyword, current_rank=rank,
                                    task_status=self.task_status, error_message=str(exc),
                                )
                                input(f"猎聘页面连接丢失（{exc}），修复后点击重新连接并继续：")
                                self.page_recovery_waiting = False
                                search_page, detail_page = self._reconnect_runtime()
                                self.task_status = "running"
                                self._write_task_state(task_status=self.task_status, error_message="")
                                infra_failures = 0
                        self.processed_job_count += 1
                        summary["processed_count"] += 1
                        self.pending_urls = eligible[rank:]
                        self.completed_job_ids.append(self.job_id(url))
                        if status == "captured":
                            summary["valid_count"] += 1
                            summary["captured_count"] += 1
                        else:
                            summary["invalid_count"] += 1
                            summary["failed_count"] += 1
                        self._write_task_state(
                            current_keyword=keyword, current_rank=rank, task_status="running"
                        )
                    summary["status"] = (
                        "partial_failed" if summary["invalid_count"] else "completed"
                    )
                except LiepinSearchValidationError as exc:
                    summary["status"] = "search_failed"
                    summary["failed_count"] += 1
                    summary["error_message"] = str(exc)
                    self.failed_count += 1
                    self._log("error", "猎聘关键词搜索失败：%s 原因=%s processed=0", keyword, exc)
                except LiepinNoResultsError as exc:
                    summary["status"] = "no_results"
                    summary["error_message"] = str(exc)
                    self._log("info", "猎聘关键词无结果：%s 原因=%s processed=0", keyword, exc)
                except (LiepinPageUnavailableError, LiepinInfrastructureError) as exc:
                    summary["status"] = "page_lost"
                    summary["error_message"] = str(exc)
                    self.infrastructure_failed_count += 1
                    self._log("error", "猎聘页面丢失：%s 原因=%s processed=0", keyword, exc)
                finally:
                    summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    self._log(
                        "info", "关键词执行结果：keyword=%s status=%s processed=%d valid=%d invalid=%d error=%s",
                        keyword, summary["status"], summary["processed_count"],
                        summary["valid_count"], summary["invalid_count"], summary["error_message"],
                    )
            statuses = [row["status"] for row in self.keyword_summaries]
            successful = {"completed", "no_results", "no_new_jobs", "historical_skipped"}
            self.task_status = (
                "completed" if statuses and all(x in successful for x in statuses)
                else (
                    "partial_failed"
                    if any(x in successful or x == "partial_failed" for x in statuses)
                    else "failed"
                )
            )
            self._write_task_state(task_status=self.task_status, current_keyword="", current_rank=0)
            return {
                "captured": self.captured_count, "skipped": self.skipped_count,
                "failed": self.failed_count, "status": self.task_status,
                "infrastructure_failed_count": self.infrastructure_failed_count,
                "browser_disconnect_count": self.browser_disconnect_count,
                "pending_count": len(self.pending_urls),
                "message": f"猎聘执行 {len(keywords)} 个关键词",
            }
        finally:
            if self.detail_page_owned and self.detail_page is not None:
                try:
                    if not self.detail_page.is_closed():
                        self.detail_page.close()
                except Exception:
                    pass
                self.detail_page_owned = False

    @staticmethod
    def job_id(url: str) -> str:
        path = urlparse(url or "").path
        match = re.search(r"/(?:job|a)/([^/?]+?)(?:\.shtml)?$", path.rstrip("/"))
        return match.group(1) if match else ""

    def _save_debug(self, page: Page, keyword: str, prefix: str) -> None:
        if not self.debug:
            return
        folder = self.data_dir / "debug" / "liepin" / safe_directory_name(keyword)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(folder / f"{prefix}.png"), full_page=True)
            (folder / f"{prefix}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass

    def _log_search_state(self, state: dict[str, Any], *, confirmed: bool) -> None:
        self._log("info", "搜索框匹配：%s", "是" if state.get("input_matches") else "否")
        self._log("info", "岗位卡片数量：%d", int(state.get("card_count", 0) or 0))
        self._log("info", "列表已更新：%s", "是" if state.get("result_updated") else "否")
        self._log("info", "搜索确认：%s", "成功" if confirmed else "失败")

    def _save_search_validation_debug(self, page: Page, keyword: str,
                                      state: dict[str, Any], reason: str) -> None:
        """搜索失败总是保留现场，不依赖debug开关。"""
        folder = self.data_dir / "debug" / "liepin" / safe_directory_name(keyword)
        folder.mkdir(parents=True, exist_ok=True)
        prefix = "search_validation_failed"
        cards = list(state.get("cards", []))
        payload = {
            "url": getattr(page, "url", ""), "keyword": keyword,
            "search_input_value": state.get("input_value", ""),
            "input_matches": bool(state.get("input_matches")),
            "card_count": int(state.get("card_count", 0) or 0),
            "result_updated": bool(state.get("result_updated")),
            "first_three_cards": [str(card.get("text", "")) for card in cards[:3]],
            "reason": reason,
        }
        try:
            (folder / f"{prefix}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            page.screenshot(path=str(folder / f"{prefix}.png"), full_page=True)
            (folder / f"{prefix}.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            self._log("warning", "保存猎聘搜索失败现场不完整：%s", exc)

    def close(self) -> None:
        if self.detail_page_owned and self.detail_page is not None:
            try:
                if not self.detail_page.is_closed():
                    self.detail_page.close()
            except Exception:
                pass
        self.detail_page_owned = False
        self._disconnect()
