from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def safe_filename(value: str, max_length: int = 120) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    return (value or "unnamed")[:max_length].rstrip(" .")


def safe_directory_name(value: str, max_length: int = 80) -> str:
    """Sanitize a directory name and avoid Windows reserved names."""
    name = safe_filename(value, max_length)
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = f"_{name}"
    return name


class JsonlStore:
    def __init__(self, path: Path, logger=None):
        self.path, self.logger = path, logger
        path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_url(url: str) -> str:
        if not url: return ""
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "from", "ka"))]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

    def write_all(self, records: list[dict[str, Any]]) -> None:
        """Atomically rewrite JSONL after merging matched keywords."""
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    def replace_by_url(self, record: dict[str, Any]) -> bool:
        """Atomically replace a stored record, typically after screenshot completion."""
        target = self.normalize_url(str(record.get("url", "")))
        if not target:
            return False
        records = self.read_all()
        for index, existing in enumerate(records):
            if self.normalize_url(str(existing.get("url", ""))) == target:
                records[index] = dict(record)
                self.write_all(records)
                return True
        return False

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists(): return []
        rows = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try: rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if self.logger: self.logger.error("Ignoring malformed JSONL data on line %d: %s", number, exc)
        return rows

    @staticmethod
    def fallback_key(company: str, title: str, city: str) -> str:
        parts = [str(company).strip(), str(title).strip(), str(city).strip()]
        return "|".join(parts) if all(parts) else ""

    def load_keys(self) -> tuple[set[str], set[str]]:
        urls, fallback = set(), set()
        for row in self.read_all():
            if row.get("url"): urls.add(self.normalize_url(row["url"]))
            else:
                key = self.fallback_key(row.get("company", ""), row.get("title", ""), row.get("city", ""))
                if key: fallback.add(key)
        return urls, fallback

    def add_matched_keyword(self, *, keyword: str, url: str = "", job_id: str = "",
                            company: str = "", title: str = "", city: str = "") -> bool:
        """Merge a keyword into an existing primary record and return whether it matched."""
        records = self.read_all()
        normalized = self.normalize_url(url)
        fallback = self.fallback_key(company, title, city)
        for record in records:
            same_url = bool(normalized and self.normalize_url(record.get("url", "")) == normalized)
            same_job_id = bool(job_id and str(record.get("job_id", "")).strip() == job_id)
            same_fallback = bool(fallback and self.fallback_key(
                record.get("company", ""), record.get("title", ""), record.get("city", "")
            ) == fallback)
            if not (same_url or same_job_id or (not normalized and same_fallback)):
                continue
            matched = record.get("matched_keywords") or []
            if isinstance(matched, str):
                matched = [x.strip() for x in matched.split(",") if x.strip()]
            primary = str(record.get("search_keyword", "")).strip()
            merged = list(dict.fromkeys([*matched, *([primary] if primary else []), keyword]))
            if merged != record.get("matched_keywords"):
                record["matched_keywords"] = merged
                self.write_all(records)
            return True
        return False
