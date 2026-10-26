"""Validate the ranking the model writes for an analysis."""

TIERS = ("architecture", "interface", "behavior", "detail")
ATTENTION = ("read", "skim", "hide")


def validate(data: object, expected: set[str], known: set[str], lines: dict[str, set[int]] | None = None) -> list[str]:
    """Return what's wrong with a ranking, or an empty list.

    expected are the fingerprints that must be ranked, known those that
    may be ranked. lines maps fingerprints to the file lines a highlight
    may point at.
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
        errors += _highlight_errors(i, unit.get("highlights", []), (lines or {}).get(fingerprint))
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
    return errors
