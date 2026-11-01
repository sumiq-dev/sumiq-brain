"""Thin wrapper around the git CLI for versioning vault changes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FALLBACK_NAME = "AI Brain"
FALLBACK_EMAIL = "brain@localhost"


class GitStore:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def installed() -> bool:
        return shutil.which("git") is not None

    @staticmethod
    def lfs_installed() -> bool:
        return shutil.which("git-lfs") is not None

    def enabled(self) -> bool:
        return self.installed() and (self.root / ".git").exists()

    def _run(self, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=check,
            env={**os.environ, **(env or {})},
        )

    def init(self) -> None:
        self._run("init", "-q")
        if self.lfs_installed():
            self._run("lfs", "install", "--local", check=False)

    def _identity_env(self, author: str) -> dict:
        env = {"GIT_AUTHOR_NAME": author}
        if not self._run("config", "user.name", check=False).stdout.strip():
            env["GIT_COMMITTER_NAME"] = FALLBACK_NAME
        if not self._run("config", "user.email", check=False).stdout.strip():
            env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = FALLBACK_EMAIL
        return env

    def commit(self, paths: list[str], message: str, author: str) -> bool:
        """Stage the given vault-relative paths (including deletions) and commit them.
        Returns False if there was nothing to commit."""
        if not self.enabled() or not paths:
            return False
        # Deleted files that git never tracked can't be staged; skip them.
        tracked = set(self._run("ls-files", "-z", "--", *paths).stdout.split("\0"))
        paths = [p for p in paths if (self.root / p).exists() or p in tracked]
        if not paths:
            return False
        self._run("add", "-A", "--", *paths)
        if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self._run("commit", "-q", "-m", message, env=self._identity_env(author))
        return True
