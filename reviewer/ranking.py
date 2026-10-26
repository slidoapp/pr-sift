"""Validate the ranking the model writes for an analysis."""

import re

TIERS = ("architecture", "interface", "behavior", "detail")
BRACKETS_ONLY = re.compile(r"^[\s()\[\]{},;:]*$")
ATTENTION = ("read", "skim", "hide")


def validate(data: object, expected: set[str], known: set[str], lines: dict[str, dict[int, str]] | None = None) -> list[str]:
    """Return what's wrong with a ranking, or an empty list.

    expected are the fingerprints that must be ranked, known those that
    may be ranked. lines maps fingerprints to the file lines a highlight
    or section may point at, with their text.
    """
    if not isinstance(data, dict):
        return ["the ranking must be a JSON object"]
    errors = []
    if not isinstance(data.get("overview"), str) or not data["overview"].strip():
        errors.append("overview must be a non-empty string")
    if not isinstance(data.get("architecture_summary"), (str, type(None))):
        errors.append("architecture_summary must be a string or null")

    components = data.get("components")
    if not isinstance(components, list):
        errors.append("components must be a list")
    else:
        for i, component in enumerate(components):
            if not (
                isinstance(component, dict)
                and isinstance(component.get("name"), str)
                and isinstance(component.get("behavior"), list)
                and all(isinstance(b, str) for b in component["behavior"])
            ):
                errors.append(f"components[{i}] must have a name string and a behavior list of strings")

    units = data.get("units")
    if not isinstance(units, list):
        errors.append("units must be a list")
        return errors
    ranked = set()
    for i, unit in enumerate(units):
        if not isinstance(unit, dict):
            errors.append(f"units[{i}] must be an object")
            continue
        fingerprint = unit.get("fingerprint")
        if fingerprint not in known:
            errors.append(f"units[{i}]: unknown fingerprint {fingerprint!r}")
        if unit.get("tier") not in TIERS:
            errors.append(f"units[{i}]: tier must be one of {', '.join(TIERS)}")
        for key in ("reason", "summary"):
            if not isinstance(unit.get(key), str) or not unit[key].strip():
                errors.append(f"units[{i}]: {key} must be a non-empty string")
        if not isinstance(unit.get("description"), (str, type(None))):
            errors.append(f"units[{i}]: description must be a string or null")
        if unit.get("attention", "read") not in ATTENTION:
            errors.append(f"units[{i}]: attention must be one of {', '.join(ATTENTION)}")
        allowed = (lines or {}).get(fingerprint)
        errors += _highlight_errors(i, unit.get("highlights", []), allowed)
        errors += _section_errors(i, unit.get("sections", []), allowed)
        ranked.add(fingerprint)
    if missing := expected - ranked:
        errors.append(f"{len(missing)} units are not ranked: {', '.join(sorted(missing))}")
    return errors


def _highlight_errors(i, highlights, allowed):
    if not isinstance(highlights, list):
        return [f"units[{i}]: highlights must be a list"]
    errors = []
    for j, highlight in enumerate(highlights):
        where = f"units[{i}].highlights[{j}]"
        if not isinstance(highlight, dict) or not isinstance(highlight.get("note"), str) or not highlight["note"].strip():
            errors.append(f"{where} must have a non-empty note")
            continue
        line = highlight.get("line")
        if not isinstance(line, int) or isinstance(line, bool):
            errors.append(f"{where}: line must be a file line number")
        elif allowed is not None and line not in allowed:
            errors.append(f"{where}: line {line} is not in the unit's diff")
        elif isinstance(allowed, dict) and BRACKETS_ONLY.match(allowed[line]):
            errors.append(f"{where}: line {line} holds only brackets; point at the line with the code it's about")
    return errors


def _section_errors(i, sections, allowed):
    if not isinstance(sections, list):
        return [f"units[{i}]: sections must be a list"]
    errors = []
    for j, section in enumerate(sections):
        where = f"units[{i}].sections[{j}]"
        if not isinstance(section, dict) or not isinstance(section.get("title"), str) or not section["title"].strip():
            errors.append(f"{where} must have a non-empty title")
            continue
        line = section.get("line")
        if not isinstance(line, int) or isinstance(line, bool):
            errors.append(f"{where}: line must be a file line number")
        elif allowed is not None and line not in allowed:
            errors.append(f"{where}: line {line} is not in the unit's diff")
        elif isinstance(allowed, dict) and BRACKETS_ONLY.match(allowed[line]):
            errors.append(f"{where}: line {line} holds only brackets; point at the line with the code it's about")
    return errors


BLOCK_TYPES = ("text", "code", "units", "architecture")


def validate_walkthrough(blocks: object, units: dict[str, dict]) -> list[str]:
    """Return what's wrong with a walkthrough, or an empty list.

    units maps each qualname to {"attention": read | skim | hide | hidden,
    "lines": file lines of the unit}. Every read unit must be shown in a
    code block and every skim unit must at least be listed, so that
    reading the walkthrough covers the review.
    """
    if not isinstance(blocks, list) or not blocks:
        return ["walkthrough must be a non-empty list of blocks"]
    errors, shown, listed = [], set(), set()
    for i, block in enumerate(blocks):
        where = f"walkthrough[{i}]"
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in BLOCK_TYPES:
            errors.append(f"{where}: type must be one of {', '.join(BLOCK_TYPES)}")
        elif kind == "text":
            if not isinstance(block.get("text"), str) or not block["text"].strip():
                errors.append(f"{where}: text must be a non-empty string")
        elif kind == "code":
            name = block.get("unit")
            if name not in units:
                errors.append(f"{where}: unknown unit {name!r}")
                continue
            lines = block.get("lines")
            if lines is not None:
                if not (isinstance(lines, list) and len(lines) == 2 and all(isinstance(n, int) for n in lines)):
                    errors.append(f"{where}: lines must be [first, last] file lines")
                elif not any(lines[0] <= n <= lines[1] for n in units[name]["lines"]):
                    errors.append(f"{where}: lines {lines} are outside {name}")
            shown.add(name)
        elif kind == "units":
            names = block.get("units")
            if not isinstance(block.get("title"), str) or not block["title"].strip():
                errors.append(f"{where}: title must be a non-empty string")
            if not isinstance(names, list) or not names:
                errors.append(f"{where}: units must be a non-empty list of unit names")
                continue
            for name in names:
                if name not in units:
                    errors.append(f"{where}: unknown unit {name!r}")
            listed.update(names)
    for name, unit in sorted(units.items()):
        if unit["attention"] == "read" and name not in shown:
            errors.append(f"read unit {name} is not shown in a code block")
        elif unit["attention"] == "skim" and name not in shown | listed:
            errors.append(f"skim unit {name} is not shown or listed")
    return errors


def to_offsets(entries: list[dict], old_lines: list[int], new_lines: list[int]) -> list[dict]:
    """Store highlight or section positions relative to the unit, so they
    stay right when the unit moves (a force-push adding code above it, or
    the same code in another PR): {"line": 226} becomes {"side": "new",
    "offset": 6}, the index in the unit's new (or else old) lines."""
    stored = []
    for entry in entries:
        line = entry["line"]
        side, lines = ("new", new_lines) if line in new_lines else ("old", old_lines)
        stored.append({k: v for k, v in entry.items() if k != "line"} | {"side": side, "offset": lines.index(line)})
    return stored


def to_lines(entries: list[dict], old_lines: list[int], new_lines: list[int]) -> list[dict]:
    """The inverse of to_offsets for the unit as it is now. Entries stored
    with absolute lines by earlier versions are kept as they are."""
    shown = []
    for entry in entries:
        if "line" in entry:
            shown.append(entry)
            continue
        lines = new_lines if entry["side"] == "new" else old_lines
        if 0 <= entry["offset"] < len(lines):
            shown.append({k: v for k, v in entry.items() if k not in ("side", "offset")} | {"line": lines[entry["offset"]]})
    return shown
