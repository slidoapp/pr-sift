"""Write a PR's report as one static HTML file that opens without a server.

The page carries its data and every asset inline. Review marks work
within the open page only; questions and comments need `pr-sift serve`.
The copy leaves out what is the reviewer's own: review marks, drafts,
questions and posted reviews.
"""

import json
import re

from pr_sift.report import page_data
from pr_sift.server import WEB
from pr_sift.store import Store

# Each tag of index.html that loads an asset, and how it's inlined.
ASSETS = {
    '<link rel="stylesheet" href="/static/styles.css">': ("<style>", "styles.css", "</style>"),
    '<script src="/static/vendor/highlight.min.js" defer></script>': ("<script>", "vendor/highlight.min.js", "</script>"),
    '<script src="/static/vendor/mermaid.min.js" defer></script>': ("<script>", "vendor/mermaid.min.js", "</script>"),
    '<script src="/static/app.js" type="module"></script>': ('<script type="module">', "app.js", "</script>"),
}


def static_data(store: Store, pr_id: int) -> dict:
    data = page_data(store, pr_id)
    for unit in data["units"]:
        unit |= {"state": "new", "flagged": False, "since_review": None}
    return data | {
        "has_reviews": False,
        "submissions": [],
        "comments": [],
        "questions": [],
        "agent": {"available": False, "command": "claude", "web_fetch": False, "web_fetch_domains": []},
        "static": True,
    }


def static_page(store: Store, pr_id: int) -> str:
    page = (WEB / "index.html").read_text().replace("__TOKEN__", "")
    # "<" escaped, the JSON can't end its script element early. It goes in
    # before the assets, whose code may hold "</head>" too.
    data = json.dumps(static_data(store, pr_id)).replace("<", "\\u003c")
    page = page.replace("</head>", f'<script type="application/json" id="pr-sift-data">{data}</script>\n</head>', 1)
    for tag, (start, path, end) in ASSETS.items():
        if tag not in page:
            raise ValueError(f"index.html no longer has {tag}")
        page = page.replace(tag, start + _inline((WEB / path).read_text()) + end)
    return page


def _inline(text: str) -> str:
    """Text safe to put inside a <style> or <script> element. In the
    scripts "</script" only appears in strings and regular expressions,
    where "<\\/" means the same."""
    return re.sub(r"</(script|style)", r"<\\/\1", text, flags=re.IGNORECASE)
