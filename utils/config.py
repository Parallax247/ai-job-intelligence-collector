from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "platform": "boss",
    "jobs_per_keyword": 30,
    "search_keywords": [],
    "city": "",
    "experience": "",
    "education": "",
    "salary": "",
    "save_mode": "snapshot",
    "wait_seconds_min": 5,
    "wait_seconds_max": 10,
    "keyword_wait_seconds_min": 8,
    "keyword_wait_seconds_max": 15,
    "cdp_url": "http://127.0.0.1:9222",
    "timeout_ms": 10000,
    "navigation_timeout_ms": 30000,
    "scroll_wait_ms": 2000,
    "search_wait_ms": 2500,
    "max_stagnant_scrolls": 5,
    "abnormal_markers": ["验证码", "安全验证", "访问异常", "请登录", "captcha", "verify"],
}


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Configuration file is not valid JSON: {path} ({exc})") from exc
    # Backward compatibility for the first-version {"boss": {...}} structure.
    if "boss" in raw and isinstance(raw["boss"], dict):
        raw = {**raw, **raw["boss"]}
        raw.pop("boss", None)
    config = {**DEFAULTS, **raw}
    if config["platform"] not in {"boss", "liepin"}:
        raise ValueError("platform must be boss or liepin")
    if not isinstance(config["search_keywords"], list):
        raise ValueError("search_keywords must be an array of strings")
    config["search_keywords"] = [str(x).strip() for x in config["search_keywords"] if str(x).strip()]
    if int(config["jobs_per_keyword"]) < 1:
        raise ValueError("jobs_per_keyword must be greater than 0")
    config["jobs_per_keyword"] = int(config["jobs_per_keyword"])
    if config.get("save_mode") not in {"snapshot", "new_only"}:
        raise ValueError("save_mode must be snapshot or new_only")
    return config
