"""Label units whose change doesn't need a reviewer's attention.

A label is the reason the report gives for hiding a unit. Units are
never dropped.
"""

import ast
import posixpath
from collections.abc import Iterable
from fnmatch import fnmatch

from reviewer.units import FUNCS, Unit, is_public, normalize, strip_docstrings

DEFAULT_GENERATED = ("*.lock", "package-lock.json", "*_pb2.py", "*_pb2_grpc.py")


def classify(units: list[Unit], generated: Iterable[str] = DEFAULT_GENERATED) -> list[Unit]:
    generated = tuple(generated)
    for unit in units:
        unit.noise = reason(unit, generated)
    return units


def reason(unit: Unit, generated: Iterable[str] = DEFAULT_GENERATED) -> str | None:
    name = posixpath.basename(unit.path)
    if any(fnmatch(unit.path, pattern) or fnmatch(name, pattern) for pattern in generated):
        return "generated"
    old, new = unit.old, unit.new
    if old is None or new is None or old.tree is None or new.tree is None:
        return None

    # Renaming or moving a public symbol breaks its callers.
    if unit.status in ("moved", "renamed"):
        if is_public(unit.qualname) or is_public(unit.old_qualname):
            return None
        return "move" if unit.status == "moved" else "symbol-rename"

    if ast.dump(old.tree) == ast.dump(new.tree):
        return "comment" if old.comments != new.comments else "formatting"
    if ast.dump(strip_docstrings(old.tree)) == ast.dump(strip_docstrings(new.tree)):
        return "docstring"
    if unit.kind == "imports" and _imports_and_rest(old.tree) == _imports_and_rest(new.tree):
        return "imports"
    if isinstance(new.tree, FUNCS) and normalize(old) == normalize(new):
        # Callers may pass parameters of a public function by keyword.
        if is_public(unit.qualname) and _keyword_params(old.tree) != _keyword_params(new.tree):
            return None
        return "local-rename"
    return None


def _imports_and_rest(module):
    imports, rest = [], []
    for stmt in strip_docstrings(module).body:
        if isinstance(stmt, ast.Import):
            imports += [("import", "", a.name, a.asname, 0) for a in stmt.names]
        elif isinstance(stmt, ast.ImportFrom):
            imports += [("from", stmt.module or "", a.name, a.asname, stmt.level) for a in stmt.names]
        else:
            rest.append(ast.dump(stmt))
    return sorted(imports, key=repr), rest


def _keyword_params(func):
    return [a.arg for a in func.args.args + func.args.kwonlyargs]
