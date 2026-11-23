"""Issue #3928：引擎后端 CLI 能力探针（worker / 服务端共用无状态层）。

引擎类型 → 后端可执行文件映射 + ``shutil.which`` 探测 + TTL 缓存。结果四态：

- ``available``：命令可解析（可参与认领）；
- ``unavailable``：明确未找到命令（不参与认领，熔断可恢复）；
- ``degraded``：命令存在但探活 / spawn 失败（不参与认领，保留观察）；
- ``unknown``：未纳管引擎或探针自身异常（不摘除，保持旧行为）。

探针只做轻量路径查找，不起子进程；缓存避免每次注册/心跳重复探测。
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

STATUS_AVAILABLE = "available"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"

BLOCKING_STATUSES = (STATUS_DEGRADED, STATUS_UNAVAILABLE)

#: 已纳管引擎 → 后端命令。未登记引擎默认 unknown（不误伤新引擎）。
ENGINE_BACKEND_COMMANDS = {
    "opencode": "opencode",
    "codearts": "codearts",
    "hermes": "hermes",
    "codex": "codex",
    "dsh": "dsh",
    "atomcode": "atomcode",
    "hwcloud": "hwcloud",
    "aishell-dsh": "aishell",
    "cursor": "cursor",
}

DEFAULT_PROBE_TTL = float(os.environ.get("RUNTIME_BACKEND_PROBE_TTL", "300"))

_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_LOCK = threading.Lock()

_MISSING_CLI_RE = re.compile(
    r"(cli\s+not\s+found|not\s+found\s+in\s+path|"
    r"no\s+such\s+file(?:\s+or\s+directory)?|\bnot\s+installed\b)",
    re.I)
_COMMAND_NOT_FOUND_RE = re.compile(
    r"\b(?:codearts|opencode|hermes|hwcloud|aishell|codex|dsh|atomcode|cursor)"
    r"\s+not\s+found\b", re.I)


def canonical_engine_type(engine_name: str) -> str:
    """实例名/四段云名 → 引擎类型（codearts-cred0 → codearts；codearts:tpl:p:1 → codearts）。"""
    raw = (engine_name or "").strip().lower()
    if not raw:
        return ""
    head = raw.split(":", 1)[0]
    for key in sorted(ENGINE_BACKEND_COMMANDS, key=len, reverse=True):
        if head == key or head.startswith(key + "-") or head.startswith(key + "_"):
            return key
    return head


def backend_command(engine_name: str) -> str:
    return ENGINE_BACKEND_COMMANDS.get(canonical_engine_type(engine_name), "")


def is_missing_cli_error(text: str) -> bool:
    """错误文本是否明确表达「后端命令/可执行文件不存在」。"""
    s = str(text or "")
    return bool(_MISSING_CLI_RE.search(s) or _COMMAND_NOT_FOUND_RE.search(s))


def _path_usable(path: str) -> str:
    if not path:
        return ""
    try:
        if not os.path.isfile(path):
            return ""
        if os.name != "nt" and not os.access(path, os.X_OK):
            return ""
    except OSError:
        return ""
    return path


def probe_engine_capability(engine_name: str, cli_path: str = "",
                            *, which=None, now: float | None = None,
                            ttl: float | None = None,
                            force: bool = False) -> dict:
    """探测引擎后端命令可用性（带 TTL 缓存，纯路径查找，不起子进程）。"""
    engine = canonical_engine_type(engine_name)
    command = ENGINE_BACKEND_COMMANDS.get(engine, "")
    if not command:
        return {"status": STATUS_UNKNOWN, "engine": engine, "command": "",
                "executable": "",
                "reason": f"engine '{engine_name}' is not registered in backend capability map",
                "checked_at": datetime.now(timezone.utc).isoformat()}

    key = (engine, cli_path or "")
    _ttl = DEFAULT_PROBE_TTL if ttl is None else ttl
    _now = time.time() if now is None else now
    if not force:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and _now - float(cached.get("_cached_at") or 0) < _ttl:
                return {k: v for k, v in cached.items() if k != "_cached_at"}

    resolver = which or shutil.which
    executable = ""
    reason = ""
    try:
        # which 只返回可执行解析结果（已含 PATH/PATHEXT 规则），直接信任；
        # 仅 cli_path（worker 侧 find_cli 绝对路径）再做文件/执行位校验。
        executable = str(resolver(command) or "")
    except Exception as exc:  # noqa: BLE001 - 探针自身故障记 unknown，不摘除
        return {"status": STATUS_UNKNOWN, "engine": engine, "command": command,
                "executable": "",
                "reason": f"probe error: {type(exc).__name__}: {exc}"[:240],
                "checked_at": datetime.now(timezone.utc).isoformat()}
    if not executable and cli_path:
        # worker 侧已由 find_cli 解析出的绝对路径（PATH 之外安装目录）
        executable = _path_usable(cli_path)
    if executable:
        status = STATUS_AVAILABLE
    else:
        status = STATUS_UNAVAILABLE
        reason = f"{command} CLI not found in PATH"

    result = {"status": status, "engine": engine, "command": command,
              "executable": executable, "reason": reason,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    with _CACHE_LOCK:
        _CACHE[key] = {**result, "_cached_at": _now}
    return result


def status_from_probe(engine_name: str, probe_status: str, error: str = "") -> str:
    """把 worker probe_results 映射为后端能力状态（兼容未升级的旧 worker）。"""
    engine = canonical_engine_type(engine_name)
    if engine not in ENGINE_BACKEND_COMMANDS:
        return STATUS_UNKNOWN
    ps = (probe_status or "").strip().lower()
    err = str(error or "")
    if ps == "ok":
        return STATUS_AVAILABLE
    if ps in ("spawn_failed", "model_unreachable"):
        return STATUS_DEGRADED
    if ps in ("fail", "failed"):
        return STATUS_UNAVAILABLE if is_missing_cli_error(err) else STATUS_DEGRADED
    if ps == "skip":
        return STATUS_UNAVAILABLE if is_missing_cli_error(err) else STATUS_UNKNOWN
    return STATUS_UNKNOWN


def clear_cache() -> None:
    """清空探针缓存（测试 / 运行期强制重探）。"""
    with _CACHE_LOCK:
        _CACHE.clear()