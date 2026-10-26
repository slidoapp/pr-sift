"""Diffs of unit bodies with file line numbers."""

from difflib import SequenceMatcher

CONTEXT = 3


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


def numbered_diff(old: str, new: str, old_lines: list[int], new_lines: list[int], max_lines: int) -> str:
    """A diff for the model: changed lines with a little context, each
    prefixed by its file line (the new one, or the old one for removed
    lines), so the model can point at lines exactly."""
    rows = diff_rows(old, new, old_lines, new_lines)
    changed = [i for i, row in enumerate(rows) if row[0] != " "]
    keep = {j for i in changed for j in range(i - CONTEXT, i + CONTEXT + 1)}
    out, previous = [], None
    for i, (kind, old_line, new_line, text) in enumerate(rows):
        if i not in keep:
            continue
        if previous is not None and i != previous + 1:
            out.append("   ...")
        line = new_line if kind != "-" else old_line
        out.append(f"{line if line is not None else '':>5} {kind} {text}")
        previous = i
    if len(out) > max_lines:
        out = out[:max_lines] + [f"... {len(out) - max_lines} more lines"]
    return "\n".join(out)
