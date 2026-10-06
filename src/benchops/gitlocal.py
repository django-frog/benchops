"""Local git operations for the deploy pipeline.

A deploy ships the developer's commits plus what they staged with
`git add`. Both travel as one *staged commit*: its tree is their index and
its parent is their HEAD. It is built with plumbing commands only, so their
branch, index, working tree and stash are never touched; unstaged edits and
untracked files stay on their machine.
"""

import getpass
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(Exception):
    """Raised when a local git command fails or the repository is unusable."""


@dataclass
class StagedState:
    commit: str
    tree: str
    base: str
    branch: str | None
    staged: list[tuple[str, str]] = field(default_factory=list)
    unstaged: list[tuple[str, str]] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)


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

    def branch(self) -> str | None:
        try:
            return self._git("symbolic-ref", "--short", "-q", "HEAD").strip()
        except GitError:
            return None

    def _identity_env(self, env: dict) -> dict:
        """commit-tree needs an identity; fall back to the OS user if git has none."""
        if not self._ok("var", "GIT_COMMITTER_IDENT"):
            user = getpass.getuser()
            for role in ("AUTHOR", "COMMITTER"):
                env.setdefault(f"GIT_{role}_NAME", user)
                env.setdefault(f"GIT_{role}_EMAIL", f"{user}@localhost")
        return env

    def staged_state(self, deployer: str, build_outputs: list[str] = ()) -> StagedState:
        """Capture HEAD plus the index as a staged commit, and list what is
        staged, unstaged and untracked. Build outputs are left out of the
        lists: they ship from the build, not from git."""
        if self._git("ls-files", "--unmerged").strip():
            raise GitError("The index has unresolved merge conflicts; resolve them before deploying.")

        def keep(path: str) -> bool:
            return not any(path == out or path.startswith(out + "/") for out in build_outputs)

        base = self._git("rev-parse", "HEAD").strip()
        branch = self.branch()
        tree = self._git("write-tree").strip()
        staged = [(s, p) for s, p in self._name_status(base, tree) if keep(p)]
        unstaged = [(s, p) for s, p in self._name_status() if keep(p)]
        untracked = [
            p for p in self._git("ls-files", "--others", "--exclude-standard", "-z").split("\0") if p and keep(p)
        ]

        message = f"benchops staged changes: {branch or '(detached)'}@{base[:10]}, {len(staged)} staged file(s)"
        message += f"\n\nDeployed by {deployer}."
        commit = self._git(
            "commit-tree", tree, "-p", base, "-m", message, env=self._identity_env(dict(os.environ))
        ).strip()
        return StagedState(
            commit=commit, tree=tree, base=base, branch=branch, staged=staged, unstaged=unstaged, untracked=untracked
        )

    def commits_between(self, old: str, new: str) -> int | None:
        """How many commits `new` is ahead of `old`, if `old` is known here."""
        if not self.has_commit(old):
            return None
        return int(self._git("rev-list", "--count", f"{old}..{new}").strip())

    def _name_status(self, *revs: str) -> list[tuple[str, str]]:
        parts = [p for p in self._git("diff", "--name-status", "-z", "--no-renames", *revs).split("\0") if p]
        return list(zip(parts[0::2], parts[1::2]))

    def update_ref(self, ref: str, commit: str) -> None:
        self._git("update-ref", ref, commit)

    def create_bundle(self, path: str, ref: str, exclude: list[str]) -> None:
        """Bundle `ref` with everything reachable from `exclude` left out —
        the remote already has those commits."""
        self._git("bundle", "create", "-q", path, ref, *[f"^{commit}" for commit in exclude])
