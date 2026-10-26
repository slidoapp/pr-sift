"""Run the deterministic analysis of a change and prepare it for ranking."""

import ast
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from reviewer.diffs import numbered_diff
from reviewer.folds import find_folds
from reviewer.graph import GraphDiff, build_graph, diff_graphs, graph_signals, to_mermaid
from reviewer.interfaces import InterfaceChange, annotate, diff_interfaces
from reviewer.noise import classify, hide_plain_imports
from reviewer.repo import changed_paths, list_files, read_files
from reviewer.units import Unit, diff_files, module_name

# Longest diff sent to the model per unit; the report always shows all of it.
MAX_DIFF_LINES = 400


@dataclass
class Analysis:
    units: list[Unit]
    interfaces: list[InterfaceChange]
    graph: GraphDiff


def analyze_revisions(git_dir: Path, base: str, head: str) -> Analysis:
    old_paths, new_paths = changed_paths(git_dir, base, head)
    old_files = read_files(git_dir, base, old_paths)
    new_files = read_files(git_dir, head, new_paths)

    units = classify(diff_files(old_files, new_files))
    interfaces = diff_interfaces(old_files, new_files)
    annotate(units, interfaces)

    # Unchanged files are the same at both revisions, so read them once.
    changed = set(old_paths)
    base_python = read_files(git_dir, base, [p for p in list_files(git_dir, base) if p.endswith(".py")])
    head_python = {p: s for p, s in base_python.items() if p not in changed}
    head_python |= {p: s for p, s in new_files.items() if p.endswith(".py")}
    old_graph, new_graph = build_graph(base_python), build_graph(head_python)
    changed_modules = {module_name(u.path) for u in units if u.noise is None and u.path.endswith(".py")}
    graph = diff_graphs(old_graph, new_graph, changed_modules)
    for qualname, signals in graph_signals(old_graph, new_graph).items():
        for unit in units:
            if qualname in (unit.qualname, unit.old_qualname):
                unit.signals += sorted(signals - set(unit.signals))
    hide_plain_imports(units)

    return Analysis(units, interfaces, graph)


def report_data(analysis: Analysis) -> dict:
    """The parts of an analysis the report shows besides the units."""
    graph = analysis.graph
    return {
        "architecture": {
            "depth": graph.depth,
            "mermaid": to_mermaid(graph),
            "nodes": graph.nodes,
            "edges": [{"from": a, "to": b, "status": s} for (a, b), s in graph.edges.items()],
            "external_added": graph.external_added,
            "external_removed": graph.external_removed,
        },
        "interfaces": [asdict(c) for c in analysis.interfaces],
        "counts": {
            "units": len(analysis.units),
            "hidden": dict(Counter(u.noise for u in analysis.units if u.noise)),
        },
    }


def ranking_input(pr: dict, analysis: Analysis, ranked: dict) -> dict:
    """The analysis as handed to the model: the units to rank, which are
    neither noise nor already ranked, and an outline of all visible units
    with their cached rankings, for writing the walkthrough.

    ranked maps fingerprints to stored ranking rows."""
    units, seen = [], set()
    for unit in analysis.units:
        if unit.noise or unit.fingerprint in ranked or unit.fingerprint in seen:
            continue
        seen.add(unit.fingerprint)
        units.append(
            {
                "fingerprint": unit.fingerprint,
                "path": unit.path,
                "old_path": unit.old_path,
                "qualname": unit.qualname,
                "old_qualname": unit.old_qualname,
                "kind": unit.kind,
                "status": unit.status,
                "signals": unit.signals,
                "diff": unit_diff(unit),
                "folds": [
                    {k: f[k] for k in ("start", "end", "title", "summary", "columns")} | {"rows": len(f["rows"])}
                    for f in (fold.to_dict() for fold in find_folds(unit.new_body, list(unit.new_lines), unit.path))
                ],
            }
        )
    data = {"pr": pr, **report_data(analysis), "units": units, "outline": outline(analysis.units, ranked)}
    data["counts"] |= {
        "ranked": sum(1 for u in analysis.units if not u.noise and u.fingerprint in ranked),
        "to_rank": len(units),
    }
    return data


def outline(units: list[Unit], ranked: dict) -> list[dict]:
    """Every visible unit, with how changed units use each other and the
    cached ranking if there is one."""
    visible = [u for u in units if not u.noise]
    uses = _uses(visible)
    used_by: dict[str, list[str]] = {}
    for name, targets in uses.items():
        for target in targets:
            used_by.setdefault(target, []).append(name)
    rows = []
    for unit in visible:
        ranking = ranked.get(unit.fingerprint)
        rows.append(
            {
                "qualname": unit.qualname,
                "kind": unit.kind,
                "status": unit.status,
                "changed_lines": _changed_lines(unit),
                "uses": uses.get(unit.qualname, []),
                "used_by": used_by.get(unit.qualname, []),
                "ranking": (
                    {k: ranking[k] for k in ("tier", "attention", "summary")}
                    | {"highlights": len(json.loads(ranking["highlights"] or "[]"))}
                    if ranking
                    else None
                ),
            }
        )
    return rows


def _changed_lines(unit):
    return sum(1 for line in numbered_diff(unit.old_body, unit.new_body, [], [], 10**6).splitlines() if line[6:7] in "+-")


def _uses(units: list[Unit]) -> dict[str, list[str]]:
    """For each unit, the other changed units it names: calls, attribute
    access or references, matched by their last name part."""
    by_leaf: dict[str, list[str]] = {}
    for unit in units:
        if unit.kind in ("function", "method", "class", "statement"):
            leaf = unit.qualname.rsplit(".", 1)[-1]
            by_leaf.setdefault(leaf, []).append(unit.qualname)
    uses = {}
    for unit in units:
        if unit.new is None or unit.new.tree is None:
            continue
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(unit.new.tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        targets = sorted({q for name in names for q in by_leaf.get(name, []) if q != unit.qualname})
        if targets:
            uses[unit.qualname] = targets
    return uses


def unit_diff(unit: Unit, max_lines: int = MAX_DIFF_LINES) -> str:
    return numbered_diff(unit.old_body, unit.new_body, list(unit.old_lines), list(unit.new_lines), max_lines)
