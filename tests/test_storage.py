import json

from utils.storage import JsonlStore, safe_directory_name, safe_filename


def test_safe_filename():
    assert safe_filename('0001_某/公司: Python*工程师?') == "0001_某_公司__Python_工程师"


def test_jsonl_append_and_resume_dedup(tmp_path):
    store = JsonlStore(tmp_path / "jobs.jsonl")
    store.append({"url": "https://www.zhipin.com/job_detail/abc?utm_source=x", "company": "A", "title": "开发"})
    store.append({"url": "", "company": "B", "title": "测试", "city": "上海"})
    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["company"] == "A"
    urls, fallback = store.load_keys()
    assert "https://www.zhipin.com/job_detail/abc" in urls
    assert "B|测试|上海" in fallback


def test_keyword_directory_name_cleanup():
    assert safe_directory_name("AI/交易:系统*工程师?") == "AI_交易_系统_工程师"
    assert safe_directory_name("CON") == "_CON"


def test_matched_keywords_merge(tmp_path):
    store = JsonlStore(tmp_path / "jobs.jsonl")
    store.append({"url": "https://www.zhipin.com/job_detail/abc", "company": "A", "title": "开发",
                  "city": "上海", "search_keyword": "Python", "matched_keywords": ["Python"]})
    assert store.add_matched_keyword(url="https://www.zhipin.com/job_detail/abc?utm_source=x",
                                     keyword="交易系统")
    rows = store.read_all()
    assert len(rows) == 1
    assert rows[0]["matched_keywords"] == ["Python", "交易系统"]
