import json

from main import load_historical_urls
from utils.run_paths import write_run_config
from utils.storage import JsonlStore


def test_historical_urls_are_loaded_only_from_finished_runs(tmp_path):
    previous = tmp_path / "2026-07-13_20-00_历史任务"
    previous.mkdir()
    write_run_config(previous / "run_config.json", {"status": "completed"})
    JsonlStore(previous / "jobs.jsonl").append({
        "job_id": "history123",
        "url": "https://www.zhipin.com/job_detail/history123.html?ka=old",
    })
    running = tmp_path / ".running_20260713_210000"
    running.mkdir()
    write_run_config(running / "run_config.json", {"status": "running"})
    (running / "jobs.jsonl").write_text(
        json.dumps({
            "url": "https://www.zhipin.com/job_detail/current123.html"
        }) + "\n",
        encoding="utf-8",
    )

    urls = load_historical_urls(tmp_path)

    assert "https://www.zhipin.com/job_detail/history123.html" in urls
    assert not any("current123" in url for url in urls)
