"""Split sources into symbol-level units and fingerprint their changes.

A unit is a function, method, class body (without its methods and
nested classes) or the module-level code of a Python file. Non-Python
files and files that fail to parse are a single file-level unit.
"""

import ast
import copy
import hashlib
import io
import tokenize
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFS = (*FUNCS, ast.ClassDef)


@dataclass(frozen=True)
class Symbol:
    qualname: str  # e.g. "pkg.mod.Client.get"; the path for file units
    kind: str  # function | method | class | module | file
    text: str
    lines: tuple[int, ...]  # source line number of each line of text
    tree: ast.AST | None  # None for file units
    comments: tuple[str, ...] = ()


@dataclass
class Unit:
    path: str  # head path, or base path if removed
    qualname: str
    kind: str
    status: str  # added | removed | modified | renamed | moved
    old_body: str
    new_body: str
    old_lines: tuple[int, ...]
    new_lines: tuple[int, ...]
    fingerprint: str
    old_path: str | None = None  # set only when it differs from path
    old_qualname: str | None = None  # set only when it differs from qualname
    noise: str | None = None
    signals: list[str] = field(default_factory=list)
    old: Symbol | None = field(default=None, repr=False, compare=False)
    new: Symbol | None = field(default=None, repr=False, compare=False)


def module_name(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__" and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts)


def is_public(qualname: str | None) -> bool:
    if qualname is None:
        return False
    return not any(
        part.startswith("_") and not (part.startswith("__") and part.endswith("__"))
        for part in qualname.split(".")
    )


def extract_symbols(path: str, source: str) -> dict[str, Symbol]:
    """Return the symbols of one file, keyed by qualname."""
    lines = source.splitlines()
    if path.endswith(".py"):
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            pass
        else:
            symbols: dict[str, Symbol] = {}
            _collect(tree, module_name(path), "module", lines, _comments_by_line(source), symbols)
            return symbols
    return {path: Symbol(path, "file", "\n".join(lines), tuple(range(1, len(lines) + 1)), None)}


def _collect(node, qualname, kind, lines, comments, out):
    if isinstance(node, FUNCS):
        span = list(_span(node, len(lines)))
        tree = node
    else:
        children = [c for c in node.body if isinstance(c, DEFS)]
        removed = {n for c in children for n in _span(c, len(lines))}
        span = _trim_blank_runs([n for n in _span(node, len(lines)) if n not in removed], lines)
        tree = copy.copy(node)
        tree.body = [s for s in node.body if not isinstance(s, DEFS)]
        if isinstance(node, ast.ClassDef) and not tree.body:
            tree.body = [ast.Pass()]
        for child in children:
            child_kind = "class" if isinstance(child, ast.ClassDef) else "method" if kind == "class" else "function"
            _collect(child, f"{qualname}.{child.name}", child_kind, lines, comments, out)
        if kind == "module" and not span:
            return
    out[qualname] = Symbol(
        qualname,
        kind,
        "\n".join(lines[n - 1] for n in span),
        tuple(span),
        tree,
        tuple(comments[n] for n in span if n in comments),
    )


def _span(node, nlines):
    if isinstance(node, ast.Module):
        return range(1, nlines + 1)
    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
    return range(start, node.end_lineno + 1)


def _trim_blank_runs(numbers, lines):
    """Drop blank lines at the edges of each run of consecutive lines."""
    runs: list[list[int]] = []
    for n in numbers:
        if runs and runs[-1][-1] == n - 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    kept = []
    for run in runs:
        code = [n for n in run if lines[n - 1].strip()]
        if code:
            kept.extend(n for n in run if code[0] <= n <= code[-1])
    return kept


def _comments_by_line(source):
    comments = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments[token.start[0]] = token.string
    except (tokenize.TokenError, SyntaxError):
        pass
    return comments


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """Return a copy of tree without module, class and function docstrings."""
    tree = copy.deepcopy(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *DEFS)) and node.body and _is_docstring(node.body[0]):
            node.body = node.body[1:]
            if not node.body and not isinstance(node, ast.Module):
                node.body = [ast.Pass()]
    return tree


def _is_docstring(stmt):
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def normalize(symbol: Symbol, anonymous: bool = False) -> str:
    """Return a form of the symbol that ignores formatting, comments,
    docstrings and the names of function locals.

    With anonymous=True the symbol's own name is ignored too, which is
    used to recognize renamed and moved symbols.
    """
    if symbol.tree is None:
        return symbol.text
    tree = strip_docstrings(symbol.tree)
    if isinstance(tree, FUNCS):
        _LocalRenamer(tree).visit(tree)
    if anonymous and isinstance(tree, DEFS):
        tree.name = "_"
    return ast.dump(tree)


def fingerprint(old: Symbol | None, new: Symbol | None) -> str:
    a = normalize(old) if old else ""
    b = normalize(new) if new else ""
    return hashlib.sha256(f"{a}\0{b}".encode()).hexdigest()


def _local_names(func):
    declared, bound = set(), set()
    for node in ast.walk(func):
        match node:
            case ast.Global(names=names) | ast.Nonlocal(names=names):
                declared.update(names)
            case ast.arg(arg=name) | ast.Name(id=name, ctx=ast.Store() | ast.Del()):
                bound.add(name)
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name) | ast.ClassDef(name=name):
                if node is not func:
                    bound.add(name)
            case ast.alias(name=name, asname=asname):
                bound.add(asname or name.split(".")[0])
            case (
                ast.ExceptHandler(name=str() as name)
                | ast.MatchAs(name=str() as name)
                | ast.MatchStar(name=str() as name)
                | ast.MatchMapping(rest=str() as name)
            ):
                bound.add(name)
    return bound - declared


class _LocalRenamer(ast.NodeVisitor):
    """Rename a function's locals to _v0, _v1, ... in order of appearance."""

    def __init__(self, func):
        self.root = func
        self.names = _local_names(func)
        self.mapping: dict[str, str] = {}

    def _rename(self, name):
        if name not in self.names:
            return name
        return self.mapping.setdefault(name, f"_v{len(self.mapping)}")

    def visit_arg(self, node):
        node.arg = self._rename(node.arg)
        self.generic_visit(node)

    def visit_Name(self, node):
        node.id = self._rename(node.id)

    def visit_FunctionDef(self, node):
        if node is not self.root:
            node.name = self._rename(node.name)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

    def visit_alias(self, node):
        bound = node.asname or node.name.split(".")[0]
        if bound in self.names:
            node.asname = self._rename(bound)

    def visit_ExceptHandler(self, node):
        if node.name:
            node.name = self._rename(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            node.name = self._rename(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            node.rest = self._rename(node.rest)
        self.generic_visit(node)


def diff_files(old_files: Mapping[str, str], new_files: Mapping[str, str]) -> list[Unit]:
    """Return the changed units between two sets of files.

    The mappings hold path -> source at base and at head. Paths missing
    on one side are added or removed files.
    """
    old_symbols = _symbols(old_files, new_files)
    new_symbols = _symbols(new_files, old_files)
    units, removed, added = [], [], []
    for qualname in sorted(old_symbols.keys() | new_symbols.keys()):
        old, new = old_symbols.get(qualname), new_symbols.get(qualname)
        if old and new:
            if old[1].text != new[1].text:
                units.append(_unit("modified", old, new))
        elif old:
            removed.append(old)
        else:
            added.append(new)

    by_shape = defaultdict(list)
    for item in added:
        if item[1].kind != "file":
            by_shape[(item[1].kind, normalize(item[1], anonymous=True))].append(item)
    paired = set()
    for old in removed:
        candidates = by_shape.get((old[1].kind, normalize(old[1], anonymous=True)))
        if old[1].kind == "file" or not candidates:
            units.append(_unit("removed", old, None))
            continue
        leaf = _leaf(old[1].qualname)
        new = next((c for c in candidates if _leaf(c[1].qualname) == leaf), candidates[0])
        candidates.remove(new)
        paired.add(new[1].qualname)
        units.append(_unit("moved" if _leaf(new[1].qualname) == leaf else "renamed", old, new))
    units.extend(_unit("added", None, item) for item in added if item[1].qualname not in paired)

    return sorted(units, key=lambda u: (u.path, (u.new_lines or u.old_lines or (0,))[0]))


def _symbols(files, other):
    symbols = {}
    for path, source in files.items():
        if other.get(path) == source:
            continue
        for symbol in extract_symbols(path, source).values():
            symbols[symbol.qualname] = (path, symbol)
    return symbols


def _leaf(qualname):
    return qualname.rsplit(".", 1)[-1]


def _unit(status, old, new):
    old_path, old_symbol = old or (None, None)
    new_path, new_symbol = new or (None, None)
    path = new_path or old_path
    symbol = new_symbol or old_symbol
    return Unit(
        path=path,
        qualname=symbol.qualname,
        kind=symbol.kind,
        status=status,
        old_body=old_symbol.text if old_symbol else "",
        new_body=new_symbol.text if new_symbol else "",
        old_lines=old_symbol.lines if old_symbol else (),
        new_lines=new_symbol.lines if new_symbol else (),
        fingerprint=fingerprint(old_symbol, new_symbol),
        old_path=old_path if old_path and old_path != path else None,
        old_qualname=old_symbol.qualname if old_symbol and old_symbol.qualname != symbol.qualname else None,
        old=old_symbol,
        new=new_symbol,
    )
