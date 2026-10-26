from reviewer.graph import build_graph, default_depth, diff_graphs, graph_signals, to_mermaid

BASE = {
    "src/app/__init__.py": "",
    "src/app/http/__init__.py": "",
    "src/app/http/client.py": "import requests\nfrom ..core import settings\n",
    "src/app/core/__init__.py": "from .settings import load\n",
    "src/app/core/settings.py": "import os\nimport yaml\n",
}


def test_build_graph_resolves_relative_and_absolute_imports():
    graph = build_graph(BASE)
    assert graph.modules == {"app", "app.http", "app.http.client", "app.core", "app.core.settings"}
    assert set(graph.edges) == {
        ("app.http.client", "app.core.settings"),
        ("app.core", "app.core.settings"),
    }
    assert dict(graph.external) == {"requests": {"app.http.client"}, "yaml": {"app.core.settings"}}


def test_imports_are_attributed_to_the_symbol_holding_them():
    graph = build_graph({"a.py": "def f():\n    import b\n", "b.py": ""})
    assert graph.edges[("a", "b")] == {"a.f"}


def test_from_package_import_name_points_at_package():
    graph = build_graph({"p/__init__.py": "X = 1\n", "m.py": "from p import X\n"})
    assert set(graph.edges) == {("m", "p")}


def test_default_depth():
    assert default_depth(["app.a.x", "app.b.y", "tests.t"]) == 3
    many = [f"app.m{i}.sub" for i in range(30)]
    assert default_depth(many) == 1


def head():
    files = dict(BASE)
    files["src/app/retry.py"] = "import tenacity\n"
    files["src/app/http/client.py"] = "import requests\nfrom ..retry import policy\n"
    return files


def test_diff_graphs():
    old, new = build_graph(BASE), build_graph(head())
    diff = diff_graphs(old, new, changed_modules={"app.http.client"}, depth=2)
    assert diff.nodes == {"app.core": "unchanged", "app.http": "changed", "app.retry": "added"}
    assert diff.edges == {("app.http", "app.core"): "removed", ("app.http", "app.retry"): "added"}
    assert diff.external_added == ["tenacity"]
    assert diff.external_removed == []


def test_graph_signals():
    signals = graph_signals(build_graph(BASE), build_graph(head()))
    assert signals == {
        "app.http.client": {"new-module-edge", "removed-module-edge"},
        "app.retry": {"new-dependency"},
    }


def test_to_mermaid():
    diff = diff_graphs(build_graph(BASE), build_graph(head()), {"app.http.client"}, depth=2)
    text = to_mermaid(diff)
    assert text.startswith("flowchart LR\n")
    assert '  n2["app.retry"]:::added' in text
    assert "  n1 -.-> n0" in text
    assert "  linkStyle 1 stroke:#1a7f37,stroke-width:2px" in text
