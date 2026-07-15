import json

from utils.config import load_config


def test_load_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "platform": "boss", "jobs_per_keyword": 2,
        "search_keywords": [" 交易系统 ", "AI工程师"], "city": "上海"
    }, ensure_ascii=False), encoding="utf-8")
    config = load_config(path)
    assert config["jobs_per_keyword"] == 2
    assert config["search_keywords"] == ["交易系统", "AI工程师"]
    assert config["city"] == "上海"
    assert config["wait_seconds_min"] == 5
    assert config["save_mode"] == "snapshot"
