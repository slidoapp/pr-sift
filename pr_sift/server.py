"""Serve one PR's report, its review state, comment drafts and questions
on localhost.

Only requests addressed to this server (by Host header) and carrying
the per-session token are answered, so other web pages open in the
browser can neither read the report, change review state nor start
agents.
"""

import json
import mimetypes
import re
import secrets
import shutil
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pr_sift import github, repo
from pr_sift.agent import AgentError, Agents
from pr_sift.anchors import AnchorError, make_anchor
from pr_sift.config import AgentConfig
from pr_sift.posting import GitHubPoster, PlanError, match_posted, parse_diff, plan_review
from pr_sift.report import FLAGGED, REVIEWED, page_data
from pr_sift.store import Store
from pr_sift.threads import comment_view, draft_prompt, first_prompt, question_view, stored_walkthrough, thread_data

WEB = Path(__file__).parent / "web"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
)
MAX_TEXT = 20_000
COMMENT = re.compile(r"^/api/comments/(\d+)$")
QUESTION = re.compile(r"^/api/questions/(\d+)(/messages|/cancel|/events|/draft)?$")


class ReportServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        store: Store,
        pr_id: int,
        port: int = 0,
        agent_config: AgentConfig | None = None,
        workdir: Callable[[], Path] | None = None,
        poster=None,
    ):
        super().__init__(("127.0.0.1", port), Handler)
        self.store = store
        self.pr_id = pr_id
        self.token = secrets.token_urlsafe(32)
        # The page is read once, so it always matches the API of this
        # server, even when pr-sift is upgraded while it runs.
        self.index = (WEB / "index.html").read_text().replace("__TOKEN__", self.token).encode()
        self.static = {p.relative_to(WEB).as_posix(): p.read_bytes() for p in WEB.rglob("*") if p.is_file()}
        self.agent_config = agent_config or AgentConfig()
        self.workdir = workdir or self._worktree
        self.agents = Agents(store, self.agent_config, self.question_view, pr_id)
        self.poster = poster or GitHubPoster(store.pr_by_id(pr_id))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"

    def _worktree(self) -> Path:
        pr = self.store.pr_by_id(self.pr_id)
        return repo.worktree(pr["repo"], pr["number"], pr["head_sha"])

    def question_view(self, question_id: int) -> dict:
        with self.store.lock:
            row = self.store.question(question_id)
            units = self.store.units(self.pr_id)
            walkthrough = stored_walkthrough(self.store.summary(self.pr_id))
            messages = self.store.messages([question_id])[question_id]
            return question_view(row, messages, units, walkthrough, self.agents.running(question_id))

    def page(self) -> dict:
        config = self.agent_config
        return (
            page_data(self.store, self.pr_id)
            | thread_data(self.store, self.pr_id, self.agents.running)
            | {
                "agent": {
                    "available": shutil.which(config.command[0]) is not None,
                    "command": config.command[0],
                    "web_fetch": config.web_fetch,
                    "web_fetch_domains": config.web_fetch_domains,
                },
            }
        )

    def server_close(self) -> None:
        self.agents.shutdown()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: ReportServer

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self._checked_path()
        if path is None:
            return
        if path == "/":
            self._send(HTTPStatus.OK, self.server.index, "text/html; charset=utf-8")
        elif path.startswith("/static/") and (name := path.removeprefix("/static/")) in self.server.static:
            kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, self.server.static[name], kind)
        elif path == "/api/pr":
            if self._authorized():
                with self.server.store.lock:
                    self._json(HTTPStatus.OK, self.server.page())
        elif path == "/api/head":
            if self._authorized():
                self._head()
        elif (match := QUESTION.match(path)) and match[2] == "/events":
            # EventSource can't send headers: the token comes in the query.
            token = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            if self._authorized(token):
                self._events(int(match[1]))
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self):
        self._mutate("PUT")

    def do_POST(self):
        self._mutate("POST")

    def do_PATCH(self):
        self._mutate("PATCH")

    def do_DELETE(self):
        self._mutate("DELETE")

    def _mutate(self, method):
        path = self._checked_path()
        if path is None or not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "expected a JSON object"})
        if method == "POST" and path in ("/api/review/preview", "/api/review"):
            # These wait on GitHub, so they take the lock only while they
            # use the store.
            return self._review(body, submit=path == "/api/review")
        if method == "POST" and (match := QUESTION.match(path)) and match[2] == "/draft":
            return self._draft(int(match[1]))
        with self.server.store.lock:
            self._route(method, path, body)

    def _route(self, method, path, body):
        prefix, suffix = "/api/units/", "/review"
        if method == "PUT" and path.startswith(prefix) and path.endswith(suffix):
            return self._update_review(unquote(path[len(prefix) : -len(suffix)]), body)
        if method == "POST" and path == "/api/comments":
            return self._add_comment(body)
        if method == "POST" and path == "/api/questions":
            return self._ask(body)
        if (match := COMMENT.match(path)) and method in ("PATCH", "DELETE"):
            return self._change_comment(method, int(match[1]), body)
        if match := QUESTION.match(path):
            question_id, action = int(match[1]), match[2]
            if (method, action) == ("POST", "/messages"):
                return self._follow_up(question_id, body)
            if (method, action) == ("POST", "/cancel"):
                return self._cancel(question_id)
            if action is None and method in ("PATCH", "DELETE"):
                return self._change_question(method, question_id, body)
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _update_review(self, qualname, body):
        store, pr_id = self.server.store, self.server.pr_id
        unit = next((u for u in store.units(pr_id) if u["qualname"] == qualname), None)
        if unit is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no unit {qualname}"})
        if body.get("fingerprint") != unit["fingerprint"]:
            return self._json(HTTPStatus.CONFLICT, {"error": "the unit changed; reload the report"})
        for key, status in (("reviewed", REVIEWED), ("flagged", FLAGGED)):
            if key not in body:
                continue
            if body[key]:
                store.set_review(pr_id, qualname, unit["fingerprint"], unit["new_body"], status)
            else:
                store.clear_review(pr_id, qualname, status)
        view = next(u for u in page_data(store, pr_id)["units"] if u["qualname"] == qualname)
        self._json(HTTPStatus.OK, view)

    # Comments

    def _anchor(self, data):
        store, pr_id = self.server.store, self.server.pr_id
        return make_anchor(data, store.units(pr_id), stored_walkthrough(store.summary(pr_id)))

    def _comment_view(self, comment_id):
        store, pr_id = self.server.store, self.server.pr_id
        row = store.comment(comment_id)
        return comment_view(row, store.units(pr_id), stored_walkthrough(store.summary(pr_id)))

    def _add_comment(self, body):
        store = self.server.store
        text = _text(body.get("body"))
        if text is None:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "a comment needs a body"})
        try:
            anchor = self._anchor(body.get("anchor"))
        except AnchorError as e:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        if anchor["anchor_kind"] == "walkthrough":
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "comments can't attach to the walkthrough"})
        question_id = body.get("question_id")
        if question_id is not None and not self._own_question(question_id):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": f"no question {question_id}"})
        comment_id = store.add_comment(self.server.pr_id, anchor, text, question_id)
        self._json(HTTPStatus.CREATED, self._comment_view(comment_id))

    def _change_comment(self, method, comment_id, body):
        store = self.server.store
        row = store.comment(comment_id)
        if row is None or row["pr_id"] != self.server.pr_id:
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no comment {comment_id}"})
        if row["state"] != "draft":
            return self._json(HTTPStatus.CONFLICT, {"error": "the comment was already posted"})
        if method == "DELETE":
            store.delete_comment(comment_id)
            return self._json(HTTPStatus.OK, {"deleted": comment_id})
        text = _text(body.get("body"))
        if text is None:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "a comment needs a body"})
        store.update_comment(comment_id, text)
        self._json(HTTPStatus.OK, self._comment_view(comment_id))

    # Posting a review

    def _review(self, body, submit):
        """Preview or post the drafts as one review. The preview says what
        goes where; posting checks that the drafts and the PR head are
        still the ones previewed."""
        store, server = self.server.store, self.server
        with store.lock:
            pr = store.pr_by_id(server.pr_id)
            units = store.units(server.pr_id)
            walkthrough = stored_walkthrough(store.summary(server.pr_id))
            drafts = [comment_view(c, units, walkthrough) for c in store.comments(server.pr_id) if c["state"] == "draft"]
            try:
                files = parse_diff(server.poster.diff(pr["merge_base"], pr["head_sha"]))
            except repo.GitError as e:
                return self._json(HTTPStatus.CONFLICT, {"error": f"can't read the PR's diff: {e}"})
            summary = body.get("body") or ""
            if not isinstance(summary, str):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be text"})
            try:
                plan = plan_review(drafts, {u["qualname"]: u for u in units}, files, body.get("event"), summary,
                                   bool(body.get("include_orphaned")))
            except PlanError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        try:
            remote = server.poster.head_sha()
        except github.GitHubError as e:
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"can't reach GitHub: {e}"})
        preview = {
            "event": plan.event,
            "body": plan.body,
            "comments": [c["payload"] | {"draft": c["draft"]} for c in plan.comments],
            "placed": plan.placed,
            "drafts": sorted(p["draft"] for p in plan.placed if p["where"] != "skipped"),
            "head_sha": pr["head_sha"],
            "remote_head_sha": remote,
            "head_moved": remote != pr["head_sha"],
        }
        if not submit:
            return self._json(HTTPStatus.OK, preview)

        if preview["head_moved"]:
            return self._json(HTTPStatus.CONFLICT, {
                "error": f"the PR has new commits ({remote[:7]}) since it was analyzed; "
                         "run /pr-sift again, check your drafts, then post",
            })
        if body.get("drafts") != preview["drafts"] or body.get("head_sha") != pr["head_sha"]:
            return self._json(HTTPStatus.CONFLICT, {"error": "the drafts changed since the preview; preview again"})
        try:
            review = server.poster.create_review(plan.payload(pr["head_sha"]))
        except github.GitHubError as e:
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"GitHub didn't take the review: {e}"})
        try:
            posted = match_posted(plan, server.poster.review_comments(review["id"])) if plan.comments else {}
        except github.GitHubError:
            posted = {}  # the review is posted; only the comments' links are missing
        url = review.get("html_url")
        with store.lock:
            submission_id = store.add_submission(server.pr_id, review["id"], plan.event, plan.body, url, pr["head_sha"])
            for place in plan.placed:
                if place["where"] == "skipped":
                    continue
                comment = posted.get(place["draft"], {})
                store.mark_posted(place["draft"], submission_id, comment.get("id"), comment.get("html_url") or url)
            self._json(HTTPStatus.CREATED, {"url": url, "event": plan.event} | thread_data(store, server.pr_id, server.agents.running))

    # Questions

    def _own_question(self, question_id):
        row = self.server.store.question(question_id) if isinstance(question_id, int) else None
        return row if row is not None and row["pr_id"] == self.server.pr_id else None

    def _ask(self, body):
        store, server = self.server.store, self.server
        text = _text(body.get("text"))
        if text is None:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "a question needs text"})
        try:
            anchor = self._anchor(body.get("anchor"))
        except AnchorError as e:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        try:
            workdir = server.workdir()
        except repo.GitError as e:
            return self._json(HTTPStatus.CONFLICT, {"error": f"can't check out the PR for the agent: {e}"})
        question_id = store.add_question(server.pr_id, anchor)
        store.add_message(question_id, "you", text)
        view = server.question_view(question_id)
        server.agents.ask(question_id, first_prompt(store, server.pr_id, view["anchor"], text), workdir)
        self._json(HTTPStatus.CREATED, server.question_view(question_id))

    def _follow_up(self, question_id, body):
        store, server = self.server.store, self.server
        row = self._own_question(question_id)
        if row is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no question {question_id}"})
        if server.agents.running(question_id):
            return self._json(HTTPStatus.CONFLICT, {"error": "the agent is still answering"})
        text = _text(body.get("text"))
        if text is None:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "a question needs text"})
        previous = server.question_view(question_id)
        store.add_message(question_id, "you", text)
        store.update_question(question_id, resolved=0)
        # Resume the thread's session where it ran; else start a new one
        # that is told what was said so far.
        workdir = Path(row["workdir"]) if row["workdir"] else None
        if row["session_id"] and workdir and workdir.exists():
            server.agents.ask(question_id, text, workdir, row["session_id"])
        else:
            try:
                workdir = server.workdir()
            except repo.GitError as e:
                return self._json(HTTPStatus.CONFLICT, {"error": f"can't check out the PR for the agent: {e}"})
            prompt = first_prompt(store, server.pr_id, previous["anchor"], text, previous["messages"])
            server.agents.ask(question_id, prompt, workdir)
        self._json(HTTPStatus.OK, server.question_view(question_id))

    def _draft(self, question_id):
        """A review comment the agent writes from the thread. It waits on
        the agent, so it takes the lock only while it uses the store."""
        store, server = self.server.store, self.server
        with store.lock:
            row = self._own_question(question_id)
            if row is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": f"no question {question_id}"})
            if server.agents.running(question_id):
                return self._json(HTTPStatus.CONFLICT, {"error": "the agent is still answering"})
            view = server.question_view(question_id)
            if not any(m["role"] == "agent" for m in view["messages"]):
                return self._json(HTTPStatus.CONFLICT, {"error": "the question has no answer yet"})
            prompt = draft_prompt(store, server.pr_id, view["anchor"], view["messages"])
            workdir = Path(row["workdir"]) if row["workdir"] else None
        try:
            if not workdir or not workdir.exists():
                workdir = server.workdir()
            body = server.agents.draft(prompt, workdir)
        except repo.GitError as e:
            return self._json(HTTPStatus.CONFLICT, {"error": f"can't check out the PR for the agent: {e}"})
        except AgentError as e:
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
        self._json(HTTPStatus.OK, {"body": body[:MAX_TEXT]})

    def _cancel(self, question_id):
        if self._own_question(question_id) is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no question {question_id}"})
        self.server.agents.cancel(question_id)
        self._json(HTTPStatus.OK, self.server.question_view(question_id))

    def _change_question(self, method, question_id, body):
        store = self.server.store
        if self._own_question(question_id) is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"no question {question_id}"})
        if method == "DELETE":
            self.server.agents.cancel(question_id)
            store.delete_question(question_id)
            return self._json(HTTPStatus.OK, {"deleted": question_id})
        if not isinstance(body.get("resolved"), bool):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "expected resolved: true or false"})
        store.update_question(question_id, resolved=int(body["resolved"]))
        self._json(HTTPStatus.OK, self.server.question_view(question_id))

    def _events(self, question_id):
        """The question's running answer as server-sent events. A question
        with no run in this server gets one final event."""
        with self.server.store.lock:
            if self._own_question(question_id) is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": f"no question {question_id}"})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            sent = False
            for event in self.server.agents.events(question_id):
                self.wfile.write(b": keep-alive\n\n" if event is None else _event(event))
                self.wfile.flush()
                sent = sent or event is not None
            if not sent:
                self.wfile.write(_event({"type": "done", "question": self.server.question_view(question_id)}))
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page went away

    # Helpers

    def _checked_path(self):
        """The request path, or None after rejecting a foreign Host."""
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host"})
            return None
        return urlsplit(self.path).path

    def _head(self):
        """Whether the PR moved on since the analysis the report shows."""
        head = self.server.store.pr_by_id(self.server.pr_id)["head_sha"]
        try:
            remote = self.server.poster.head_sha()
        except github.GitHubError as e:
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"can't reach GitHub: {e}"})
        self._json(HTTPStatus.OK, {"head_sha": head, "remote_head_sha": remote, "head_moved": remote != head})

    def _authorized(self, token=None):
        given = token if token is not None else self.headers.get("X-PR-Sift-Token", "")
        if secrets.compare_digest(given.encode(), self.server.token.encode()):
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong token"})
        return False

    def _json(self, status, data):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _text(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:MAX_TEXT]


def _event(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()
