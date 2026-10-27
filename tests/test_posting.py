import json
import threading
import urllib.request

import pytest

from pr_sift.github import GitHubError
from pr_sift.noise import classify
from pr_sift.posting import PlanError, parse_diff, plan_review, unwrap
from pr_sift.repo import git
from pr_sift.server import ReportServer
from pr_sift.store import Store
from pr_sift.units import diff_files

OLD = "def f():\n" + "".join(f"    a{i} = {i}\n" for i in range(20)) + "    return a0\n"
NEW = OLD.replace("    a1 = 1\n", "    a1 = 100\n").replace("    a18 = 18\n", "    a18 = 180\n")


def diff_of(git_repo, old, new):
    base = git_repo.commit(old)
    head = git_repo.commit(new)
    return git(git_repo.git_dir, "diff", "--find-renames", "--no-color", "--unified=3", base, head).decode()


def test_parse_diff_finds_the_lines_github_can_comment_on(git_repo):
    files = parse_diff(diff_of(git_repo, {"pkg/mod.py": OLD, "gone.py": "X = 1\n"}, {"pkg/mod.py": NEW, "gone.py": None}))
    by_path = {f.path: f for f in files}
    mod = by_path["pkg/mod.py"]
    # Two hunks: line 3 changed with 3 lines of context, and line 20.
    assert [(sorted(old), sorted(new)) for old, new in mod.hunks] == [
        ([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]),
        ([17, 18, 19, 20, 21, 22], [17, 18, 19, 20, 21, 22]),
    ]
    assert (mod.hunk("new", 3), mod.hunk("new", 20), mod.hunk("new", 10)) == (0, 1, None)
    assert (by_path["gone.py"].old_path, by_path["gone.py"].hunks) == ("gone.py", [({1}, set())])


def test_parse_diff_follows_renames(git_repo):
    body = "".join(f"V{i} = {i}\n" for i in range(10))
    files = parse_diff(diff_of(git_repo, {"old_name.py": body}, {"old_name.py": None, "new_name.py": body.replace("V5 = 5", "V5 = 50")}))
    [renamed] = files
    assert (renamed.path, renamed.old_path, renamed.hunk("old", 6), renamed.hunk("new", 6)) == ("new_name.py", "old_name.py", 0, 0)


def make(store, old, new):
    return store.save_analysis("github.com/o/r", 1, {"title": "T", "head_sha": "head1", "merge_base": "base1"},
                               classify(diff_files(old, new)), {})


def draft(id, anchor, body="Why?"):
    return {"id": id, "anchor": {"state": "current", "quote": None} | anchor, "body": body}


@pytest.fixture
def setup(git_repo):
    store = Store()
    pr_id = make(store, {"pkg/mod.py": OLD}, {"pkg/mod.py": NEW})
    units = {u["qualname"]: u for u in store.units(pr_id)}
    files = parse_diff(diff_of(git_repo, {"pkg/mod.py": OLD}, {"pkg/mod.py": NEW}))
    return units, files


def test_drafts_on_diff_lines_go_inline_and_others_to_the_body(setup):
    units, files = setup
    f = "pkg.mod.f"
    drafts = [
        draft(1, {"kind": "line", "qualname": f, "side": "new", "line": 3}, "Why 100?"),
        draft(2, {"kind": "line", "qualname": f, "side": "old", "line": 3}, "Was 1."),
        draft(3, {"kind": "range", "qualname": f, "side": "new", "line": 2, "end_side": "new", "end_line": 4}, "These three."),
        draft(4, {"kind": "range", "qualname": f, "side": "new", "line": 4, "end_side": "new", "end_line": 20}, "Both changes."),
        draft(5, {"kind": "line", "qualname": f, "side": "new", "line": 10}, "Outside the diff."),
        draft(6, {"kind": "unit", "qualname": f}, "The whole function."),
        draft(7, {"kind": "pr"}, "Thanks!"),
    ]
    plan = plan_review(drafts, units, files, "COMMENT", "Looks good.\nTwo questions.")
    assert [c["payload"] for c in plan.comments] == [
        {"path": "pkg/mod.py", "line": 3, "side": "RIGHT", "body": "Why 100?"},
        {"path": "pkg/mod.py", "line": 3, "side": "LEFT", "body": "Was 1."},
        {"path": "pkg/mod.py", "line": 4, "side": "RIGHT", "body": "These three.", "start_line": 2, "start_side": "RIGHT"},
        {"path": "pkg/mod.py", "line": 20, "side": "RIGHT", "body": "_(Lines 4–20.)_\n\nBoth changes."},
    ]
    assert [(p["draft"], p["where"]) for p in plan.placed] == [
        (1, "inline"), (2, "inline"), (3, "inline"), (4, "inline"), (5, "body"), (6, "body"), (7, "body"),
    ]
    assert plan.body == (
        "Looks good. Two questions.\n\nThanks!\n\n"
        "**On `f` in `pkg/mod.py`, line 10:**\n\nOutside the diff.\n\n"
        "**On `f` in `pkg/mod.py`:**\n\nThe whole function."
    )
    assert plan.payload("head1") == {
        "commit_id": "head1", "event": "COMMENT", "comments": [c["payload"] for c in plan.comments], "body": plan.body,
    }


def test_orphaned_drafts_are_posted_only_when_asked(setup):
    units, files = setup
    orphan = draft(1, {"kind": "line", "qualname": "pkg.mod.gone", "side": "new", "line": 3, "state": "orphaned"}, "Hm.")
    plan = plan_review([orphan], units, files, "APPROVE", "")
    assert (plan.body, plan.comments, plan.placed[0]["where"]) == ("", [], "skipped")
    plan = plan_review([orphan], units, files, "APPROVE", "", include_orphaned=True)
    assert plan.body == "**On `pkg.mod.gone` (no longer in the PR):**\n\nHm."


def test_events_need_something_to_post(setup):
    units, files = setup
    with pytest.raises(PlanError, match="event must be"):
        plan_review([], units, files, "MERGE", "x")
    with pytest.raises(PlanError, match="nothing to post"):
        plan_review([], units, files, "COMMENT", "  ")
    with pytest.raises(PlanError, match="requesting changes needs"):
        plan_review([draft(1, {"kind": "line", "qualname": "pkg.mod.f", "side": "new", "line": 3})], units, files, "REQUEST_CHANGES", "")
    assert plan_review([], units, files, "APPROVE", "").body == ""


def test_unwrap_joins_paragraph_lines_only():
    text = (
        "A paragraph that was\nwrapped by hand.\n\n"
        "- an item\n  that continues\n- another\n\n"
        "1. first\n2. second\n\n"
        "```python\nx = 1\ny = 2\n```\n\n"
        "> quoted\n> lines\n\n"
        "Hard  \nbreak\n\n"
        "| a | b |\n|---|---|\n"
    )
    assert unwrap(text) == (
        "A paragraph that was wrapped by hand.\n\n"
        "- an item that continues\n- another\n\n"
        "1. first\n2. second\n\n"
        "```python\nx = 1\ny = 2\n```\n\n"
        "> quoted\n> lines\n\n"
        "Hard  \nbreak\n\n"
        "| a | b |\n|---|---|"
    )


# The API, with GitHub faked

class FakePoster:
    def __init__(self, diff):
        self._diff = diff
        self.head = "head1"
        self.posted = []
        self.fail = None

    def head_sha(self):
        return self.head

    def diff(self, base, head):
        assert (base, head) == ("base1", "head1")
        return self._diff

    def create_review(self, payload):
        if self.fail:
            raise GitHubError(self.fail)
        self.posted.append(payload)
        return {"id": 77, "html_url": "https://github.com/o/r/pull/1#pullrequestreview-77"}

    def review_comments(self, review_id):
        return [
            {"id": 900 + i, "path": c["path"], "body": c["body"], "html_url": f"https://github.com/o/r/pull/1#discussion_r{900 + i}"}
            for i, c in enumerate(self.posted[-1]["comments"])
        ]


@pytest.fixture
def server(git_repo):
    store = Store()
    pr_id = make(store, {"pkg/mod.py": OLD}, {"pkg/mod.py": NEW})
    poster = FakePoster(diff_of(git_repo, {"pkg/mod.py": OLD}, {"pkg/mod.py": NEW}))
    server = ReportServer(store, pr_id, poster=poster)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, method, path, body=None):
    headers = {"Content-Type": "application/json", "X-PR-Sift-Token": server.token}
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(server.url.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def add_drafts(server):
    inline = call(server, "POST", "/api/comments", {"anchor": {"kind": "line", "qualname": "pkg.mod.f", "side": "new", "line": 3}, "body": "Why 100?"})[1]
    general = call(server, "POST", "/api/comments", {"anchor": {"kind": "pr"}, "body": "Thanks!"})[1]
    return inline, general


def test_preview_then_post(server):
    inline, general = add_drafts(server)
    status, preview = call(server, "POST", "/api/review/preview", {"event": "COMMENT", "body": "Summary"})
    assert status == 200
    assert (preview["drafts"], preview["head_moved"], preview["body"]) == ([inline["id"], general["id"]], False, "Summary\n\nThanks!")
    assert server.poster.posted == []

    confirm = {"event": "COMMENT", "body": "Summary", "drafts": preview["drafts"], "head_sha": "head1"}
    status, result = call(server, "POST", "/api/review", confirm)
    assert status == 201
    assert server.poster.posted == [{
        "commit_id": "head1", "event": "COMMENT", "body": "Summary\n\nThanks!",
        "comments": [{"path": "pkg/mod.py", "line": 3, "side": "RIGHT", "body": "Why 100?"}],
    }]
    by_id = {c["id"]: c for c in result["comments"]}
    assert (by_id[inline["id"]]["state"], by_id[inline["id"]]["github_url"]) == ("posted", "https://github.com/o/r/pull/1#discussion_r900")
    assert by_id[general["id"]]["github_url"] == "https://github.com/o/r/pull/1#pullrequestreview-77"
    assert [s["event"] for s in result["submissions"]] == ["COMMENT"]
    [row] = [c for c in server.store.comments(server.pr_id) if c["id"] == inline["id"]]
    assert row["github_id"] == 900

    # Posted drafts can't be edited, and there's nothing left to post.
    assert call(server, "PATCH", f"/api/comments/{inline['id']}", {"body": "x"})[0] == 409
    assert call(server, "POST", "/api/review/preview", {"event": "COMMENT"})[0] == 400


def test_posting_stops_when_the_head_moved_or_the_drafts_changed(server):
    inline, general = add_drafts(server)
    _, preview = call(server, "POST", "/api/review/preview", {"event": "APPROVE"})
    confirm = {"event": "APPROVE", "drafts": preview["drafts"], "head_sha": "head1"}

    call(server, "DELETE", f"/api/comments/{general['id']}")
    status, error = call(server, "POST", "/api/review", confirm)
    assert (status, error["error"]) == (409, "the drafts changed since the preview; preview again")

    server.poster.head = "head2"
    _, preview = call(server, "POST", "/api/review/preview", {"event": "APPROVE"})
    assert preview["head_moved"] is True
    status, error = call(server, "POST", "/api/review", confirm | {"drafts": preview["drafts"]})
    assert status == 409 and "new commits (head2)" in error["error"]
    assert server.poster.posted == []


def test_head_reports_whether_the_pr_moved_since_the_analysis(server, monkeypatch):
    assert call(server, "GET", "/api/head") == (200, {"head_sha": "head1", "remote_head_sha": "head1", "head_moved": False})

    server.poster.head = "head2"
    assert call(server, "GET", "/api/head")[1]["head_moved"] is True

    def unreachable():
        raise GitHubError("offline")

    monkeypatch.setattr(server.poster, "head_sha", unreachable)
    status, error = call(server, "GET", "/api/head")
    assert status == 502 and "offline" in error["error"]


def test_a_rejected_review_keeps_the_drafts(server):
    add_drafts(server)
    _, preview = call(server, "POST", "/api/review/preview", {"event": "APPROVE"})
    server.poster.fail = "Can not approve your own pull request (HTTP 422)"
    status, error = call(server, "POST", "/api/review", {"event": "APPROVE", "drafts": preview["drafts"], "head_sha": "head1"})
    assert status == 502 and "Can not approve your own pull request" in error["error"]
    assert {c["state"] for c in server.store.comments(server.pr_id)} == {"draft"}
