"""Temporary worktree management: clone source + targets into a temp dir."""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import git
from .config import Config


@dataclass
class Worktree:
    config: Config
    base: Path
    _cleanup: bool = True

    source_repo: Path = None  # type: ignore[assignment]
    target_repos: dict = field(default_factory=dict)

    @classmethod
    def create(cls, config: Config) -> "Worktree":
        base = Path(config.tmpdir) if config.tmpdir else None
        tmp = Path(tempfile.mkdtemp(prefix="gitsync-", dir=str(base) if base else None))
        wt = cls(config=config, base=tmp)
        wt._clone_all()
        return wt

    def _clone_all(self) -> None:
        gitcode = self.config.remotes["gitcode"]
        self.source_repo = self.base / "source"
        git.clone_bare(gitcode, str(self.source_repo))
        # 只 clone 启用的目标(需求2: 禁用的 target 不参与, 不拉取远端)
        for t in self.config.active_targets:
            remote_url = self.config.remotes[t.remote]
            repo = self.base / f"target-{t.name}"
            git.clone_bare(remote_url, str(repo))
            self.target_repos[t.name] = repo

    def source_branch_head(self) -> str:
        return git.rev_parse(
            f"refs/heads/{self.config.source_branch}", cwd=str(self.source_repo)
        )

    def master_head(self) -> str:
        return git.rev_parse(f"refs/heads/{self.config.source_master}", cwd=str(self.source_repo))

    def target_head(self, target_name: str) -> str:
        t = self._target(target_name)
        repo = self.target_repos[target_name]
        return git.rev_parse(f"refs/heads/{t.branch}", cwd=str(repo))

    def target_has_branch(self, target_name: str) -> bool:
        t = self._target(target_name)
        repo = self.target_repos[target_name]
        return ref_exists(repo, t.branch)

    def _target(self, name: str):
        for t in self.config.targets:
            if t.name == name:
                return t
        raise KeyError(f"no target named {name}")

    def close(self) -> None:
        if self._cleanup and self.base.exists():
            shutil.rmtree(self.base, ignore_errors=True)


def ref_exists(repo: Path, branch: str) -> bool:
    import subprocess

    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        cwd=str(repo), capture_output=True, text=True,
    )
    return proc.returncode == 0
