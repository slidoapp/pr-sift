"""Build a repository's import graph and diff it between revisions."""

import ast
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from reviewer.units import extract_symbols, module_name

MAX_NODES = 25
MAX_CONTEXT = 12
COLORS = {"added": "#1a7f37", "removed": "#cf222e"}


@dataclass
class Graph:
    modules: set[str] = field(default_factory=set)
    # (importer, imported) -> qualnames of the symbols holding the import
    edges: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    # top-level third-party package -> qualnames of the symbols importing it
    external: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


@dataclass
class GraphDiff:
    depth: int
    nodes: dict[str, str]  # node -> added | removed | changed | unchanged
    edges: dict[tuple[str, str], str]  # edge -> added | removed | unchanged
    external_added: list[str]
    external_removed: list[str]


def build_graph(files: Mapping[str, str]) -> Graph:
    """Build the import graph of all Python files of one revision."""
    python = {p: s for p, s in files.items() if p.endswith(".py")}
    graph = Graph(modules={module_name(p) for p in python})
    top_level = {m.split(".")[0] for m in graph.modules}
    for path, source in python.items():
        module = module_name(path)
        package = module if path.endswith("__init__.py") else module.rpartition(".")[0]
        for symbol in extract_symbols(path, source).values():
            if symbol.tree is None:
                continue
            for node in ast.walk(symbol.tree):
                for target, absolute in _targets(node, package):
                    resolved = _resolve(target, graph.modules)
                    if resolved:
                        if resolved != module:
                            graph.edges[(module, resolved)].add(symbol.qualname)
                    elif absolute:
                        top = target.split(".")[0]
                        if top not in top_level and top not in sys.stdlib_module_names and top != "__future__":
                            graph.external[top].add(symbol.qualname)
    return graph


def _targets(node, package):
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name, True
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            parts = package.split(".") if package else []
            base = ".".join(parts[: len(parts) - (node.level - 1)])
            prefix = ".".join(p for p in (base, node.module) if p)
        else:
            prefix = node.module or ""
        for alias in node.names:
            yield (prefix if alias.name == "*" else f"{prefix}.{alias.name}"), not node.level


def _resolve(target, modules):
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in modules:
            return candidate
    return None


def _collapse(module, depth):
    return ".".join(module.split(".")[:depth])


def diff_graphs(old: Graph, new: Graph, changed_modules: Iterable[str], depth: int | None = None) -> GraphDiff:
    """Diff two graphs at a package depth.

    The result keeps only what the change touches: added, removed and
    changed nodes and the ends of added and removed edges. The direct
    neighbors of changed nodes are added for context while the diagram
    stays within MAX_CONTEXT nodes. Only edges touching the change are
    kept. Without a depth, the deepest one whose diagram has at most
    MAX_NODES nodes is used.
    """
    changed_modules = set(changed_modules)
    if depth is not None:
        return _diff_at(old, new, changed_modules, depth)
    max_depth = max((m.count(".") + 1 for m in old.modules | new.modules), default=1)
    for depth in range(max_depth, 1, -1):
        diff = _diff_at(old, new, changed_modules, depth)
        if len(diff.nodes) <= MAX_NODES:
            return diff
    return _diff_at(old, new, changed_modules, 1)


def _diff_at(old, new, changed_modules, depth):
    def collapse_edges(graph):
        edges = {(_collapse(a, depth), _collapse(b, depth)) for a, b in graph.edges}
        return {(a, b) for a, b in edges if a != b}

    old_nodes = {_collapse(m, depth) for m in old.modules}
    new_nodes = {_collapse(m, depth) for m in new.modules}
    changed = {_collapse(m, depth) for m in changed_modules}
    old_edges, new_edges = collapse_edges(old), collapse_edges(new)

    nodes = {}
    for node in old_nodes | new_nodes:
        if node not in old_nodes:
            nodes[node] = "added"
        elif node not in new_nodes:
            nodes[node] = "removed"
        else:
            nodes[node] = "changed" if node in changed else "unchanged"
    edges = {e: "added" if e not in old_edges else "removed" if e not in new_edges else "unchanged" for e in old_edges | new_edges}

    core = {n for n, status in nodes.items() if status != "unchanged"}
    core |= {n for e, status in edges.items() if status != "unchanged" for n in e}
    context = {n for e in edges for n in e if "changed" in (nodes[e[0]], nodes[e[1]])}
    keep = core | context if len(core | context) <= MAX_CONTEXT else core

    def touches_change(edge, status):
        return status != "unchanged" or nodes[edge[0]] != "unchanged" or nodes[edge[1]] != "unchanged"

    return GraphDiff(
        depth=depth,
        nodes={n: s for n, s in sorted(nodes.items()) if n in keep},
        edges={
            e: s
            for e, s in sorted(edges.items())
            if e[0] in keep and e[1] in keep and touches_change(e, s)
        },
        external_added=sorted(new.external.keys() - old.external.keys()),
        external_removed=sorted(old.external.keys() - new.external.keys()),
    )


def graph_signals(old: Graph, new: Graph) -> dict[str, set[str]]:
    """Map qualnames to the architecture signals of the imports they hold."""
    signals = defaultdict(set)
    for edge in new.edges.keys() - old.edges.keys():
        for qualname in new.edges[edge]:
            signals[qualname].add("new-module-edge")
    for edge in old.edges.keys() - new.edges.keys():
        for qualname in old.edges[edge]:
            signals[qualname].add("removed-module-edge")
    for package in new.external.keys() - old.external.keys():
        for qualname in new.external[package]:
            signals[qualname].add("new-dependency")
    return signals


def to_mermaid(diff: GraphDiff) -> str:
    ids = {node: f"n{i}" for i, node in enumerate(diff.nodes)}
    lines = ["flowchart LR"]
    for node, status in diff.nodes.items():
        label = node.replace('"', "#quot;")
        lines.append(f'  {ids[node]}["{label}"]' + (f":::{status}" if status != "unchanged" else ""))
    styles = []
    for i, ((a, b), status) in enumerate(diff.edges.items()):
        lines.append(f"  {ids[a]} {'-.->' if status == 'removed' else '-->'} {ids[b]}")
        if status in COLORS:
            styles.append(f"  linkStyle {i} stroke:{COLORS[status]},stroke-width:2px")
    lines += styles
    lines += [
        "  classDef added stroke:#1a7f37,stroke-width:2px",
        "  classDef removed stroke:#cf222e,stroke-width:2px,stroke-dasharray:4",
        "  classDef changed stroke:#9a6700,stroke-width:2px",
    ]
    return "\n".join(lines)
