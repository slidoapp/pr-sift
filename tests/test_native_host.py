import base64
import hashlib
import io
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from pr_sift import native_host
from pr_sift.config import AgentConfig
from pr_sift.github import GitHubError, PrRef
from pr_sift.native_host import Host, Sifts, encode_message, read_log, read_message
from pr_sift.noise import classify
from pr_sift.store import Store
from pr_sift.units import diff_files

PR = "https://github.com/o/r/pull/1"
REF = PrRef("github.com", "o", "r", 1)
BASE = {"pkg/mod.py": "def f():\n    return 1\n\n\ndef g():\n    return 1\n"}
HEAD = {"pkg/mod.py": "def f():\n    return 2\n\n\ndef g():\n    return 2\n"}

# Stands in for `claude -p --output-format stream-json` sifting a PR:
# $FAKE_SIFT picks how it goes: ok, fail (an error result), crash (no
# result) or slow.
FAKE_SIFTER = """\
import json, os, sys, time
mode = os.environ.get("FAKE_SIFT", "ok")
open("args.json", "w").write(json.dumps(sys.argv[1:]))
def emit(m): print(json.dumps(m), flush=True)
def tool(name, **given): emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": given}]}})
emit({"type": "system", "subtype": "init"})
tool("Bash", command="pr-sift analyze https://github.com/o/r/pull/1 --out ./analysis.json")
if mode == "slow":
    time.sleep(30)
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(3)
tool("Write", file_path=os.path.join(os.getcwd(), "ranking.json"))
emit({"type": "result", "subtype": "success", "is_error": mode == "fail", "result": "ingest kept failing" if mode == "fail" else "done"})
"""


@pytest.fixture
def sifter(tmp_path):
    path = tmp_path / "fake_sifter.py"
    path.write_text(FAKE_SIFTER)
    return AgentConfig(command=[sys.executable, str(path)])


def wait_for(sifts, state, step=None, timeout=10):
    deadline = time.monotonic() + timeout
    while (status := sifts.status(REF))["state"] != state or (step and status.get("step") != step):
        assert time.monotonic() < deadline, status
        time.sleep(0.05)
    return status


def analyze(store, head_sha="abc"):
    units = classify(diff_files(BASE, HEAD))
    info = {"title": "T", "url": PR, "head_sha": head_sha}
    return store.save_analysis("github.com/o/r", 1, info, units, {"architecture": None})


def replies():
    sent = []
    return sent, sent.append


def test_messages_are_length_prefixed_json():
    stream = io.BytesIO(encode_message({"id": 1, "type": "status"}) + encode_message({"id": 2}))
    assert read_message(stream) == {"id": 1, "type": "status"}
    assert read_message(stream) == {"id": 2}
    assert read_message(stream) is None


def test_the_log_tells_the_step_and_the_result(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pr-sift analyze 1 --out a/analysis.json"}},
        ]}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/s/analysis.json"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "jq '.units | length' analysis.json"}},
        ]}}) + "\n"
        + '{"type": "assist'  # still being written
    )
    assert read_log(log) == ("Reading the analysis", None)

    log.write_text(log.read_text().rsplit("\n", 1)[0] + "\n" + json.dumps({"type": "result", "is_error": False}) + "\n")
    assert read_log(log)[1] == {"type": "result", "is_error": False}
    assert read_log(tmp_path / "missing.jsonl") == (None, None)


def test_a_sift_runs_the_skill_headless_in_its_own_directory(tmp_path, sifter):
    sifts = Sifts(sifter, tmp_path / "sifts")
    assert sifts.status(REF) == {"state": "none"}

    assert sifts.start(REF)["state"] == "running"
    status = wait_for(sifts, "done")
    assert status["started_at"]

    directory = tmp_path / "sifts/github.com/o/r/1"
    args = json.loads((directory / "args.json").read_text())
    prompt = args[args.index("-p") + 1]
    assert PR in prompt and "pr-sift skill" in prompt and "--rerank" not in prompt
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "Bash(pr-sift ingest:*)" in args[args.index("--allowedTools") + 1].split(",")

    # A later host reads how it went from the directory.
    assert Sifts(sifter, tmp_path / "sifts").status(REF)["state"] == "done"

    sifts.start(REF, rerank=True)
    wait_for(sifts, "done")
    args = json.loads((directory / "args.json").read_text())
    assert "--rerank" in args[args.index("-p") + 1]


@pytest.mark.parametrize(("mode", "error"), [("fail", "ingest kept failing"), ("crash", "boom")])
def test_a_failed_sift_says_why(tmp_path, sifter, monkeypatch, mode, error):
    monkeypatch.setenv("FAKE_SIFT", mode)
    sifts = Sifts(sifter, tmp_path / "sifts")
    sifts.start(REF)
    assert wait_for(sifts, "failed")["error"] == error


def test_one_sift_at_a_time_and_cancel(tmp_path, sifter, monkeypatch):
    monkeypatch.setenv("FAKE_SIFT", "slow")
    sifts = Sifts(sifter, tmp_path / "sifts")
    sifts.start(REF)
    wait_for(sifts, "running", step="Analyzing the diff")
    with pytest.raises(native_host.HostError, match="already being sifted"):
        sifts.start(REF)
    assert sifts.cancel(REF)["state"] == "cancelled"


def test_status_of_a_pr_never_sifted(sifter, tmp_path):
    sent, send = replies()
    host = Host(send, Store(), sifter)
    host.handle({"id": 7, "type": "status", "pr": PR})
    assert sent == [{"id": 7, "ok": True, "pr": "o/r#1", "sift": {"state": "none"}, "analyzed": False}]


def test_status_counts_units_and_review_progress():
    store = Store()
    pr_id = analyze(store)
    unit = next(u for u in store.units(pr_id) if u["qualname"] == "pkg.mod.f")
    store.set_review(pr_id, "pkg.mod.f", unit["fingerprint"], unit["new_body"], "reviewed")
    store.set_review(pr_id, "pkg.mod.g", "old", "def g():\n    return 0\n", "reviewed")
    g = next(u for u in store.units(pr_id) if u["qualname"] == "pkg.mod.g")
    store.save_rankings([{"fingerprint": g["fingerprint"], "tier": "detail", "reason": "r", "summary": "s",
                          "attention": "hide"}], None)
    sent, send = replies()
    host = Host(send, store, head_sha=lambda pr: "def")

    host.handle({"id": 1, "type": "status", "pr": "o/r#1"})
    host.handle({"id": 2, "type": "status", "pr": PR, "check_head": True})
    first, second = sent
    assert first["ok"] and first["analyzed"] and not first["ranked"]
    assert {k: first[k] for k in ("units", "hidden", "read", "skim", "reviewed", "changed", "flagged")} == {
        "units": 1, "hidden": 1, "read": 1, "skim": 0, "reviewed": 1, "changed": 0, "flagged": 0,
    }
    assert "head_moved" not in first
    assert second["remote_head_sha"] == "def" and second["head_moved"] is True


def test_status_survives_github_being_unreachable():
    store = Store()
    analyze(store)

    def unreachable(pr):
        raise GitHubError("gh failed")

    sent, send = replies()
    Host(send, store, head_sha=unreachable).handle({"id": 1, "type": "status", "pr": PR, "check_head": True})
    assert sent[0]["ok"] and sent[0]["head_error"] == "gh failed"


def test_open_serves_the_report_once_per_pr():
    store = Store()
    analyze(store)
    sent, send = replies()
    host = Host(send, store)
    try:
        host.handle({"id": 1, "type": "open", "pr": PR})
        host.handle({"id": 2, "type": "open", "pr": PR})
        assert sent[0]["url"] == sent[1]["url"]
        with urllib.request.urlopen(sent[0]["url"]) as response:
            assert b"pr-sift-token" in response.read()
        host.handle({"id": 3, "type": "status", "pr": PR})
        assert sent[2]["report"] == sent[0]["url"]
    finally:
        host.close()


def test_bad_requests_get_errors():
    sent, send = replies()
    host = Host(send, Store())
    host.handle({"id": 1, "type": "open", "pr": PR})
    host.handle({"id": 2, "type": "explode", "pr": PR})
    host.handle({"id": 3, "type": "status", "pr": "not a pr"})
    assert [r["ok"] for r in sent] == [False, False, False]
    assert sent[0]["error"] == "o/r#1 has not been sifted yet"
    assert "unknown request" in sent[1]["error"]


def test_install_writes_the_wrapper_and_a_manifest_per_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/opt/tools/bin:/usr/bin")
    chrome = tmp_path / "Chrome"
    wrapper, manifests = native_host.install([chrome, tmp_path / "Brave"])
    assert manifests == [chrome / "NativeMessagingHosts/pr_sift.json", tmp_path / "Brave/NativeMessagingHosts/pr_sift.json"]
    manifest = json.loads(manifests[0].read_text())
    assert manifest["name"] == native_host.HOST_NAME and manifest["path"] == str(wrapper)
    assert manifest["allowed_origins"] == [f"chrome-extension://{native_host.EXTENSION_ID}/"]
    script = wrapper.read_text()
    assert "export PATH=/opt/tools/bin:/usr/bin\n" in script
    assert f"{sys.executable} -m pr_sift.cli extension host" in script
    assert wrapper.stat().st_mode & 0o111


def test_install_finds_the_browsers_there_are(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/chromium").mkdir(parents=True)
    _, manifests = native_host.install(platform="linux")
    assert manifests == [tmp_path / ".config/chromium/NativeMessagingHosts/pr_sift.json"]


def test_the_extension_id_is_the_one_its_key_pins():
    manifest = json.loads((native_host.EXTENSION_DIR / "manifest.json").read_text())
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == native_host.EXTENSION_ID


def test_the_host_talks_over_stdin_and_stdout(tmp_path):
    """As Chrome runs it: messages in, replies out, nothing else on stdout."""
    process = subprocess.Popen(
        [sys.executable, "-m", "pr_sift.cli", "extension", "host", f"chrome-extension://{native_host.EXTENSION_ID}/"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=Path(__file__).parent.parent,
    )
    out, _ = process.communicate(encode_message({"id": 1, "type": "status", "pr": PR}), timeout=30)
    assert process.returncode == 0
    stream = io.BytesIO(out)
    assert read_message(stream) == {"id": 1, "ok": True, "pr": "o/r#1", "sift": {"state": "none"}, "analyzed": False}
    assert read_message(stream) is None
