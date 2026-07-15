from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseScraper(ABC):
    """所有职位平台适配器必须提供的统一接口。"""

    @abstractmethod
    def bind_page(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def search_keyword(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def validate_search_results(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def collect_job_urls(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def extract_job_detail(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def validate_record(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError

    @abstractmethod
    def save_screenshot(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
