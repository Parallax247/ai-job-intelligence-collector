import pytest

from scrapers.base import BaseScraper
from scrapers.boss import BossScraper
from scrapers.linkedin import LinkedInScraper
from scrapers.liepin import LiepinScraper


def test_base_scraper_exposes_future_platform_interface():
    for name in (
        "bind_page", "search_keyword", "validate_search_results", "collect_job_urls", "extract_job_detail",
        "validate_record", "save_screenshot",
    ):
        assert hasattr(BaseScraper, name)
        assert hasattr(BossScraper, name)


@pytest.mark.parametrize("adapter", [LinkedInScraper()])
def test_unimplemented_platforms_raise_clear_error(adapter):
    with pytest.raises(NotImplementedError, match="该平台尚未实现"):
        adapter.bind_page()
