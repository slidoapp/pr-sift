"""Build the data the report page shows for one PR."""

import ast
import json
import textwrap

from reviewer.diffs import diff_rows
from reviewer.folds import Fold, find_folds
from reviewer.skeleton import skeleton_lines
from reviewer.store import Store
from reviewer.units import DEFS, module_name

REVIEWED, FLAGGED = "reviewed", "flagged"


def page_data(store: Store, pr_id: int) -> dict:
    pr = store.pr_by_id(pr_id)
    analysis = json.loads(pr["analysis"] or "{}")
    summary = store.summary(pr_id)
    units = store.units(pr_id)
    rankings = store.rankings(u["fingerprint"] for u in units)
    reviews = store.reviews(pr_id)
    reviewed = {r["qualname"]: r for r in reviews if r["status"] == REVIEWED}
    flagged = {r["qualname"] for r in reviews if r["status"] == FLAGGED}
    reviewed_fingerprints = {r["fingerprint"] for r in reviewed.values()}

    interfaces = analysis.get("interfaces", [])
    views = [unit_view(u, rankings.get(u["fingerprint"]), reviewed, reviewed_fingerprints, flagged) for u in units]
    owner_repo = pr["repo"].split("/", 1)[1]
    return {
        "pr": {
            "id": pr["id"],
            "repo": owner_repo,
            "number": pr["number"],
            "title": pr["title"],
            "url": pr["url"],
            "head_sha": pr["head_sha"],
            "analyzed_at": pr["analyzed_at"],
        },
        "summary": (
            {
                "overview": summary["overview"],
                "architecture_summary": summary["architecture_summary"],
                "components": json.loads(summary["components"]),
                "stale": summary["head_sha"] != pr["head_sha"],
            }
            if summary
            else None
        ),
        "architecture": analysis.get("architecture"),
        "interfaces": interfaces,
        "has_reviews": bool(reviewed),
        "units": views,
    }


def unit_view(unit, ranking, reviewed, reviewed_fingerprints, flagged) -> dict:
    """One unit with its ranking, review state and diff rows."""
    qualname = unit["qualname"]
    old_lines, new_lines = json.loads(unit["old_lines"]), json.loads(unit["new_lines"])
    diff = diff_rows(unit["old_body"], unit["new_body"], old_lines, new_lines)
    highlights = json.loads(ranking["highlights"]) if ranking and ranking["highlights"] else []
    sections = json.loads(ranking["sections"]) if ranking and ranking["sections"] else []
    review = reviewed.get(qualname) or reviewed.get(unit["old_qualname"])
    if unit["fingerprint"] in reviewed_fingerprints:
        state = "seen"
    elif review:
        state = "changed"
    else:
        state = "new"
    return {
        "qualname": qualname,
        "module": module_name(unit["path"]) if unit["kind"] != "file" else None,
        "old_qualname": unit["old_qualname"],
        "path": unit["path"],
        "old_path": unit["old_path"],
        "kind": unit["kind"],
        "status": unit["status"],
        "fingerprint": unit["fingerprint"],
        "noise": unit["noise"],
        "signals": json.loads(unit["signals"]),
        "tier": ranking["tier"] if ranking else None,
        "reason": ranking["reason"] if ranking else None,
        "summary": ranking["summary"] if ranking else None,
        "description": ranking["description"] if ranking else None,
        "attention": attention(unit, ranking, highlights),
        "highlights": highlights,
        "sections": sorted(sections, key=lambda s: s["line"]),
        "folds": shown_folds(find_folds(unit["new_body"], new_lines, unit["path"]), diff),
        "key_lines": key_lines(unit, old_lines, new_lines, diff),
        "docstring": doc_line(unit["new_body"] or unit["old_body"]) if unit["kind"] != "file" else None,
        "state": state,
        "flagged": qualname in flagged,
        "language": "python" if unit["path"].endswith(".py") else None,
        "changed_lines": sum(1 for row in diff if row[0] != " "),
        "diff": diff,
        "since_review": (
            diff_rows(review["reviewed_body"], unit["new_body"], [], new_lines) if state == "changed" else None
        ),
    }


def attention(unit, ranking, highlights) -> str:
    """hidden for noise; read for anything with a highlight, since a line
    worth a look must not be skimmed past; else what Claude decided."""
    if unit["noise"]:
        return "hidden"
    if highlights:
        return "read"
    return ranking["attention"] if ranking and ranking["attention"] else "read"


def shown_folds(folds: list[Fold], diff: list[list]) -> list[dict]:
    """The folds that can replace lines of this diff: those whose lines
    are all added or all unchanged, with no removed lines between them.
    Each gets the number of changed lines it hides."""
    shown = []
    for fold in folds:
        rows = [i for i, row in enumerate(diff) if row[2] is not None and fold.start <= row[2] <= fold.end]
        if not rows:
            continue
        span = diff[rows[0] : rows[-1] + 1]
        if {row[0] for row in span} in ({"+"}, {" "}):
            shown.append(fold.to_dict() | {"changed_lines": sum(row[0] == "+" for row in span)})
    return shown


def key_lines(unit, old_lines: list[int], new_lines: list[int], diff: list[list]) -> dict[str, list[int]]:
    """The skeleton of a Python unit on both sides of its diff: new file
    lines for added and unchanged rows, old file lines for removed ones."""
    if not unit["path"].endswith(".py"):
        return {"new": [], "old": []}
    top_level = unit["kind"] in ("statement", "imports")
    added = {row[2] for row in diff if row[0] == "+"}
    removed = {row[1] for row in diff if row[0] == "-"}
    new = skeleton_lines(unit["new_body"], new_lines, added, top_level) if unit["new_body"] else set()
    old = skeleton_lines(unit["old_body"], old_lines, removed, top_level) & removed if unit["old_body"] else set()
    # Never hide every change: then the changes themselves are the key lines.
    if (added or removed) and not (new & added or old):
        new |= added
        old = removed
    return {"new": sorted(new), "old": sorted(old)}


def doc_line(body: str) -> str | None:
    """The first line of the docstring of a unit's body, if it has one."""
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return None
    node = tree.body[0] if len(tree.body) == 1 and isinstance(tree.body[0], DEFS) else tree
    doc = ast.get_docstring(node)
    return doc.strip().splitlines()[0] if doc and doc.strip() else None
