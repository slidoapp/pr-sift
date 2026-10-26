"""Comment drafts and question threads as the report shows them, and the
prompt that gives the agent a question's context."""

import json

from reviewer.anchors import resolve
from reviewer.diffs import diff_rows
from reviewer.store import Store

# Diff rows of the asked-about unit sent with the first question.
MAX_PROMPT_ROWS = 400
# Units listed for a question about the whole PR.
MAX_PROMPT_UNITS = 80


def stored_walkthrough(summary) -> list[dict]:
    return json.loads(summary["walkthrough"] or "[]") if summary and "walkthrough" in summary.keys() else []


def comment_view(row, units, walkthrough) -> dict:
    return {
        "id": row["id"],
        "anchor": resolve(row, units, walkthrough),
        "body": row["body"],
        "state": row["state"],
        "question_id": row["question_id"],
        "github_url": row["github_url"],
        "posted_at": row["posted_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def question_view(row, messages, units, walkthrough, running: bool) -> dict:
    status, error = row["status"], row["error"]
    if status in ("queued", "running") and not running:
        # Its run belonged to a server that stopped.
        status, error = "failed", "The report server stopped while this was running."
    return {
        "id": row["id"],
        "anchor": resolve(row, units, walkthrough),
        "status": status,
        "resolved": bool(row["resolved"]),
        "error": error,
        "messages": [
            {"role": m["role"], "body": m["body"], "sources": json.loads(m["sources"]), "created_at": m["created_at"]}
            for m in messages
        ],
        "created_at": row["created_at"],
    }


def thread_data(store: Store, pr_id: int, running) -> dict:
    """Comments and questions of a PR; running(question_id) tells whether
    its agent is still at work."""
    units = store.units(pr_id)
    walkthrough = stored_walkthrough(store.summary(pr_id))
    questions = store.questions(pr_id)
    messages = store.messages(q["id"] for q in questions)
    return {
        "submissions": [
            {"id": s["id"], "event": s["event"], "url": s["url"], "created_at": s["created_at"]}
            for s in store.submissions(pr_id)
        ],
        "comments": [comment_view(c, units, walkthrough) for c in store.comments(pr_id)],
        "questions": [question_view(q, messages[q["id"]], units, walkthrough, running(q["id"])) for q in questions],
    }


def first_prompt(store: Store, pr_id: int, anchor: dict, question: str, history: list[dict] = ()) -> str:
    """The first message of a thread's session: the PR, what the question
    is anchored on, and the question. history holds earlier turns of the
    thread when its session can't be resumed."""
    pr = store.pr_by_id(pr_id)
    summary = store.summary(pr_id)
    units = store.units(pr_id)
    parts = [
        f"I'm reviewing pull request {pr['repo']}#{pr['number']}: {pr['title']}",
        f"URL: {pr['url']}" if pr["url"] else None,
        f"The working directory is a checkout of its head, {pr['head_sha']}. The diff is against the merge base "
        f"{pr['merge_base']}; the old side of the code is only what the diffs below show.",
    ]
    if summary:
        parts.append(f"Overview of the PR:\n{summary['overview']}")

    kind = anchor["kind"]
    unit = next((u for u in units if u["qualname"] == anchor.get("qualname")), None)
    if kind == "pr":
        parts.append("My question is about the PR as a whole. Its changed units:\n" + _unit_list(store, units))
    elif kind == "walkthrough":
        blocks = stored_walkthrough(summary)
        block = blocks[anchor["block"]] if anchor["block"] < len(blocks) else None
        parts.append("My question is about this part of the PR's walkthrough (a top-down explanation of the change):")
        if block:
            parts.append(_quote(_block_text(block, units)))
        if anchor.get("quote"):
            parts.append(f"In particular:\n{_quote(anchor['quote'])}")
    elif unit:
        parts.append(_unit_context(store, unit))
        if kind in ("line", "range"):
            parts.append(_lines_context(unit, anchor))
        if anchor.get("quote"):
            parts.append(f"I selected:\n{_quote(anchor['quote'])}")

    for turn in history:
        who = "I asked" if turn["role"] == "you" else "You answered"
        parts.append(f"{who} earlier:\n{_quote(turn['body'])}")
    parts.append(f"My question: {question}")
    return "\n\n".join(p for p in parts if p)


def _unit_list(store, units) -> str:
    rankings = store.rankings(u["fingerprint"] for u in units)
    lines = []
    for u in units[:MAX_PROMPT_UNITS]:
        ranking = rankings.get(u["fingerprint"])
        what = f"noise ({u['noise']})" if u["noise"] else ranking["summary"] if ranking else "not ranked"
        lines.append(f"- `{u['qualname']}` in {u['path']} ({u['status']}): {what}")
    if len(units) > MAX_PROMPT_UNITS:
        lines.append(f"- … and {len(units) - MAX_PROMPT_UNITS} more")
    return "\n".join(lines)


def _unit_context(store, unit) -> str:
    ranking = store.rankings([unit["fingerprint"]]).get(unit["fingerprint"])
    head = f"My question is about `{unit['qualname']}` in {unit['path']} ({unit['kind']}, {unit['status']})."
    if ranking:
        head += f"\nThe review tool ranked it {ranking['tier']}: {ranking['summary']}"
    rows = _rows(unit)
    shown = rows[:MAX_PROMPT_ROWS]
    diff = "\n".join(_row_text(row) for row in shown)
    if len(rows) > len(shown):
        diff += f"\n… {len(rows) - len(shown)} more lines"
    return f"{head}\nIts diff, with old and new file line numbers:\n```diff\n{diff}\n```"


def _lines_context(unit, anchor) -> str:
    rows = _rows(unit)

    def index(side, line):
        column = 1 if side == "old" else 2
        return next((i for i, row in enumerate(rows) if row[column] == line and (row[0] != "-") == (side == "new")), None)

    start = index(anchor["side"], anchor["line"])
    end = index(anchor.get("end_side") or anchor["side"], anchor.get("end_line", anchor["line"]))
    if start is None or end is None:
        return ""
    start, end = min(start, end), max(start, end)
    where = f"line {anchor['line']}" if start == end else f"lines {anchor['line']}–{anchor['end_line']}"
    side = f"{anchor['side']} side" if anchor.get("end_side", anchor["side"]) == anchor["side"] else "both sides"
    text = "\n".join(_row_text(row) for row in rows[start : end + 1])
    return f"I'm asking about {where} ({side}):\n```diff\n{text}\n```"


def _rows(unit):
    return diff_rows(unit["old_body"], unit["new_body"], json.loads(unit["old_lines"]), json.loads(unit["new_lines"]))


def _row_text(row) -> str:
    kind, old, new, text = row
    return f"{old if old is not None else '':>5} {new if new is not None else '':>5} {kind} {text}"


def _block_text(block: dict, units) -> str:
    if block.get("type") == "text":
        return block["text"]
    if block.get("type") == "code":
        lines = block.get("lines")
        return f"[code of `{block['unit']}`" + (f", lines {lines[0]}–{lines[1]}]" if lines else "]")
    if block.get("type") == "units":
        return block.get("title", "") + "\n" + "\n".join(f"- `{name}`" for name in block.get("units", []))
    return "[the architecture diagram]"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())
