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
