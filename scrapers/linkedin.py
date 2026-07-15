from __future__ import annotations

from typing import Any

from scrapers.base import BaseScraper


class LinkedInScraper(BaseScraper):
    """LinkedIn适配器占位；本轮不实现任何采集行为。"""

    @staticmethod
    def _unsupported(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("该平台尚未实现")

    bind_page = _unsupported
    search_keyword = _unsupported
    validate_search_results = _unsupported
    collect_job_urls = _unsupported
    extract_job_detail = _unsupported
    validate_record = _unsupported
    save_screenshot = _unsupported
    run = _unsupported
    close = _unsupported
