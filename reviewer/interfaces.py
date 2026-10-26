"""Find changes to what callers and clients depend on.

Interfaces are public signatures, model fields, HTTP routes, CLI
commands and options, environment variables, dependencies and
migrations.
"""

import ast
import io
import posixpath
import re
import tokenize
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

from reviewer.units import DEFS, FUNCS, Symbol, Unit, extract_symbols, is_public, module_name

SIGNALS = {
    "signature": "public-signature",
    "model-field": "model-field",
    "route": "route",
    "cli": "cli",
    "env": "env",
    "dependency": "dependency",
    "migration": "migration",
}
CATEGORY_ORDER = list(SIGNALS)

SIGNATURE_DECORATORS = {
    "property", "cached_property", "setter", "deleter",
    "staticmethod", "classmethod", "abstractmethod", "overload",
}
MODEL_DECORATORS = {"dataclass", "define", "frozen", "mutable"}
MODEL_BASES = {
    "BaseModel", "BaseSettings", "TypedDict", "NamedTuple", "DeclarativeBase",
    "SQLModel", "Base", "Model", "Schema", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag",
}
FIELD_CALLS = {"Column", "mapped_column", "relationship", "Field", "field"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace", "websocket"}
ENV_CALLS = {"os.getenv", "getenv", "os.environ.get", "environ.get"}


@dataclass(frozen=True)
class Item:
    category: str
    name: str
    value: str
    path: str
    qualname: str | None = None
    scope: str = ""  # disambiguates equal names, e.g. the same route path in two modules
    doc: str | None = None  # a field's comment


@dataclass
class InterfaceChange:
    category: str
    name: str
    status: str  # added | removed | changed
    before: str | None
    after: str | None
    path: str
    qualnames: tuple[str, ...]
    doc: str | None = None


def extract_interfaces(path: str, source: str) -> list[Item]:
    name = posixpath.basename(path)
    if name == "pyproject.toml":
        return _pyproject(path, source)
    if re.fullmatch(r"requirements.*\.txt", name):
        return _requirements(path, source)
    if not path.endswith(".py"):
        return []
    items = []
    if "migrations" in path.split("/") or "/alembic/versions/" in f"/{path}":
        items.append(Item("migration", path, "", path))
    symbols = extract_symbols(path, source)
    exported = _dunder_all(symbols.get(f"{module_name(path)}.__all__"))
    for symbol in symbols.values():
        if symbol.tree is not None:
            items += _symbol_items(symbol, path, exported)
    return items


def diff_interfaces(old_files: Mapping[str, str], new_files: Mapping[str, str]) -> list[InterfaceChange]:
    old, new = _index(old_files, new_files), _index(new_files, old_files)
    changes = []
    for key in old.keys() | new.keys():
        before, after = old.get(key), new.get(key)
        before_value = _join(before)
        after_value = _join(after)
        if before_value == after_value:
            continue
        item = (after or before)[0]
        qualnames = dict.fromkeys(i.qualname for i in (after or []) + (before or []) if i.qualname)
        changes.append(
            InterfaceChange(
                category=item.category,
                name=item.name,
                status="added" if before is None else "removed" if after is None else "changed",
                before=before_value,
                after=after_value,
                path=item.path,
                qualnames=tuple(qualnames),
                doc=next((i.doc for i in (after or []) + (before or []) if i.doc), None),
            )
        )
    return sorted(changes, key=lambda c: (CATEGORY_ORDER.index(c.category), c.name, c.path))


def belongs(change: InterfaceChange, qualname: str, old_qualname: str | None, path: str, old_path: str | None) -> bool:
    """Whether an interface change belongs to the unit with these names.

    Changes without a symbol, like dependencies and migrations, belong
    to every unit of their file.
    """
    if change.qualnames:
        return qualname in change.qualnames or old_qualname in change.qualnames
    return change.path in (path, old_path)


def annotate(units: list[Unit], changes: list[InterfaceChange]) -> None:
    """Add a signal to each unit an interface change belongs to."""
    for change in changes:
        signal = SIGNALS[change.category]
        for unit in units:
            if belongs(change, unit.qualname, unit.old_qualname, unit.path, unit.old_path) and signal not in unit.signals:
                unit.signals.append(signal)


def _index(files, other):
    index: dict[tuple, list[Item]] = {}
    for path, source in files.items():
        if other.get(path) == source:
            continue
        for item in extract_interfaces(path, source):
            index.setdefault((item.category, item.scope, item.name), []).append(item)
    return index


def _join(items):
    if items is None:
        return None
    return "; ".join(sorted({i.value for i in items}))


def _dunder_all(module: Symbol | None):
    if module is None or module.tree is None:
        return set()
    for stmt in module.tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets)
            and isinstance(stmt.value, (ast.List, ast.Tuple))
        ):
            return {e.value for e in stmt.value.elts if isinstance(e, ast.Constant)}
    return set()


def _symbol_items(symbol, path, exported):
    node = symbol.tree
    qualname = symbol.qualname
    module = module_name(path)
    items = []
    public = is_public(qualname) or (qualname.rsplit(".", 1)[-1] in exported and symbol.kind != "method")
    if isinstance(node, DEFS) and public:
        items.append(Item("signature", qualname, _signature(node), path, qualname))
    if isinstance(node, ast.ClassDef) and public and _is_model(node):
        source = dict(zip(symbol.lines, symbol.text.splitlines()))
        for field_name, text, stmt in _fields(node):
            doc = _comment(stmt, source)
            items.append(Item("model-field", f"{qualname}.{field_name}", text, path, qualname, doc=doc))
    if isinstance(node, FUNCS):
        for decorator in node.decorator_list:
            items += _decorator_items(decorator, node, symbol, path, module)
    for child in ast.walk(node):
        if isinstance(child, (ast.Call, ast.Subscript)):
            items += _call_items(child, symbol, path, module)
    return items


def _signature(node):
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases]
        bases += [f"{k.arg}={ast.unparse(k.value)}" if k.arg else f"**{ast.unparse(k.value)}" for k in node.keywords]
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    decorators = "".join(
        f"@{ast.unparse(d)} " for d in node.decorator_list if _last_name(d) in SIGNATURE_DECORATORS
    )
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{decorators}{prefix} {node.name}({ast.unparse(node.args)}){returns}"


def _last_name(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_model(node):
    return (
        any(_last_name(d) in MODEL_DECORATORS for d in node.decorator_list)
        or any(_last_name(b) in MODEL_BASES for b in node.bases)
        or any(
            isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in s.targets)
            for s in node.body
        )
    )


def _fields(node):
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            yield stmt.target.id, ast.unparse(stmt), stmt
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and not stmt.targets[0].id.startswith("__")
        ):
            enum = any(_last_name(b) in {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"} for b in node.bases)
            if enum or (isinstance(stmt.value, ast.Call) and _last_name(stmt.value) in FIELD_CALLS):
                yield stmt.targets[0].id, ast.unparse(stmt), stmt


def _comment(stmt, source):
    """The comment on the lines right above a statement, or else at the
    end of its line."""
    above = []
    line = stmt.lineno - 1
    while line in source and source[line].strip().startswith("#"):
        above.insert(0, source[line].strip().lstrip("#").strip())
        line -= 1
    if above:
        return " ".join(above)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source.get(stmt.end_lineno, "")).readline)
        return next((t.string.lstrip("#").strip() for t in tokens if t.type == tokenize.COMMENT), None)
    except (tokenize.TokenError, SyntaxError):
        return None


def _string_args(call):
    return [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _keyword(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _decorator_items(decorator, func, symbol, path, module):
    if not isinstance(decorator, ast.Call):
        if _last_name(decorator) in {"command", "group"}:
            return [Item("cli", f"command {func.name.replace('_', '-')}", ast.unparse(decorator), path, symbol.qualname, module)]
        return []
    name = _last_name(decorator)
    text = ast.unparse(decorator)
    strings = _string_args(decorator)
    route_path = strings[0] if strings else None
    if route_path is None and isinstance(p := _keyword(decorator, "path") or _keyword(decorator, "rule"), ast.Constant):
        route_path = p.value
    if isinstance(decorator.func, ast.Attribute) and route_path is not None:
        if name in HTTP_METHODS:
            return [Item("route", f"{name.upper()} {route_path}", text, path, symbol.qualname, module)]
        if name in {"route", "api_route"}:
            methods = _keyword(decorator, "methods")
            names = (
                [e.value for e in methods.elts if isinstance(e, ast.Constant)]
                if isinstance(methods, (ast.List, ast.Tuple, ast.Set))
                else ["GET"]
            )
            return [Item("route", f"{m.upper()} {route_path}", text, path, symbol.qualname, module) for m in names]
    if name in {"command", "group"}:
        command = strings[0] if strings else None
        if command is None and isinstance(k := _keyword(decorator, "name"), ast.Constant):
            command = k.value
        return [Item("cli", f"command {command or func.name.replace('_', '-')}", text, path, symbol.qualname, module)]
    if name in {"option", "argument"} and strings:
        return [Item("cli", f"{func.name}: {' / '.join(strings)}", text, path, symbol.qualname, module)]
    return []


def _call_items(node, symbol, path, module):
    leaf = symbol.qualname.rsplit(".", 1)[-1]
    if isinstance(node, ast.Subscript):
        if ast.unparse(node.value) in {"os.environ", "environ"} and isinstance(node.slice, ast.Constant):
            return [Item("env", str(node.slice.value), "required", path, symbol.qualname)]
        return []
    func = ast.unparse(node.func)
    strings = _string_args(node)
    if func in ENV_CALLS and node.args and isinstance(node.args[0], ast.Constant):
        default = node.args[1] if len(node.args) > 1 else _keyword(node, "default")
        value = f"default={ast.unparse(default)}" if default is not None else "default=None"
        return [Item("env", str(node.args[0].value), value, path, symbol.qualname)]
    if isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument" and strings:
        return [Item("cli", f"{leaf}: {' / '.join(strings)}", ast.unparse(node), path, symbol.qualname, module)]
    if isinstance(node.func, ast.Attribute) and node.func.attr == "add_parser" and strings:
        return [Item("cli", f"command {strings[0]}", ast.unparse(node), path, symbol.qualname, module)]
    return []


def _package(spec):
    return re.split(r"[\s<>=!~;\[@(]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _pyproject(path, source):
    try:
        data = tomllib.loads(source)
    except tomllib.TOMLDecodeError:
        return []
    specs = []
    project = data.get("project", {})
    specs += [("", s) for s in project.get("dependencies", [])]
    for group, values in project.get("optional-dependencies", {}).items():
        specs += [(group, s) for s in values]
    for group, values in data.get("dependency-groups", {}).items():
        specs += [(group, s) for s in values if isinstance(s, str)]
    poetry = data.get("tool", {}).get("poetry", {})
    groups = {"": poetry.get("dependencies", {})}
    groups |= {g: v.get("dependencies", {}) for g, v in poetry.get("group", {}).items()}
    for group, values in groups.items():
        specs += [(group, f"{n} {v}") for n, v in values.items() if n != "python"]
    return [
        Item("dependency", f"{_package(spec)} [{group}]" if group else _package(spec), spec, path, scope=path)
        for group, spec in specs
    ]


def _requirements(path, source):
    items = []
    for line in source.splitlines():
        spec = line.split("#", 1)[0].strip()
        if spec and not spec.startswith("-"):
            items.append(Item("dependency", _package(spec), spec, path, scope=path))
    return items
