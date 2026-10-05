"""Local git operations for the deploy pipeline.

A deploy ships a *snapshot commit*: the developer's working tree (tracked
and untracked files, minus anything ignored) committed on top of HEAD
without touching their branch, index, or stash. The snapshot's tree hash is
the content identity of what gets deployed; its parent is the developer's
real HEAD, which is what branch and ancestry checks are based on.
"""

import getpass
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class GitError(Exception):
    """Raised when a local git command fails or the repository is unusable."""


@dataclass
class Snapshot:
    commit: str
    tree: str
    base: str
    branch: str
    uncommitted: list[tuple[str, str]] = field(default_factory=list)


class LocalRepo:
    """A Frappe app's own git repository on the developer's machine."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def open(cls, app_dir: Path) -> "LocalRepo":
        if shutil.which("git") is None:
            raise GitError("git is not installed or not on PATH.")
        repo = cls(app_dir)
        try:
            toplevel = Path(repo._git("rev-parse", "--show-toplevel").strip())
        except GitError:
            raise GitError(f"'{app_dir}' is not a git repository; BenchOps deploys apps from git.") from None
        if toplevel.resolve() != app_dir.resolve():
            raise GitError(
                f"'{app_dir}' is inside the repository '{toplevel}', but BenchOps expects the app "
                "to be the root of its own repository."
            )
        if not repo.has_commit("HEAD"):
            raise GitError(f"'{app_dir}' has no commits yet; commit at least once before deploying.")
        return repo

    def _git(self, *args: str, env: dict | None = None) -> str:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _ok(self, *args: str) -> bool:
        try:
            self._git(*args)
            return True
        except GitError:
            return False

    def has_commit(self, rev: str) -> bool:
        return self._ok("rev-parse", "-q", "--verify", f"{rev}^{{commit}}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.has_commit(ancestor) and self._ok("merge-base", "--is-ancestor", ancestor, descendant)

    def is_pushed(self, commit: str) -> bool:
        """True if `commit` is contained in any remote-tracking branch."""
        return bool(self._git("branch", "-r", "--contains", commit).strip())

    def is_tracked(self, rel_path: str) -> bool:
        return bool(self._git("ls-files", "--", rel_path).strip())

    def user_name(self) -> str:
        try:
            return self._git("config", "user.name").strip() or getpass.getuser()
        except GitError:
            return getpass.getuser()

    def branch(self) -> str:
        try:
            return self._git("symbolic-ref", "--short", "-q", "HEAD").strip()
        except GitError:
            return "(detached)"

    def _identity_env(self, env: dict) -> dict:
        """commit-tree needs an identity; fall back to the OS user if git has none."""
        if not self._ok("var", "GIT_COMMITTER_IDENT"):
            user = getpass.getuser()
            for role in ("AUTHOR", "COMMITTER"):
                env.setdefault(f"GIT_{role}_NAME", user)
                env.setdefault(f"GIT_{role}_EMAIL", f"{user}@localhost")
        return env

    def snapshot(self, app_name: str, deployer: str) -> Snapshot:
        """Commit the current working tree on top of HEAD using a throwaway index."""
        base = self._git("rev-parse", "HEAD").strip()
        branch = self.branch()
        index = Path(self._git("rev-parse", "--git-path", "index").strip())
        if not index.is_absolute():
            index = self.path / index

        with tempfile.TemporaryDirectory() as tmp:
            tmp_index = Path(tmp) / "index"
            if index.exists():
                shutil.copy2(index, tmp_index)
            env = self._identity_env(dict(os.environ, GIT_INDEX_FILE=str(tmp_index)))
            self._git("add", "-A", env=env)
            # Build output and caches never belong in the snapshot, even when
            # the app's .gitignore forgets them; dist ships separately.
            self._git(
                "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
                ":(glob)**/__pycache__/**",
                ":(glob)**/*.pyc",
                ":(glob)**/node_modules/**",
                f":(glob){app_name}/public/dist/**",
                env=env,
            )
            tree = self._git("write-tree", env=env).strip()

        uncommitted = self._name_status(base, tree)
        message = f"benchops snapshot: {branch}@{base[:10]}"
        if uncommitted:
            message += f" + {len(uncommitted)} uncommitted file(s)"
        message += f"\n\nDeployed by {deployer}."
        commit = self._git(
            "commit-tree", tree, "-p", base, "-m", message, env=self._identity_env(dict(os.environ))
        ).strip()
        return Snapshot(commit=commit, tree=tree, base=base, branch=branch, uncommitted=uncommitted)

    def _name_status(self, old: str, new: str) -> list[tuple[str, str]]:
        parts = [p for p in self._git("diff", "--name-status", "-z", "--no-renames", old, new).split("\0") if p]
        return list(zip(parts[0::2], parts[1::2]))

    def update_ref(self, ref: str, commit: str) -> None:
        self._git("update-ref", ref, commit)

    def create_bundle(self, path: str, ref: str, exclude: list[str]) -> None:
        """Bundle `ref` with everything reachable from `exclude` left out —
        the remote already has those commits."""
        self._git("bundle", "create", "-q", path, ref, *[f"^{commit}" for commit in exclude])
