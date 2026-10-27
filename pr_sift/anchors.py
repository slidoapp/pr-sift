"""Where comments and questions attach: a line, a range of lines, a
unit, a walkthrough block or the whole PR.

Lines are stored as offsets into the unit's lines on one side, like
highlights, so an anchor follows its symbol when code above it moves.
After a new analysis an anchor is current (same fingerprint), stale
(the unit or walkthrough block changed) or orphaned (the unit is gone).
"""

import hashlib
import json

KINDS = ("line", "range", "unit", "walkthrough", "pr")
SIDES = ("old", "new")
FIELDS = ("anchor_kind", "qualname", "fingerprint", "side", "start_offset", "end_side", "end_offset", "block", "quote")
MAX_QUOTE = 2000


class AnchorError(ValueError):
    pass


def block_fingerprint(block: dict) -> str:
    """Identifies a walkthrough block's content, ignoring what the report adds."""
    content = {k: v for k, v in block.items() if k != "stale"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def make_anchor(data: object, units: list, walkthrough: list[dict]) -> dict:
    """The stored form of an anchor the page sent:
    {"kind": "line", "qualname": ..., "side": "new", "line": 42},
    a "range" with "end_side" and "end_line" as well, {"kind": "unit",
    "qualname": ...}, {"kind": "walkthrough", "block": 3, "quote": ...}
    or {"kind": "pr"}."""
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        raise AnchorError(f"anchor kind must be one of {', '.join(KINDS)}")
    kind = data["kind"]
    anchor = dict.fromkeys(FIELDS) | {"anchor_kind": kind}
    quote = data.get("quote")
    if quote is not None:
        if not isinstance(quote, str):
            raise AnchorError("quote must be text")
        anchor["quote"] = quote.strip()[:MAX_QUOTE] or None

    if kind == "walkthrough":
        block = data.get("block")
        if not isinstance(block, int) or not 0 <= block < len(walkthrough):
            raise AnchorError("no such walkthrough block")
        return anchor | {"block": block, "fingerprint": block_fingerprint(walkthrough[block])}
    if kind == "pr":
        return anchor

    unit = next((u for u in units if u["qualname"] == data.get("qualname")), None)
    if unit is None:
        raise AnchorError(f"no unit {data.get('qualname')}")
    anchor |= {"qualname": unit["qualname"], "fingerprint": unit["fingerprint"]}
    if kind == "unit":
        return anchor
    anchor["side"], anchor["start_offset"] = _offset(unit, data.get("side"), data.get("line"))
    if kind == "range":
        anchor["end_side"], anchor["end_offset"] = _offset(unit, data.get("end_side"), data.get("end_line"))
    return anchor


def _offset(unit, side, line) -> tuple[str, int]:
    if side not in SIDES:
        raise AnchorError("side must be old or new")
    lines = json.loads(unit[f"{side}_lines"])
    if line not in lines:
        raise AnchorError(f"line {line} is not on the {side} side of {unit['qualname']}")
    return side, lines.index(line)


def resolve(row, units: list, walkthrough: list[dict]) -> dict:
    """An anchor as it is now: its lines in the current units, and its
    state: current, stale or orphaned. A stale line anchor whose offset
    no longer fits its unit keeps the unit but loses its lines."""
    kind = row["anchor_kind"]
    view = {"kind": kind, "qualname": row["qualname"], "quote": row["quote"], "state": "current"}
    if kind == "pr":
        return view
    if kind == "walkthrough":
        block = row["block"]
        view["block"] = block
        if block >= len(walkthrough) or block_fingerprint(walkthrough[block]) != row["fingerprint"]:
            view["state"] = "stale"
        return view

    unit = next((u for u in units if u["qualname"] == row["qualname"]), None)
    unit = unit or next((u for u in units if u["old_qualname"] == row["qualname"]), None)
    if unit is None:
        return view | {"state": "orphaned"}
    view["qualname"] = unit["qualname"]
    if unit["fingerprint"] != row["fingerprint"]:
        view["state"] = "stale"
    if kind == "unit":
        return view
    start = _line(unit, row["side"], row["start_offset"])
    end = _line(unit, row["end_side"], row["end_offset"]) if kind == "range" else start
    if start is None or end is None:
        return view | {"kind": "unit", "state": "stale"}
    return view | {"side": row["side"], "line": start, "end_side": row["end_side"] or row["side"], "end_line": end}


def _line(unit, side, offset):
    lines = json.loads(unit[f"{side}_lines"])
    return lines[offset] if 0 <= offset < len(lines) else None
