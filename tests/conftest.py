import subprocess
from pathlib import Path

import pytest


class GitRepo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")

    @property
    def git_dir(self) -> Path:
        return self.path / ".git"

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True, text=True).stdout

    def commit(self, files: dict[str, str | bytes | None], message: str = "change") -> str:
        """Write (or delete, for None) files and commit them; return the SHA."""
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def git_repo(tmp_path):
    return GitRepo(tmp_path / "repo")


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REVIEWER_CACHE_DIR", str(tmp_path / "cache"))
