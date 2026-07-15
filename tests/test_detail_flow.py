import inspect
import logging
from pathlib import Path

from scrapers.boss import (
    BossScraper,
    DETAIL_SELECTORS,
    SALARY_PATTERN,
    job_record_errors,
    split_jd_sections,
    validate_job_record,
)


def test_salary_pattern_accepts_common_boss_formats():
    assert SALARY_PATTERN.search("10-15K")
    assert SALARY_PATTERN.search("20-30K·13薪")


def test_validate_job_record_rejects_shell_and_accepts_full_record():
    shell = {"title": "", "salary": "", "jd_text": "", "url": "https://www.zhipin.com/"}
    assert not validate_job_record(shell)
    assert job_record_errors(shell) == ["title为空", "salary为空", "jd_text少于100字", "URL不是job_detail"]
    valid = {
        "title": "交易系统运维工程师", "salary": "20-30K", "jd_text": "职责与要求" * 30,
        "url": "https://www.zhipin.com/job_detail/abc.html",
    }
    assert validate_job_record(valid)


def test_split_jd_sections_preserves_responsibility_and_requirement_blocks():
    jd = "岗位职责：\n1. 维护交易系统\n2. 处理故障\n任职要求：\n1. 本科\n2. 三年经验\n职位福利：五险一金"
    responsibilities, requirements = split_jd_sections(jd)
    assert "维护交易系统" in responsibilities
    assert "任职要求" not in responsibilities
    assert "三年经验" in requirements
    assert "职位福利" not in requirements


def test_detail_flow_uses_dedicated_goto_and_no_card_click_wait():
    source = inspect.getsource(BossScraper)
    capture = inspect.getsource(BossScraper._capture_detail)
    collect = inspect.getsource(BossScraper._collect_job_urls)
    assert 'page.goto(job_url, wait_until="domcontentloaded", timeout=30000)' in capture
    assert "_wait_for_right_detail_update" not in source
    assert "_click_card_in_place" not in source
    assert "data-job-scanner-card" not in source
    assert "_read_result_job_links" in collect
    assert 'a[href*="/job_detail/"]' in inspect.getsource(BossScraper._read_result_job_links)
    assert ".click(" not in collect


def test_detail_selector_candidates_cover_semantic_regions():
    assert ".job-banner .job-name" in DETAIL_SELECTORS["title"]
    assert ".job-banner .salary" in DETAIL_SELECTORS["salary"]
    assert ".job-sec-text" in DETAIL_SELECTORS["jd"]
    assert "a[ka='job-detail-company_custompage']" in DETAIL_SELECTORS["company"]
    assert ".sider-company p:has(.icon-scale)" in DETAIL_SELECTORS["company_size"]
    assert ".sider-company a[ka='job-detail-brandindustry']" in DETAIL_SELECTORS["company_industry"]


def test_basic_info_parser():
    parsed = BossScraper._parse_basic_info("上海\n3-5年\n本科", "上海·浦东新区·陆家嘴")
    assert parsed == ("上海", "浦东新区", "3-5年", "本科")
    assert BossScraper._parse_basic_info("上海\n经验不限\n本科", "上海虹口区四川北路") == \
        ("上海", "虹口区", "经验不限", "本科")


def test_desktop_screenshot_paths_are_absolute_when_output_is_outside_project(tmp_path):
    class Page:
        url = "https://www.zhipin.com/job_detail/abc.html"
        def evaluate(self, script): pass
        def wait_for_timeout(self, timeout): pass
        def screenshot(self, path, full_page, timeout, animations): Path(path).write_bytes(b"png")
        def content(self): return "<html>detail</html>"

    scraper = BossScraper.__new__(BossScraper)
    scraper.root = tmp_path / "project"
    scraper.root.mkdir()
    scraper.data_dir = tmp_path / "Desktop" / "岗位采集器" / "结果" / "20260713_200000"
    scraper.logger = logging.getLogger("test-desktop-screenshot")
    scraper.screenshot_failed_count = 0
    record = {"company": "测试公司", "title": "测试岗位"}
    scraper._save_valid_detail(Page(), record, 1, "交易系统运维")
    assert Path(record["screenshot_path"]).is_absolute()
    assert Path(record["html_path"]).is_absolute()
    assert record["screenshot_status"] == "success"
