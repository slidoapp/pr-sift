from reviewer.ranking import validate


def ranking(**overrides):
    data = {
        "overview": "Adds retries.",
        "architecture_summary": None,
        "components": [{"name": "http", "behavior": ["Retries on 429"]}],
        "units": [{"fingerprint": "a", "tier": "behavior", "reason": "new retry loop", "summary": "Retries."}],
    }
    return data | overrides


def test_valid_ranking():
    assert validate(ranking(), expected={"a"}, known={"a", "b"}) == []


def test_missing_and_unknown_units():
    errors = validate(
        ranking(units=[{"fingerprint": "z", "tier": "behavior", "reason": "r", "summary": "s"}]),
        expected={"a"},
        known={"a"},
    )
    assert errors == ["units[0]: unknown fingerprint 'z'", "1 units are not ranked: a"]


def test_field_errors():
    errors = validate(
        ranking(
            overview="",
            components=[{"name": "x"}],
            units=[{"fingerprint": "a", "tier": "major", "reason": "", "summary": "s"}],
        ),
        expected=set(),
        known={"a"},
    )
    assert errors == [
        "overview must be a non-empty string",
        "components[0] must have a name string and a behavior list of strings",
        "units[0]: tier must be one of architecture, interface, behavior, detail",
        "units[0]: reason must be a non-empty string",
    ]


def test_not_an_object():
    assert validate([], set(), set()) == ["the ranking must be a JSON object"]


def test_description_is_optional_but_must_be_a_string():
    unit = {"fingerprint": "a", "tier": "interface", "reason": "r", "summary": "s"}
    assert validate(ranking(units=[unit | {"description": "Does x."}]), {"a"}, {"a"}) == []
    assert validate(ranking(units=[unit | {"description": None}]), {"a"}, {"a"}) == []
    assert validate(ranking(units=[unit | {"description": 3}]), {"a"}, {"a"}) == [
        "units[0]: description must be a string or null"
    ]


def test_attention_and_highlights():
    unit = {"fingerprint": "a", "tier": "behavior", "reason": "r", "summary": "s"}
    lines = {"a": {10, 11, 12}}
    good = unit | {"attention": "skim", "highlights": [{"line": 11, "note": "max int as id"}]}
    assert validate(ranking(units=[good]), {"a"}, {"a"}, lines) == []
    assert validate(ranking(units=[unit]), {"a"}, {"a"}, lines) == []

    bad = unit | {
        "attention": "ignore",
        "highlights": [{"line": 99, "note": "x"}, {"line": "11", "note": "x"}, {"line": 11, "note": ""}],
    }
    assert validate(ranking(units=[bad]), {"a"}, {"a"}, lines) == [
        "units[0]: attention must be one of read, skim, hide",
        "units[0].highlights[0]: line 99 is not in the unit's diff",
        "units[0].highlights[1]: line must be a file line number",
        "units[0].highlights[2] must have a non-empty note",
    ]
    assert validate(ranking(units=[unit | {"highlights": {}}]), {"a"}, {"a"}, lines) == ["units[0]: highlights must be a list"]


def test_sections():
    unit = {"fingerprint": "a", "tier": "behavior", "reason": "r", "summary": "s"}
    lines = {"a": {10, 11, 12}}
    ok = unit | {"sections": [{"line": 10, "title": "Load"}, {"line": 12, "title": "Write"}]}
    assert validate(ranking(units=[ok]), {"a"}, {"a"}, lines) == []
    bad = unit | {"sections": [{"line": 99, "title": "x"}, {"line": 10, "title": " "}]}
    assert validate(ranking(units=[bad]), {"a"}, {"a"}, lines) == [
        "units[0].sections[0]: line 99 is not in the unit's diff",
        "units[0].sections[1] must have a non-empty title",
    ]


def test_walkthrough():
    from reviewer.ranking import validate_walkthrough

    units = {
        "m.run": {"attention": "read", "lines": {10, 11, 12}},
        "m.helper": {"attention": "skim", "lines": {20}},
        "m.CONST": {"attention": "hide", "lines": {1}},
    }
    good = [
        {"type": "text", "text": "Adds a **runner**."},
        {"type": "architecture"},
        {"type": "code", "unit": "m.run", "lines": [10, 11]},
        {"type": "units", "title": "Helpers", "units": ["m.helper"]},
    ]
    assert validate_walkthrough(good, units) == []

    bad = [
        {"type": "text", "text": ""},
        {"type": "code", "unit": "m.nope"},
        {"type": "code", "unit": "m.run", "lines": [50, 60]},
        {"type": "units", "title": "", "units": []},
        {"type": "chart"},
    ]
    assert validate_walkthrough(bad, units) == [
        "walkthrough[0]: text must be a non-empty string",
        "walkthrough[1]: unknown unit 'm.nope'",
        "walkthrough[2]: lines [50, 60] are outside m.run",
        "walkthrough[3]: title must be a non-empty string",
        "walkthrough[3]: units must be a non-empty list of unit names",
        "walkthrough[4]: type must be one of text, code, units, architecture",
        "skim unit m.helper is not shown or listed",
    ]
    assert validate_walkthrough([], units) == ["walkthrough must be a non-empty list of blocks"]


def test_highlights_and_sections_must_not_point_at_bracket_lines():
    unit = {"fingerprint": "a", "tier": "behavior", "reason": "r", "summary": "s"}
    lines = {"a": {10: "    return {", 11: "        k: v,", 12: "    }"}}
    bad = unit | {"highlights": [{"line": 12, "note": "n"}], "sections": [{"line": 12, "title": "t"}]}
    assert validate(ranking(units=[bad]), {"a"}, {"a"}, lines) == [
        "units[0].highlights[0]: line 12 holds only brackets; point at the line with the code it's about",
        "units[0].sections[0]: line 12 holds only brackets; point at the line with the code it's about",
    ]
    good = unit | {"highlights": [{"line": 11, "note": "n"}]}
    assert validate(ranking(units=[good]), {"a"}, {"a"}, lines) == []
