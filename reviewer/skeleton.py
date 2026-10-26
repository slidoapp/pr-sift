"""Pick the lines that show what code does: its skeleton.

The skeleton is a unit's signature, the headers of blocks that contain
changes, exits (return, raise, assert, break, continue), changed
statements that call something with an effect, and changed statements
that write a name a decision reads (an if or while condition, a
return, raise or assert), like the appends to a list that is checked
later. Logging, docstrings, comments, other plain assignments and calls
to trivial helpers are left out.
"""

import ast
import textwrap

LOG_ROOTS = {"logging", "logger", "log", "LOGGER", "LOG", "_logger", "_log", "warnings"}
LOG_FUNCS = {"print", "pprint"}
TRIVIAL_FUNCS = {
    "len", "str", "int", "float", "bool", "bytes", "list", "dict", "set", "tuple", "frozenset", "repr", "type",
    "isinstance", "issubclass", "sorted", "reversed", "range", "enumerate", "zip", "map", "filter", "min", "max",
    "sum", "any", "all", "abs", "round", "getattr", "hasattr", "iter", "next", "id", "hash", "format",
}
TRIVIAL_METHODS = {
    "append", "extend", "add", "update", "get", "setdefault", "pop", "insert", "remove", "discard", "clear", "copy",
    "items", "keys", "values", "format", "join", "split", "rsplit", "strip", "lstrip", "rstrip", "lower", "upper",
    "replace", "startswith", "endswith", "encode", "decode", "count", "index",
}
EXITS = (ast.Return, ast.Raise, ast.Assert, ast.Break, ast.Continue, ast.Delete)
MUTATING_METHODS = {
    "append", "extend", "add", "update", "insert", "remove", "discard", "pop", "setdefault", "clear",
}
# A kept statement longer than this shows only its first line.
MAX_STATEMENT_LINES = 6


def skeleton_lines(body: str, lines: list[int], changed: set[int], top_level: bool = False) -> set[int]:
    """File lines of body's skeleton. lines maps body lines to file lines,
    changed holds the file lines the diff adds or removes. For top-level
    statement units (top_level), each statement itself counts as key."""
    source = textwrap.dedent(body)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    text = source.splitlines()

    def file_line(n):
        return lines[n - 1] if 0 < n <= len(lines) else None

    def touched(node):
        start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
        return any(file_line(n) in changed for n in range(start, node.end_lineno + 1))

    keep: set[int] = set()
    decided: set[str] = set()  # names read by kept decisions
    candidates = []  # touched statements left out, which may write those names

    def add(start, end):
        for n in range(start, end + 1):
            line = file_line(n)
            stripped = text[n - 1].strip() if n <= len(text) else ""
            if line is not None and stripped and not stripped.startswith("#"):
                keep.add(line)

    def keep_statement(node):
        if node.end_lineno - node.lineno < MAX_STATEMENT_LINES:
            add(node.lineno, node.end_lineno)
            return
        # A long statement shows its first line, and, when only part of it
        # changed (an edited argument of a long call), the changed lines.
        add(node.lineno, node.lineno)
        span = range(node.lineno, node.end_lineno + 1)
        edited = [n for n in span if file_line(n) in changed]
        if len(edited) < len(span):
            for n in edited:
                add(n, n)

    def header(start, end):
        # An unchanged header longer than two lines shows its first line.
        if end - start >= 2 and not any(file_line(n) in changed for n in range(start, end + 1)):
            end = start
        add(start, end)

    def statement(node, depth):
        blocks = _blocks(node)
        if blocks:
            # The unit's own definition always shows; nested blocks only
            # when they contain changes.
            unit_root = depth == 0 and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if touched(node) or unit_root:
                for start, end, block in _headers(node, text):
                    if block is None or any(touched(child) for child in block):
                        header(start, end)
                if isinstance(node, (ast.If, ast.While)):
                    decided.update(_names(node.test))
            for block in blocks:
                for child in block:
                    statement(child, depth + 1)
            return
        if not touched(node):
            return
        if (top_level and depth == 0) or _important(node):
            keep_statement(node)
            if isinstance(node, (ast.Return, ast.Raise, ast.Assert)):
                decided.update(_names(node))
        else:
            candidates.append(node)

    for node in tree.body:
        statement(node, 0)
    for node in candidates:
        if _writes(node) & decided:
            keep_statement(node)
    return keep


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _writes(node):
    """Names a statement assigns or mutates in place."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        func = node.value.func
        if isinstance(func, ast.Attribute) and func.attr in MUTATING_METHODS and isinstance(func.value, ast.Name):
            return {func.value.id}
    return set()


def _blocks(node):
    blocks = []
    for name in ("body", "orelse", "finalbody"):
        value = getattr(node, name, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            blocks.append(value)
    for handler in getattr(node, "handlers", []):
        blocks.append(handler.body)
    for case in getattr(node, "cases", []):
        blocks.append(case.body)
    return blocks


def _headers(node, text):
    """(start, end, block) for a compound statement's header lines: its
    own (block None: it always shows with the statement), and those of
    its else, except, finally and case clauses with the block each one
    introduces."""
    start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    first_body = node.body[0]
    headers = [(start, _header_end(node.lineno, first_body, text), None)]
    for handler in getattr(node, "handlers", []):
        headers.append((handler.lineno, _header_end(handler.lineno, handler.body[0], text), handler.body))
    for case in getattr(node, "cases", []):
        headers.append((case.pattern.lineno, _header_end(case.pattern.lineno, case.body[0], text), case.body))
    for name, word in (("orelse", "else"), ("finalbody", "finally")):
        block = getattr(node, name, None)
        if not block or not isinstance(block[0], ast.stmt):
            continue
        if name == "orelse" and isinstance(node, ast.If) and isinstance(block[0], ast.If) and _is_elif(block[0], text):
            continue  # an elif is a nested If with its own header
        for n in range(block[0].lineno - 1, node.lineno, -1):
            if text[n - 1].strip().startswith(word):
                headers.append((n, n, block))
                break
    return headers


def _header_end(lineno, first_body, text):
    """The last line of a header that starts at lineno: the line before
    the body, or lineno itself for a body on the same line."""
    if first_body.lineno == lineno:
        return lineno
    end = first_body.lineno - 1
    # Comments and the docstring don't belong to the header.
    while end > lineno and (not text[end - 1].strip() or text[end - 1].strip().startswith("#")):
        end -= 1
    return end


def _is_elif(node, text):
    return text[node.lineno - 1].lstrip().startswith("elif")


def _important(node) -> bool:
    if isinstance(node, EXITS):
        return True
    if isinstance(node, ast.Expr):
        if isinstance(node.value, ast.Constant):
            return False
        if isinstance(node.value, (ast.Yield, ast.YieldFrom, ast.Await)):
            return True
    return any(_effectful(call) for call in ast.walk(node) if isinstance(call, ast.Call))


def _effectful(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id not in TRIVIAL_FUNCS and func.id not in LOG_FUNCS
    if isinstance(func, ast.Attribute):
        root = func
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in LOG_ROOTS:
            return False
        return func.attr not in TRIVIAL_METHODS
    return True
