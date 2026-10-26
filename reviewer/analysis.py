"""Run the deterministic analysis of a change and prepare it for ranking."""

import difflib
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from reviewer.graph import GraphDiff, build_graph, diff_graphs, graph_signals, to_mermaid
from reviewer.interfaces import InterfaceChange, annotate, diff_interfaces
from reviewer.noise import classify
from reviewer.repo import changed_paths, list_files, read_files
from reviewer.units import Unit, diff_files, module_name

# Longest diff sent to the model per unit; the report always shows all of it.
MAX_DIFF_LINES = 400
HUNK_RE = re.compile(r"@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@")


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


def ranking_input(pr: dict, analysis: Analysis, ranked: set[str]) -> dict:
    """The analysis as handed to the model: everything except units that
    are noise or already ranked."""
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
            }
        )
    data = {"pr": pr, **report_data(analysis), "units": units}
    data["counts"] |= {
        "ranked": sum(1 for u in analysis.units if not u.noise and u.fingerprint in ranked),
        "to_rank": len(units),
    }
    return data


def unit_diff(unit: Unit, max_lines: int = MAX_DIFF_LINES) -> str:
    lines = list(
        difflib.unified_diff(
            unit.old_body.splitlines(),
            unit.new_body.splitlines(),
            fromfile=f"a/{unit.old_path or unit.path}",
            tofile=f"b/{unit.path}",
            lineterm="",
        )
    )
    lines = [_file_line_numbers(line, unit) if line.startswith("@@") else line for line in lines]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... {len(lines) - max_lines} more lines"]
    return "\n".join(lines)


def _file_line_numbers(header, unit):
    """Rewrite a hunk header from body line numbers to file line numbers."""
    m = HUNK_RE.match(header)
    if not m:
        return header
    old_start, old_rest, new_start, new_rest = int(m[1]), m[2] or "", int(m[3]), m[4] or ""
    if 0 < old_start <= len(unit.old_lines):
        old_start = unit.old_lines[old_start - 1]
    if 0 < new_start <= len(unit.new_lines):
        new_start = unit.new_lines[new_start - 1]
    return f"@@ -{old_start}{old_rest} +{new_start}{new_rest} @@"
