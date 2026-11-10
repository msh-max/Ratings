"""Load and validate the source extracts of one month."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import validate_extract

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass
class LoadResult:
    extracts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sources_dir(root: Path, month: str) -> Path:
    return Path(root) / "data" / "sources" / month


def latest_month(root: Path) -> str | None:
    """Most recent YYYY-MM folder under data/sources that holds at least one extract."""
    base = Path(root) / "data" / "sources"
    if not base.is_dir():
        return None
    months = sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and MONTH_RE.match(p.name) and any(p.glob("*.json"))
    )
    return months[-1] if months else None


def load_month(root: Path, month: str) -> LoadResult:
    if not MONTH_RE.match(month):
        raise ValueError(f"month must look like YYYY-MM, got {month!r}")
    res = LoadResult()
    folder = sources_dir(root, month)
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        res.errors.append(f"no extracts found in {folder}")
        return res
    seen: dict[str, str] = {}
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            res.errors.append(f"{path.name}: unreadable JSON ({exc})")
            continue
        problems = validate_extract(data)
        if problems:
            res.errors.extend(f"{path.name}: {p}" for p in problems)
            continue
        sid = data["source_id"]
        if sid in seen:
            res.errors.append(f"{path.name}: duplicate source_id {sid!r} (also in {seen[sid]})")
            continue
        seen[sid] = path.name
        if path.stem != sid:
            res.warnings.append(f"{path.name}: file name differs from source_id {sid!r}")
        if not data["entries"]:
            res.warnings.append(f"{path.name}: no entries")
        res.extracts.append(data)
    res.extracts.sort(key=lambda d: d["source_id"])
    return res
