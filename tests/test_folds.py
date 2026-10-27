from textwrap import dedent

from pr_sift.folds import find_folds


def folds(source, path="m.py", first_line=1):
    body = dedent(source).strip("\n")
    lines = list(range(first_line, first_line + len(body.splitlines())))
    return find_folds(body, lines, path)


CASES = """
CASES = [
    Case(name="embed", path="/embed", payload={"text": "a"}),
    Case(
        name="index",
        path="/index",
        payload={"id": 1},
    ),
    # Must run last
    Case(name="delete", path="/delete", payload={}, validate=check),
    Case(name="search", path="/search", payload={"q": "a   b"}),
]
"""


def test_list_of_calls_becomes_a_table():
    [fold] = folds(CASES, first_line=10)
    assert (fold.start, fold.end, fold.title, fold.summary) == (10, 20, "CASES", "4 × Case")
    assert fold.columns == ["name", "path", "payload", "validate"]
    assert [(r.start, r.end, r.cells["name"]) for r in fold.rows] == [
        (11, 11, "embed"),
        (12, 16, "index"),
        (18, 18, "delete"),
        (19, 19, "search"),
    ]
    assert fold.rows[2].cells["validate"] == "check"
    assert fold.rows[3].cells["payload"] == "{'q': 'a b'}"
    assert fold.rows[2].comment == "Must run last"


def test_mixed_callees_get_a_call_column():
    [fold] = folds(
        """
        CASES = [
            Case(name="a"),
            make_case("b"),
            make_case("c"),
            Case(
                name="d",
            ),
            make_case("e"),
        ]
        """
    )
    assert fold.summary == "5 calls: make_case ×3, Case ×2"
    assert fold.columns == ["call", "name", "#1"]
    assert fold.rows[1].cells == {"call": "make_case", "#1": "b"}


def test_tuples_are_named_after_the_comprehension_target():
    [fold] = folds(
        """
        CASES = [
            Case(path=path, limit=limit)
            for path, limit in [
                ("/a", 1),
                ("/b", 2),
                ("/c", 3),
                ("/d", 4),
                ("/e", 5),
                ("/f", 6),
            ]
        ]
        """
    )
    assert fold.title == "for path, limit in"
    assert fold.columns == ["path", "limit"]
    assert fold.rows[0].cells == {"path": "/a", "limit": "1"}


def test_short_or_small_collections_stay_unfolded():
    assert folds("X = [1, 2, 3, 4, 5, 6, 7, 8, 9]\n") == []
    assert folds("X = {\n    'a': 1,\n    'b': 2,\n    'c': 3,\n    'd': 4,\n}\n") == []
    assert folds("X = [\n    1,\n\n\n\n\n\n    2,\n]\n") == []


def test_dicts_fold_by_key():
    [fold] = folds("X = {\n" + "".join(f"    '{k}': {i},\n" for i, k in enumerate("abcdefg")) + "    **BASE,\n}\n")
    assert fold.summary == "8 entries"
    assert fold.columns == ["key", "value"]
    assert fold.rows[0].cells == {"key": "a", "value": "0"}
    assert fold.rows[-1].cells == {"key": "**", "value": "BASE"}


def test_nested_collections_fold_only_at_the_top():
    source = "X = [\n" + "".join(f"    [\n        {i},\n        {i},\n        {i},\n    ],\n" for i in range(3)) + "]\n"
    [fold] = folds(source)
    assert fold.summary == "3 items"
    assert fold.rows[0].cells == {"value": "[0, 0, 0]"}


def test_collections_inside_functions_and_calls():
    [fold] = folds(
        """
        def run():
            return send(cases=[
                {"name": "a", "path": "/a"},
                {"name": "b", "path": "/b"},
                {"name": "c", "path": "/c"},
                {"name": "d", "path": "/d"},
                {"name": "e", "path": "/e"},
                {"name": "f", "path": "/f"},
            ])
        """
    )
    assert fold.title == "cases="
    assert fold.columns == ["name", "path"]


def test_long_cells_are_capped():
    long = "x" * 400
    [fold] = folds("X = [\n" + "".join(f"    '{long}',\n" for _ in range(8)) + "]\n")
    assert len(fold.rows[0].cells["value"]) == 300
    assert fold.rows[0].cells["value"].endswith("…")


def test_json_file_becomes_a_table():
    body = '{\n  "a": {"x": 1, "y": {"z": 2}},\n  "b": {"x": 3},\n  "c": {"y": "text"}\n}'
    [fold] = find_folds(body, [1, 2, 3, 4, 5], "data/fixture.json")
    assert (fold.start, fold.end, fold.summary) == (1, 5, "3 entries")
    assert fold.columns == ["key", "x", "y"]
    assert [r.cells for r in fold.rows] == [
        {"key": "a", "x": "1", "y": '{"z": 2}'},
        {"key": "b", "x": "3"},
        {"key": "c", "y": "text"},
    ]


def test_other_files_and_broken_code_have_no_folds():
    assert find_folds("a: 1\n", [1], "config.yaml") == []
    assert find_folds("X = [\n", [1], "m.py") == []
    assert find_folds("{not json", [1], "f.json") == []
