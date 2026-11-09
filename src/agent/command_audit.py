"""Issue #757 消费方接线：服务端 bash 命令「写目标」审计（默认关闭）。

背景：`src/agent/command_paths.extract_write_targets` 是语法感知的写目标提取实现，
但合入后没有任何运行时调用方（只有单测）。本模块把它接到服务端工具事件流上，让实现
真正被消费，服务于「命令审计」这一加法能力，**不改变任何任务判定语义**：

- 从 tool_use 的 bash 命令中取出声明写入的路径（heredoc / python -c 内容不会误判）；
- 对受保护路径（.git 内部、.env、凭据文件、系统目录）记 warning，只告警不阻断；
- 事件日志里附上 written_paths，便于出问题时定位“这条命令想写哪”。

开关：环境变量 `GRAPE_COMMAND_PATH_AUDIT=1`（默认 0 关闭，零行为变化）。
上限：命令长度 > _MAX_AUDIT_CHARS(256KB) 直接跳过，避免超长 heredoc 放大 CPU
（实测 1MB heredoc 82~126ms，典型命令仅 ~6µs）。
"""
from __future__ import annotations

import os
import re

from src.agent.command_paths import extract_write_targets
from src.config import log

_MAX_AUDIT_CHARS = 256 * 1024

# 受保护写入目标：命中即告警（不阻断）。按“路径形态”判定，不做工作区相对化换算，
# 因此对相对/绝对路径都生效。
_PROTECTED_PATTERNS = (
    (re.compile(r"(^|/)\.git/"), "vcs-internal"),
    (re.compile(r"(^|/)\.env(\.|$)"), "env-secret"),
    (re.compile(r"(^|/)\.ssh/"), "credential"),
    (re.compile(r"(^|/)(id_rsa|id_ed25519|\.npmrc|\.pypirc|credentials\.json)$"), "credential"),
    (re.compile(r"^/etc/"), "system"),
    (re.compile(r"^/root/"), "system"),
)


def audit_enabled() -> bool:
    """审计开关（默认关闭）：GRAPE_COMMAND_PATH_AUDIT=1/true/yes/on 时启用。"""
    return os.environ.get("GRAPE_COMMAND_PATH_AUDIT", "0").strip().lower() in ("1", "true", "yes", "on")


def audit_command(command: str) -> dict:
    """解析单条命令的写目标并标注受保护命中。

    返回 {"targets": [...], "protected": [(path, kind)], "skipped": bool, "enabled": bool}
    - skipped=True：命令超过大小上限，未解析（targets 为空）；
    - 任何解析异常都吞掉并返回空结果（审计是旁路，绝不影响主流程）。
    """
    info = {"targets": [], "protected": [], "skipped": False, "enabled": audit_enabled()}
    if not info["enabled"] or not command or not isinstance(command, str):
        return info
    if len(command) > _MAX_AUDIT_CHARS:
        info["skipped"] = True
        return info
    try:
        targets = extract_write_targets(command)
    except Exception as e:
        log.warning(f"command path audit: extract failed: {e}")
        return info
    info["targets"] = targets
    for path in targets:
        for rx, kind in _PROTECTED_PATTERNS:
            if rx.search(path):
                info["protected"].append((path, kind))
                break
    return info


def audit_tool_use(tool_use: dict, repo_key: str = "", agent_type: str = "") -> dict:
    """审计一条 tool_use 事件（取出 bash 命令 → audit_command），命中受保护路径记 warning。

    返回 audit_command 的结果；开关关闭时直接返回空结果（零成本）。
    """
    if not audit_enabled():
        return {"targets": [], "protected": [], "skipped": False, "enabled": False}
    try:
        from src.api.event_store import _extract_tool_command
        cmd = _extract_tool_command(tool_use) or ""
    except Exception:
        cmd = ""
    info = audit_command(cmd)
    if info.get("protected"):
        log.warning(
            "command path audit: protected write target "
            f"{info['protected']} repo={repo_key or '-'} agent={agent_type or '-'} "
            f"targets={info['targets']} cmd={cmd[:200]!r}"
        )
    elif info.get("targets"):
        log.info(
            f"command path audit: write targets {info['targets']} "
            f"repo={repo_key or '-'} agent={agent_type or '-'}"
        )
    return info