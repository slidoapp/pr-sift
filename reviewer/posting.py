"""Turn comment drafts into one GitHub review.

GitHub takes inline comments only on lines its diff of the PR shows:
changed lines and the three lines of context around them. A draft on
such a line (or a range within one hunk) becomes an inline comment;
any other draft goes into the review body, saying where it belongs.
Text is unwrapped first, since GitHub keeps line breaks inside a
paragraph.
"""

import re
from dataclasses import dataclass, field

from reviewer import github, repo
from reviewer.units import module_name

EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class DiffFile:
    """One file of the PR's diff: its paths and, per hunk, the old and
    new lines GitHub lets you comment on."""

    path: str  # what GitHub calls the file: its new path, or the old one if deleted
    old_path: str | None
    hunks: list[tuple[set[int], set[int]]] = field(default_factory=list)

    def hunk(self, side: str, line: int) -> int | None:
        for i, (old, new) in enumerate(self.hunks):
            if line in (old if side == "old" else new):
                return i
        return None


def parse_diff(text: str) -> list[DiffFile]:
    """The files and hunks of `git diff -M` output."""
    files: list[DiffFile] = []
    current = None
    old_line = new_line = 0
    old_name = new_name = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = None
            old_name = new_name = None
            continue
        if line.startswith("--- "):
            old_name = None if line == "--- /dev/null" else _strip(line[4:], "a/")
            continue
        if line.startswith("+++ "):
            new_name = None if line == "+++ /dev/null" else _strip(line[4:], "b/")
            current = DiffFile(new_name or old_name, old_name)
            files.append(current)
            continue
        match = HUNK.match(line)
        if match and current is not None:
            old_line, new_line = int(match[1]), int(match[3])
            current.hunks.append((set(), set()))
            continue
        if current is None or not current.hunks or line.startswith("\\"):
            continue
        old, new = current.hunks[-1]
        if line.startswith("-"):
            old.add(old_line)
            old_line += 1
        elif line.startswith("+"):
            new.add(new_line)
            new_line += 1
        elif line.startswith(" ") or line == "":
            old.add(old_line)
            new.add(new_line)
            old_line += 1
            new_line += 1
    return files


def _strip(name: str, prefix: str) -> str:
    name = name.rstrip("\t")
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].encode().decode("unicode_escape").encode("latin-1").decode()
    return name.removeprefix(prefix)


@dataclass
class Plan:
    """What a review would post: inline comments (each with the draft it
    comes from), the body, and where every draft went."""

    event: str
    body: str
    comments: list[dict]  # {"draft": id, "payload": {path, line, side, ...}}
    placed: list[dict]  # {"draft": id, "where": "inline" | "body" | "skipped", "label", "reason"}

    def payload(self, commit_id: str) -> dict:
        data = {"commit_id": commit_id, "event": self.event, "comments": [c["payload"] for c in self.comments]}
        if self.body:
            data["body"] = self.body
        return data


class PlanError(ValueError):
    pass


def plan_review(drafts: list[dict], units: dict, files: list[DiffFile], event: str, summary: str,
                include_orphaned: bool = False) -> Plan:
    """drafts are comment views (see threads.comment_view) in the order
    they were written; units maps qualname to its unit row."""
    if event not in EVENTS:
        raise PlanError(f"event must be one of {', '.join(EVENTS)}")
    by_new = {f.path: f for f in files}
    by_old = {f.old_path: f for f in files if f.old_path}
    comments, placed, general, elsewhere = [], [], [], []

    for draft in drafts:
        anchor, body = draft["anchor"], unwrap(draft["body"])
        unit = units.get(anchor.get("qualname"))
        if anchor["state"] == "orphaned":
            if not include_orphaned:
                placed.append(_placed(draft, "skipped", anchor["qualname"], "its unit is no longer part of the PR"))
                continue
            elsewhere.append(_in_body(f"`{anchor['qualname']}` (no longer in the PR)", body))
            placed.append(_placed(draft, "body", anchor["qualname"], "its unit is no longer part of the PR"))
            continue
        if anchor["kind"] == "pr":
            general.append(body)
            placed.append(_placed(draft, "body", "the PR", "a general comment"))
            continue
        name = f"`{_short(anchor['qualname'], unit)}` in `{unit['path']}`"
        if anchor["kind"] == "unit":
            elsewhere.append(_in_body(name, body))
            placed.append(_placed(draft, "body", name, "comments on a whole unit go in the body"))
            continue

        end_side = anchor.get("end_side") or anchor["side"]
        end_line = anchor.get("end_line") or anchor["line"]
        start = _locate(unit, anchor["side"], anchor["line"], by_new, by_old)
        end = _locate(unit, end_side, end_line, by_new, by_old)
        lines = f"line {anchor['line']}" if anchor["kind"] == "line" else f"lines {anchor['line']}–{end_line}"
        if end is None:
            elsewhere.append(_in_body(f"{name}, {lines}", body))
            placed.append(_placed(draft, "body", f"{unit['path']}:{end_line}", "the line is outside GitHub's diff"))
            continue
        payload = {"path": end[0], "line": end_line, "side": _github_side(end_side), "body": body}
        label = f"{end[0]}:{end_line}"
        reason = None
        if anchor["kind"] == "range":
            if start is not None and start[0] == end[0] and start[1] == end[1]:
                payload |= {"start_line": anchor["line"], "start_side": _github_side(anchor["side"])}
                label = f"{end[0]}:{anchor['line']}–{end_line}"
            else:
                payload["body"] = f"_(Lines {anchor['line']}–{end_line}.)_\n\n{body}"
                reason = "the range spans lines GitHub can't comment on together, so it's on its last line"
        comments.append({"draft": draft["id"], "payload": payload})
        placed.append(_placed(draft, "inline", label, reason))

    body = "\n\n".join(part for part in [unwrap(summary), *general, *elsewhere] if part)
    if event == "REQUEST_CHANGES" and not body:
        raise PlanError("requesting changes needs a summary or a general comment")
    if event == "COMMENT" and not body and not comments:
        raise PlanError("there is nothing to post")
    return Plan(event, body, comments, placed)


def _locate(unit, side, line, by_new, by_old):
    """(GitHub path, hunk index) of a unit's line, or None when GitHub's
    diff doesn't show it."""
    diff_file = by_new.get(unit["path"]) if side == "new" else by_old.get(unit["old_path"] or unit["path"])
    if diff_file is None:
        return None
    hunk = diff_file.hunk(side, line)
    return None if hunk is None else (diff_file.path, hunk)


def _github_side(side: str) -> str:
    return "LEFT" if side == "old" else "RIGHT"


def _short(qualname, unit) -> str:
    module = module_name(unit["path"]) if unit["path"].endswith(".py") else None
    return qualname[len(module) + 1 :] if module and qualname.startswith(f"{module}.") else qualname


def _in_body(where: str, body: str) -> str:
    return f"**On {where}:**\n\n{body}"


def _placed(draft, where, label, reason):
    return {"draft": draft["id"], "where": where, "label": label, "reason": reason}


LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s")
BLOCK_START = re.compile(r"^\s*(#{1,6}\s|>|\||```|~~~|<)")


def unwrap(text: str) -> str:
    """Join the lines of each paragraph and list item, leaving code
    blocks, quotes, headings, tables and hard breaks (two trailing
    spaces or a backslash) as they are."""
    out: list[str] = []
    fenced = False
    joinable = False  # whether the previous line can take the next one
    for raw in (text or "").strip().splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            out.append(raw)
            joinable = False
            continue
        if fenced:
            out.append(raw)
            continue
        if not stripped:
            out.append("")
            joinable = False
            continue
        if BLOCK_START.match(line):
            out.append(line)
            joinable = False
            continue
        if LIST_ITEM.match(line):
            out.append(line)
            joinable = True
            continue
        # Keep the two trailing spaces of a hard break.
        if raw.endswith("  "):
            line, stripped = f"{line}  ", f"{stripped}  "
        if joinable and out:
            out[-1] = f"{out[-1]} {stripped}"
        else:
            out.append(line)
        joinable = not raw.endswith("  ") and not line.endswith("\\")
    return "\n".join(out).strip()


class GitHubPoster:
    """Reads the PR's diff from the cached clone and posts through gh."""

    def __init__(self, pr_row):
        host, owner, name = pr_row["repo"].split("/", 2)
        self.ref = github.PrRef(host, owner, name, pr_row["number"])
        self.repo_key = pr_row["repo"]

    def head_sha(self) -> str:
        return github.head_sha(self.ref)

    def diff(self, base: str, head: str) -> str:
        args = ("diff", "--find-renames", "--no-color", "--no-ext-diff", "--unified=3", base, head)
        return repo.git(repo.clone_path(self.repo_key), *args).decode("utf-8", errors="replace")

    def create_review(self, payload: dict) -> dict:
        return github.create_review(self.ref, payload)

    def review_comments(self, review_id: int) -> list[dict]:
        return github.review_comments(self.ref, review_id)


def match_posted(plan: Plan, posted: list[dict]) -> dict[int, dict]:
    """The GitHub comment each inline draft became, by draft id, matched
    on path and body in the order they were sent."""
    left = list(posted)
    found = {}
    for comment in plan.comments:
        payload = comment["payload"]
        match = next((p for p in left if p.get("path") == payload["path"] and p.get("body") == payload["body"]), None)
        if match:
            left.remove(match)
            found[comment["draft"]] = match
    return found
