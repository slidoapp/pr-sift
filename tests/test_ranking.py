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
