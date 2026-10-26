from reviewer.diffs import numbered_diff


def test_numbered_diff_keeps_context_around_changes():
    old = "\n".join(f"line {i}" for i in range(1, 21))
    new = old.replace("line 2\n", "line two\n").replace("line 18", "line eighteen")
    lines = list(range(101, 121))
    assert numbered_diff(old, new, lines, lines, max_lines=100).splitlines() == [
        "  101   line 1",
        "  102 - line 2",
        "  102 + line two",
        "  103   line 3",
        "  104   line 4",
        "  105   line 5",
        "   ...",
        "  115   line 15",
        "  116   line 16",
        "  117   line 17",
        "  118 - line 18",
        "  118 + line eighteen",
        "  119   line 19",
        "  120   line 20",
    ]


def test_numbered_diff_is_capped():
    new = "\n".join(f"x{i}" for i in range(10))
    assert numbered_diff("", new, [], list(range(1, 11)), max_lines=3).splitlines()[-1] == "... 7 more lines"
