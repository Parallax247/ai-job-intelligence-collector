from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from utils.runtime_options import normalize_keywords


MAX_SAVED_KEYWORDS = 100


def keyword_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def selected_keywords(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        return normalize_keywords(value) if value.strip() else []
    source = "\n".join(str(item) for item in value)
    return normalize_keywords(source) if source.strip() else []


def keywords_to_text(keywords: Iterable[str]) -> str:
    return "\n".join(selected_keywords(list(keywords)))


def toggle_keyword_selection(current: str | Iterable[str], keyword: str) -> list[str]:
    values = selected_keywords(current)
    target = keyword_key(keyword)
    if not target:
        return values
    if any(keyword_key(value) == target for value in values):
        return [value for value in values if keyword_key(value) != target]
    values.append(str(keyword).strip())
    return values


class KeywordLibrary:
    """Persistent keyword library using atomic writes in the same directory."""

    def __init__(self, path: str | Path, *, max_keywords: int = MAX_SAVED_KEYWORDS,
                 now: Callable[[], datetime] | None = None):
        self.path = Path(path).expanduser()
        self.max_keywords = max(1, int(max_keywords))
        self._now = now or (lambda: datetime.now().astimezone())

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @staticmethod
    def _record(value: dict[str, Any]) -> dict[str, Any] | None:
        name = str(value.get("name", "")).strip()
        if not keyword_key(name):
            return None
        try:
            use_count = max(0, int(value.get("use_count", 0) or 0))
        except (TypeError, ValueError):
            use_count = 0
        return {
            "name": name,
            "created_at": str(value.get("created_at", "") or ""),
            "last_used_at": str(value.get("last_used_at", "") or ""),
            "use_count": use_count,
            "pinned": bool(value.get("pinned", False)),
        }

    def _dedupe(self, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in records:
            record = self._record(raw)
            if record is None:
                continue
            key = keyword_key(record["name"])
            if key in seen:
                continue
            seen.add(key)
            result.append(record)
            if len(result) >= self.max_keywords:
                break
        return result

    def _atomic_write(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = {"keywords": self._dedupe(records)}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _backup_corrupt_file(self) -> Path | None:
        if not self.path.exists():
            return None
        stamp = self._now().strftime("%Y%m%d_%H%M%S")
        backup = self.path.with_name(f"{self.path.stem}.corrupt_{stamp}{self.path.suffix}")
        number = 2
        while backup.exists():
            backup = self.path.with_name(
                f"{self.path.stem}.corrupt_{stamp}_{number}{self.path.suffix}"
            )
            number += 1
        os.replace(self.path, backup)
        return backup

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            try:
                self._atomic_write([])
            except OSError:
                return []
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("keywords"), list):
                raise ValueError("Invalid keyword library format")
            records = self._dedupe(
                value for value in payload["keywords"] if isinstance(value, dict)
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            try:
                self._backup_corrupt_file()
                self._atomic_write([])
            except OSError:
                return []
            return []
        return records

    def save_keywords(self, keywords: str | Iterable[str]) -> list[dict[str, Any]]:
        records = self.load()
        seen = {keyword_key(record["name"]) for record in records}
        created_at = self._timestamp()
        for name in selected_keywords(keywords):
            key = keyword_key(name)
            if not key or key in seen or len(records) >= self.max_keywords:
                continue
            records.append({
                "name": name, "created_at": created_at, "last_used_at": "",
                "use_count": 0, "pinned": False,
            })
            seen.add(key)
        self._atomic_write(records)
        return records

    def delete(self, keyword: str) -> list[dict[str, Any]]:
        target = keyword_key(keyword)
        records = [
            record for record in self.load() if keyword_key(record["name"]) != target
        ]
        self._atomic_write(records)
        return records

    def toggle_pin(self, keyword: str) -> list[dict[str, Any]]:
        target = keyword_key(keyword)
        records = self.load()
        for record in records:
            if keyword_key(record["name"]) == target:
                record["pinned"] = not bool(record.get("pinned"))
                break
        self._atomic_write(records)
        return records

    def mark_used(self, keywords: str | Iterable[str]) -> list[dict[str, Any]]:
        names = selected_keywords(keywords)
        records = self.save_keywords(names)
        targets = {keyword_key(name) for name in names}
        used_at = self._timestamp()
        for record in records:
            if keyword_key(record["name"]) in targets:
                record["last_used_at"] = used_at
                record["use_count"] = int(record.get("use_count", 0) or 0) + 1
        self._atomic_write(records)
        return records

    def sorted_keywords(self, records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        values = list(records if records is not None else self.load())
        return sorted(
            values,
            key=lambda record: (
                1 if record.get("pinned") else 0,
                str(record.get("last_used_at", "")),
                int(record.get("use_count", 0) or 0),
                str(record.get("created_at", "")),
            ),
            reverse=True,
        )

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        used = [record for record in self.load() if record.get("last_used_at")]
        return sorted(
            used,
            key=lambda record: (
                str(record.get("last_used_at", "")),
                int(record.get("use_count", 0) or 0),
            ),
            reverse=True,
        )[:limit]
