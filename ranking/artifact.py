"""Single-file HTML fragment of the site, for publishing as a Claude Artifact.

The fragment has no doctype/html/head/body tags (the artifact host adds them). It holds, in
order: the <title>, the viewport-fit meta, the inlined CSS, the page markup from
docs/index.html, and one inline script that sets ``window.RANKING_DATA`` and then runs the
inlined docs/app.js. With the data inlined the app does not fetch anything and hides the Map
tab (external map tiles cannot load in the artifact sandbox).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .build import site_copy

TITLE = "Riyadh Best Restaurants"
PAGES_URL = "https://msh-max.github.io/Ratings/"
SIDE_PADDING = ("padding-left: max(16px, env(safe-area-inset-left, 0px))",
                "padding-right: max(16px, env(safe-area-inset-right, 0px))")

_BODY_RE = re.compile(r"<body[^>]*>(.*?)<script\s+src=\"app\.js\"\s*>\s*</script>", re.S | re.I)


def _script_safe_json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # never let the data close the <script> element or open a comment
    return (text.replace("</", "<\\/").replace("<!--", "<\\!--")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _script_safe_js(code: str) -> str:
    if re.search(r"</script", code, re.I):
        raise ValueError("app.js contains '</script' and cannot be inlined")
    return code


def page_markup(index_html: str) -> str:
    m = _BODY_RE.search(index_html)
    if not m:
        raise ValueError("docs/index.html: could not find <body> ... <script src=\"app.js\">")
    return m.group(1).strip()


def render_artifact(output: dict[str, Any], docs_dir: Path) -> str:
    """Return the HTML fragment for one month's ranking output (the full build output)."""
    docs_dir = Path(docs_dir)
    css = (docs_dir / "styles.css").read_text(encoding="utf-8").strip()
    markup = page_markup((docs_dir / "index.html").read_text(encoding="utf-8"))
    app = _script_safe_js((docs_dir / "app.js").read_text(encoding="utf-8").strip())
    if re.search(r"</style", css, re.I):
        raise ValueError("styles.css contains '</style' and cannot be inlined")
    data = site_copy(output)
    head = [f"<title>{TITLE}</title>"]
    if all(p in css for p in SIDE_PADDING):
        head.append('<meta name="claude:viewport-fit-sides" content="cover">')
    head.append('<meta name="description" content="A monthly consensus ranking of Riyadh\'s best '
                'restaurants, built from published guides, awards, editorial lists and community threads.">')
    return "\n".join([
        *head,
        f"<style>\n{css}\n</style>",
        markup,
        "<script>",
        f"window.RANKING_DATA = {_script_safe_json(data)};",
        app,
        "</script>",
        "",
    ])


def write_artifact(root: Path, month: str, out: Path) -> Path:
    root = Path(root)
    src = root / "data" / "rankings" / "restaurants" / f"{month}.json"
    if not src.is_file():
        raise FileNotFoundError(f"{src} not found: run `python -m ranking build --month {month}` first")
    output = json.loads(src.read_text(encoding="utf-8"))
    out = Path(out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_artifact(output, root / "docs"), encoding="utf-8")
    return out
