"""Name normalisation and de-duplication of mentions into places."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml
from rapidfuzz import fuzz, process

MERGE_THRESHOLD = 92

STOPWORDS = {"restaurant", "restaurants", "restaurante", "مطعم", "the", "riyadh", "الرياض", "by"}
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي",
                              "ؤ": "و", "ئ": "ي", "ـ": None})
_APOSTROPHES = re.compile(r"['’‘`´]")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def has_arabic(text: str | None) -> bool:
    return bool(text and _ARABIC_RE.search(text))


def _strip_marks(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def canonical_key(name: str | None) -> str:
    """Lowercase, accent-free, punctuation-free key with the "al" article normalised.

    "Al-Romansiah", "Al Romansiah", "El Romansiah" and "Alromansiah" all become
    "alromansiah"; "الرومانسية" becomes "al" + its stem so Arabic articles fold the same way.
    """
    if not name:
        return ""
    text = _strip_marks(name).lower().translate(_ARABIC_FOLD)
    text = text.replace("&", " and ")
    text = _APOSTROPHES.sub("", text)
    raw_tokens = [t for t in _NON_WORD.sub(" ", text).split() if t and t != "_"]

    tokens: list[str] = []
    for tok in raw_tokens:
        if tok in ("el", "al"):
            tokens.append("al")
        elif tok.startswith("ال") and len(tok) > 3 and tok not in STOPWORDS:
            tokens.extend(["al", tok[2:]])
        else:
            tokens.append(tok)
    kept = [t for t in tokens if t not in STOPWORDS] or tokens

    joined: list[str] = []
    i = 0
    while i < len(kept):
        if kept[i] == "al" and i + 1 < len(kept):
            joined.append("al" + kept[i + 1])
            i += 2
        else:
            joined.append(kept[i])
            i += 1
    return " ".join(joined)


def slugify(text: str) -> str:
    ascii_text = _strip_marks(text).encode("ascii", "ignore").decode().lower()
    ascii_text = _APOSTROPHES.sub("", ascii_text)
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


# ---------------------------------------------------------------- aliases

@dataclass
class Aliases:
    """Manual merges: canonical name -> list of other spellings (any script)."""
    key_map: dict[str, str] = field(default_factory=dict)
    distinct: set[frozenset[str]] = field(default_factory=set)

    def resolve(self, key: str) -> str:
        return self.key_map.get(key, key)

    def kept_apart(self, a: str, b: str) -> bool:
        return frozenset((a, b)) in self.distinct


def load_aliases(path: Path | None) -> Aliases:
    out = Aliases()
    if path is None or not Path(path).is_file():
        return out
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    for canonical, variants in data.items():
        if canonical == "_distinct":
            for pair in variants or []:
                keys = [canonical_key(n) for n in pair]
                if len(keys) == 2:
                    out.distinct.add(frozenset(keys))
            continue
        target = canonical_key(str(canonical))
        out.key_map[target] = target
        for variant in variants or []:
            out.key_map[canonical_key(str(variant))] = target
    return out


# ---------------------------------------------------------------- clustering

class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # keep the smaller index as root: deterministic
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def cluster_mentions(mentions: list[dict[str, Any]], aliases: Aliases | None = None,
                     threshold: int = MERGE_THRESHOLD) -> list[list[dict[str, Any]]]:
    """Group mention dicts (each with ``name`` and optional ``name_ar``) into places.

    Two mentions merge when their keys (after alias resolution) are equal, when either
    Arabic/Latin spelling matches, or when rapidfuzz token_set_ratio >= threshold.
    Returns clusters in first-seen order; mention order inside a cluster is preserved.
    """
    aliases = aliases or Aliases()
    keys_per_mention: list[list[str]] = []
    for m in mentions:
        ks = []
        for nm in (m.get("name"), m.get("name_ar")):
            k = aliases.resolve(canonical_key(nm))
            if k and k not in ks:
                ks.append(k)
        keys_per_mention.append(ks)

    uniq: list[str] = sorted({k for ks in keys_per_mention for k in ks})
    index = {k: i for i, k in enumerate(uniq)}
    uf = _UnionFind(len(uniq))
    for ks in keys_per_mention:
        for k in ks[1:]:
            uf.union(index[ks[0]], index[k])

    if len(uniq) > 1:
        latin = [k for k in uniq if not has_arabic(k)]
        arabic = [k for k in uniq if has_arabic(k)]
        for group in (latin, arabic):
            if len(group) < 2:
                continue
            matrix = process.cdist(group, group, scorer=fuzz.token_set_ratio,
                                   score_cutoff=threshold, workers=1)
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if matrix[i][j] >= threshold and not aliases.kept_apart(group[i], group[j]):
                        uf.union(index[group[i]], index[group[j]])

    clusters: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for m, ks in zip(mentions, keys_per_mention):
        root = uf.find(index[ks[0]]) if ks else -1 - len(order)
        if root not in clusters:
            clusters[root] = []
            order.append(root)
        clusters[root].append(m)
    return [clusters[r] for r in order]


# ---------------------------------------------------------------- place attributes

def _most_common(values: Iterable[str | None]) -> str | None:
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return None
    counts = Counter(vals)
    first_seen = {v: i for i, v in reversed(list(enumerate(vals)))}
    return sorted(counts, key=lambda v: (-counts[v], first_seen[v]))[0]


def pick_names(mentions: list[dict[str, Any]]) -> tuple[str, str | None, list[str]]:
    """Return (name, name_ar, aliases) for a cluster."""
    latin = [m["name"] for m in mentions if m.get("name") and not has_arabic(m["name"])]
    arabic = [n for m in mentions for n in (m.get("name_ar"), m.get("name"))
              if n and has_arabic(n)]
    name_ar = _most_common(arabic)
    name = _most_common(latin) or name_ar or mentions[0]["name"]
    spellings = {n.strip() for m in mentions for n in (m.get("name"), m.get("name_ar")) if n}
    alias_list = sorted(s for s in spellings if s not in (name, name_ar))
    return name, (name_ar if name_ar != name else None), alias_list


_PRICE_RE = re.compile(r"^\s*(\$+)\s*$")


def normalise_price(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    m = _PRICE_RE.match(value)
    if m:
        return "$" * min(len(m.group(1)), 4)
    return value.strip()


def split_cuisines(values: Iterable[str | None]) -> list[str]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    first: dict[str, int] = {}
    for v in values:
        if not v:
            continue
        for part in re.split(r"[,/;|]", v):
            part = part.strip()
            if not part:
                continue
            key = part.lower()
            counts[key] += 1
            display.setdefault(key, part[0].upper() + part[1:])
            first.setdefault(key, len(first))
    return [display[k] for k in sorted(counts, key=lambda k: (-counts[k], first[k]))]


def place_attributes(mentions: list[dict[str, Any]]) -> dict[str, Any]:
    name, name_ar, alias_list = pick_names(mentions)
    branches = sorted({m["branch"].strip() for m in mentions if m.get("branch") and m["branch"].strip()})
    return {
        "name": name,
        "name_ar": name_ar,
        "aliases": alias_list,
        "cuisines": split_cuisines(m.get("cuisine") for m in mentions),
        "area": _most_common(m.get("area") for m in mentions),
        "price_band": _most_common(normalise_price(m.get("price_band")) for m in mentions),
        "branches": branches,
    }
