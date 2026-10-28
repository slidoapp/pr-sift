"""The native messaging host that connects the PR Sift Chrome extension
to pr-sift.

Chrome starts `pr-sift extension host` when the extension connects and
talks to it over stdin and stdout: each message is UTF-8 JSON preceded
by its length as a 32-bit integer in native byte order. Only the
extension named in the host manifest may connect, so requests need no
token.

The extension asks for a PR's status, to sift it, and to open its
report. Sifting is the pr-sift skill run by a headless agent, which
analyzes and ranks the PR; it runs in its own process group, so it
outlives this host. Reports are served from this process, one
ReportServer per PR, for as long as the browser keeps the host running.
"""

import json
import os
import shlex
import signal
import struct
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

from pr_sift import github, repo
from pr_sift.config import AgentConfig, ConfigError, load_agent_config
from pr_sift.report import page_data
from pr_sift.server import ReportServer
from pr_sift.store import Store, data_dir, now

HOST_NAME = "pr_sift"
# Pinned by the "key" in chrome/manifest.json.
EXTENSION_ID = "jpibknnhgainnmiefnneeniekjfkemgc"
EXTENSION_DIR = Path(__file__).parent / "chrome"
# The user data directories of Chromium browsers, whose
# NativeMessagingHosts folder Chrome reads host manifests from.
BROWSER_DIRS = {
    "darwin": [
        "~/Library/Application Support/Google/Chrome",
        "~/Library/Application Support/Google/Chrome Beta",
        "~/Library/Application Support/Google/Chrome Canary",
        "~/Library/Application Support/Google/Chrome for Testing",
        "~/Library/Application Support/Chromium",
        "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "~/Library/Application Support/Microsoft Edge",
        "~/Library/Application Support/Arc/User Data",
    ],
    "linux": [
        "~/.config/google-chrome",
        "~/.config/google-chrome-beta",
        "~/.config/chromium",
        "~/.config/BraveSoftware/Brave-Browser",
        "~/.config/microsoft-edge",
    ],
}
# Chrome starts the host with a bare environment: the wrapper brings back
# these variables as they were at install time.
KEPT_ENV = ("PATH", "GH_HOST", "GH_CONFIG_DIR", "PR_SIFT_DATA_DIR", "PR_SIFT_CACHE_DIR", "PR_SIFT_CONFIG_DIR")

SIFT_TOOLS = ("Skill", "Bash(pr-sift analyze:*)", "Bash(pr-sift ingest:*)", "Bash(jq:*)",
              "Read", "Write", "Edit", "Glob", "Grep")
SIFT_PROMPT = """\
Sift the pull request {url} with the pr-sift skill: do its steps 1 to 4 \
(analyze, read the analysis, rank, store it){rerank}. The current directory \
is your scratch directory. Skip steps 5 and 6: don't run pr-sift serve and \
don't write a summary, the reviewer opens the report from GitHub when you're \
done. If pr-sift ingest reports validation errors, fix ranking.json and run \
it again. The analysis is all you can read of the PR: other commands are \
denied, so where it leaves a question open, say so in the walkthrough."""
# What the extension shows while a sift runs, by the tool call it is at.
STEPS = (
    (lambda tool, arg: tool == "Bash" and "pr-sift analyze" in arg, "Analyzing the diff"),
    (lambda tool, arg: tool == "Read" and arg.endswith("analysis.json"), "Reading the analysis"),
    (lambda tool, arg: tool in ("Write", "Edit") and arg.endswith("ranking.json"), "Ranking the changes"),
    (lambda tool, arg: tool == "Bash" and "pr-sift ingest" in arg, "Storing the ranking"),
)
# Lines of stderr kept for the error message of a failed sift.
STDERR_TAIL = 10


class HostError(RuntimeError):
    pass


# Messages


def read_message(stream: BinaryIO) -> dict | None:
    """The next message, or None when the browser closed the stream."""
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    return json.loads(stream.read(length).decode())


def encode_message(message: dict) -> bytes:
    body = json.dumps(message).encode()
    return struct.pack("=I", len(body)) + body


# Sifting


class Sifts:
    """Sift runs, at most one per PR. Each runs in its own directory,
    which keeps its job file, the agent's stream-json log and its
    stderr, so a later host can tell how a run it didn't start went."""

    def __init__(self, config: AgentConfig, root: Path | None = None):
        self.config = config
        self.root = root or data_dir() / "sifts"
        self.children: dict[Path, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def directory(self, pr: github.PrRef) -> Path:
        return self.root / pr.host / pr.owner / pr.repo / str(pr.number)

    def command(self, pr: github.PrRef, rerank: bool) -> list[str]:
        url = f"https://{pr.host}/{pr.owner}/{pr.repo}/pull/{pr.number}"
        prompt = SIFT_PROMPT.format(url=url, rerank=", passing --rerank to pr-sift analyze" if rerank else "")
        return [
            *self.config.command, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            # Anything outside these tools is denied, not asked for.
            "--allowedTools", ",".join(SIFT_TOOLS),
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
        ]

    def start(self, pr: github.PrRef, rerank: bool = False) -> dict:
        with self.lock:
            directory = self.directory(pr)
            if self._running(directory):
                raise HostError(f"{pr} is already being sifted")
            directory.mkdir(parents=True, exist_ok=True)
            for name in ("analysis.json", "ranking.json"):
                (directory / name).unlink(missing_ok=True)
            with open(directory / "log.jsonl", "wb") as log, open(directory / "stderr.txt", "wb") as stderr:
                try:
                    process = subprocess.Popen(
                        self.command(pr, rerank), cwd=directory, stdin=subprocess.DEVNULL, stdout=log,
                        stderr=stderr, start_new_session=True,
                    )
                except OSError as e:
                    raise HostError(f"couldn't start {self.config.command[0]}: {e}") from e
            self.children[directory] = process
            _write_json(directory / "job.json", {"pid": process.pid, "started_at": now(), "rerank": rerank})
        return self.status(pr)

    def cancel(self, pr: github.PrRef) -> dict:
        with self.lock:
            directory = self.directory(pr)
            job = _read_json(directory / "job.json")
            if job and self._running(directory):
                try:
                    os.killpg(job["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                _write_json(directory / "job.json", job | {"cancelled": True})
                if child := self.children.get(directory):
                    child.wait()
        return self.status(pr)

    def status(self, pr: github.PrRef) -> dict:
        """{"state": "none" | "running" | "done" | "failed" | "cancelled"}
        with the step a running sift is at, and why a sift failed."""
        directory = self.directory(pr)
        job = _read_json(directory / "job.json")
        if job is None:
            return {"state": "none"}
        status = {"started_at": job["started_at"]}
        step, result = read_log(directory / "log.jsonl")
        if self._running(directory):
            return status | {"state": "running", "step": step or "Starting the agent"}
        if job.get("cancelled"):
            return status | {"state": "cancelled"}
        if result is not None and not result.get("is_error"):
            return status | {"state": "done"}
        if result is not None:
            error = result.get("result") or result.get("subtype") or "the agent failed"
        else:
            tail = _read_text(directory / "stderr.txt").strip().splitlines()[-STDERR_TAIL:]
            error = "\n".join(tail) or "the agent stopped before it finished"
        return status | {"state": "failed", "error": error}

    def _running(self, directory: Path) -> bool:
        if child := self.children.get(directory):
            return child.poll() is None
        job = _read_json(directory / "job.json")
        return bool(job) and _alive(job["pid"])

    def shutdown(self) -> None:
        """Reap finished runs; running ones go on without this host."""
        for child in self.children.values():
            child.poll()


def read_log(path: Path) -> tuple[str | None, dict | None]:
    """The step a sift's stream-json log is at, and its result message
    once the run finished."""
    step, result = None, None
    for line in _read_text(path).splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line the run is still writing
        if message.get("type") == "result":
            result = message
        if message.get("type") != "assistant":
            continue
        for block in (message.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_use":
                continue
            given = block.get("input") or {}
            arg = str(given.get("command") or given.get("file_path") or "")
            step = next((text for matches, text in STEPS if matches(block.get("name"), arg)), step)
    return step, result


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


# The host


class Host:
    """Answers the extension's requests. Each request has an "id", which
    its reply repeats, a "type" and the PR's URL as "pr"; a reply has
    "ok" and either the answer or an "error"."""

    def __init__(
        self,
        send: Callable[[dict], None],
        store: Store | None = None,
        agent_config: AgentConfig | None = None,
        head_sha: Callable[[github.PrRef], str] = github.head_sha,
    ):
        self.send = send
        self.store = store or Store()
        self.agent_config = agent_config or AgentConfig()
        self.sifts = Sifts(self.agent_config)
        self.head_sha = head_sha
        self.servers: dict[int, ReportServer] = {}
        self.lock = threading.Lock()

    def handle(self, message: dict) -> None:
        handlers = {"status": self.status, "sift": self.sift, "cancel": self.cancel, "open": self.open}
        reply = {"id": message.get("id")}
        try:
            handler = handlers.get(message.get("type"))
            if handler is None:
                raise HostError(f"unknown request {message.get('type')!r}")
            pr = github.parse_pr(str(message.get("pr") or ""))
            reply |= {"ok": True} | handler(pr, message)
        except (HostError, github.GitHubError, repo.GitError) as e:
            reply |= {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            reply |= {"ok": False, "error": f"pr-sift failed: {e}"}
        self.send(reply)

    def _row(self, pr: github.PrRef):
        with self.store.lock:
            return self.store.pr(pr.repo_arg, pr.number)

    def status(self, pr: github.PrRef, message: dict) -> dict:
        """What pr-sift knows about the PR: whether it is analyzed and
        ranked, its units by attention, review progress and, when asked
        with "check_head", whether the PR moved on since the analysis."""
        status = {"pr": str(pr), "sift": self.sifts.status(pr)}
        row = self._row(pr)
        if row is None:
            return status | {"analyzed": False}
        with self.store.lock:
            data = page_data(self.store, row["id"])
        # Hidden as the report hides them: noise, and what Claude hid.
        units = [u for u in data["units"] if u["attention"] not in ("hidden", "hide")]
        status |= {
            "analyzed": True,
            "ranked": data["summary"] is not None and not data["summary"]["stale"],
            "head_sha": row["head_sha"],
            "analyzed_at": row["analyzed_at"],
            "units": len(units),
            "hidden": len(data["units"]) - len(units),
            "read": sum(u["attention"] == "read" for u in units),
            "skim": sum(u["attention"] == "skim" for u in units),
            "reviewed": sum(u["state"] == "seen" for u in units),
            "changed": sum(u["state"] == "changed" for u in units),
            "flagged": sum(u["flagged"] for u in units),
            "report": self.servers[row["id"]].url if row["id"] in self.servers else None,
        }
        if message.get("check_head"):
            try:
                remote = self.head_sha(pr)
                status |= {"remote_head_sha": remote, "head_moved": remote != row["head_sha"]}
            except github.GitHubError as e:
                status["head_error"] = str(e)
        return status

    def sift(self, pr: github.PrRef, message: dict) -> dict:
        return {"sift": self.sifts.start(pr, bool(message.get("rerank")))}

    def cancel(self, pr: github.PrRef, message: dict) -> dict:
        return {"sift": self.sifts.cancel(pr)}

    def open(self, pr: github.PrRef, message: dict) -> dict:
        """The URL of the PR's report, served from here from now on."""
        row = self._row(pr)
        if row is None:
            raise HostError(f"{pr} has not been sifted yet")
        with self.lock:
            server = self.servers.get(row["id"])
            if server is None:
                server = ReportServer(self.store, row["id"], 0, self.agent_config)
                threading.Thread(target=server.serve_forever, name=f"report-{pr}", daemon=True).start()
                self.servers[row["id"]] = server
        return {"url": server.url}

    def close(self) -> None:
        for server in self.servers.values():
            server.shutdown()
            server.server_close()
        self.sifts.shutdown()


def run(stdin: BinaryIO | None = None) -> int:
    """Serve the extension until the browser closes the connection."""
    # Messages own stdout: anything else printed there, by this process or
    # the tools it runs, goes to stderr instead, which Chrome logs.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    write_lock = threading.Lock()

    def send(message: dict) -> None:
        with write_lock:
            out.write(encode_message(message))
            out.flush()

    try:
        config = load_agent_config()
    except ConfigError as e:
        print(f"pr-sift: {e}", file=sys.stderr)
        config = AgentConfig()
    host = Host(send, agent_config=config)
    stdin = stdin or sys.stdin.buffer
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="request") as pool:
        while (message := read_message(stdin)) is not None:
            pool.submit(host.handle, message)
    host.close()
    return 0


# Installing


def install(browser_dirs: list[Path] | None = None, platform: str = sys.platform) -> tuple[Path, list[Path]]:
    """Write the host's wrapper script and its manifest for each browser
    found, or each given user data directory. Returns the wrapper and
    the manifests."""
    wrapper = data_dir() / "native-host"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    env = "".join(f"export {k}={shlex.quote(os.environ[k])}\n" for k in KEPT_ENV if k in os.environ)
    wrapper.write_text(
        "#!/bin/sh\n"
        "# Written by pr-sift extension install. Chrome starts this, with a bare\n"
        "# environment, when the PR Sift extension connects.\n"
        f"{env}exec {shlex.quote(sys.executable)} -m pr_sift.cli extension host \"$@\"\n"
    )
    wrapper.chmod(0o755)
    if browser_dirs is None:
        browser_dirs = [d for d in map(Path, BROWSER_DIRS.get(platform, [])) if d.expanduser().is_dir()]
    manifest = {
        "name": HOST_NAME,
        "description": "PR Sift: sift GitHub pull requests from the browser",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    written = []
    for directory in browser_dirs:
        path = directory.expanduser() / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        written.append(path)
    return wrapper, written
