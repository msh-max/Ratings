"""Places kept out of the restaurants ranking (cafes, coffee shops, bakeries, dessert shops)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .normalize import Aliases, canonical_key

# Words that suggest a cafe / coffee / bakery / dessert place; used only by QA to flag
# places that nobody has reviewed yet (never to exclude automatically).
CAFE_HINT = re.compile(
    r"\b(caf[eé]s?|caff[eè]|coffee|roast(?:ery|ers?)|bak(?:ery|ers?)|patisserie|p[aâ]tisserie|"
    r"pasticceria|tea ?house|dessert|desserts|gelato|ice cream|donuts?|doughnuts?|cookies?|"
    r"chocolate|juice|kunafa|sweets|specialty coffee)\b|قهوة|مقهى|كافيه|حلويات|مخبز",
    re.IGNORECASE)


@dataclass
class Exclusions:
    reasons: dict[str, str] = field(default_factory=dict)   # resolved key -> reason
    names: dict[str, str] = field(default_factory=dict)     # resolved key -> brand name
    keep: set[str] = field(default_factory=set)             # resolved keys reviewed as restaurants

    def _keys(self, spellings: Iterable[str | None], aliases: Aliases | None) -> set[str]:
        out = set()
        for s in spellings:
            k = canonical_key(s)
            if k:
                out.add(aliases.resolve(k) if aliases else k)
        return out

    def reason_for(self, spellings: Iterable[str | None], aliases: Aliases | None = None) -> str | None:
        for k in sorted(self._keys(spellings, aliases)):
            if k in self.reasons:
                return self.reasons[k]
        return None

    def reviewed(self, spellings: Iterable[str | None], aliases: Aliases | None = None) -> bool:
        keys = self._keys(spellings, aliases)
        return bool(keys & self.keep or keys & set(self.reasons))


def load_exclusions(path: Path | None, aliases: Aliases | None = None) -> Exclusions:
    out = Exclusions()
    if path is None or not Path(path).is_file():
        return out
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of brand name -> reason")

    def resolve(name: str) -> str:
        k = canonical_key(str(name))
        return aliases.resolve(k) if aliases else k

    for name, reason in data.items():
        if name == "_keep":
            out.keep.update(resolve(n) for n in reason or [])
            continue
        k = resolve(name)
        out.reasons[k] = str(reason or "not a restaurant").strip()
        out.names[k] = str(name)
    return out


def cafe_suspects(places: list[dict[str, Any]], exclusions: Exclusions,
                  aliases: Aliases | None = None) -> list[tuple[str, str]]:
    """Places whose name or cuisine looks like a cafe / bakery / dessert shop, not yet reviewed."""
    out = []
    for p in places:
        spellings = [p["name"], p.get("name_ar"), *p.get("aliases", [])]
        if exclusions.reviewed(spellings, aliases):
            continue
        texts = [p["name"], *(p.get("aliases") or []), *(p.get("cuisines") or [])]
        for t in texts:
            m = CAFE_HINT.search(t)
            if m:
                out.append((p["name"], m.group(0)))
                break
    return out
