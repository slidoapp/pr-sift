"""Fold long collections and data files into tables.

A list of calls, dicts or tuples becomes a table with one row per
element and one column per argument, key or position; a JSON file
becomes a table of its entries. Folds only shorten what the report
shows: the full lines stay one click away.
"""

import ast
import io
import json
import re
import textwrap
import tokenize
from collections import Counter
from dataclasses import asdict, dataclass, field

MIN_ELEMENTS = 3
MIN_LINES = 8
MAX_CELL = 300  # the page truncates cells visually and shows the full text on hover
COLLECTIONS = (ast.List, ast.Tuple, ast.Set, ast.Dict)


@dataclass
class Row:
    cells: dict[str, str]  # column -> one-line text
    start: int | None  # file lines of the element, when known
    end: int | None
    comment: str | None = None


@dataclass
class Fold:
    start: int  # first and last file line the fold replaces
    end: int
    title: str | None  # what the collection is, e.g. the variable it's assigned to
    summary: str  # e.g. "10 × SmokeCase"
    columns: list[str]
    rows: list[Row] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def find_folds(body: str, lines: list[int], path: str) -> list[Fold]:
    """Folds of a unit's body. lines holds the file line of each body line."""
    if path.endswith(".json"):
        fold = _json_fold(body, lines)
        return [fold] if fold else []
    if not path.endswith(".py"):
        return []
    source = textwrap.dedent(body)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    comments = _comments(source)
    folds: list[Fold] = []
    _visit(tree, None, folds, lines, comments)
    return folds


def _file_line(lines, n):
    return lines[n - 1] if 0 < n <= len(lines) else None


def _comments(source):
    comments = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments[token.start[0]] = token.string.lstrip("#").strip()
    except (tokenize.TokenError, SyntaxError):
        pass
    return comments


def _visit(node, parent, folds, lines, comments):
    if isinstance(node, COLLECTIONS) and _foldable(node):
        folds.append(_collection_fold(node, parent, lines, comments))
        return
    for child in ast.iter_child_nodes(node):
        _visit(child, node, folds, lines, comments)


def _foldable(node):
    count = len(node.keys) if isinstance(node, ast.Dict) else len(node.elts)
    return count >= MIN_ELEMENTS and node.end_lineno - node.lineno + 1 >= MIN_LINES


def _title(node, parent):
    if isinstance(parent, ast.Assign):
        return ", ".join(ast.unparse(t) for t in parent.targets)
    if isinstance(parent, (ast.AnnAssign, ast.AugAssign)):
        return ast.unparse(parent.target)
    if isinstance(parent, ast.keyword) and parent.arg:
        return f"{parent.arg}="
    if isinstance(parent, ast.comprehension) and parent.iter is node:
        target = parent.target
        names = target.elts if isinstance(target, ast.Tuple) else [target]
        return f"for {', '.join(ast.unparse(n) for n in names)} in"
    if isinstance(parent, ast.Return):
        return "return"
    return None


def _positions(parent, width):
    """Column names for tuple elements: the comprehension's target names
    when the collection is iterated, else #1, #2, ..."""
    if isinstance(parent, ast.comprehension) and isinstance(parent.target, ast.Tuple):
        names = [t.id if isinstance(t, ast.Name) else ast.unparse(t) for t in parent.target.elts]
        if len(names) == width:
            return names
    return [f"#{i + 1}" for i in range(width)]


def _cell(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    else:
        text = ast.unparse(node)
    return _shorten(text)


def _shorten(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def _collection_fold(node, parent, lines, comments):
    if isinstance(node, ast.Dict):
        elements = [(k, v) for k, v in zip(node.keys, node.values)]
    else:
        elements = [(None, e) for e in node.elts]

    callees = Counter(ast.unparse(v.func) for _, v in elements if isinstance(v, ast.Call))
    calls_only = len(callees) and sum(callees.values()) == len(elements)
    width = max((len(v.elts) for _, v in elements if isinstance(v, ast.Tuple)), default=0)
    positions = _positions(parent, width)

    rows, columns = [], {}
    previous_end = node.lineno
    for key, value in elements:
        cells = {}
        if isinstance(node, ast.Dict):
            cells["key"] = _cell(key) if key is not None else "**"
        if isinstance(value, ast.Call):
            if len(callees) > 1 or not calls_only:
                cells["call"] = ast.unparse(value.func)
            for i, arg in enumerate(value.args):
                cells[f"#{i + 1}"] = _cell(arg)
            for keyword in value.keywords:
                cells[keyword.arg or "**"] = _cell(keyword.value)
        elif isinstance(value, ast.Dict):
            for k, v in zip(value.keys, value.values):
                cells["**" if k is None else _cell(k)] = _cell(v)
        elif isinstance(value, ast.Tuple):
            for name, element in zip(positions, value.elts):
                cells[name] = _cell(element)
        else:
            cells["value"] = _cell(value)
        start = key.lineno if key is not None else value.lineno
        # Comments after the previous element, up to this one's last line.
        notes = [comments[n] for n in range(previous_end + 1, value.end_lineno + 1) if n in comments]
        previous_end = value.end_lineno
        for column in cells:
            columns.setdefault(column, None)
        rows.append(
            Row(
                cells=cells,
                start=_file_line(lines, start),
                end=_file_line(lines, value.end_lineno),
                comment=" ".join(notes) or None,
            )
        )

    count = len(elements)
    if calls_only and len(callees) == 1:
        summary = f"{count} × {next(iter(callees))}"
    elif calls_only:
        summary = f"{count} calls: " + ", ".join(f"{name} ×{n}" for name, n in callees.most_common())
    else:
        kind = {ast.Dict: "entries", ast.Set: "items"}.get(type(node), "items")
        summary = f"{count} {kind}"
    return Fold(
        start=_file_line(lines, node.lineno),
        end=_file_line(lines, node.end_lineno),
        title=_title(node, parent),
        summary=summary,
        columns=list(columns),
        rows=rows,
    )


def _json_fold(body, lines):
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict):
        entries = list(data.items())
        keyed = True
    elif isinstance(data, list):
        entries = list(enumerate(data))
        keyed = False
    else:
        return None
    if len(entries) < MIN_ELEMENTS and len(lines) < MIN_LINES:
        return None

    def text(value):
        return _shorten(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))

    rows, columns = [], {}
    for key, value in entries:
        cells = {"key": str(key)} if keyed else {}
        if isinstance(value, dict):
            cells |= {str(k): text(v) for k, v in value.items()}
        else:
            cells["value"] = text(value)
        for column in cells:
            columns.setdefault(column, None)
        rows.append(Row(cells=cells, start=None, end=None))
    return Fold(
        start=lines[0],
        end=lines[-1],
        title=None,
        summary=f"{len(entries)} {'entries' if keyed else 'items'}",
        columns=list(columns),
        rows=rows,
    )
