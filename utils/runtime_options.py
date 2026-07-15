from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


KEYWORD_SPLIT_PATTERN = re.compile(r"[,，;；\n\r]+")


def normalize_keywords(value: str | Iterable[Any]) -> list[str]:
    """Split on commas, semicolons, and line breaks; trim and preserve order."""
    source = value if isinstance(value, str) else "\n".join(str(item) for item in value)
    keywords: list[str] = []
    seen: set[str] = set()
    for part in KEYWORD_SPLIT_PATTERN.split(source):
        keyword = part.strip()
        if not keyword:
            continue
        if not 2 <= len(keyword) <= 50:
            raise ValueError(f"Keyword length must be between 2 and 50 characters: {keyword!r}")
        if keyword not in seen:
            seen.add(keyword)
            keywords.append(keyword)
    if not keywords:
        raise ValueError("At least one non-empty keyword is required")
    return keywords


def apply_runtime_overrides(config: dict[str, Any], *, keywords: str | None = None,
                            keyword: str | None = None, limit: int | None = None,
                            city: str | None = None, wait_min: float | None = None,
                            wait_max: float | None = None) -> dict[str, Any]:
    """Merge CLI overrides and validate runtime settings without mutating the input."""
    resolved = dict(config)
    if keywords is not None:
        resolved["search_keywords"] = normalize_keywords(keywords)
    elif keyword is not None:
        single = normalize_keywords(keyword)
        if len(single) != 1:
            raise ValueError("--keyword accepts one keyword only; use --keywords for multiple values")
        resolved["search_keywords"] = single
    else:
        resolved["search_keywords"] = normalize_keywords(resolved.get("search_keywords", []))

    if limit is not None:
        resolved["jobs_per_keyword"] = limit
    try:
        resolved["jobs_per_keyword"] = int(resolved["jobs_per_keyword"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("jobs_per_keyword must be an integer between 1 and 50") from exc
    if not 1 <= resolved["jobs_per_keyword"] <= 50:
        raise ValueError("jobs_per_keyword must be between 1 and 50")

    if city is not None:
        resolved["city"] = city.strip()
    else:
        resolved["city"] = str(resolved.get("city", "")).strip()

    if wait_min is not None:
        resolved["wait_seconds_min"] = wait_min
    if wait_max is not None:
        resolved["wait_seconds_max"] = wait_max
    try:
        resolved["wait_seconds_min"] = float(resolved.get("wait_seconds_min", 6))
        resolved["wait_seconds_max"] = float(resolved.get("wait_seconds_max", 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("Wait intervals must be non-negative numbers") from exc
    if resolved["wait_seconds_min"] < 0 or resolved["wait_seconds_max"] < 0:
        raise ValueError("Wait intervals cannot be negative")
    if resolved["wait_seconds_min"] > resolved["wait_seconds_max"]:
        raise ValueError("Minimum wait cannot exceed maximum wait")
    resolved["save_mode"] = str(resolved.get("save_mode", "snapshot") or "snapshot")
    if resolved["save_mode"] not in {"snapshot", "new_only"}:
        raise ValueError("save_mode must be snapshot or new_only")
    return resolved


def save_last_run_config(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    def compact_number(value: Any) -> int | float:
        number = float(value)
        return int(number) if number.is_integer() else number

    payload = {
        "platform": str(config.get("platform", "boss")),
        "search_keywords": list(config["search_keywords"]),
        "jobs_per_keyword": int(config["jobs_per_keyword"]),
        "city": str(config.get("city", "")),
        "wait_seconds_min": compact_number(config["wait_seconds_min"]),
        "wait_seconds_max": compact_number(config["wait_seconds_max"]),
        "save_mode": str(config.get("save_mode", "snapshot")),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
