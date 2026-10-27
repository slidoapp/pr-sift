import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from pr_sift.agent import Agents, StreamParser
from pr_sift.config import AgentConfig, ConfigError, load_agent_config
from pr_sift.noise import classify
from pr_sift.server import ReportServer
from pr_sift.store import Store
from pr_sift.units import diff_files

FAKE = Path(__file__).parent / "fake_agent.py"
BASE = {"pkg/mod.py": "def f():\n    return 1\n"}
HEAD = {"pkg/mod.py": "def f():\n    return 2\n"}


@pytest.fixture
def agent_log(tmp_path, monkeypatch):
    log = tmp_path / "agent.log"
    log.touch()
    monkeypatch.setenv("FAKE_AGENT_LOG", str(log))
    return log


def runs(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "checkout"
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "mod.py").write_text(HEAD["pkg/mod.py"])
    return path


def config(**overrides):
    return AgentConfig(command=[sys.executable, str(FAKE)], **overrides)


def make_pr(store):
    units = classify(diff_files(BASE, HEAD))
    info = {"title": "Return two", "url": "https://github.com/o/r/pull/1", "head_sha": "abc", "merge_base": "def"}
    return store.save_analysis("github.com/o/r", 1, info, units, {"architecture": None})


def ask(store, agents, pr_id, workdir, text="What does f return?", session=None):
    anchor = {"anchor_kind": "pr"} | dict.fromkeys(
        ("qualname", "fingerprint", "side", "start_offset", "end_side", "end_offset", "block", "quote")
    )
    question_id = store.add_question(pr_id, anchor)
    store.add_message(question_id, "you", text)
    agents.ask(question_id, text, workdir, session)
    return question_id


def wait(agents, question_id):
    return [e for e in agents.events(question_id, timeout=0.2) if e is not None]


def test_a_run_streams_status_text_and_sources_and_stores_the_answer(agent_log, workdir):
    store = Store()
    pr_id = make_pr(store)
    agents = Agents(store, config(), lambda q: {"id": q}, pr_id)
    question_id = ask(store, agents, pr_id, workdir)
    events = wait(agents, question_id)

    assert [e["type"] for e in events] == [
        "status", "text", "text", "status", "source", "status", "source", "text", "text", "text", "done",
    ]
    assert [e["text"] for e in events if e["type"] == "status"] == [
        "Starting the agent…", "Reading pkg/mod.py…", "Reading https://docs.python.org/3/…",
    ]
    assert events[-1] == {"type": "done", "error": None, "question": {"id": question_id}}

    row = store.question(question_id)
    assert (row["status"], row["workdir"]) == ("done", str(workdir))
    assert row["session_id"]
    [you, agent] = store.messages([question_id])[question_id]
    assert (you["role"], agent["role"], agent["body"]) == ("you", "agent", "It returns `2` now (`pkg/mod.py:2`).")
    assert json.loads(agent["sources"]) == [
        {"kind": "file", "path": "pkg/mod.py"},
        {"kind": "url", "url": "https://docs.python.org/3/"},
    ]

    [run] = runs(agent_log)
    assert run["cwd"] == str(workdir.resolve())
    assert run["prompt"] == "What does f return?"
    args = run["args"]
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--resume" not in args and "--model" not in args


def test_a_follow_up_resumes_the_session(agent_log, workdir):
    store = Store()
    pr_id = make_pr(store)
    agents = Agents(store, config(), lambda q: {"id": q}, pr_id)
    question_id = ask(store, agents, pr_id, workdir)
    wait(agents, question_id)
    session = store.question(question_id)["session_id"]

    agents.ask(question_id, "And why?", workdir, session)
    wait(agents, question_id)
    second = runs(agent_log)[1]
    assert second["args"][second["args"].index("--resume") + 1] == session
    assert second["prompt"] == "And why?"
    assert store.question(question_id)["session_id"] == session


def test_web_fetch_can_be_limited_to_domains_or_turned_off(workdir):
    store = Store()
    agents = Agents(store, config(web_fetch_domains=["pypi.org"]), lambda q: {}, 1)
    command = agents.command(None)
    assert command[command.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
    assert command[command.index("--allowedTools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch(domain:pypi.org)"

    command = Agents(store, config(web_fetch=False), lambda q: {}, 1).command("s1")
    assert command[command.index("--tools") + 1] == "Read,Grep,Glob,WebSearch"
    assert command[-2:] == ["--resume", "s1"]


@pytest.mark.parametrize("mode, error", [
    ("fail", "The agent failed: rate limited"),
    ("crash", "The agent failed: boom"),
])
def test_failed_runs_keep_their_error(agent_log, workdir, monkeypatch, mode, error):
    monkeypatch.setenv("FAKE_AGENT_MODE", mode)
    store = Store()
    pr_id = make_pr(store)
    agents = Agents(store, config(), lambda q: {"id": q}, pr_id)
    question_id = ask(store, agents, pr_id, workdir)
    events = wait(agents, question_id)
    assert events[-1]["type"] == "error"
    row = store.question(question_id)
    assert (row["status"], row["error"]) == ("failed", error)


def test_timeouts_and_cancel_stop_the_agent(agent_log, workdir, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "slow")
    store = Store()
    pr_id = make_pr(store)
    agents = Agents(store, config(timeout_seconds=1), lambda q: {"id": q}, pr_id)
    started = time.monotonic()
    question_id = ask(store, agents, pr_id, workdir)
    wait(agents, question_id)
    assert time.monotonic() - started < 10
    row = store.question(question_id)
    assert (row["status"], row["error"]) == ("failed", "The agent didn't finish within 1 seconds.")
    # What it said before it was stopped is kept.
    assert store.messages([question_id])[question_id][-1]["body"] == "Let me look."

    agents = Agents(store, config(), lambda q: {"id": q}, pr_id)
    question_id = ask(store, agents, pr_id, workdir)
    for event in agents.events(question_id, timeout=0.2):
        if event and event.get("type") == "source":
            assert agents.cancel(question_id)
    assert store.question(question_id)["status"] == "cancelled"


def test_at_most_max_parallel_runs_at_once(agent_log, workdir, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "slow")
    store = Store()
    pr_id = make_pr(store)
    agents = Agents(store, config(max_parallel=1), lambda q: {"id": q}, pr_id)
    first = ask(store, agents, pr_id, workdir)
    second = ask(store, agents, pr_id, workdir)
    # The first agent is at work (it read a file) while the second waits.
    next(e for e in agents.events(first, timeout=0.2) if e and e["type"] == "source")
    assert store.question(second)["status"] == "queued"
    agents.cancel(second)
    agents.cancel(first)
    wait(agents, first)
    wait(agents, second)
    assert [store.question(q)["status"] for q in (first, second)] == ["cancelled", "cancelled"]
    assert len(runs(agent_log)) == 1


def test_missing_web_search_is_noticed(workdir):
    parser = StreamParser(workdir, wants_search=True)
    events = parser.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s", "tools": ["Read"]}))
    assert [e["type"] for e in events] == ["session", "notice"]
    parser.feed(json.dumps({"type": "result", "subtype": "success", "result": "From the code: 2."}))
    assert parser.answer().startswith("_Web search isn't available")
    assert parser.answer().endswith("From the code: 2.")


def test_interrupted_runs_fail_when_a_server_starts(workdir):
    store = Store()
    pr_id = make_pr(store)
    anchor = {"anchor_kind": "pr"} | dict.fromkeys(
        ("qualname", "fingerprint", "side", "start_offset", "end_side", "end_offset", "block", "quote")
    )
    question_id = store.add_question(pr_id, anchor)
    store.update_question(question_id, status="running")
    Agents(store, config(), lambda q: {}, pr_id)
    assert store.question(question_id)["status"] == "failed"


def test_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_SIFT_CONFIG_DIR", str(tmp_path))
    assert load_agent_config().command == ["claude"]
    (tmp_path / "config.toml").write_text(
        '[agent]\ncommand = "/opt/claude --x"\nweb_fetch_domains = ["pypi.org"]\nmax_parallel = 0\n'
    )
    loaded = load_agent_config()
    assert (loaded.command, loaded.web_fetch_domains, loaded.max_parallel) == (["/opt/claude", "--x"], ["pypi.org"], 1)
    (tmp_path / "config.toml").write_text("[agent]\nweb_fetch = 'yes'\n")
    with pytest.raises(ConfigError, match="web_fetch must be a bool"):
        load_agent_config()


# The API, with the fake agent

@pytest.fixture
def server(agent_log, workdir):
    store = Store()
    pr_id = make_pr(store)
    store.save_summary(pr_id, "abc", "Makes f return 2.", None, [], [{"type": "text", "text": "f returns 2 now."}])
    server = ReportServer(store, pr_id, agent_config=config(), workdir=lambda: workdir)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, method, path, body=None, token=True):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-PR-Sift-Token"] = server.token
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(server.url.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def events(server, question_id, token=None):
    url = f"{server.url}api/questions/{question_id}/events?token={token or server.token}"
    with urllib.request.urlopen(url, timeout=20) as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        lines = response.read().decode().splitlines()
    return [json.loads(line[6:]) for line in lines if line.startswith("data: ")]


def test_comment_drafts(server):
    line = json.loads(server.store.units(server.pr_id)[0]["new_lines"])[1]
    anchor = {"kind": "line", "qualname": "pkg.mod.f", "side": "new", "line": line}
    status, comment = call(server, "POST", "/api/comments", {"anchor": anchor, "body": "  Why 2?  "})
    assert status == 201
    assert (comment["body"], comment["state"], comment["anchor"]["line"]) == ("Why 2?", "draft", line)

    status, updated = call(server, "PATCH", f"/api/comments/{comment['id']}", {"body": "Why two?"})
    assert (status, updated["body"]) == (200, "Why two?")
    _, page = call(server, "GET", "/api/pr")
    assert [c["body"] for c in page["comments"]] == ["Why two?"]

    assert call(server, "POST", "/api/comments", {"anchor": anchor, "body": " "})[0] == 400
    assert call(server, "POST", "/api/comments", {"anchor": {"kind": "walkthrough", "block": 0}, "body": "x"})[0] == 400
    assert call(server, "POST", "/api/comments", {"anchor": {"kind": "line", "qualname": "pkg.mod.f", "side": "new", "line": 99}, "body": "x"})[0] == 400
    assert call(server, "POST", "/api/comments", {"anchor": anchor, "body": "x"}, token=False)[0] == 403
    assert call(server, "DELETE", f"/api/comments/{comment['id']}")[0] == 200
    assert call(server, "PATCH", f"/api/comments/{comment['id']}", {"body": "x"})[0] == 404


def test_asking_streams_the_answer_and_follow_ups_resume(server, agent_log, workdir):
    anchor = {"kind": "walkthrough", "block": 0, "quote": "returns 2"}
    status, question = call(server, "POST", "/api/questions", {"anchor": anchor, "text": "Why 2?"})
    assert status == 201
    assert question["status"] in ("queued", "running")
    assert question["messages"] == [{"role": "you", "body": "Why 2?", "sources": [], "created_at": question["created_at"]}]

    stream = events(server, question["id"])
    assert stream[-1]["type"] == "done"
    final = stream[-1]["question"]
    assert final["status"] == "done"
    assert [m["role"] for m in final["messages"]] == ["you", "agent"]

    # The first prompt gives the agent the PR and the anchored text.
    prompt = runs(agent_log)[0]["prompt"]
    assert "o/r#1: Return two" in prompt
    assert "> f returns 2 now." in prompt and "> returns 2" in prompt
    assert prompt.endswith("My question: Why 2?")

    status, question = call(server, "POST", f"/api/questions/{question['id']}/messages", {"text": "Sure?"})
    assert status == 200
    stream = events(server, question["id"])
    assert [m["body"] for m in stream[-1]["question"]["messages"]][-2] == "Sure?"
    second = runs(agent_log)[1]
    assert second["prompt"] == "Sure?" and "--resume" in second["args"]

    # A finished question's stream is its final state.
    assert [e["type"] for e in events(server, question["id"])] == ["done"]

    status, resolved = call(server, "PATCH", f"/api/questions/{question['id']}", {"resolved": True})
    assert (status, resolved["resolved"]) == (200, True)
    assert call(server, "DELETE", f"/api/questions/{question['id']}")[0] == 200
    _, page = call(server, "GET", "/api/pr")
    assert page["questions"] == []
    assert page["agent"]["web_fetch"] is True


def test_a_follow_up_without_its_session_retells_the_thread(server, agent_log, workdir):
    status, question = call(server, "POST", "/api/questions", {"anchor": {"kind": "pr"}, "text": "What changed?"})
    events(server, question["id"])
    server.store.update_question(question["id"], session_id=None)
    call(server, "POST", f"/api/questions/{question['id']}/messages", {"text": "More?"})
    events(server, question["id"])
    prompt = runs(agent_log)[1]["prompt"]
    assert "I asked earlier:\n> What changed?" in prompt
    assert "You answered earlier:\n> It returns `2` now" in prompt
    assert prompt.endswith("My question: More?")


def test_question_endpoints_check_token_and_input(server):
    assert call(server, "POST", "/api/questions", {"anchor": {"kind": "pr"}, "text": ""})[0] == 400
    assert call(server, "POST", "/api/questions", {"anchor": {"kind": "pr"}, "text": "x"}, token=False)[0] == 403
    with pytest.raises(urllib.error.HTTPError) as error:
        events(server, 1, token="wrong")
    assert error.value.code == 403
    assert call(server, "POST", "/api/questions/999/messages", {"text": "x"})[0] == 404
    assert call(server, "PATCH", "/api/questions/999", {"resolved": True})[0] == 404
