"""Thin wrappers around the gh CLI."""

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

PR_FIELDS = "number,title,body,url,author,baseRefName,baseRefOid,headRefName,headRefOid"
URL_RE = re.compile(r"https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)")
SLUG_RE = re.compile(r"(?:([^/#]+)/)?([^/#]+)/([^/#]+)#(\d+)")


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrRef:
    host: str
    owner: str
    repo: str
    number: int

    @property
    def repo_arg(self) -> str:
        """The repository as gh's -R option takes it."""
        return f"{self.host}/{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def gh(*args: str) -> str:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise GitHubError("gh is not installed or not on PATH") from None
    if result.returncode:
        raise GitHubError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def default_host() -> str:
    return os.environ.get("GH_HOST", "github.com")


def parse_repo(repo: str) -> tuple[str, str, str]:
    """Parse "[host/]owner/name" or a repository URL."""
    repo = re.sub(r"^https?://", "", repo).removesuffix(".git").strip("/")
    parts = repo.split("/")
    if len(parts) == 2:
        return default_host(), parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise GitHubError(f"not a repository: {repo!r}")


def current_repo() -> tuple[str, str, str]:
    """The repository of the current directory."""
    return parse_repo(json.loads(gh("repo", "view", "--json", "url"))["url"])


def current_pr() -> "PrRef":
    """The pull request of the branch checked out in the current directory."""
    return parse_pr(json.loads(gh("pr", "view", "--json", "url"))["url"])


def parse_pr(
    spec: str,
    repo: str | None = None,
    current: Callable[[], tuple[str, str, str]] = current_repo,
) -> PrRef:
    """Parse a PR number, "[host/]owner/repo#n" or a PR URL."""
    if m := URL_RE.match(spec):
        return PrRef(m[1], m[2], m[3], int(m[4]))
    if m := SLUG_RE.fullmatch(spec):
        return PrRef(m[1] or default_host(), m[2], m[3], int(m[4]))
    if spec.lstrip("#").isdigit():
        host, owner, name = parse_repo(repo) if repo else current()
        return PrRef(host, owner, name, int(spec.lstrip("#")))
    raise GitHubError(f"not a pull request: {spec!r}")


def pr_metadata(pr: PrRef) -> dict:
    return json.loads(gh("pr", "view", str(pr.number), "-R", pr.repo_arg, "--json", PR_FIELDS))


def clone_bare(pr: PrRef, path: str) -> None:
    gh("repo", "clone", pr.repo_arg, path, "--", "--bare", "--quiet")
