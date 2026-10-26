from textwrap import dedent

import pytest

from reviewer.noise import classify
from reviewer.units import diff_files


def noise(old, new, path="pkg/mod.py"):
    [unit] = classify(diff_files({path: dedent(old)}, {path: dedent(new)}))
    return unit.noise


def test_comment_change():
    assert noise(
        """
        def f():
            return 1  # one
        """,
        """
        def f():
            # the answer
            return 1
        """,
    ) == "comment"


def test_formatting_change():
    assert noise(
        """
        def f(a, b):
            return (a+b)
        """,
        """
        def f(a, b):
            return a + b
        """,
    ) == "formatting"


def test_docstring_change():
    assert noise(
        """
        class C:
            '''Old.'''
            x = 1
        """,
        """
        class C:
            '''New and better.'''
            x = 1
        """,
    ) == "docstring"


def test_local_rename():
    assert noise(
        """
        def total(items):
            acc = 0
            for item in items:
                acc += item
            return acc
        """,
        """
        def total(items):
            result = 0
            for value in items:
                result += value
            return result
        """,
    ) == "local-rename"


def test_parameter_rename_of_private_function_is_noise():
    assert noise("def _f(a):\n    return a\n", "def _f(b):\n    return b\n") == "local-rename"


def test_parameter_rename_of_public_function_is_not_noise():
    assert noise("def f(a):\n    return a\n", "def f(b):\n    return b\n") is None


@pytest.mark.parametrize(
    "old, new",
    [
        ("def f():\n    return 1\n", "def f():\n    return 2\n"),
        ("def f():\n    global a\n    a += 1\n", "def f():\n    global b\n    b += 1\n"),
        ("def f(self):\n    return self.a\n", "def f(self):\n    return self.b\n"),
        ("def f():\n    return g(a=1)\n", "def f():\n    return g(b=1)\n"),
        ("def f():\n    return x\n", "def f():\n    return y\n"),
    ],
    ids=["logic", "global", "attribute", "keyword-argument", "outer-name"],
)
def test_meaningful_changes_are_not_noise(old, new):
    assert noise(old, new) is None


def test_import_reorder():
    assert noise("import os\nimport sys\n\nX = 1\n", "import sys\nimport os\n\nX = 1\n") == "imports"


def test_import_added_is_not_noise():
    assert noise("import os\n", "import os\nimport sys\n") is None


def test_private_move_is_noise_public_move_is_not():
    units = classify(
        diff_files(
            {"a.py": "def _f():\n    return 1\n\n\ndef g():\n    return 2\n"},
            {"a.py": "", "b.py": "def _f():\n    return 1\n\n\ndef g():\n    return 2\n"},
        )
    )
    assert {u.qualname: (u.status, u.noise) for u in units} == {
        "b._f": ("moved", "move"),
        "b.g": ("moved", None),
    }


def test_private_rename_is_noise_public_rename_is_not():
    assert noise("def _a():\n    return 1\n", "def _b():\n    return 1\n") == "symbol-rename"
    assert noise("def a():\n    return 1\n", "def b():\n    return 1\n") is None


def test_generated_files():
    assert noise("a = 1\n", "a = 2\n", path="uv.lock") == "generated"
    assert noise("a = 1\n", "a = 2\n", path="api/service_pb2.py") == "generated"
