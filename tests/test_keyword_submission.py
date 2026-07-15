import inspect

from scrapers.boss import BossScraper, SELECTORS


def test_search_button_selector_priority():
    assert SELECTORS["search_button"] == [
        'button:has-text("搜索")',
        'a:has-text("搜索")',
        ".btn-search",
        ".search-btn",
    ]


def test_search_submission_uses_button_not_enter_or_goto():
    source = inspect.getsource(BossScraper._search_keyword)
    assert '.press("Enter")' not in source
    assert ".goto(" not in source
    assert '.fill("")' in source
    assert "_wait_for_keyword_confirmation" in source
    assert "timeout_ms=15000" in source
    assert "搜索结果页被重定向回首页" not in source


def test_result_detection_does_not_require_jobs_url():
    source = inspect.getsource(BossScraper._wait_for_results_signal)
    assert "/web/geek/jobs" not in source
    assert "result_ready" in source
