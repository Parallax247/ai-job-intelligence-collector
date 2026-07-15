from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from scrapers.base import BaseScraper
from utils.browser_manager import ensure_dedicated_chrome_running
from utils.desktop_service import BOSS_URL, get_cdp_pages, open_cdp_tab
from utils.storage import JsonlStore, safe_directory_name, safe_filename


# 搜索选择器只负责发起搜索和提取真实 job_detail href；不点击职位卡片。
SELECTORS: dict[str, list[str]] = {
    "search_button": [
        'button:has-text("搜索")', 'a:has-text("搜索")', ".btn-search", ".search-btn",
    ],
    "job_links": [
        'a[href*="/job_detail/"]', 'a.job-card-left[href*="/job_detail/"]',
        '.job-card-box a[href*="/job_detail/"]', '.job-list-box a[href*="/job_detail/"]',
    ],
    "result_ready": [
        ".job-list-box", ".job-list-container", ".search-job-result",
        'ul[class*="job-list"]', '[class*="search-job-result"]',
    ],
    "job_list_scroll_container": [
        ".job-list-box", ".job-list-container", ".search-job-result",
        'ul[class*="job-list"]', '[class*="job-list"]',
    ],
    "city_trigger": [".city-label", ".city-sel", "[ka='search-select-city']", ".filter-city"],
    "city_option": [
        ".city-box li:has-text('{value}')", ".city-list li:has-text('{value}')",
        ".city-site a:has-text('{value}')", "li:has-text('{value}')", "a:has-text('{value}')",
    ],
    "experience_trigger": [".filter-experience", "[ka*='experience']", "text=工作经验"],
    "education_trigger": [".filter-degree", "[ka*='degree']", "text=学历要求"],
    "salary_trigger": [".filter-salary", "[ka*='salary']", "text=薪资要求"],
    "filter_option": ["li:has-text('{value}')", "a:has-text('{value}')", "span:has-text('{value}')"],
}


# 详情页优先使用这些稳定语义区域；extract_detail_fields 还会用标题文本、最长正文、
# 薪资正则和公司信息文本做结构回退，不会因单个 class 改名而写入空壳数据。
DETAIL_SELECTORS: dict[str, list[str]] = {
    "title": [
        ".job-banner .job-name", ".job-banner .name", ".job-primary .job-name",
        ".job-primary .name", ".job-detail-header .job-name", "h1",
    ],
    "salary": [
        ".job-banner .salary", ".job-primary .salary", ".job-detail-header .salary",
        "[class*='job-banner'] [class*='salary']", "[class*='salary']",
    ],
    "basic_info": [
        ".job-banner .job-primary .info-primary p", ".job-primary .info-primary p",
        ".job-banner .text-desc", ".job-primary .text-desc",
        "[class*='job-banner'] [class*='info']",
    ],
    "address": [
        ".location-address", ".job-address", ".job-address-desc", ".job-location",
        "[class*='job-address']", "[class*='location-address']",
    ],
    "company": [
        ".sider-company .company-name", ".company-info .company-name",
        "a[ka='job-detail-company_custompage']", "a[href*='/gongsi/']",
    ],
    "benefits": [
        ".job-tags span", ".job-tags li", ".job-keyword-list li",
        "[class*='welfare'] span", "[class*='benefit'] span",
    ],
    "jd": [
        ".job-sec-text", ".job-detail-section .text", ".job-detail-body",
        "[class*='job-sec-text']", "[class*='job-description']",
    ],
    "recruiter": [
        ".boss-info-attr .name", ".boss-info .name", ".job-boss-info .name",
        "[class*='boss-info'] [class*='name']",
    ],
    "company_info": [
        ".sider-company", ".company-info", ".job-company-info", "[class*='sider-company']",
    ],
    "company_size": [
        ".sider-company p:has(.icon-scale)", "[class*='sider-company'] p:has([class*='icon-scale'])",
    ],
    "company_industry": [
        ".sider-company a[ka='job-detail-brandindustry']",
        ".sider-company .icon-industry", "[class*='sider-company'] [class*='industry']",
    ],
    "financing_stage": [
        ".sider-company p:has(.icon-stage)", "[class*='sider-company'] p:has([class*='stage'])",
    ],
}


SALARY_PATTERN = re.compile(r"\d{1,3}\s*-\s*\d{1,3}K(?:·\d+薪)?", re.IGNORECASE)
EDUCATION_PATTERN = re.compile(r"学历不限|初中|高中|中专(?:/中技)?|大专|本科|硕士|博士")
EXPERIENCE_PATTERN = re.compile(r"经验不限|无经验|在校/应届|应届生?|\d+(?:\s*-\s*\d+)?年(?:以上|以内)?")
FINANCING_PATTERN = re.compile(r"不需要融资|未融资|天使轮|Pre-A轮|A轮|B轮|C轮|D轮及以上|已上市")
COMPANY_SIZE_PATTERN = re.compile(r"\d+\s*-\s*\d+人|\d+人以上")


class StopScanError(RuntimeError):
    """当前搜索页无法安全继续时终止扫描。"""


class BossPageUnavailableError(StopScanError):
    """BOSS普通Page或CDP context丢失且自动恢复失败。"""


class SearchValidationError(RuntimeError):
    """搜索未进入与当前关键词匹配的真实结果页。"""

    def __init__(self, keyword: str, reason: str):
        self.keyword = keyword
        self.reason = reason
        super().__init__(f"{keyword}：{reason}")


class RuntimeInfrastructureError(RuntimeError):
    """浏览器、context 或 Page 已失效。

    该异常不属于职位数据无效，不得写入 Invalid_Records。
    """


def is_target_closed_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "targetclosed" in text or "target page, context or browser has been closed" in text


def is_infrastructure_error(exc: BaseException) -> bool:
    if isinstance(exc, RuntimeInfrastructureError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return is_target_closed_error(exc) or any(marker in text for marker in (
        "browser has been disconnected", "browser disconnected", "context closed",
        "page closed", "connection closed", "object has been collected",
    ))


def _visible_locator_exists(page: Page, selectors: list[str]) -> bool:
    for selector in selectors:
        try:
            locators = page.locator(selector)
            for index in range(locators.count()):
                if locators.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def _visible_result_keyword_match(page: Page, keyword: str) -> bool:
    expected = str(keyword).strip()
    if not expected:
        return False
    try:
        if expected in str(page.title() or ""):
            return True
    except Exception:
        pass
    for selector in SELECTORS["result_ready"]:
        try:
            locators = page.locator(selector)
            for index in range(min(locators.count(), 5)):
                locator = locators.nth(index)
                if locator.is_visible() and expected in str(locator.inner_text(timeout=500) or ""):
                    return True
        except Exception:
            continue
    return False


def _result_job_ids(page: Page) -> list[str]:
    try:
        values = page.evaluate(
            r"""selectors => {
              const visible = el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
                return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
              const roots=[];
              for (const selector of selectors) { try {
                for (const el of document.querySelectorAll(selector)) if (visible(el)) roots.push(el);
              } catch (_) {} }
              const source=roots.length ? roots : [document];
              const ids=[];
              for (const root of source) for (const a of root.querySelectorAll('a[href*="/job_detail/"]')) {
                if (!visible(a)) continue;
                const match=(a.href||a.getAttribute('href')||'').match(/\/job_detail\/([^/.?]+)/);
                if (match && !ids.includes(match[1])) ids.push(match[1]);
              }
              return ids;
            }""",
            SELECTORS["result_ready"],
        )
        return [str(value) for value in (values or []) if str(value).strip()]
    except Exception:
        return []


def inspect_search_results_state(page: Page, keyword: str,
                                 previous_job_ids: set[str] | None = None) -> dict[str, Any]:
    expected = str(keyword).strip()
    try:
        url = page.url
    except Exception as exc:
        return {
            "url": "", "path": "", "url_query": "", "input_value": "",
            "has_results": False, "has_home_recommendations": False,
            "url_query_matches": False, "input_matches": False,
            "result_keyword_matches": False, "job_list_updated": False,
            "confirmation_count": 0, "confirmed": False,
            "error": f"页面URL不可读取：{exc}",
        }
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    url_query = (parse_qs(parsed.query).get("query") or [""])[0].strip()
    input_value = ""
    for selector in (
        'input[name="query"][placeholder*="搜索职位"]',
        'input[placeholder="搜索职位、公司"]',
        'input[placeholder*="搜索职位"]',
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                input_value = str(locator.input_value() or "").strip()
                break
        except Exception:
            continue
    has_results = _visible_locator_exists(page, SELECTORS["result_ready"])
    home_path = parsed.path.rstrip("/") in {"", "/shanghai"}
    has_featured_marker = _visible_locator_exists(
        page, ['text="精选职位"', 'text="推荐职位"', '[class*="recommend-job"]']
    )
    current_job_ids = set(_result_job_ids(page))
    prior_ids = set(previous_job_ids or set())
    url_query_matches = url_query == expected
    input_matches = input_value == expected
    result_keyword_matches = _visible_result_keyword_match(page, expected)
    job_list_updated = bool(current_job_ids and (not prior_ids or current_job_ids != prior_ids))
    evidence = {
        "url_query_matches": url_query_matches,
        "input_matches": input_matches,
        "result_keyword_matches": result_keyword_matches,
        "has_results": has_results,
        "job_list_updated": job_list_updated,
    }
    confirmation_count = sum(bool(value) for value in evidence.values())
    identity_matches = bool(url_query_matches or input_matches or result_keyword_matches)
    is_results_path = "/web/geek/jobs" in parsed.path
    has_home_recommendations = bool(
        home_path or (has_featured_marker and not is_results_path)
    )
    return {
        "url": url,
        "path": parsed.path,
        "hostname": hostname,
        "url_query": url_query,
        "input_value": input_value,
        "has_results": has_results,
        "has_home_recommendations": has_home_recommendations,
        "url_query_matches": url_query_matches,
        "input_matches": input_matches,
        "result_keyword_matches": result_keyword_matches,
        "job_list_updated": job_list_updated,
        "job_ids": sorted(current_job_ids),
        "confirmation_count": confirmation_count,
        "identity_matches": identity_matches,
        "confirmed": bool(
            is_results_path and not has_home_recommendations and has_results
            and identity_matches and confirmation_count >= 2
        ),
        "keyword": expected,
        "error": "",
    }


def is_valid_search_results_page(page: Page, keyword: str,
                                 previous_job_ids: set[str] | None = None) -> tuple[bool, str]:
    state = inspect_search_results_state(page, keyword, previous_job_ids)
    if state.get("error"):
        return False, str(state["error"])
    hostname = str(state.get("hostname", ""))
    if hostname != "zhipin.com" and not hostname.endswith(".zhipin.com"):
        return False, "当前页面不是BOSS普通页面"
    if "/web/geek/jobs" not in str(state.get("path", "")):
        return False, "未进入关键词搜索结果页，已阻止采集首页推荐岗位"
    if state.get("has_home_recommendations"):
        return False, "检测到BOSS首页精选职位，已阻止采集首页推荐岗位"
    if not state.get("has_results"):
        return False, "未检测到真实搜索结果列表，已阻止采集首页推荐岗位"
    if not state.get("identity_matches"):
        return False, "URL、搜索框和结果区域均未确认当前关键词"
    if int(state.get("confirmation_count", 0)) < 2:
        return False, "当前关键词确认依据不足，页面可能仍在加载或保留上一关键词"
    return True, ""


def get_boss_page(context: BrowserContext) -> Page | None:
    """反向选择最后一个未关闭的 zhipin.com 域页面。"""
    return ensure_boss_search_page(context)


def ensure_boss_search_page(context: BrowserContext, current_page: Page | None = None) -> Page | None:
    """保留有效绑定；失效时只从现有标签页中重新选择BOSS页面。"""
    if current_page is not None:
        try:
            if not current_page.is_closed() and is_boss_page_url(current_page.url):
                return current_page
        except Exception:
            pass
    candidates: list[Page] = []
    for page in context.pages:
        try:
            if not page.is_closed() and is_boss_page_url(page.url):
                candidates.append(page)
        except Exception:
            continue
    return min(candidates, key=lambda page: boss_page_priority(page.url), default=None)


def is_boss_page_url(url: str) -> bool:
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    lowered = (url or "").lower()
    excluded = ("socket-worker", "/assets/", "service_worker", "chrome://", "localhost")
    return (
        (hostname == "zhipin.com" or hostname.endswith(".zhipin.com"))
        and not any(marker in lowered for marker in excluded)
        and not parsed.path.lower().endswith(".js")
    )


def boss_page_priority(url: str) -> int:
    path = urlparse(url or "").path.lower()
    if "/web/geek/jobs" in path:
        return 0
    if path.startswith("/shanghai"):
        return 1
    if "/job_detail/" in path:
        return 2
    return 3


def absolute_job_url(base_url: str, href: str | None) -> str:
    if not href:
        return ""
    url = urljoin(base_url, href.strip())
    return url if "/job_detail/" in urlparse(url).path else ""


def connect_cdp(playwright: Playwright, endpoint: str) -> tuple[Browser, BrowserContext]:
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint)
    except PlaywrightError as exc:
        raise RuntimeError("未发现9222端口Chrome，请先运行Chrome启动命令。") from exc
    if not browser.contexts:
        raise RuntimeError("已连接9222端口Chrome，但没有可用浏览器上下文。")
    return browser, browser.contexts[0]


def ensure_separate_pages(search_page: Page, detail_page: Page) -> None:
    if search_page is detail_page:
        raise RuntimeError("详情页与搜索主页面相同，拒绝导航以保护搜索页")


def dismiss_overlays(page: Page) -> dict[str, Any]:
    selector = "div.overseas-nav-box"
    result: dict[str, Any] = {"detected": False, "dismissed": False, "action": "none"}
    overlay = page.locator(selector).first
    if overlay.count() == 0:
        return result
    result["detected"] = True
    try:
        overlay.evaluate(
            "(el) => { el.style.setProperty('display','none','important'); "
            "el.style.setProperty('pointer-events','none','important'); }"
        )
        result.update({"dismissed": True, "action": "css_hide"})
    except Exception:
        result["action"] = "failed"
    return result


def inspect_results_dom(page: Page, output_dir: Path | None = None) -> dict[str, Any]:
    """无 URL 时保存搜索页链接、薪资元素、完整 HTML 和截图。"""
    output_dir = output_dir or Path("data/debug")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = page.evaluate(
        r"""() => {
          const visible = el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
            return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
          const box = el => { const r=el.getBoundingClientRect(); return {
            x:r.x,y:r.y,width:r.width,height:r.height,top:r.top,left:r.left,right:r.right,bottom:r.bottom}; };
          const salary=/\d{1,3}\s*-\s*\d{1,3}K(?:·\d+薪)?/i;
          const anchors=[...document.querySelectorAll('a')].filter(visible).map(el => ({
            text:(el.innerText||'').trim(), href:el.href||el.getAttribute('href')||'',
            class:el.getAttribute('class')||'', boundingBox:box(el)}));
          const salaryElements=[...document.querySelectorAll('body *')].filter(el => {
            const text=(el.innerText||'').trim(); return visible(el) && text.length<=120 && salary.test(text);
          }).map(el => ({text:(el.innerText||'').trim(), tag:el.tagName.toLowerCase(),
            class:el.getAttribute('class')||'', boundingBox:box(el)}));
          return {anchors, salaryElements};
        }"""
    )
    try:
        title = page.title()
    except Exception as exc:
        title = f"<读取失败：{exc}>"
    report.update({"url": page.url, "title": title})
    (output_dir / "result_dom_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "result_page.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(output_dir / "result_page.png"), full_page=True)
    return report


def job_record_errors(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(record.get("title", "")).strip():
        errors.append("title为空")
    if not str(record.get("salary", "")).strip():
        errors.append("salary为空")
    if len(str(record.get("jd_text", "")).strip()) < 100:
        errors.append("jd_text少于100字")
    if "/job_detail/" not in str(record.get("url", "")):
        errors.append("URL不是job_detail")
    return errors


def validate_job_record(record: dict[str, Any]) -> bool:
    return not job_record_errors(record)


def split_jd_sections(jd_text: str) -> tuple[str, str]:
    """尽力拆分职责和要求；无法可靠拆分时允许返回空串，完整 JD 始终保留。"""
    text = (jd_text or "").strip()
    responsibility_labels = ("岗位职责", "工作职责", "职位职责")
    requirement_labels = ("任职要求", "职位要求", "岗位要求")
    all_labels = (*responsibility_labels, *requirement_labels, "岗位福利", "职位福利", "福利待遇")

    def extract(labels: tuple[str, ...]) -> str:
        match = re.search(rf"(?:^|\n)\s*(?:{'|'.join(map(re.escape, labels))})\s*[：:]?\s*", text)
        if not match:
            return ""
        tail = text[match.end():]
        stops = [
            found.start() for label in all_labels
            if label not in labels
            if (found := re.search(rf"(?:^|\n)\s*{re.escape(label)}\s*[：:]?", tail))
        ]
        return tail[:min(stops)].strip() if stops else tail.strip()

    return extract(responsibility_labels), extract(requirement_labels)


class BossScraper(BaseScraper):
    def __init__(self, root: Path, config: dict[str, Any], store: JsonlStore, logger,
                 debug: bool = False, data_dir: Path | None = None,
                 historical_urls: set[str] | None = None):
        self.root, self.config, self.store, self.logger, self.debug = root, config, store, logger, debug
        self.data_dir = Path(data_dir) if data_dir is not None else root / "data"
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.detail_page: Page | None = None
        self.detail_page_owned = False
        self.detail_page_created = False
        self.captured_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self.screenshot_failed_count = 0
        self.processed_job_count = 0
        self.infrastructure_failed_count = 0
        self.browser_disconnect_count = 0
        self.task_status = "pending"
        self.collected_urls: list[str] = []
        self.completed_job_ids: list[str] = []
        self.pending_urls: list[str] = []
        self.current_index = 0
        self.page_recovery_waiting = False
        self.keyword_summaries: list[dict[str, Any]] = []
        self.invalid_store = JsonlStore(self.data_dir / "invalid_records.jsonl", logger)
        self.invalid_records = self.invalid_store.read_all()
        self._invalid_keys = {self._invalid_key(row) for row in self.invalid_records}
        self._migrate_existing_invalid_records()
        self.seen_urls, self.seen_fallback = store.load_keys()
        self.seen_job_ids = {
            str(row.get("job_id", "")).strip() or self._job_id(str(row.get("url", "")))
            for row in store.read_all()
            if str(row.get("job_id", "")).strip() or self._job_id(str(row.get("url", "")))
        }
        self.historical_urls = {
            store.normalize_url(url) for url in (historical_urls or set()) if str(url).strip()
        }
        self.historical_job_ids = {
            self._job_id(url) for url in self.historical_urls if self._job_id(url)
        }

    def _migrate_existing_invalid_records(self) -> None:
        records = self.store.read_all()
        valid_records: list[dict[str, Any]] = []
        migrated = 0
        for record in records:
            if validate_job_record(record):
                valid_records.append(record)
                continue
            invalid = dict(record)
            invalid.update({
                "invalid_reason": "; ".join(job_record_errors(record)),
                "invalid_source": "migrated_from_jobs_jsonl",
                "invalidated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            self._append_invalid(invalid)
            migrated += 1
        if migrated:
            self.store.write_all(valid_records)
            self.logger.warning("已将 %d 条空壳/无效记录迁移到 Invalid_Records", migrated)

    @staticmethod
    def _invalid_key(record: dict[str, Any]) -> str:
        return "|".join((
            str(record.get("url", "")), str(record.get("captured_at", "")),
            str(record.get("invalid_reason", "")), str(record.get("search_keyword", "")),
        ))

    def _append_invalid(self, record: dict[str, Any]) -> None:
        key = self._invalid_key(record)
        if key in self._invalid_keys:
            return
        self.invalid_store.append(record)
        self.invalid_records.append(record)
        self._invalid_keys.add(key)

    def _connect(self) -> BrowserContext:
        self.playwright = sync_playwright().start()
        self.browser, self.context = connect_cdp(
            self.playwright, str(self.config.get("cdp_url", "http://127.0.0.1:9222"))
        )
        return self.context

    @property
    def task_state_path(self) -> Path:
        return self.data_dir / "task_state.json"

    def _write_task_state(self, **values: Any) -> None:
        payload = {
            "collected_urls": list(dict.fromkeys(self.collected_urls)),
            "completed_job_ids": list(dict.fromkeys(self.completed_job_ids)),
            "pending_urls": list(self.pending_urls),
            "current_index": self.current_index,
            "valid_count": self.captured_count,
            "invalid_count": self.failed_count,
            "infrastructure_failed_count": self.infrastructure_failed_count,
            "browser_disconnect_count": self.browser_disconnect_count,
            "pending_count": len(self.pending_urls),
            "task_status": self.task_status,
        }
        payload.update(values)
        self.task_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.task_state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.task_state_path)

    # 统一平台接口；现有run流程仍调用已验证的内部实现。
    def bind_page(self) -> Page:
        context = self.context or self._connect()
        return self._wait_and_get_boss_page(context)

    def search_keyword(self, page: Page, keyword: str) -> Page:
        return self._search_keyword(page, keyword)

    def validate_search_results(self, page: Page, keyword: str) -> tuple[bool, str]:
        return is_valid_search_results_page(page, keyword)

    def collect_job_urls(self, page: Page, limit: int) -> list[str]:
        keyword = ""
        try:
            keyword = page.locator(
                'input[name="query"][placeholder*="搜索职位"]'
            ).first.input_value().strip()
        except Exception:
            pass
        return self._collect_job_urls(page, limit, keyword)

    def extract_job_detail(self, page: Page, job_url: str, rank: int, keyword: str,
                           keyword_run_id: str) -> dict[str, Any]:
        record, _ = self._extract_detail_record(page, job_url, rank, keyword, keyword_run_id)
        return record

    def validate_record(self, record: dict[str, Any]) -> bool:
        return validate_job_record(record)

    def save_screenshot(self, page: Page, record: dict[str, Any], rank: int, keyword: str) -> None:
        self._save_valid_detail(page, record, rank, keyword)

    def run(self, keywords: list[str], limit: int) -> dict[str, Any]:
        self._wait_for_cdp_chrome()
        context = self._connect()
        search_page = self._wait_and_get_boss_page(context)
        search_page.bring_to_front()
        self._log_search_page("绑定后", search_page)
        self.task_status = "running"
        self._write_task_state(current_keyword="", target_count=limit)
        try:
            return self._run_keywords(search_page, keywords, limit)
        finally:
            self._close_owned_detail_page()

    def ensure_runtime_pages(self, search_page: Page) -> tuple[Page, Page]:
        """每个职位前校验实时 CDP 连接和两个 Page，不只信任旧引用。"""
        if not get_cdp_pages(timeout=1.0):
            raise RuntimeInfrastructureError("9222专用Chrome不存在或CDP不可用")
        if self.browser is None:
            raise RuntimeInfrastructureError("browser尚未连接")
        try:
            if not self.browser.is_connected():
                raise RuntimeInfrastructureError("browser disconnected")
        except RuntimeInfrastructureError:
            raise
        except Exception as exc:
            raise RuntimeInfrastructureError(f"browser连接状态不可读：{exc}") from exc
        if self.context is None:
            raise RuntimeInfrastructureError("context不存在")
        try:
            list(self.context.pages)
        except Exception as exc:
            raise RuntimeInfrastructureError(f"context closed：{exc}") from exc
        try:
            if search_page.is_closed() or not is_boss_page_url(search_page.url):
                raise RuntimeInfrastructureError("search_page已关闭或不再是BOSS普通页面")
        except RuntimeInfrastructureError:
            raise
        except Exception as exc:
            raise RuntimeInfrastructureError(f"search_page状态不可读：{exc}") from exc
        detail_page = self._ensure_detail_page(search_page)
        try:
            if detail_page.is_closed():
                raise RuntimeInfrastructureError("detail_page已关闭")
        except RuntimeInfrastructureError:
            raise
        except Exception as exc:
            raise RuntimeInfrastructureError(f"detail_page状态不可读：{exc}") from exc
        return search_page, detail_page

    def _disconnect_worker_connection(self) -> None:
        """仅断开worker自己的Playwright连接，绝不关闭Chrome/context/用户页。"""
        self.detail_page = None
        self.detail_page_owned = False
        self.detail_page_created = False
        self.context = None
        self.browser = None
        playwright, self.playwright = self.playwright, None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                self.logger.debug("断开旧Playwright连接失败（Chrome保持运行）：%s", exc)

    def _reconnect_runtime(self) -> tuple[Page, Page, bool]:
        self.task_status = "reconnecting_browser"
        self._write_task_state(
            task_status=self.task_status, error_message="正在重新连接9222专用Chrome"
        )
        self._disconnect_worker_connection()
        chrome_restarted = ensure_dedicated_chrome_running()
        if chrome_restarted:
            self.page_recovery_waiting = True
            self.task_status = "waiting_for_login"
            self._write_task_state(
                task_status=self.task_status,
                error_message="专用Chrome已重启，请完成BOSS登录后点击继续",
            )
            self.logger.warning("专用Chrome已重启，请完成BOSS登录后点击继续")
        context = self._connect()
        search_page = self._recover_boss_search_page(None, "运行时重连")
        detail_page = self._ensure_detail_page(search_page)
        self.page_recovery_waiting = False
        self.task_status = "running"
        self._write_task_state(task_status=self.task_status, error_message="")
        return search_page, detail_page, chrome_restarted

    def _reconnect_search_runtime(self) -> Page:
        """搜索阶段的fresh CDP重连；没有收集到URL前不创建detail_page。"""
        self.task_status = "reconnecting_browser"
        self._write_task_state(
            task_status=self.task_status, error_message="正在重新连接BOSS搜索页"
        )
        self._disconnect_worker_connection()
        restarted = ensure_dedicated_chrome_running()
        if restarted:
            self.task_status = "waiting_for_login"
            self.page_recovery_waiting = True
            self._write_task_state(
                task_status=self.task_status,
                error_message="专用Chrome已重启，请完成BOSS登录后点击继续",
            )
        self._connect()
        search_page = self._recover_boss_search_page(None, "搜索阶段重连")
        self.page_recovery_waiting = False
        self.task_status = "running"
        self._write_task_state(task_status=self.task_status, error_message="")
        return search_page

    def _pause_and_reconnect(self, reason: str, *, keyword: str, rank: int) -> tuple[Page, Page]:
        self.infrastructure_failed_count += 1
        self.task_status = "paused_browser_lost"
        self.page_recovery_waiting = True
        self._write_task_state(
            current_keyword=keyword, current_rank=rank,
            error_message=reason, task_status=self.task_status,
        )
        self.logger.error(
            "浏览器连接已丢失：%s；已完成=%d 剩余=%d，任务已暂停",
            reason, self.processed_job_count, len(self.pending_urls),
        )
        while True:
            try:
                input("修复浏览器后点击“重新连接并继续”，或停止并保留当前结果：")
            except EOFError as exc:
                raise KeyboardInterrupt from exc
            self.logger.info("收到重新连接指令，从当前pending URL继续")
            try:
                search_page, detail_page, restarted = self._reconnect_runtime()
                if restarted:
                    self.logger.warning("专用Chrome已重启，请确认BOSS登录状态")
                self.page_recovery_waiting = False
                self.task_status = "running"
                self._write_task_state(
                    current_keyword=keyword, current_rank=rank,
                    error_message="", task_status=self.task_status,
                )
                return search_page, detail_page
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.logger.error("重新连接仍失败，任务继续保持暂停：%s", exc)
                self._write_task_state(error_message=str(exc), task_status=self.task_status)

    def _log_search_page(self, step: str, search_page: Page) -> None:
        try:
            closed = search_page.is_closed()
            url = search_page.url
        except Exception as exc:
            closed, url = True, f"<读取失败：{exc}>"
        self.logger.info(
            "search_page[%s] url=%s id=%s closed=%s", step, url, id(search_page), closed
        )

    def _recover_boss_search_page(self, current_page: Page | None, step: str) -> Page:
        excluded_ids: set[int] = set()
        context = self._usable_context()
        if current_page is not None:
            try:
                if not current_page.is_closed() and is_boss_page_url(current_page.url):
                    current_page.bring_to_front()
                    self._log_search_page(step, current_page)
                    return current_page
            except Exception as exc:
                excluded_ids.add(id(current_page))
                self.logger.warning("旧BOSS Page对象已失效，开始自动恢复：%s", exc)
        selected = None
        created = False
        last_error = ""
        for _attempt in range(2):
            selected = self._select_recovery_page(context, excluded_ids)
            if selected is None and not created:
                self.logger.warning("BOSS：重新绑定中，正在专用Chrome中新建BOSS页面")
                try:
                    open_cdp_tab(BOSS_URL)
                    created = True
                except Exception as exc:
                    raise BossPageUnavailableError(f"无法新建BOSS页面：{exc}") from exc
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    selected = self._select_recovery_page(context, excluded_ids)
                    if selected is not None:
                        break
                    time.sleep(0.25)
            if selected is None:
                continue
            try:
                selected.set_default_timeout(int(self.config.get("timeout_ms", 10000)))
                selected.bring_to_front()
                url = selected.url
                if selected.is_closed() or not is_boss_page_url(url):
                    raise RuntimeError("新绑定页面已关闭或URL无效")
                break
            except Exception as exc:
                last_error = str(exc)
                excluded_ids.add(id(selected))
                selected = None
        if selected is None:
            raise BossPageUnavailableError(
                f"自动恢复后仍未找到可用BOSS普通Page：{last_error or '没有候选页面'}"
            )

        if self._boss_page_needs_login(selected):
            self.page_recovery_waiting = True
            self.logger.warning("BOSS：等待登录。请在BOSS页面完成登录后点击继续")
            try:
                input("请在BOSS页面完成登录后点击继续：")
            except EOFError as exc:
                raise RuntimeInfrastructureError("人工处理输入通道已关闭") from exc
            self.page_recovery_waiting = False
            return self._recover_boss_search_page(selected, f"{step}-登录后校验")

        self.logger.info("已重新绑定BOSS页面：%s", selected.url)
        self._log_search_page(step, selected)
        return selected

    @staticmethod
    def _select_recovery_page(context: BrowserContext, excluded_ids: set[int]) -> Page | None:
        candidates: list[Page] = []
        try:
            pages = list(context.pages)
        except Exception:
            return None
        for page in pages:
            if id(page) in excluded_ids:
                continue
            try:
                if not page.is_closed() and is_boss_page_url(page.url):
                    candidates.append(page)
            except Exception:
                continue
        return min(candidates, key=lambda page: boss_page_priority(page.url), default=None)

    def _usable_context(self) -> BrowserContext:
        if self.context is not None:
            try:
                list(self.context.pages)
                return self.context
            except Exception as exc:
                self.logger.warning("原CDP context已失效，尝试重新连接：%s", exc)
        try:
            if self.playwright is None:
                return self._connect()
            self.browser, self.context = connect_cdp(
                self.playwright, str(self.config.get("cdp_url", "http://127.0.0.1:9222"))
            )
            return self.context
        except Exception as exc:
            raise BossPageUnavailableError(f"浏览器context不可用：{exc}") from exc

    @staticmethod
    def _boss_page_needs_login(page: Page) -> bool:
        try:
            path = urlparse(page.url or "").path.lower()
            if "/login" in path or "/passport/" in path or "/verify" in path:
                return True
        except Exception:
            return True
        for selector in (
            'input[placeholder*="手机号"]', 'input[placeholder*="手机号码"]',
            '[class*="login-register"]', '[class*="login-dialog"]',
            'a[ka*="header-login"]', 'a[href*="/web/user/"][href*="login"]',
            'button:has-text("登录")',
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _wait_for_cdp_chrome() -> None:
        input("请先使用指定命令启动Chrome并登录BOSS，然后按Enter继续。")

    def _wait_and_get_boss_page(self, context: BrowserContext) -> Page:
        self.context = context
        page = self._recover_boss_search_page(None, "初始绑定")
        self.logger.info("已绑定BOSS页面")
        return page

    def _log_and_get_boss_page(self, context: BrowserContext) -> Page | None:
        pages = list(context.pages)
        self.logger.info("当前标签页总数：%d", len(pages))
        for index, candidate in enumerate(pages, 1):
            try:
                url = candidate.url
            except Exception as exc:
                url = f"<无法读取：{exc}>"
            self.logger.info("标签页 %d URL：%s", index, url)
        page = get_boss_page(context)
        self.logger.info("最终选择的BOSS页面URL：%s", page.url if page else "未找到")
        return page

    def _run_keywords(self, search_page: Page, keywords: list[str], limit: int) -> dict[str, Any]:
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for keyword in keywords:
            keyword_dir = safe_directory_name(keyword)
            (self.data_dir / "screenshots" / keyword_dir).mkdir(parents=True, exist_ok=True)
            (self.data_dir / "html" / keyword_dir).mkdir(parents=True, exist_ok=True)
        for keyword_index, keyword in enumerate(keywords, 1):
            if keyword_index > 1:
                delay = random.uniform(
                    float(self.config.get("keyword_wait_seconds_min", 8)),
                    float(self.config.get("keyword_wait_seconds_max", 15)),
                )
                self.logger.info("切换关键词前等待 %.1f 秒", delay)
                time.sleep(delay)
            summary = {
                "search_keyword": keyword, "target_count": limit,
                "city": str(self.config.get("city", "")), "captured_count": 0,
                "duplicate_count": 0, "failed_count": 0,
                "historical_skipped_count": 0,
                "processed_count": 0, "valid_count": 0, "invalid_count": 0,
                "screenshot_failed_count": 0, "status": "pending", "error_message": "",
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"), "finished_at": "",
            }
            self.keyword_summaries.append(summary)
            keyword_run_id = f"{run_stamp}_{keyword_index:03d}"
            recovery_attempts = 0
            screenshot_failures_before = self.screenshot_failed_count
            while True:
                try:
                    summary["status"] = "searching"
                    search_page = self._execute_keyword(
                        search_page, keyword, keyword_index, len(keywords), limit,
                        summary, keyword_run_id,
                    )
                    if summary["status"] not in {"historical_skipped", "no_new_jobs"}:
                        summary["status"] = "completed"
                    break
                except KeyboardInterrupt:
                    paused = self.task_status == "paused_browser_lost"
                    summary["status"] = "stopped"
                    summary["error_message"] = (
                        "浏览器连接丢失后停止，已保留当前结果"
                        if paused else "用户停止"
                    )
                    summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    if paused:
                        self._write_task_state(
                            task_status="paused_browser_lost",
                            error_message=summary["error_message"],
                        )
                    self.logger.info(
                        "关键词执行结果：keyword=%s status=stopped processed=%d valid=%d "
                        "invalid=%d error=用户停止",
                        keyword, summary["processed_count"], summary["valid_count"],
                        summary["invalid_count"],
                    )
                    raise
                except BossPageUnavailableError:
                    summary["failed_count"] += 1
                    self.failed_count += 1
                    summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    raise
                except StopScanError:
                    summary["failed_count"] += 1
                    self.failed_count += 1
                    summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    raise
                except SearchValidationError as exc:
                    summary["status"] = "failed"
                    summary["error_message"] = exc.reason
                    summary["failed_count"] += 1
                    self.failed_count += 1
                    self.logger.error(
                        "关键词搜索失败：%s 原因=%s processed=0", keyword, exc.reason
                    )
                    break
                except Exception as exc:
                    if is_infrastructure_error(exc) and recovery_attempts < 1:
                        recovery_attempts += 1
                        self.logger.warning(
                            "检测到浏览器基础设施异常，暂停当前关键词并fresh reconnect：%s", exc
                        )
                        search_page = self._reconnect_search_runtime()
                        self.logger.info("BOSS页面恢复成功，重新执行当前关键词：%s", keyword)
                        continue
                    if is_infrastructure_error(exc):
                        summary["failed_count"] += 1
                        self.failed_count += 1
                        summary["finished_at"] = datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        )
                        raise BossPageUnavailableError(
                            f"BOSS页面恢复后再次失效，停止任务并等待人工处理：{exc}"
                        ) from exc
                    summary["failed_count"] += 1
                    self.failed_count += 1
                    summary["status"] = "failed"
                    summary["error_message"] = str(exc)
                    self.logger.exception("关键词 %s 执行失败，继续下一个：%s", keyword, exc)
                    self._debug_failure(
                        search_page, keyword_index, keyword=keyword, prefix="search_failure"
                    )
                    break
            summary["screenshot_failed_count"] = (
                self.screenshot_failed_count - screenshot_failures_before
            )
            summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self.logger.info(
                "关键词执行结果：keyword=%s status=%s processed=%d valid=%d invalid=%d error=%s",
                keyword, summary["status"], summary["processed_count"],
                summary["valid_count"], summary["invalid_count"], summary["error_message"],
            )
        statuses = [str(item.get("status", "failed")) for item in self.keyword_summaries]
        successful_statuses = {"completed", "historical_skipped", "no_new_jobs"}
        if "stopped" in statuses:
            task_status = "stopped"
        elif statuses and all(status in successful_statuses for status in statuses):
            task_status = "completed"
        elif any(status in successful_statuses for status in statuses):
            task_status = "partial_failed"
        else:
            task_status = "failed"
        self.task_status = task_status
        self._write_task_state(task_status=task_status, current_keyword="", current_rank=0)
        return {
            "captured": self.captured_count, "skipped": self.skipped_count,
            "failed": self.failed_count, "status": task_status,
            "infrastructure_failed_count": self.infrastructure_failed_count,
            "browser_disconnect_count": self.browser_disconnect_count,
            "pending_count": len(self.pending_urls),
            "message": f"执行 {len(keywords)} 个关键词",
        }

    def _execute_keyword(self, search_page: Page, keyword: str, keyword_index: int,
                         keyword_total: int, limit: int, summary: dict[str, Any],
                         keyword_run_id: str) -> Page:
        search_page = self._recover_boss_search_page(search_page, "关键词开始前校验")
        self._log_search_page("关键词开始", search_page)
        self.logger.info("[%d/%d] 自动搜索关键词：%s", keyword_index, keyword_total, keyword)
        rebound_page = self._search_keyword(search_page, keyword)
        if rebound_page is not None:
            search_page = rebound_page
        self._apply_configured_filters(search_page)
        summary["status"] = "collecting_urls"
        valid_search, validation_reason = is_valid_search_results_page(search_page, keyword)
        if not valid_search:
            raise SearchValidationError(keyword, validation_reason)
        self.logger.info("collect_job_urls前二次校验通过：keyword=%s", keyword)
        job_urls = self._collect_job_urls(search_page, limit, keyword)
        search_page = getattr(self, "_collection_search_page", search_page)
        if not job_urls:
            inspect_results_dom(search_page, self.data_dir / "debug")
            summary["status"] = "no_new_jobs"
            self.logger.info(
                "关键词无新增岗位：keyword=%s status=no_new_jobs reason=搜索成功但结果页没有可采集职位URL",
                keyword,
            )
            return search_page

        eligible_urls: list[str] = []
        current_duplicates = 0
        historical_skipped = 0
        new_only = str(self.config.get("save_mode", "snapshot")) == "new_only"
        for job_url in job_urls:
            normalized = self.store.normalize_url(job_url)
            job_id = self._job_id(job_url)
            if normalized in self.seen_urls or (job_id and job_id in self.seen_job_ids):
                self.store.add_matched_keyword(url=job_url, job_id=job_id, keyword=keyword)
                current_duplicates += 1
                self.skipped_count += 1
                continue
            if new_only and (
                normalized in self.historical_urls
                or (job_id and job_id in self.historical_job_ids)
            ):
                historical_skipped += 1
                self.skipped_count += 1
                continue
            eligible_urls.append(job_url)
        summary["duplicate_count"] += current_duplicates
        summary["historical_skipped_count"] += historical_skipped
        if not eligible_urls:
            if historical_skipped:
                summary["status"] = "historical_skipped"
                self.logger.info(
                    "关键词无新增岗位：keyword=%s status=historical_skipped historical=%d current_duplicate=%d",
                    keyword, historical_skipped, current_duplicates,
                )
            else:
                summary["status"] = "no_new_jobs"
                self.logger.info(
                    "关键词无新增岗位：keyword=%s status=no_new_jobs historical=0 current_duplicate=%d",
                    keyword, current_duplicates,
                )
            return search_page

        summary["status"] = "collecting_details"
        detail_page = self._ensure_detail_page(search_page)
        self.collected_urls.extend(
            url for url in eligible_urls if url not in self.collected_urls
        )
        self.pending_urls = list(eligible_urls)
        self._write_task_state(
            current_keyword=keyword, current_rank=0, target_count=len(eligible_urls),
            task_status="running",
        )
        runtime_checks_enabled = self.browser is not None and self.playwright is not None
        for rank, job_url in enumerate(eligible_urls, 1):
            self.current_index = rank
            self.pending_urls = eligible_urls[rank - 1:]
            self._write_task_state(
                current_keyword=keyword, current_rank=rank, task_status="running"
            )
            infrastructure_errors = 0
            while True:
                try:
                    if runtime_checks_enabled:
                        search_page, detail_page = self.ensure_runtime_pages(search_page)
                    status = self._capture_detail(
                        detail_page, job_url, rank, keyword, keyword_run_id
                    )
                    break
                except RuntimeInfrastructureError as exc:
                    infrastructure_errors += 1
                    self.browser_disconnect_count += 1
                    self.logger.error(
                        "职位[%d]遇到浏览器基础设施异常（%d/2）：%s",
                        rank, infrastructure_errors, exc,
                    )
                    if infrastructure_errors == 1:
                        try:
                            search_page, detail_page, _ = self._reconnect_runtime()
                            self.logger.info("浏览器运行时恢复成功，对当前职位重试一次")
                            continue
                        except Exception as reconnect_exc:
                            if not is_infrastructure_error(reconnect_exc) and not isinstance(
                                reconnect_exc, (BossPageUnavailableError, PlaywrightError)
                            ):
                                raise
                            infrastructure_errors = 2
                            exc = RuntimeInfrastructureError(str(reconnect_exc))
                    search_page, detail_page = self._pause_and_reconnect(
                        str(exc), keyword=keyword, rank=rank
                    )
                    infrastructure_errors = 0
                    runtime_checks_enabled = True
            summary["processed_count"] += 1
            self.processed_job_count += 1
            job_id = self._job_id(job_url) or self.store.normalize_url(job_url)
            if job_id and job_id not in self.completed_job_ids:
                self.completed_job_ids.append(job_id)
            self.pending_urls = eligible_urls[rank:]
            if status == "captured":
                summary["captured_count"] += 1
                summary["valid_count"] += 1
            else:
                summary["failed_count"] += 1
                summary["invalid_count"] += 1
            self._write_task_state(
                current_keyword=keyword, current_rank=rank, task_status="running"
            )
        return search_page

    def _search_keyword(self, page: Page, keyword: str) -> Page:
        last_reason = ""
        for attempt in (1, 2):
            page = self._recover_boss_search_page(page, f"第{attempt}次搜索-浮层处理前")
            self._pause_if_abnormal(page)
            previous_job_ids = set(_result_job_ids(page))
            overlay_result = dismiss_overlays(page)
            page = self._recover_boss_search_page(page, f"第{attempt}次搜索-浮层处理后")
            self.logger.info(
                "是否检测到overseas-nav-box：%s", "是" if overlay_result["detected"] else "否"
            )
            page = self._recover_boss_search_page(page, f"第{attempt}次搜索-搜索框定位前")
            search_input = self._find_search_input(page)
            if search_input is None:
                self._save_search_input_debug(page, keyword)
                raise StopScanError("未找到固定search_page上的搜索框，已保存debug现场并停止")
            search_input.wait_for(
                state="visible", timeout=int(self.config.get("timeout_ms", 10000))
            )
            self.logger.info("搜索框最终选择器：input[name=\"query\"][placeholder*=\"搜索职位\"]")
            self.logger.info("输入的关键词：%s", keyword)
            search_selector = 'input[name="query"][placeholder*="搜索职位"]'
            page = self._recover_boss_search_page(page, f"第{attempt}次搜索-清空关键词前")
            page.locator(search_selector).first.wait_for(state="visible", timeout=2500)
            page.locator(search_selector).first.fill("")
            page = self._recover_boss_search_page(page, f"第{attempt}次搜索-关键词输入前")
            page.locator(search_selector).first.wait_for(state="visible", timeout=2500)
            page.locator(search_selector).first.fill(keyword)
            page = self._recover_boss_search_page(page, f"第{attempt}次搜索-提交前")
            dismiss_overlays(page)
            button_selector, button = self._find_search_button(page)
            if button is None:
                self._save_search_input_debug(page, keyword)
                raise StopScanError("未找到可见的真实搜索按钮，已保存debug现场并停止")
            before = urlparse(page.url)
            self.logger.info("提交前URL：%s", before.path or "/")
            self.logger.info("实际点击的搜索按钮选择器：%s", button_selector)
            button.click()
            state = self._wait_for_keyword_confirmation(
                page, keyword, previous_job_ids, timeout_ms=15000
            )
            if state.get("confirmed"):
                self._pause_if_abnormal(page)
                self._log_keyword_confirmation(keyword, state)
                return page
            _, last_reason = is_valid_search_results_page(page, keyword, previous_job_ids)
            self.logger.error(
                "第%d次搜索在15秒内未确认：%s（path=%s query=%s input=%s evidence=%s）",
                attempt, last_reason, state.get("path", ""), state.get("url_query", ""),
                state.get("input_value", ""), state.get("confirmation_count", 0),
            )
            if attempt == 1:
                page = self._recover_boss_search_page(page, "搜索失败后重新绑定")
        raise SearchValidationError(
            keyword, last_reason or "未进入关键词搜索结果页，已阻止采集首页推荐岗位"
        )

    @staticmethod
    def _wait_for_keyword_confirmation(page: Page, keyword: str,
                                       previous_job_ids: set[str],
                                       timeout_ms: int = 15000) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        state: dict[str, Any] = {}
        while True:
            state = inspect_search_results_state(page, keyword, previous_job_ids)
            if state.get("confirmed"):
                return state
            if time.monotonic() >= deadline:
                return state
            page.wait_for_timeout(500)

    def _log_keyword_confirmation(self, keyword: str, state: dict[str, Any]) -> None:
        self.logger.info("关键词已确认：%s", keyword)
        self.logger.info("确认依据：")
        self.logger.info("- URL query匹配：%s", "是" if state.get("url_query_matches") else "否")
        self.logger.info("- 搜索框value匹配：%s", "是" if state.get("input_matches") else "否")
        self.logger.info(
            "- 结果区域关键词匹配：%s", "是" if state.get("result_keyword_matches") else "否"
        )
        self.logger.info("- 搜索结果列表出现：%s", "是" if state.get("has_results") else "否")
        self.logger.info("- 职位列表已更新：%s", "是" if state.get("job_list_updated") else "否")

    def _wait_for_results_signal(self, page: Page) -> bool:
        try:
            page.wait_for_function(
                r"""selectors => {
                  const visible=el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
                    return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
                  return selectors.some(selector => { try {
                    return [...document.querySelectorAll(selector)].some(visible); } catch (_) { return false; } });
                }""",
                arg=SELECTORS["result_ready"], timeout=int(self.config.get("navigation_timeout_ms", 15000)),
            )
            return True
        except PlaywrightTimeoutError:
            return False

    def _find_search_input(self, page: Page) -> Locator | None:
        locator = page.locator('input[name="query"][placeholder*="搜索职位"]').first
        try:
            locator.wait_for(state="visible", timeout=2500)
            return locator
        except Exception as exc:
            self.logger.debug("固定search_page搜索框定位失败：%s", exc)
            return None

    def _find_search_button(self, page: Page) -> tuple[str, Locator | None]:
        for selector in SELECTORS["search_button"]:
            try:
                locators = page.locator(selector)
                for index in range(locators.count()):
                    candidate = locators.nth(index)
                    if candidate.is_visible():
                        return selector, candidate
            except Exception as exc:
                self.logger.debug("搜索按钮候选失败 %s：%s", selector, exc)
        return "", None

    def _apply_configured_filters(self, page: Page) -> None:
        filters = (("city", "city_trigger", "city_option"),
                   ("experience", "experience_trigger", "filter_option"),
                   ("education", "education_trigger", "filter_option"),
                   ("salary", "salary_trigger", "filter_option"))
        for config_key, trigger_key, option_key in filters:
            value = str(self.config.get(config_key, "")).strip()
            if not value:
                continue
            if self._configured_filter_already_applied(page, config_key, value):
                self.logger.info("筛选已由搜索结果URL满足，跳过重复点击 %s=%s", config_key, value)
                continue
            try:
                trigger = self._first_visible(page, trigger_key)
                if trigger is None:
                    raise RuntimeError(f"未找到 {trigger_key}")
                trigger.click()
                option = self._first_visible(page, option_key, value=value)
                if option is None:
                    raise RuntimeError(f"未找到选项 {value}")
                option.click()
                page.wait_for_timeout(1000)
                self._pause_if_abnormal(page)
                self.logger.info("已应用筛选 %s=%s", config_key, value)
            except Exception as exc:
                self.logger.warning("筛选 %s=%s 未能可靠应用，继续采集：%s", config_key, value, exc)

    @staticmethod
    def _configured_filter_already_applied(page: Page, config_key: str, value: str) -> bool:
        """避免在已限定上海的结果页上再次点击宽泛的“上海”文本。"""
        if config_key != "city" or value != "上海":
            return False
        try:
            parsed = urlparse(page.url)
        except Exception:
            return False
        city_code = parse_qs(parsed.query).get("city", [""])[0]
        return "/web/geek/jobs" in parsed.path and city_code == "101020100"

    def _first_visible(self, page: Page, key: str, value: str = "") -> Locator | None:
        for template in SELECTORS[key]:
            selector = template.format(value=value)
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception as exc:
                self.logger.debug("选择器失败 %s: %s (%s)", key, selector, exc)
        return None

    def _collect_job_urls(self, page: Page, limit: int, keyword: str) -> list[str]:
        """在真实结果列表中滚动读取URL；页面状态丢失时只恢复一次当前关键词。"""
        self._collection_search_page = page
        self._log_search_page("收集URL开始", page)
        urls: list[str] = []
        seen_job_ids: set[str] = set()
        seen_url_keys: set[str] = set()
        stagnant_rounds = 0
        recovery_used = False
        max_rounds = min(max(int(self.config.get("max_scroll_rounds", 30)), 1), 30)

        for scroll_round in range(1, max_rounds + 1):
            valid, reason = is_valid_search_results_page(page, keyword)
            if not valid:
                if recovery_used:
                    self.logger.error("滚动过程中搜索状态再次丢失：%s", reason)
                    raise SearchValidationError(keyword, reason)
                recovery_used = True
                self.logger.warning(
                    "滚动过程中搜索状态丢失，重新执行当前关键词一次：%s", reason
                )
                page = self._search_keyword(page, keyword)
                self._collection_search_page = page
                valid, reason = is_valid_search_results_page(page, keyword)
                if not valid:
                    raise SearchValidationError(keyword, reason)

            before = len(urls)
            items = self._read_result_job_links(page)
            for item in items:
                full = absolute_job_url(page.url, str(item.get("href", "")))
                if not full:
                    continue
                job_id = self._job_id(full)
                url_key = self.store.normalize_url(full)
                if (job_id and job_id in seen_job_ids) or (not job_id and url_key in seen_url_keys):
                    continue
                if job_id:
                    seen_job_ids.add(job_id)
                seen_url_keys.add(url_key)
                urls.append(full)
                self.logger.info(
                    "收集职位URL %d：%s（卡片文本=%s）", len(urls), full,
                    str(item.get("text", ""))[:100].replace("\n", " "),
                )
                if len(urls) >= limit:
                    break

            added = len(urls) - before
            stagnant_rounds = stagnant_rounds + 1 if added == 0 else 0
            self.logger.info("滚动轮次：%d", scroll_round)
            self.logger.info("当前唯一岗位：%d", len(urls))
            self.logger.info("本轮新增：%d", added)
            self.logger.info("目标：%d", limit)
            if len(urls) >= limit:
                break
            if stagnant_rounds >= 3:
                self.logger.info("连续3轮没有新增职位URL，停止滚动")
                break

            scroll_state = self._scroll_job_results(page)
            self.logger.debug(
                "职位列表滚动方式：%s at_end=%s no_more=%s",
                scroll_state.get("mode", "unknown"), scroll_state.get("at_end", False),
                scroll_state.get("no_more", False),
            )
            if scroll_state.get("no_more"):
                self.logger.info("检测到没有更多职位，停止滚动")
                break
            page.wait_for_timeout(random.randint(1000, 2000))
            self._pause_if_abnormal(page)

        self._collection_search_page = page
        self.logger.info("关键词共收集到 %d 个真实job_detail URL", len(urls))
        return urls[:limit]

    def _read_result_job_links(self, page: Page) -> list[dict[str, str]]:
        try:
            values = page.evaluate(
                r"""selectors => {
                  const visible=el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
                    return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
                  const candidates=[];
                  for (const selector of selectors) { try {
                    for (const root of document.querySelectorAll(selector)) {
                      if (!visible(root)) continue;
                      const rect=root.getBoundingClientRect();
                      const links=[...root.querySelectorAll('a[href*="/job_detail/"]')].filter(visible);
                      if (links.length) candidates.push({root,selector,links,rect});
                    }
                  } catch (_) {} }
                  candidates.sort((a,b) => b.links.length-a.links.length || a.rect.left-b.rect.left);
                  let links=candidates.length ? candidates[0].links :
                    [...document.querySelectorAll('a[href*="/job_detail/"]')].filter(a => {
                      if (!visible(a)) return false; const r=a.getBoundingClientRect();
                      return r.left < window.innerWidth*0.72;
                    });
                  return links.map(a => { const r=a.getBoundingClientRect(); return {
                    href:a.href||a.getAttribute('href')||'', text:(a.innerText||'').trim(),
                    top:r.top+window.scrollY, left:r.left};
                  }).sort((a,b) => a.top-b.top || a.left-b.left);
                }""",
                SELECTORS["job_list_scroll_container"],
            )
            return [dict(item) for item in (values or []) if isinstance(item, dict)]
        except Exception as exc:
            self.logger.debug("读取职位列表URL失败：%s", exc)
            return []

    def _scroll_job_results(self, page: Page) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""selectors => {
                  const visible=el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
                    return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
                  const roots=[];
                  for (const selector of selectors) { try {
                    for (const root of document.querySelectorAll(selector)) {
                      if (!visible(root)) continue;
                      const links=root.querySelectorAll('a[href*="/job_detail/"]').length;
                      if (links) roots.push({root,selector,links});
                    }
                  } catch (_) {} }
                  roots.sort((a,b) => b.links-a.links);
                  const root=roots.length ? roots[0].root : null;
                  let target=root;
                  while (target && target!==document.body) {
                    const style=getComputedStyle(target);
                    if (/(auto|scroll)/.test(style.overflowY) && target.scrollHeight>target.clientHeight+20) break;
                    target=target.parentElement;
                  }
                  const markerText=(root?.innerText || document.body?.innerText || '');
                  const noMore=/(没有更多|没有更多了|到底了|暂无更多)/.test(markerText);
                  if (target && target!==document.body) {
                    const atEnd=target.scrollTop+target.clientHeight>=target.scrollHeight-4;
                    target.scrollBy(0, Math.max(target.clientHeight*0.7, 300));
                    return {mode:'container',selector:roots[0]?.selector||'',at_end:atEnd,no_more:noMore};
                  }
                  const atEnd=window.scrollY+window.innerHeight>=document.documentElement.scrollHeight-4;
                  window.scrollBy(0, window.innerHeight*0.8);
                  return {mode:'window',selector:'window',at_end:atEnd,no_more:noMore};
                }""",
                SELECTORS["job_list_scroll_container"],
            )
            return dict(result or {})
        except Exception as exc:
            self.logger.debug("内部列表滚动失败，回退window滚动：%s", exc)
            try:
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            except Exception:
                pass
            return {"mode": "window", "at_end": False, "no_more": False}

    def _ensure_detail_page(self, search_page: Page) -> Page:
        if self.detail_page_created:
            try:
                if self.detail_page is not None and not self.detail_page.is_closed():
                    return self.detail_page
            except Exception:
                pass
            # 只有已有详情页失效时才重建；不会关闭或替换search_page。
            self.detail_page = None
            self.detail_page_owned = False
            self.detail_page_created = False
            self.logger.warning("脚本detail_page已失效，将在当前任务内重建")
        if self.context is None:
            raise RuntimeError("浏览器上下文尚未连接")
        detail_page = self.context.new_page()
        ensure_separate_pages(search_page, detail_page)
        detail_page.set_default_timeout(int(self.config.get("timeout_ms", 10000)))
        self.detail_page = detail_page
        self.detail_page_owned = True
        self.detail_page_created = True
        self.logger.info("已创建脚本专用detail_page，搜索页保持固定：search_id=%s detail_id=%s",
                         id(search_page), id(detail_page))
        return detail_page

    def _capture_detail(self, page: Page, job_url: str, rank: int,
                        keyword: str, keyword_run_id: str) -> str:
        last_record: dict[str, Any] = {"url": job_url, "search_keyword": keyword, "search_rank": rank}
        last_errors: list[str] = []
        for attempt in (1, 2):
            try:
                self.logger.info("详情页第 %d 次加载 [%d]：%s", attempt, rank, job_url)
                page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
                self._pause_if_abnormal(page)
                stable = self._wait_for_detail_stable(page)
                page.wait_for_timeout(800)
                record, selector_hits = self._extract_detail_record(
                    page, job_url, rank, keyword, keyword_run_id
                )
                last_record = record
                last_errors = job_record_errors(record)
                if not stable and not validate_job_record(record):
                    last_errors.insert(0, "详情页内容等待超时")
                self.logger.info("详情页实际命中选择器：%s", selector_hits)
                self.logger.info("详情字段：title=%s salary=%s city=%s experience=%s education=%s jd=%d字",
                                 record["title"], record["salary"], record["city"],
                                 record["experience"], record["education"], len(record["jd_text"]))
                if validate_job_record(record):
                    record["screenshot_status"] = "pending"
                    record["screenshot_error"] = ""
                    self.store.append(record)
                    self.logger.info("有效岗位已写入JSONL：序号=%d URL=%s", rank, job_url)
                    self.seen_urls.add(self.store.normalize_url(job_url))
                    if record.get("job_id"):
                        self.seen_job_ids.add(str(record["job_id"]))
                    fallback = self.store.fallback_key(record["company"], record["title"], record["city"])
                    if fallback:
                        self.seen_fallback.add(fallback)
                    self.captured_count += 1
                    try:
                        self._save_valid_detail(page, record, rank, keyword)
                    except Exception as exc:
                        record["screenshot_path"] = ""
                        record["screenshot_status"] = "failed"
                        record["screenshot_error"] = str(exc)
                        self.screenshot_failed_count += 1
                        self.logger.exception("岗位截图失败：序号=%d URL=%s 原因=%s", rank, job_url, exc)
                    self.store.replace_by_url(record)
                    delay = random.uniform(float(self.config.get("wait_seconds_min", 5)),
                                           float(self.config.get("wait_seconds_max", 10)))
                    self.logger.info("详情采集完成，低频等待 %.1f 秒", delay)
                    time.sleep(delay)
                    return "captured"
                self._save_invalid_detail(page, record, last_errors, rank, keyword, attempt)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if is_infrastructure_error(exc):
                    raise RuntimeInfrastructureError(str(exc)) from exc
                last_errors = [f"详情加载/提取异常：{exc}"]
                self.logger.exception("详情页第 %d 次处理失败：%s", attempt, exc)
                self._save_invalid_detail(page, last_record, last_errors, rank, keyword, attempt)
            if attempt == 1:
                self.logger.warning("详情页首次失败，将对同一URL重试一次；不操作search_page")

        invalid = dict(last_record)
        invalid.update({
            "invalid_reason": "; ".join(dict.fromkeys(last_errors)) or "未知详情采集失败",
            "invalid_source": "detail_capture",
            "invalidated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        self._append_invalid(invalid)
        self.failed_count += 1
        self.logger.error("最终失败岗位：序号=%d URL=%s 原因=%s", rank, job_url,
                          invalid["invalid_reason"])
        return "failed"

    def _wait_for_detail_stable(self, page: Page) -> bool:
        try:
            page.wait_for_function(
                r"""() => {
                  const visible=el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
                    return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
                  const body=(document.body?.innerText||'').trim();
                  return [...document.querySelectorAll('.job-banner .name, .job-banner .salary')]
                    .some(el => visible(el) && (el.innerText||'').trim()) || body.includes('职位描述');
                }""",
                timeout=min(int(self.config.get("detail_stable_timeout_ms", 10000)), 10000),
                polling=500,
            )
            return True
        except PlaywrightTimeoutError:
            self.logger.warning("详情页内容等待超时（最多10秒）：URL=%s", page.url)
            return False

    def _extract_detail_record(self, page: Page, job_url: str, rank: int,
                               keyword: str, keyword_run_id: str) -> tuple[dict[str, Any], dict[str, str]]:
        data = page.evaluate(
            r"""selectors => {
              const visible=el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
                return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
              const clean=value => (value||'').replace(/\u00a0/g,' ').replace(/[ \t]+\n/g,'\n').trim();
              const first=(sels) => { for(const sel of sels){ for(const el of document.querySelectorAll(sel)){
                const text=clean(el.innerText); if(visible(el)&&text) return {text,selector:sel}; } }
                return {text:'',selector:''}; };
              const all=(sels) => { const values=[]; let selector=''; for(const sel of sels){
                for(const el of document.querySelectorAll(sel)){ const text=clean(el.innerText);
                  if(visible(el)&&text&&!values.includes(text)){ values.push(text); selector ||= sel; } }
                if(values.length) break; } return {values,selector}; };
              const longest=(sels) => { let best={text:'',selector:''}; for(const sel of sels){
                for(const el of document.querySelectorAll(sel)){ const text=clean(el.innerText);
                  if(visible(el)&&text.length>best.text.length) best={text,selector:sel}; } } return best; };
              const directText=el => clean([...el.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE)
                .map(n=>n.textContent).join(' '));
              const semanticSection=(label) => {
                const nodes=[...document.querySelectorAll('h1,h2,h3,h4,h5,p,span,div')].filter(visible);
                for(const heading of nodes){ const own=directText(heading)||clean(heading.innerText);
                  if(own!==label && !own.startsWith(label+'：') && !own.startsWith(label+':')) continue;
                  const parent=heading.parentElement;
                  const candidates=[heading.nextElementSibling,
                    parent?.querySelector('.job-sec-text'), parent?.querySelector('[class*="text"]')].filter(Boolean);
                  let best=''; for(const candidate of candidates){ const text=clean(candidate.innerText);
                    if(text.length>best.length) best=text; }
                  if(best.length>=50) return {text:best,selector:'semantic:'+label};
                  if(parent){ const text=clean(parent.innerText).replace(new RegExp('^'+label+'[：:]?\\s*'),'');
                    if(text.length>=50) return {text,selector:'semantic-parent:'+label}; }
                } return {text:'',selector:''}; };
              const body=clean(document.body?.innerText);
              const title=first(selectors.title), salary=first(selectors.salary), basic=first(selectors.basic_info);
              const address=first(selectors.address), company=first(selectors.company);
              const benefits=all(selectors.benefits), recruiter=first(selectors.recruiter);
              const companyInfo=longest(selectors.company_info);
              const companySize=first(selectors.company_size), companyIndustry=first(selectors.company_industry);
              const financingStage=first(selectors.financing_stage);
              let jd=semanticSection('职位描述'); if(!jd.text) jd=longest(selectors.jd);
              let salaryText=salary.text; if(!/\d{1,3}\s*-\s*\d{1,3}K/i.test(salaryText)){
                const m=body.match(/\d{1,3}\s*-\s*\d{1,3}K(?:·\d+薪)?/i); salaryText=m?m[0]:salaryText; }
              return {title:title.text,salary:salaryText,basicInfo:basic.text,address:address.text,
                company:company.text,benefits:benefits.values,jdText:jd.text,recruiter:recruiter.text,
                companyInfo:companyInfo.text,companySize:companySize.text,
                companyIndustry:companyIndustry.text,financingStage:financingStage.text,bodyLength:body.length,
                selectors:{title:title.selector,salary:salary.selector,basic_info:basic.selector,
                  address:address.selector,company:company.selector,benefits:benefits.selector,
                  jd:jd.selector,recruiter:recruiter.selector,company_info:companyInfo.selector,
                  company_size:companySize.selector,company_industry:companyIndustry.selector,
                  financing_stage:financingStage.selector}};
            }""",
            DETAIL_SELECTORS,
        )
        title = self._clean_title(data.get("title", ""))
        salary_match = SALARY_PATTERN.search(str(data.get("salary", "")))
        salary = salary_match.group(0) if salary_match else str(data.get("salary", "")).strip()
        city, district, experience, education = self._parse_basic_info(
            str(data.get("basicInfo", "")), str(data.get("address", ""))
        )
        jd_text = str(data.get("jdText", "")).strip()
        responsibilities, requirements = split_jd_sections(jd_text)
        company_info = str(data.get("companyInfo", "")).strip()
        size_text = str(data.get("companySize", "")).strip()
        stage_text = str(data.get("financingStage", "")).strip()
        company_size = (COMPANY_SIZE_PATTERN.search(size_text or company_info) or [""])[0]
        financing_stage = (FINANCING_PATTERN.search(stage_text or company_info) or [""])[0]
        company_industry = self._clean_company_fact(data.get("companyIndustry", "")) or \
            self._parse_company_industry(company_info, company_size, financing_stage)
        record = {
            "job_id": self._job_id(job_url), "platform": "boss", "search_keyword": keyword,
            "search_rank": rank, "title": title, "company": self._clean_company(data.get("company", "")),
            "salary": salary, "city": city, "district": district, "experience": experience,
            "education": education, "benefits": data.get("benefits", []),
            "responsibilities": responsibilities, "requirements": requirements, "jd_text": jd_text,
            "recruiter": str(data.get("recruiter", "")).strip(), "company_size": company_size,
            "company_industry": company_industry, "financing_stage": financing_stage, "url": job_url,
            "screenshot_path": "", "screenshot_status": "", "screenshot_error": "", "html_path": "",
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "keyword_run_id": keyword_run_id, "matched_keywords": [keyword],
        }
        return record, dict(data.get("selectors", {}))

    @staticmethod
    def _clean_title(value: Any) -> str:
        lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        for line in lines:
            if not SALARY_PATTERN.fullmatch(line) and line not in {"在线", "急聘"}:
                return SALARY_PATTERN.sub("", line).strip(" -·")
        return ""

    @staticmethod
    def _clean_company(value: Any) -> str:
        lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        return lines[0] if lines else ""

    @staticmethod
    def _clean_company_fact(value: Any) -> str:
        lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        return lines[-1] if lines else ""

    @staticmethod
    def _parse_basic_info(basic_info: str, address: str) -> tuple[str, str, str, str]:
        combined = "\n".join((basic_info or "", address or ""))
        city_match = re.search(r"(?:^|[\s·•])((?:北京|上海|天津|重庆|广州|深圳|杭州|苏州|南京|成都|武汉|西安|长沙|合肥|宁波|厦门|青岛|济南|郑州|东莞|佛山))(?:市)?(?:[\s·•]|$)", combined)
        district_match = re.search(r"([\u4e00-\u9fa5]{1,8}(?:新区|区|县))", address or basic_info or "")
        experience_match = EXPERIENCE_PATTERN.search(basic_info or "")
        education_match = EDUCATION_PATTERN.search(basic_info or "")
        city = city_match.group(1) if city_match else ""
        district = district_match.group(1) if district_match else ""
        if city and district.startswith(city):
            district = district[len(city):]
        return (
            city,
            district,
            experience_match.group(0) if experience_match else "",
            education_match.group(0) if education_match else "",
        )

    @staticmethod
    def _parse_company_industry(company_info: str, company_size: str, financing_stage: str) -> str:
        excluded = {
            company_size, financing_stage, "公司信息", "公司基本信息", "工商信息", "查看全部职位",
        }
        lines = [line.strip() for line in company_info.splitlines() if line.strip()]
        for line in lines:
            if line in excluded or COMPANY_SIZE_PATTERN.fullmatch(line) or FINANCING_PATTERN.fullmatch(line):
                continue
            if 2 <= len(line) <= 30 and not re.search(r"招聘|地址|成立|法定代表|注册", line):
                return line
        return ""

    def _save_valid_detail(self, page: Page, record: dict[str, Any], rank: int, keyword: str) -> None:
        keyword_dir = safe_directory_name(keyword)
        stem = safe_filename(f"{rank:04d}_{record['company']}_{record['title']}")
        screenshot = self.data_dir / "screenshots" / keyword_dir / f"{stem}.png"
        html_file = self.data_dir / "html" / keyword_dir / f"{stem}.html"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        html_file.parent.mkdir(parents=True, exist_ok=True)
        record["screenshot_path"] = ""
        record["screenshot_status"] = "failed"
        record["screenshot_error"] = ""
        if "/job_detail/" not in urlparse(page.url).path:
            error = f"截图前页面已离开真实职位详情：{page.url}"
            record["screenshot_error"] = error
            self.screenshot_failed_count += 1
            self.logger.error("岗位截图失败：序号=%d URL=%s 原因=%s", rank, record.get("url", ""), error)
        else:
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(300)
            except Exception as exc:
                self.logger.warning("截图前回到页面顶部失败，将继续截图：%s", exc)
            full_error = ""
            try:
                page.screenshot(
                    path=str(screenshot), full_page=True, timeout=20000, animations="disabled"
                )
            except Exception as exc:
                full_error = str(exc)
                self.logger.warning("全页截图失败，降级为视口截图：%s", exc)
                try:
                    page.screenshot(
                        path=str(screenshot), full_page=False, timeout=10000,
                        animations="disabled",
                    )
                except Exception as fallback_exc:
                    error = f"全页截图失败：{full_error}；视口截图失败：{fallback_exc}"
                    record["screenshot_error"] = error
                    self.screenshot_failed_count += 1
                    self.logger.error("岗位截图失败：序号=%d URL=%s 原因=%s",
                                      rank, record.get("url", ""), error)
                else:
                    record["screenshot_status"] = "success"
            else:
                record["screenshot_status"] = "success"

            if record["screenshot_status"] == "success":
                try:
                    record["screenshot_path"] = str(screenshot.relative_to(self.root))
                except ValueError:
                    record["screenshot_path"] = str(screenshot)
                self.logger.info("已保存真实详情页截图：%s", screenshot)

        try:
            html_file.write_text(page.content(), encoding="utf-8")
            try:
                record["html_path"] = str(html_file.relative_to(self.root))
            except ValueError:
                record["html_path"] = str(html_file)
        except Exception as exc:
            record["html_path"] = ""
            self.logger.warning("详情HTML保存失败（岗位数据已保留）：%s", exc)

    def _save_invalid_detail(self, page: Page, record: dict[str, Any], errors: list[str],
                             rank: int, keyword: str, attempt: int) -> None:
        folder = self.data_dir / "debug" / safe_directory_name(keyword or "general")
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"invalid_detail_{rank:04d}_attempt{attempt}"
        debug_screenshot = folder / f"{stem}.png"
        debug_html = folder / f"{stem}.html"
        metadata = folder / f"{stem}.json"
        try:
            page.screenshot(path=str(debug_screenshot), full_page=True)
        except Exception as exc:
            self.logger.warning("无效详情截图失败：%s", exc)
        try:
            debug_html.write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            self.logger.warning("无效详情HTML保存失败：%s", exc)
        metadata.write_text(json.dumps({
            "target_url": record.get("url", ""), "final_url": getattr(page, "url", ""),
            "errors": errors, "record": record,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger.warning("详情尝试未通过验证：第%d次，原因=%s", attempt, "; ".join(errors))

    def _save_search_input_debug(self, page: Page, keyword: str) -> None:
        folder = self.data_dir / "debug" / safe_directory_name(keyword or "general")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"search_input_missing_{stamp}"
        try:
            title = page.title()
        except Exception as exc:
            title = f"<读取失败：{exc}>"
        try:
            inputs = page.locator("input").evaluate_all(
                "els => els.map(e => ({placeholder:e.getAttribute('placeholder')||'', "
                "class:e.getAttribute('class')||'', type:e.getAttribute('type')||''}))"
            )
        except Exception as exc:
            inputs = [{"error": str(exc)}]
        (folder / f"{stem}.json").write_text(json.dumps(
            {"url": page.url, "title": title, "inputs": inputs}, ensure_ascii=False, indent=2
        ), encoding="utf-8")
        try:
            page.screenshot(path=str(folder / f"{stem}.png"), full_page=True)
            (folder / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            self.logger.warning("搜索框诊断现场保存失败：%s", exc)

    def _pause_if_abnormal(self, page: Page) -> None:
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body = ""
        markers = self.config.get("abnormal_markers", [])
        if any(marker.lower() in (page.url + " " + body).lower() for marker in markers):
            self.logger.warning("检测到验证码、登录失效或访问异常。请在浏览器人工处理。")
            try:
                input("处理完成后按 Enter 继续（Ctrl+C 退出）：")
            except EOFError as exc:
                raise RuntimeInfrastructureError("人工处理输入通道已关闭") from exc

    @staticmethod
    def _job_id(url: str) -> str:
        match = re.search(r"/job_detail/([^/.?]+)", urlparse(url).path)
        if match:
            return match.group(1)
        return (parse_qs(urlparse(url).query).get("jobId") or [""])[0]

    def _debug_failure(self, page: Page, index: int, keyword: str = "", prefix: str = "failure") -> None:
        if not self.debug:
            return
        try:
            folder = self.data_dir / "debug" / safe_directory_name(keyword or "general")
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f"{prefix}_{index:04d}.png"), full_page=True)
            (folder / f"{prefix}_{index:04d}.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            self.logger.debug("失败现场保存失败：%s", exc)

    def _close_owned_detail_page(self) -> None:
        if not self.detail_page_owned or self.detail_page is None:
            return
        try:
            if not self.detail_page.is_closed():
                self.detail_page.close()
                self.logger.info("已关闭脚本自己创建的detail_page")
        except Exception as exc:
            self.logger.warning("关闭脚本detail_page失败：%s", exc)
        finally:
            self.detail_page_owned = False
            self.detail_page = None
            self.detail_page_created = False

    def close(self) -> None:
        # 只允许关闭脚本自己创建的详情页；不关闭用户页面、context、browser或Chrome。
        self._close_owned_detail_page()
        self._disconnect_worker_connection()
