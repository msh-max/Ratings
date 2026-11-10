"""Tier parsing, base weights, recency and the per-place score (spec section 2)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_REGISTRY = Path(__file__).with_name("sources.yaml")

_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "single": 1,
            "first": 1, "second": 2, "third": 3}
_NUM = r"(\d+|one|two|three|four|five|single)"


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _WORDNUM[tok]


@dataclass(frozen=True)
class Tier:
    kind: str                 # stars | toques | bib | rank | winner | finalist | special | recommended | creator | listed
    n: int | None = None
    green: bool = False


def parse_tier(tier: Any, position: Any = None) -> Tier:
    """Parse a free-text tier into a structured Tier. Never raises."""
    text = unicodedata.normalize("NFKC", str(tier or "")).lower().strip()
    pos = parse_position(position)
    green = "green star" in text or "étoile verte" in text
    text_wo_green = text.replace("green star", " ")

    m = re.search(r"recommended by\s+(\d+)|(\d+)\s*(?:commenters?|comments?|mentions?|upvotes?|users?|people|replies)", text)
    if m:
        return Tier("recommended", int(m.group(1) or m.group(2)), green)
    if "creator" in text:
        return Tier("creator", pos, green)
    if "recommended by" in text and pos:
        return Tier("recommended", pos, green)
    m = re.search(_NUM + r"\s*(?:michelin\s*)?stars?\b", text_wo_green)
    if m:
        return Tier("stars", _num(m.group(1)), green)
    if "bib" in text:
        return Tier("bib", None, green)
    m = re.search(_NUM + r"\s*toques?\b", text) or re.search(r"toques?\s*[:x]?\s*(\d)", text)
    if m:
        return Tier("toques", _num(m.group(1)), green)
    if "special award" in text or text.startswith("special") or "award:" in text:
        return Tier("special", pos, green)
    if re.search(r"highly commended|finalist|nominee|nominated|shortlist|runner[- ]?up|silver|bronze|commended", text):
        return Tier("finalist", pos, green)
    if re.search(r"\bwinner\b|\bwon\b|\bwins\b|\bgold\b|\bgrand prix\b", text):
        return Tier("winner", pos, green)
    m = (re.search(r"#\s*(\d+)", text) or re.search(r"\bno\.?\s*(\d+)\b", text)
         or re.search(r"\brank(?:ed)?\s*(?:no\.?\s*)?(\d+)", text)
         or re.search(r"^(\d+)(?:st|nd|rd|th)?$", text))
    if m:
        return Tier("rank", int(m.group(1)), green)
    if pos is not None:
        return Tier("rank", pos, green)
    return Tier("listed", None, green)


def parse_position(position: Any) -> int | None:
    if position is None or isinstance(position, bool):
        return None
    if isinstance(position, (int, float)):
        return int(position) if position > 0 else None
    m = re.search(r"\d+", str(position))
    return int(m.group()) if m and int(m.group()) > 0 else None


# ---------------------------------------------------------------- registry

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’]", "", text)
    return " " + " ".join(re.sub(r"[^\w]+", " ", text).split()) + " "


class Registry:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.schemes: dict[str, dict[str, Any]] = data["schemes"]
        self.type_defaults: dict[str, str] = data["type_defaults"]
        self.publishers: list[dict[str, Any]] = data.get("publishers") or []
        self.recency_rules = data["recency"]
        self.undated = float(data["undated_multiplier"])
        self.cap = float(data["publisher_cap"])

    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        return cls(yaml.safe_load(Path(path or DEFAULT_REGISTRY).read_text(encoding="utf-8")))

    def publisher(self, raw: str) -> dict[str, Any] | None:
        n = _norm(raw)
        for pub in self.publishers:
            for token in pub.get("match", []):
                if _norm(str(token)) in n:
                    return pub
        return None

    def publisher_key(self, raw: str) -> str:
        pub = self.publisher(raw)
        if pub and not pub.get("group_by_raw"):
            return pub["id"]
        return _norm(raw).strip()

    def publisher_name(self, raw: str) -> str:
        pub = self.publisher(raw)
        return pub["name"] if pub and not pub.get("group_by_raw") else raw

    def award_label(self, raw: str) -> str:
        pub = self.publisher(raw)
        if pub and not pub.get("group_by_raw"):
            return pub.get("award_label") or pub["name"]
        return raw

    def scheme_for(self, publisher: str, source_type: str) -> dict[str, Any]:
        pub = self.publisher(publisher)
        name = None
        if pub:
            name = (pub.get("schemes") or {}).get(source_type)
        name = name or self.type_defaults.get(source_type, "editorial")
        return self.schemes[name]

    # ------------------------------------------------------------ weights

    def base_weight(self, tier: Tier, scheme: dict[str, Any]) -> float:
        if "flat" in scheme:
            return float(scheme["flat"])
        listed = float(scheme.get("listed", 0))
        w: float | None = None
        if tier.kind == "stars" and "stars" in scheme:
            w = scheme["stars"].get(min(tier.n or 1, 3))
        elif tier.kind == "toques" and "toques" in scheme:
            w = scheme["toques"].get(max(1, min(tier.n or 1, 5)))
        elif tier.kind == "bib":
            w = scheme.get("bib")
        elif tier.kind in ("winner", "finalist", "special", "creator"):
            w = scheme.get(tier.kind)
            if w is None and tier.kind == "special":
                w = scheme.get("winner")
        elif tier.kind == "recommended" and "recommended_brackets" in scheme:
            w = _recommended(scheme, tier.n)
        elif tier.kind == "rank":
            if "rank_brackets" in scheme:
                w = _rank(scheme, tier.n)
            elif "recommended_brackets" in scheme:
                w = _recommended(scheme, tier.n)
        w = listed if w is None else float(w)
        if tier.green and "green_star_bonus" in scheme:
            w += float(scheme["green_star_bonus"])
        return w

    def recency(self, published_at: str | None, build_month: str) -> float:
        age = months_between(published_at, build_month)
        if age is None:
            return self.undated
        for rule in self.recency_rules:
            if rule["max_months"] is None or age <= rule["max_months"]:
                return float(rule["multiplier"])
        return float(self.recency_rules[-1]["multiplier"])


def _rank(scheme: dict[str, Any], n: int | None) -> float | None:
    if n is None:
        return None
    for b in scheme["rank_brackets"]:
        if n <= b["max"]:
            return float(b["weight"])
    return float(scheme.get("rank_beyond", scheme.get("listed", 0)))


def _recommended(scheme: dict[str, Any], n: int | None) -> float | None:
    if n is None:
        return None
    for b in scheme["recommended_brackets"]:
        if n >= b["min"]:
            return float(b["weight"])
    return None


def months_between(published_at: str | None, build_month: str) -> int | None:
    """Whole months from the published date to the build month (never negative)."""
    if not published_at:
        return None
    m = re.match(r"^(\d{4})(?:-(\d{2}))?", str(published_at))
    if not m:
        return None
    py = int(m.group(1))
    pm = int(m.group(2)) if m.group(2) else 6     # year-only: assume mid-year
    by, bm = (int(x) for x in build_month.split("-"))
    return max(0, (by - py) * 12 + (bm - pm))


# ---------------------------------------------------------------- per place

def score_place(mentions: list[dict[str, Any]], registry: Registry) -> tuple[float, list[dict[str, Any]], int]:
    """Score one place.

    ``mentions`` carry: source_id, publisher (raw), source_type, tier, position,
    published_at and a precomputed ``weight`` (= base x recency).
    Returns (score, counted_mentions, n_publishers). One mention is counted per source
    (the heaviest); each publisher contributes at most ``cap`` x its best mention.
    """
    best: dict[str, dict[str, Any]] = {}
    for m in mentions:
        cur = best.get(m["source_id"])
        if cur is None or m["weight"] > cur["weight"]:
            best[m["source_id"]] = m
    by_pub: dict[str, list[float]] = {}
    for m in best.values():
        by_pub.setdefault(registry.publisher_key(m["publisher"]), []).append(m["weight"])
    total = 0.0
    for weights in by_pub.values():
        total += min(sum(weights), registry.cap * max(weights))
    counted = sorted(best.values(), key=lambda m: (-m["weight"], -(_date_key(m["published_at"])), m["source_id"]))
    return round(total + 1e-9, 1), counted, len(by_pub)


def _date_key(published_at: str | None) -> int:
    if not published_at:
        return 0
    digits = re.sub(r"\D", "", str(published_at))[:8].ljust(8, "0")
    return int(digits)


def confidence(n_publishers: int) -> str:
    return "strong" if n_publishers >= 3 else "medium" if n_publishers == 2 else "single"
