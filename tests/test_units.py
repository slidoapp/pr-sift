from textwrap import dedent

from reviewer.units import diff_files, extract_symbols, is_public, module_name

SOURCE = '''\
import os

X = 1


@decorator
def f(a):
    return a


class C(Base):
    """Doc."""
    attr = 1

    def m(self):
        return 1

    class Inner:
        pass
'''


def test_extract_symbols():
    symbols = extract_symbols("src/pkg/mod.py", SOURCE)

    assert {q: s.kind for q, s in symbols.items()} == {
        "pkg.mod.<imports>": "imports",
        "pkg.mod.X": "statement",
        "pkg.mod.f": "function",
        "pkg.mod.C": "class",
        "pkg.mod.C.m": "method",
        "pkg.mod.C.Inner": "class",
    }
    assert symbols["pkg.mod.<imports>"].text == "import os"
    assert symbols["pkg.mod.X"].text == "X = 1"
    assert symbols["pkg.mod.f"].text == "@decorator\ndef f(a):\n    return a"
    assert symbols["pkg.mod.f"].lines == (6, 7, 8)
    assert symbols["pkg.mod.C"].text == 'class C(Base):\n    """Doc."""\n    attr = 1'
    assert symbols["pkg.mod.C"].lines == (11, 12, 13)


def test_top_level_statements_are_units_with_their_comments():
    source = dedent(
        """\
        \"\"\"Module doc.\"\"\"
        import os

        # The limit
        # in seconds
        LIMIT = 30
        a, b = 1, 2
        setup()
        setup()
        if os.environ.get("DEBUG"):
            LIMIT = 300

        # Helpers
        def f():
            pass
        """
    )
    symbols = extract_symbols("m.py", source)
    assert sorted(symbols) == [
        "m.<docstring>", "m.<if os.environ.get('DEBUG')>", "m.<imports>", "m.<setup()>", "m.<setup()> #2",
        "m.LIMIT", "m.a,b", "m.f",
    ]
    assert symbols["m.LIMIT"].text == "# The limit\n# in seconds\nLIMIT = 30"
    assert symbols["m.LIMIT"].lines == (4, 5, 6)
    assert symbols["m.f"].text == "# Helpers\ndef f():\n    pass"


def test_long_names_are_shortened():
    symbols = extract_symbols("m.py", "for item in fetch_all_the_things_from_somewhere(with_a_long_argument):\n    pass\n")
    assert list(symbols) == ["m.<for item in fetch_all_the_things_from_somewhere(wit…>"]


def test_module_without_top_level_code_has_no_module_symbol():
    assert list(extract_symbols("a.py", "def f():\n    pass\n")) == ["a.f"]


def test_non_python_and_unparsable_files_are_file_symbols():
    assert extract_symbols("README.md", "# Hi\n")["README.md"].kind == "file"
    assert extract_symbols("a.py", "def f(:\n")["a.py"].kind == "file"


def test_module_name():
    assert module_name("src/pkg/mod.py") == "pkg.mod"
    assert module_name("pkg/__init__.py") == "pkg"
    assert module_name("mod.py") == "mod"


def test_is_public():
    assert is_public("pkg.mod.Client.get")
    assert is_public("pkg.mod.Client.__init__")
    assert not is_public("pkg.mod._helper")
    assert not is_public("pkg._internal.Client")
    assert not is_public("pkg.mod.Client.__secret")


def diff(old, new, path="pkg/mod.py"):
    return diff_files({path: dedent(old)}, {path: dedent(new)})


def test_unchanged_and_reordered_symbols_are_not_units():
    old = """
        def f():
            return 1


        def g():
            return 2
    """
    new = """
        def g():
            return 2


        def f():
            return 1
    """
    assert diff(old, new) == []


def test_modified_unit_has_bodies_and_head_lines():
    units = diff(
        """
        def f():
            return 1
        """,
        """
        X = 1


        def f():
            return 2
        """,
    )
    assert [(u.qualname, u.status) for u in units] == [("pkg.mod.X", "added"), ("pkg.mod.f", "modified")]
    unit = units[1]
    assert unit.old_body == "def f():\n    return 1"
    assert unit.new_body == "def f():\n    return 2"
    assert unit.new_lines == (5, 6)


def test_added_and_removed_files():
    units = diff_files({"old.py": "def gone():\n    pass\n"}, {"new.py": "X = 1\n\ndef born():\n    return 1\n"})
    assert sorted((u.qualname, u.status) for u in units) == [
        ("new.X", "added"),
        ("new.born", "added"),
        ("old.gone", "removed"),
    ]


def test_symbol_moved_between_files():
    [unit] = diff_files(
        {"a.py": "def _f(x):\n    return x\n", "b.py": "X = 1\n"},
        {"a.py": "", "b.py": "X = 1\n\n\ndef _f(y):\n    return y\n"},
    )
    assert (unit.status, unit.qualname, unit.old_qualname, unit.path, unit.old_path) == (
        "moved", "b._f", "a._f", "b.py", "a.py",
    )


def test_symbol_renamed():
    [unit] = diff("def _old(x):\n    return x\n", "def _new(x):\n    return x\n")
    assert (unit.status, unit.qualname, unit.old_qualname) == ("renamed", "pkg.mod._new", "pkg.mod._old")


def test_fingerprint_ignores_formatting_and_docstrings_on_either_side():
    base = "def f(a, b):\n    return a + b\n"
    head = "def f(a, b):\n    return a * b\n"
    [reviewed] = diff(base, head)
    [current] = diff(
        "def f(a, b):\n    return (a+b)  # sum\n",
        'def f(a, b):\n    """Multiply."""\n    return a * b\n',
    )
    assert current.fingerprint == reviewed.fingerprint


def test_force_push_changes_only_touched_fingerprints():
    base = "def f():\n    return 1\n\n\ndef g():\n    return 1\n"
    first = "def f():\n    return 2\n\n\ndef g():\n    return 2\n"
    second = "def f():\n    return 3\n\n\ndef g():\n    return 2\n"
    before = {u.qualname: u.fingerprint for u in diff(base, first)}
    after = {u.qualname: u.fingerprint for u in diff(base, second)}
    assert after["pkg.mod.g"] == before["pkg.mod.g"]
    assert after["pkg.mod.f"] != before["pkg.mod.f"]
