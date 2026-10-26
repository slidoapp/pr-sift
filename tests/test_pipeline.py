"""End-to-end: analyze a PR in a local repository, rank it, ingest it."""

import json

import pytest

from reviewer import cli, github, repo
from reviewer.analysis import analyze_revisions
from reviewer.store import Store

BASE = {
    "pyproject.toml": '[project]\ndependencies = ["requests"]\n',
    "app/__init__.py": "",
    "app/client.py": (
        "import requests\n\n\n"
        "def fetch(url):\n"
        '    """Fetch a URL."""\n'
        "    return requests.get(url)\n\n\n"
        "def _parse(text):\n"
        "    data = text.strip()\n"
        "    return data\n"
    ),
}
HEAD = {
    "pyproject.toml": '[project]\ndependencies = ["requests", "tenacity"]\n',
    "app/client.py": (
        "import requests\n\n"
        "from app.retry import with_retry\n\n\n"
        "def fetch(url, retries=3):\n"
        '    """Fetch a URL, retrying."""\n'
        "    return with_retry(requests.get, url, retries)\n\n\n"
        "def _parse(raw):\n"
        "    value = raw.strip()\n"
        "    return value\n"
    ),
    "app/retry.py": "import tenacity\n\n\ndef with_retry(fn, *args):\n    return fn(*args[:-1])\n",
}


@pytest.fixture
def pr_repo(git_repo):
    base = git_repo.commit(BASE)
    git_repo.git("switch", "--quiet", "-c", "feature")
    head = git_repo.commit(HEAD)
    return git_repo, base, head


def test_analyze_revisions(pr_repo):
    git_repo, base, head = pr_repo
    analysis = analyze_revisions(git_repo.git_dir, base, head)

    units = {u.qualname: (u.status, u.noise, u.signals) for u in analysis.units}
    assert units == {
        "app.client.<imports>": ("modified", None, ["new-module-edge"]),
        "app.client.fetch": ("modified", None, ["public-signature"]),
        "app.client._parse": ("modified", "local-rename", []),
        "app.retry.<imports>": ("added", None, ["new-dependency"]),
        "app.retry.with_retry": ("added", None, ["public-signature"]),
        "pyproject.toml": ("modified", None, ["dependency"]),
    }
    assert analysis.graph.edges == {("app.client", "app.retry"): "added"}
    assert analysis.graph.external_added == ["tenacity"]


@pytest.fixture
def fake_github(pr_repo, monkeypatch):
    git_repo, base, head = pr_repo
    meta = {
        "number": 7,
        "title": "Add retries",
        "body": "Retries flaky requests.",
        "url": "https://github.com/o/r/pull/7",
        "author": {"login": "someone"},
        "baseRefName": "main",
        "baseRefOid": base,
        "headRefName": "feature",
        "headRefOid": head,
    }
    monkeypatch.setattr(github, "pr_metadata", lambda pr: meta)
    monkeypatch.setattr(repo, "ensure_clone", lambda pr: git_repo.git_dir)
    monkeypatch.setattr(repo, "fetch_pr", lambda *args: None)
    return git_repo


def rank_everything(analysis_json):
    return {
        "overview": "Adds retries to fetch.",
        "architecture_summary": "The client now depends on a new retry module.",
        "components": [{"name": "client", "behavior": ["fetch retries up to 3 times"]}],
        "units": [
            {"fingerprint": u["fingerprint"], "tier": "behavior", "reason": "r", "summary": "s"}
            for u in analysis_json["units"]
        ],
    }


def test_analyze_ingest_and_reanalyze(fake_github, tmp_path, capsys):
    out = tmp_path / "analysis.json"
    assert cli.main(["analyze", "o/r#7", "--out", str(out)]) == 0
    first = json.loads(out.read_text())
    assert first["pr"]["title"] == "Add retries"
    assert first["counts"] == {"units": 6, "hidden": {"local-rename": 1}, "ranked": 0, "to_rank": 5}
    assert first["architecture"]["mermaid"].startswith("flowchart LR")
    assert {u["qualname"] for u in first["units"]} == {
        "app.client.<imports>", "app.client.fetch", "app.retry.<imports>", "app.retry.with_retry", "pyproject.toml",
    }
    fetch = next(u for u in first["units"] if u["qualname"] == "app.client.fetch")
    assert "    6 + def fetch(url, retries=3):" in fetch["diff"]

    # An incomplete ranking is rejected and nothing is stored.
    partial = rank_everything(first)
    partial["units"] = partial["units"][:1]
    (tmp_path / "partial.json").write_text(json.dumps(partial))
    assert cli.main(["ingest", "o/r#7", str(tmp_path / "partial.json")]) == 1
    assert "4 units are not ranked" in capsys.readouterr().err

    (tmp_path / "ranking.json").write_text(json.dumps(rank_everything(first)))
    assert cli.main(["ingest", "o/r#7", str(tmp_path / "ranking.json"), "--model", "test"]) == 0
    assert "stored 5 rankings; 5 behavior" in capsys.readouterr().out

    # A second analysis only asks for what isn't ranked yet.
    assert cli.main(["analyze", "o/r#7", "--out", str(out)]) == 0
    second = json.loads(out.read_text())
    assert second["counts"]["ranked"] == 5
    assert second["units"] == []

    assert cli.main(["status"]) == 0
    assert "o/r#7  Add retries" in capsys.readouterr().out

    store = Store()
    pr = store.pr("github.com/o/r", 7)
    assert store.summary(pr["id"])["overview"] == "Adds retries to fetch."


def test_ingest_before_analyze_fails(tmp_path, capsys):
    (tmp_path / "ranking.json").write_text("{}")
    assert cli.main(["ingest", "o/r#1", str(tmp_path / "ranking.json")]) == 1
    assert "has not been analyzed" in capsys.readouterr().err


def test_unit_diff_uses_file_line_numbers():
    from reviewer.analysis import unit_diff
    from reviewer.units import diff_files

    old = "X = 1\n\n\ndef f():\n    a = 1\n    return a\n"
    new = "X = 1\n\n\ndef f():\n    a = 2\n    return a\n"
    [unit] = diff_files({"m.py": old}, {"m.py": new})
    assert unit_diff(unit) == "\n".join([
        "    4   def f():",
        "    5 -     a = 1",
        "    5 +     a = 2",
        "    6       return a",
    ])


def test_imports_without_new_dependencies_are_noise(git_repo):
    base = git_repo.commit({"a.py": "import os\n\n\ndef f():\n    return os.sep\n", "b.py": "X = 1\n"})
    head = git_repo.commit({
        "a.py": "import os\nimport sys\n\n\ndef f():\n    return os.sep + sys.platform\n",
        "b.py": "import a\n\nX = 1\n",
    })
    units = {u.qualname: u for u in analyze_revisions(git_repo.git_dir, base, head).units}
    assert units["a.<imports>"].noise == "imports"
    assert units["b.<imports>"].noise is None
    assert units["b.<imports>"].signals == ["new-module-edge"]
