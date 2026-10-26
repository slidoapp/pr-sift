"""A stand-in for `claude -p --output-format stream-json`.

It logs its arguments, working directory and prompt to $FAKE_AGENT_LOG
and prints a recorded-style transcript. $FAKE_AGENT_MODE picks one:
ok (default), fail, slow (sleeps before answering), nosearch (web
search not among its tools) or crash (exits without a result).
"""

import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_AGENT_MODE", "ok")
with open(os.environ["FAKE_AGENT_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "prompt": prompt}) + "\n")

session = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
tools = ["Read", "Grep", "Glob"] + ([] if mode == "nosearch" else ["WebSearch", "WebFetch"])


def emit(message):
    print(json.dumps(message), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session, "tools": tools})
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(3)
emit({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text", "text": ""}}})
emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me look."}}})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": os.path.join(os.getcwd(), "pkg", "mod.py")}},
    {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://docs.python.org/3/"}},
]}})
if mode == "slow":
    time.sleep(30)
emit({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text", "text": ""}}})
for part in ("It returns ", "`2` now (`pkg/mod.py:2`)."):
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": part}}})
if mode == "fail":
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "rate limited"})
else:
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "It returns `2` now (`pkg/mod.py:2`).", "session_id": session})
