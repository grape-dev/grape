"""Configuration loading and validation for gitsync."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""


@dataclass
class IdentityConfig:
    name: str
    email: str


@dataclass
class TargetConfig:
    name: str
    remote: str
    branch: str
    rewrite_identity: bool
    match_strategy: str  # "content" or "sha"
    verify_against: Optional[str] = None
    enabled: bool = True  # 需求2(2026-08-05): 同步目标开关, false 时该 target 不参与同步/发布/DB 回写


@dataclass
class ClawhubConfig:
    base_url: str = "https://clawhub.ai"
    config_path: str = ""
    skills_dir: str = "skills"
    manifest_file: str = ""
    manifest_backup_file: str = ""
    name_dir_mismatch: str = "warn"  # "warn" or "block"
    organization: str = ""
    exclude_skills: List[str] = field(default_factory=list)  # 需求3: skill 黑名单(精确名/glob), 命中不发布
    publish_mode: str = "cli"  # "cli" = 官方 clawhub CLI(逐文件串行); "native" = 内置并发上传器(Plan B)
    publish_concurrency: int = 6  # native 模式下并发上传的文件数
    skill_timeout: int = 0  # 单 skill 发布总超时(秒); 0 = 按模式默认(native 900 / cli 2700)


@dataclass
class DbConfig:
    host: str = ""
    user: str = ""
    password: str = ""
    database: str = ""
    product_dir: str = ""


@dataclass
class Config:
    identity: IdentityConfig
    remotes: Dict[str, str]
    branches: Dict[str, str]
    targets: List[TargetConfig]
    clawhub: ClawhubConfig = field(default_factory=ClawhubConfig)
    db: DbConfig = field(default_factory=DbConfig)
    map_file: str = ""
    acknowledged_commits: List[str] = field(default_factory=list)
    backup_ref: str = "refs/heads/backup/pre-sync-"
    tmpdir: str = ""

    @property
    def source_master(self) -> str:
        return self.branches.get("source_master", "master")

    @property
    def source_branch(self) -> str:
        return self.branches.get("source_branch", "github")

    @property
    def active_targets(self) -> List[TargetConfig]:
        """需求2: 仅返回 enabled=true 的同步目标。

        所有消费 config.targets 的命令(sync/check/bootstrap/maprebuild/
        run/db-update)都应遍历 active_targets, 使禁用目标完全不参与。
        verify_against 引用的 target 若被禁用, 由调用方显式处理
        (禁用的参照目标视为已同步/跳过, 见 sync_engine)。
        """
        return [t for t in self.targets if t.enabled]


def _require(d: Dict[str, Any], keys: List[str], section: str) -> Dict[str, Any]:
    for k in keys:
        if k not in d or d[k] in (None, ""):
            raise ConfigError(f"config: missing required key '{section}.{k}'")
    return d


def load_config(path: Optional[str]) -> Config:
    if not path:
        path = os.environ.get("GITSYNC_CONFIG")
    if not path or not os.path.isfile(path):
        raise ConfigError(
            f"config file not found: {path or '(none)'} "
            "(pass --config or set GITSYNC_CONFIG)"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    identity = _require(data.get("identity", {}), ["name", "email"], "identity")
    remotes = _require(data.get("remotes", {}), [], "remotes")
    branches = _require(data.get("branches", {}), [], "branches")

    raw_targets = data.get("targets", [])
    targets: List[TargetConfig] = []
    for t in raw_targets:
        t = _require(t, ["name", "remote", "branch"], "targets")
        targets.append(
            TargetConfig(
                name=t["name"],
                remote=t["remote"],
                branch=t["branch"],
                rewrite_identity=bool(t.get("rewrite_identity", True)),
                match_strategy=t.get("match_strategy", "content"),
                verify_against=t.get("verify_against"),
                enabled=bool(t.get("enabled", True)),
            )
        )
    if not targets:
        raise ConfigError("config: at least one target is required under 'targets'")

    clawhub_raw = data.get("clawhub", {})
    db_raw = data.get("db", {})

    return Config(
        identity=IdentityConfig(name=identity["name"], email=identity["email"]),
        remotes=remotes,
        branches=branches,
        targets=targets,
        clawhub=ClawhubConfig(
            base_url=clawhub_raw.get("base_url", "https://clawhub.ai"),
            config_path=clawhub_raw.get("config_path", ""),
            skills_dir=clawhub_raw.get("skills_dir", "skills"),
            manifest_file=clawhub_raw.get("manifest_file", ""),
            manifest_backup_file=clawhub_raw.get("manifest_backup_file", ""),
            name_dir_mismatch=clawhub_raw.get("name_dir_mismatch", "warn"),
            organization=clawhub_raw.get("organization", ""),
            exclude_skills=[
                s.strip() for s in clawhub_raw.get("exclude_skills", [])
                if isinstance(s, str) and s.strip()
            ],
            publish_mode=clawhub_raw.get("publish_mode", "cli"),
            publish_concurrency=int(clawhub_raw.get("publish_concurrency", 6)),
            skill_timeout=int(clawhub_raw.get("skill_timeout", 0)),
        ),
        db=DbConfig(
            host=db_raw.get("host", ""),
            user=db_raw.get("user", ""),
            password=db_raw.get("password", ""),
            database=db_raw.get("database", ""),
            product_dir=db_raw.get("product_dir", ""),
        ),
        map_file=data.get("map_file", ""),
        acknowledged_commits=data.get("acknowledged_commits", []),
        backup_ref=data.get("backup_ref", "refs/heads/backup/pre-sync-"),
        tmpdir=data.get("tmpdir", ""),
    )
