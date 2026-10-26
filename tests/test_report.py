import json
import threading
import urllib.error
import urllib.request

import pytest

from reviewer.noise import classify
from reviewer.report import diff_rows, page_data
from reviewer.server import ReportServer
from reviewer.store import Store
from reviewer.units import diff_files

BASE = {"pkg/mod.py": "def f():\n    return 1\n\n\ndef g():\n    return 1\n\n\ndef _h(a):\n    return a\n"}
FIRST = {"pkg/mod.py": "def f():\n    return 2\n\n\ndef g():\n    return 2\n\n\ndef _h(b):\n    return b\n"}
SECOND = {"pkg/mod.py": "def f():\n    return 3\n\n\ndef g():\n    return 2\n\n\ndef _h(b):\n    return b\n"}


def analyze(store, head):
    units = classify(diff_files(BASE, head))
    return store.save_analysis("github.com/o/r", 1, {"title": "T", "head_sha": "abc"}, units, {"architecture": None})


def states(store, pr_id):
    return {u["qualname"]: u["state"] for u in page_data(store, pr_id)["units"]}


def unit(store, pr_id, qualname):
    return next(u for u in store.units(pr_id) if u["qualname"] == qualname)


def test_review_state_survives_a_force_push():
    store = Store()
    pr_id = analyze(store, FIRST)
    assert states(store, pr_id) == {"pkg.mod.f": "new", "pkg.mod.g": "new", "pkg.mod._h": "new"}

    for name in ("pkg.mod.f", "pkg.mod.g"):
        u = unit(store, pr_id, name)
        store.set_review(pr_id, name, u["fingerprint"], u["new_body"], "reviewed")
    assert states(store, pr_id)["pkg.mod.f"] == "seen"

    # The author force-pushes a new version of f only.
    pr_id = analyze(store, SECOND)
    data = page_data(store, pr_id)
    by_name = {u["qualname"]: u for u in data["units"]}
    assert {q: u["state"] for q, u in by_name.items()} == {
        "pkg.mod.f": "changed",
        "pkg.mod.g": "seen",
        "pkg.mod._h": "new",
    }
    assert by_name["pkg.mod.f"]["since_review"] == [
        [" ", None, 1, "def f():"],
        ["-", None, None, "    return 2"],
        ["+", None, 2, "    return 3"],
    ]
    assert by_name["pkg.mod._h"]["noise"] == "local-rename"
    assert data["has_reviews"]


def test_diff_rows_use_file_line_numbers():
    assert diff_rows("a\nb\nc", "a\nB\nc", [10, 11, 12], [20, 21, 22]) == [
        [" ", 10, 20, "a"],
        ["-", 11, None, "b"],
        ["+", None, 21, "B"],
        [" ", 12, 22, "c"],
    ]


@pytest.fixture
def server():
    store = Store()
    pr_id = analyze(store, FIRST)
    server = ReportServer(store, pr_id)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def request(server, method, path, body=None, token=True, host=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Reviewer-Token"] = server.token
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.url.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_index_carries_the_token(server):
    status, body = request(server, "GET", "/", token=False)
    assert status == 200
    assert server.token.encode() in body


def test_static_files_and_traversal(server):
    assert request(server, "GET", "/static/app.js", token=False)[0] == 200
    assert request(server, "GET", "/static/../server.py", token=False)[0] == 404
    assert request(server, "GET", "/static/vendor/../../server.py", token=False)[0] == 404


def test_api_requires_token_and_host(server):
    assert request(server, "GET", "/api/pr", token=False)[0] == 403
    assert request(server, "GET", "/api/pr", host="evil.example:80")[0] == 403
    status, body = request(server, "GET", "/api/pr")
    assert status == 200
    assert json.loads(body)["pr"]["title"] == "T"
    assert request(server, "GET", "/api/nope")[0] == 404


def test_mark_reviewed_and_flag(server):
    fingerprint = unit(server.store, server.pr_id, "pkg.mod.f")["fingerprint"]
    status, body = request(server, "PUT", "/api/units/pkg.mod.f/review", {"fingerprint": fingerprint, "reviewed": True})
    assert status == 200
    assert json.loads(body)["state"] == "seen"

    status, body = request(server, "PUT", "/api/units/pkg.mod.f/review", {"fingerprint": fingerprint, "flagged": True})
    assert (json.loads(body)["state"], json.loads(body)["flagged"]) == ("seen", True)

    status, body = request(server, "PUT", "/api/units/pkg.mod.f/review", {"fingerprint": fingerprint, "reviewed": False})
    assert (json.loads(body)["state"], json.loads(body)["flagged"]) == ("new", True)


def test_review_of_stale_or_unknown_unit_is_rejected(server):
    assert request(server, "PUT", "/api/units/pkg.mod.f/review", {"fingerprint": "old", "reviewed": True})[0] == 409
    assert request(server, "PUT", "/api/units/pkg.mod.nope/review", {"fingerprint": "x", "reviewed": True})[0] == 404
    assert request(server, "PUT", "/api/units/pkg.mod.f/review", {"reviewed": True}, token=False)[0] == 403


def test_doc_line():
    from reviewer.report import doc_line

    assert doc_line('    def get(self):\n        """Fetch it.\n\n        More."""\n        return 1') == "Fetch it."
    assert doc_line('"""Module doc."""\nX = 1') == "Module doc."
    assert doc_line("def f():\n    return 1") is None
    assert doc_line("def f(:") is None


def test_units_carry_changed_lines_and_docstrings():
    store = Store()
    old = {"pkg/mod.py": 'def fetch(url):\n    """Fetch a URL."""\n    return url\n'}
    new = {"pkg/mod.py": 'def fetch(url, retries=3):\n    """Fetch a URL."""\n    return url\n'}
    from reviewer.analysis import Analysis, report_data
    from reviewer.graph import GraphDiff
    from reviewer.interfaces import annotate, diff_interfaces

    units = classify(diff_files(old, new))
    interfaces = diff_interfaces(old, new)
    annotate(units, interfaces)
    graph = GraphDiff(1, {}, {}, [], [])
    pr_id = store.save_analysis("github.com/o/r", 2, {"title": "T"}, units, report_data(Analysis(units, interfaces, graph)))
    store.save_rankings(
        [{"fingerprint": units[0].fingerprint, "tier": "interface", "reason": "r", "summary": "s", "description": "d"}],
        model=None,
    )

    data = page_data(store, pr_id)
    [view] = data["units"]
    assert view["changed_lines"] == 2
    assert view["docstring"] == "Fetch a URL."
    assert view["description"] == "d"
    assert [(c["category"], c["name"], c["after"], c["qualnames"]) for c in data["interfaces"]] == [
        ("signature", "pkg.mod.fetch", "def fetch(url, retries=3)", ["pkg.mod.fetch"]),
    ]


def test_old_databases_get_the_description_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE rankings (fingerprint TEXT PRIMARY KEY, tier TEXT NOT NULL, reason TEXT NOT NULL,"
        " summary TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO rankings VALUES ('f', 'detail', 'r', 's', NULL, 'now')")
    conn.commit()
    conn.close()

    store = Store(path)
    assert store.rankings(["f"])["f"]["description"] is None
    store.save_rankings([{"fingerprint": "g", "tier": "detail", "reason": "r", "summary": "s", "description": "d"}], None)
    assert store.rankings(["g"])["g"]["description"] == "d"


def test_folds_apply_only_to_wholly_added_or_unchanged_lines():
    from reviewer.folds import Fold
    from reviewer.report import shown_folds

    diff = [["+", None, 1, "a"], ["+", None, 2, "b"], [" ", 3, 3, "c"], ["-", 4, None, "d"], [" ", 5, 4, "e"]]
    folds = [Fold(1, 2, None, "s", []), Fold(2, 3, None, "s", []), Fold(3, 4, None, "s", []), Fold(9, 9, None, "s", [])]
    assert [(f["start"], f["changed_lines"]) for f in shown_folds(folds, diff)] == [(1, 2)]


def test_units_carry_attention_and_highlights():
    store = Store()
    pr_id = analyze(store, FIRST)
    f = unit(store, pr_id, "pkg.mod.f")
    g = unit(store, pr_id, "pkg.mod.g")
    store.save_rankings(
        [
            # A highlight makes a unit read, whatever Claude said.
            {"fingerprint": f["fingerprint"], "tier": "behavior", "reason": "r", "summary": "s",
             "attention": "skim", "highlights": [{"line": 2, "note": "n"}]},
            {"fingerprint": g["fingerprint"], "tier": "detail", "reason": "r", "summary": "s", "attention": "hide"},
        ],
        model=None,
    )
    views = {u["qualname"]: u for u in page_data(store, pr_id)["units"]}
    assert (views["pkg.mod.f"]["attention"], views["pkg.mod.f"]["highlights"]) == ("read", [{"line": 2, "note": "n"}])
    assert (views["pkg.mod.g"]["attention"], views["pkg.mod.g"]["highlights"]) == ("hide", [])
    assert views["pkg.mod._h"]["attention"] == "hidden"


def test_key_lines_never_hide_every_change():
    from reviewer.report import key_lines

    unit_row = {"path": "m.py", "kind": "function", "old_body": "def f():\n    x = 1\n", "new_body": "def f():\n    x = 2\n"}
    diff = [[" ", 1, 1, "def f():"], ["-", 2, None, "    x = 1"], ["+", None, 2, "    x = 2"]]
    assert key_lines(unit_row, [1, 2], [1, 2], diff) == {"new": [1, 2], "old": [2]}


def test_walkthrough_blocks_go_stale_when_their_unit_changes():
    store = Store()
    pr_id = analyze(store, FIRST)
    f = unit(store, pr_id, "pkg.mod.f")
    blocks = [
        {"type": "text", "text": "Intro"},
        {"type": "code", "unit": "pkg.mod.f", "fingerprint": f["fingerprint"]},
        {"type": "units", "title": "More", "units": ["pkg.mod.g"], "fingerprints": [unit(store, pr_id, "pkg.mod.g")["fingerprint"]]},
    ]
    store.save_summary(pr_id, "abc", "o", None, [], blocks)
    fresh = page_data(store, pr_id)["summary"]["walkthrough"]
    assert (fresh[1]["stale"], fresh[2]["stale"]) == (False, [False])

    pr_id = analyze(store, SECOND)  # f changes, g doesn't
    later = page_data(store, pr_id)["summary"]["walkthrough"]
    assert (later[1]["stale"], later[2]["stale"]) == (True, [False])


def test_migrating_a_column_another_process_added_is_fine(tmp_path):
    """Two processes open the store at once: both see the column missing,
    and the second one's ALTER TABLE finds it already added."""
    path = tmp_path / "db.sqlite"
    store = Store(path)
    store.conn.execute("ALTER TABLE pr_summary DROP COLUMN walkthrough")
    stale = store.conn.execute("PRAGMA table_info(pr_summary)").fetchall()
    store.conn.execute("ALTER TABLE pr_summary ADD COLUMN walkthrough TEXT")  # the other process

    class SeesTheOldSchema:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            if sql == "PRAGMA table_info(pr_summary)":
                return stale
            return self.conn.execute(sql, *args)

        def __enter__(self):
            return self.conn.__enter__()

        def __exit__(self, *exc):
            return self.conn.__exit__(*exc)

    store.conn = SeesTheOldSchema(store.conn)
    store._migrate()


def test_highlights_follow_their_unit_when_it_moves(tmp_path, monkeypatch):
    import json as _json

    from reviewer import cli

    store = Store()
    pr_id = analyze(store, FIRST)
    f = unit(store, pr_id, "pkg.mod.f")
    line = _json.loads(f["new_lines"])[1]  # "return 2"
    ranking = {
        "overview": "o", "architecture_summary": None, "components": [],
        "units": [
            {"fingerprint": f["fingerprint"], "tier": "behavior", "reason": "r", "summary": "s",
             "highlights": [{"line": line, "note": "n"}]},
            {"fingerprint": unit(store, pr_id, "pkg.mod.g")["fingerprint"], "tier": "detail", "reason": "r",
             "summary": "s"},
        ],
        "walkthrough": [{"type": "code", "unit": "pkg.mod.f"}, {"type": "code", "unit": "pkg.mod.g"}],
    }
    path = tmp_path / "ranking.json"
    path.write_text(_json.dumps(ranking))
    assert cli.main(["ingest", "o/r#1", str(path)]) == 0
    stored = _json.loads(store.rankings([f["fingerprint"]])[f["fingerprint"]]["highlights"])
    assert stored == [{"note": "n", "side": "new", "offset": 1}]

    # The same change, three lines further down: the highlight moves with it.
    moved = {"pkg/mod.py": "X = 1\n\n\n" + FIRST["pkg/mod.py"]}
    pr_id = store.save_analysis("github.com/o/r", 1, {"title": "T"}, classify(diff_files(BASE, moved)), {})
    view = next(u for u in page_data(store, pr_id)["units"] if u["qualname"] == "pkg.mod.f")
    assert view["highlights"] == [{"note": "n", "side": "new", "line": line + 3}]
