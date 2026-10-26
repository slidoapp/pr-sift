"""Build the data the report page shows for one PR."""

import json
from difflib import SequenceMatcher

from reviewer.store import Store
from reviewer.units import module_name

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
        "interfaces": analysis.get("interfaces", []),
        "has_reviews": bool(reviewed),
        "units": views,
    }


def unit_view(unit, ranking, reviewed, reviewed_fingerprints, flagged) -> dict:
    """One unit with its ranking, review state and diff rows."""
    qualname = unit["qualname"]
    old_lines, new_lines = json.loads(unit["old_lines"]), json.loads(unit["new_lines"])
    review = reviewed.get(qualname) or reviewed.get(unit["old_qualname"])
    if unit["fingerprint"] in reviewed_fingerprints:
        state = "seen"
    elif review:
        state = "changed"
    else:
        state = "new"
    return {
        "qualname": qualname,
        "module": module_name(unit["path"]) if unit["kind"] not in ("module", "file") else None,
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
        "state": state,
        "flagged": qualname in flagged,
        "language": "python" if unit["path"].endswith(".py") else None,
        "diff": diff_rows(unit["old_body"], unit["new_body"], old_lines, new_lines),
        "since_review": (
            diff_rows(review["reviewed_body"], unit["new_body"], [], new_lines) if state == "changed" else None
        ),
    }


def diff_rows(old: str, new: str, old_lines: list[int], new_lines: list[int]) -> list[list]:
    """A full diff of two bodies as [kind, old line, new line, text] rows.

    kind is " " (unchanged), "-" or "+". Line numbers are file line
    numbers where known, else None.
    """
    a, b = old.splitlines(), new.splitlines()

    def number(lines, i):
        return lines[i] if i < len(lines) else None

    rows = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows += [[" ", number(old_lines, i1 + k), number(new_lines, j1 + k), a[i1 + k]] for k in range(i2 - i1)]
            continue
        rows += [["-", number(old_lines, i), None, a[i]] for i in range(i1, i2)]
        rows += [["+", None, number(new_lines, j), b[j]] for j in range(j1, j2)]
    return rows
