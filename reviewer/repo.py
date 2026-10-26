"""Cached bare clones of the repositories under review.

Reviewing never touches your working checkout.
"""

import os
import subprocess
from pathlib import Path

from reviewer import github

# Bytes of a file checked for NUL to decide it is binary, as git does.
BINARY_PROBE = 8000


class GitError(RuntimeError):
    pass


def cache_dir() -> Path:
    return Path(os.environ.get("REVIEWER_CACHE_DIR", "~/.cache/reviewer")).expanduser()


def git(git_dir: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "--git-dir", str(git_dir), *args], input=input, capture_output=True)
    if result.returncode:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def ensure_clone(pr: github.PrRef) -> Path:
    path = cache_dir() / pr.host / pr.owner / f"{pr.repo}.git"
    if not (path / "HEAD").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        github.clone_bare(pr, str(path))
    return path


def fetch_pr(git_dir: Path, number: int, base_ref: str) -> None:
    git(
        git_dir,
        "fetch",
        "--quiet",
        "--force",
        "origin",
        f"+refs/pull/{number}/head:refs/reviewer/pull/{number}",
        f"+refs/heads/{base_ref}:refs/reviewer/base/{base_ref}",
    )


def merge_base(git_dir: Path, a: str, b: str) -> str:
    return git(git_dir, "merge-base", a, b).decode().strip()


def changed_paths(git_dir: Path, base: str, head: str) -> tuple[list[str], list[str]]:
    """Return the changed paths as they are at base and at head.

    Added files appear only at head, deleted files only at base.
    """
    fields = git(git_dir, "diff", "--name-status", "--no-renames", "-z", base, head).decode().split("\0")
    old, new = [], []
    for status, path in zip(fields[::2], fields[1::2]):
        if status != "A":
            old.append(path)
        if status != "D":
            new.append(path)
    return old, new


def list_files(git_dir: Path, rev: str) -> list[str]:
    return [p for p in git(git_dir, "ls-tree", "-r", "-z", "--name-only", rev).decode().split("\0") if p]


def read_files(git_dir: Path, rev: str, paths: list[str]) -> dict[str, str]:
    """Read text files at a revision. Missing and binary files are skipped."""
    if not paths:
        return {}
    out = git(git_dir, "cat-file", "--batch", input="".join(f"{rev}:{p}\n" for p in paths).encode())
    files, pos = {}, 0
    for path in paths:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if header[-1] == b"missing":
            pos = end + 1
            continue
        size = int(header[2])
        content = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
        if header[1] == b"blob" and b"\0" not in content[:BINARY_PROBE]:
            files[path] = content.decode("utf-8", errors="replace")
    return files


def clone_path(repo_key: str) -> Path:
    """The cached bare clone of host/owner/name."""
    host, owner, name = repo_key.split("/", 2)
    return cache_dir() / host / owner / f"{name}.git"


def worktree(repo_key: str, number: int, sha: str) -> Path:
    """A detached checkout of sha for agents to read, made from the cached
    clone. Older checkouts of the same PR are removed."""
    git_dir = clone_path(repo_key)
    if not (git_dir / "HEAD").exists():
        raise GitError(f"no cached clone of {repo_key}; run reviewer analyze first")
    parent = cache_dir() / "worktrees" / repo_key / f"pr-{number}"
    path = parent / sha[:12]
    if (path / ".git").exists():
        return path
    if parent.exists():
        for old in parent.iterdir():
            subprocess.run(["git", "--git-dir", str(git_dir), "worktree", "remove", "--force", str(old)], capture_output=True)
    git(git_dir, "worktree", "prune")
    parent.mkdir(parents=True, exist_ok=True)
    git(git_dir, "worktree", "add", "--detach", "--force", str(path), sha)
    return path
