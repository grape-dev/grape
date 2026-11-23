"""BwrapProvider + BwrapSandbox——bubblewrap 进程级沙箱。

build_bwrap_argv() 是纯函数，不碰 self，将来能独立成 bwrap/args.py 时零改动。
BwrapSandbox 组合持有 4 个组件（network/limiter/audit/credential）。
__enter__ 启动资源（失败抛异常），wrap() 纯构造（可多次调用），cleanup() 清理组合件。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
from typing import Any, Callable

from ..api import Sandbox, SandboxProvider
from ..types import SandboxState, IsolationLevel, AlertSeverity, ExecutionMode
from ..errors import SandboxUnavailableError
from ..credential import CredentialStrategy, NullCredentialStrategy
from ..isolation.network import NetworkIsolation, NullNetworkIsolation
from ..isolation.resource import ResourceLimiter, NullResourceLimiter, CgroupLimiter
from ..isolation.seccomp import SeccompProgram, NullSeccompProgram
from ..alert import AlertEngine, NullAlertEngine
from src.domains.runtime.agents.common.audit import AuditListener, NullAuditLogger, AuditEvent, AuditKind

log = logging.getLogger("devops-bot")


# 引擎路径解析缓存——进程内 dict，key=binary 名
# 存 tuple（防调用方污染），返回 list 拷贝
_ENGINE_PATH_CACHE: dict[str, tuple[str, ...]] = {}

# 沙箱 HOME 基础路径——登录用户 HOME 下的持久子目录
# 按引擎类型 + task_id 隔离，不会被 /tmp 清理，不会 noexec
_SANDBOX_HOME_BASE = os.path.join(os.path.expanduser("~"), ".cache", "grape-sandbox")

# 向后兼容：旧代码引用 _SANDBOX_HOME 做日志
_SANDBOX_HOME = _SANDBOX_HOME_BASE

__all__ = ["sandbox_home_for", "build_bwrap_argv", "resolve_engine_runtime_paths",
           "hermes_overlay_path", "BwrapSandbox", "BwrapProvider"]


def sandbox_home_for(engine_type: str = "", task_id: str = "") -> str:
    """计算沙箱 HOME 路径：~/.cache/grape-sandbox/<engine_type>/<task_id>/

    持久、可执行、不会被 /tmp 清理。按引擎类型和 task_id 隔离。
    纯函数——agent.py / prepare_task_env / bwrap._build_env 共用。
    """
    parts = [_SANDBOX_HOME_BASE]
    if engine_type:
        parts.append(engine_type)
    if task_id:
        parts.append(task_id)
    return os.path.join(*parts)


def resolve_engine_runtime_paths(binary: str) -> list[str]:
    """解析引擎 CLI 的运行时路径（binary 入口 + 安装前缀）。

    返回需要 ro-bind 的路径列表（去重后）。失败返回空列表并记 warning。

    解析策略：
    1. shutil.which(binary) 找到入口路径（可能是软链）
    2. os.path.realpath() 解析软链到真实路径（多级软链一次解析到最终 target）
    3. 从真实路径向上遍历找安装前缀（含 bin/ + lib/ 的目录），排除根目录 /
       检查 bin/ + lib/ 双条件——npm 包自带 bin/ 但不含 lib/，
       只查 bin/ 会误停在包目录层；加 lib/ 确保命中 node 安装根
       （单二进制场景无 lib/ → 走 fallback 挂 real 本身）
    4. 入口路径和安装前缀都加入返回列表
    5. 未找到前缀时 fallback：挂 real 本身（单文件），保证沙箱内 binary 可见

    覆盖场景：
    - npm -g：/usr/local/bin/opencode → /usr/local/lib/node_modules/.../bin/opencode
      前缀 /usr/local/lib/node_modules（已被 BWRAP_RO_ENGINE_COMMON 覆盖，去重跳过）
    - nvm：/root/.nvm/versions/node/vX/bin/opencode → 前缀 /root/.nvm/versions/node/vX
    - 手动解压 node：/root/node-v24.18.0-linux-x64/bin/opencode → 前缀 /root/node-v24.18.0-linux-x64
    - 软链入口：/usr/local/bin/opencode → /root/node-v24.../bin/opencode
      入口 /usr/local/bin/opencode（单文件）+ 前缀 /root/node-v24...
    - 单二进制（Go/Rust）：/usr/local/bin/codex 是 ELF，realpath 还是它自己
      入口 /usr/local/bin/codex（单文件），无前缀（向上无 bin/ 结构）→ fallback 挂 real
    - 深层安装（前缀在 5 层以外）：向上遍历 5 层未命中 → fallback 挂 real 本身
    - 多级软链：/usr/local/bin/opencode → /opt/link/opencode → /root/node.../bin/opencode
      realpath 一次解析到最终 target，逻辑不变
    - dangling symlink：/usr/local/bin/opencode → /nonexistent
      os.path.exists 返回 False → 跳过，返回空列表
    - 找不到：which 返回 None → 空列表 + warning（不缓存，允许后续重试）
    """
    # 缓存命中——返回 list 拷贝（防调用方污染缓存）
    if binary in _ENGINE_PATH_CACHE:
        return list(_ENGINE_PATH_CACHE[binary])

    paths: list[str] = []

    # 1. 找入口路径
    entry = shutil.which(binary)
    if not entry:
        log.warning(f"resolve_engine_runtime_paths: binary '{binary}' not found in PATH")
        # 不缓存负结果——允许后续重试（长驻 worker 进程中引擎可能动态安装）
        return []

    # 2. 解析软链（多级软链 realpath 一次解析到最终 target）
    real = os.path.realpath(entry)

    # 3. 入口路径加入列表（软链入口本身需要挂，否则沙箱内 PATH 找不到命令）
    paths.append(entry)

    # 4. 如果是软链（entry != real），解析安装前缀
    found_prefix = False
    if entry != real:
        # 向上遍历找安装前缀（含 bin/ 子目录的目录）
        cur = os.path.dirname(real)
        for _ in range(8):  # 最多向上 8 层（npm 包嵌套 node_modules 可能较深）
            if cur == "/" or cur == os.path.dirname(cur):
                # 到根就停——不挂整个文件系统
                break
            bin_path = os.path.join(cur, "bin")
            lib_path = os.path.join(cur, "lib")
            if os.path.isdir(bin_path) and os.path.isdir(lib_path):
                # 找到安装前缀（bin/ + lib/ 双条件，npm 包自带 bin/ 但无 lib/ 不误命中）
                paths.append(cur)
                found_prefix = True
                break
            cur = os.path.dirname(cur)

        # fallback：未找到含 bin/ 的前缀（到根 / 5 层跑满），挂 real 本身
        # 保证沙箱内 binary 至少可见（单文件 ro-bind）
        if not found_prefix:
            paths.append(real)

    # 5. 去重 + 存在性检查 + 安全验证
    seen = set()
    result = []
    for p in paths:
        if p in seen:
            continue
        # 安全验证：必须是绝对路径 + 长度 > 1 + 不以 // 开头（防相对路径 / 根目录 / POSIX // 特殊语义）
        if not os.path.isabs(p) or len(p) <= 1 or p.startswith("//"):
            log.warning(f"resolve_engine_runtime_paths: unsafe path '{p}', skipping")
            continue
        if os.path.exists(p):
            seen.add(p)
            result.append(p)
        else:
            log.warning(f"resolve_engine_runtime_paths: path '{p}' does not exist, skipping")

    # 成功路径日志（验证用）
    log.info(f"resolve_engine_runtime_paths: binary={binary!r} entry={entry} real={real} "
             f"found_prefix={found_prefix} paths={result}")

    # 缓存存 tuple（防调用方污染）
    _ENGINE_PATH_CACHE[binary] = tuple(result)
    return result


def hermes_overlay_path(cwd: str) -> str:
    """计算 hermes ACP overlay home 路径——bwrap bind mount 和 hermes_acp 共用。

    Issue #238: 从 /tmp 迁移到 ~/.cache/hermes-acp-homes（/tmp 是 bwrap tmpfs）。
    此函数是路径的唯一真相源，bwrap.py build_bwrap_argv 和 hermes_acp.py 都调它，
    避免两处硬编码不同步导致沙箱内找不到 config.yaml。
    """
    import hashlib
    _h = hashlib.md5((cwd or ".").encode()).hexdigest()[:12]
    return os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-homes", _h)


def build_bwrap_argv(
    cwd: str,
    exec_backend: str,
    network: NetworkIsolation,
    safe_env: dict[str, str],
    seccomp_fd: int = -1,
    proc_env: dict[str, str] | None = None,
    engine_type: str = "",
    task_id: str = "",
) -> list[str] | None:
    """纯函数：构造 bwrap 命令选项（不含末尾 "--"）。

    不碰 self——将来能独立成 bwrap/args.py 时零改动。
    engine_type 优先用于查 backend 配置（ro_paths/rw_paths/overlay），
    exec_backend 仅用于 API 查询和日志。
    """
    bwrap_bin = None
    from src.domains.runtime.agents.sandbox.detect import _BWRAP_CANDIDATES
    for candidate in _BWRAP_CANDIDATES:
        if os.path.basename(candidate) == candidate:
            bwrap_bin = shutil.which(candidate)
        elif os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            bwrap_bin = candidate
        if bwrap_bin:
            break
    if not bwrap_bin:
        return None
    if not cwd or not os.path.isdir(cwd):
        return None

    # bwrap 隔离：参考 koda-sandbox 方案
    # --unshare-net：内核级网络隔离，Agent 无法直连网络
    # UDS 桥接：bind-mount UDS 文件 + stage2 TCP→UDS 本地 bridge
    # seccomp 去掉——--unshare-net 已提供内核级网络隔离底线，stage2 需要 AF_INET
    opts = [bwrap_bin, "--die-with-parent",
            "--unshare-user", "--unshare-uts",
            "--unshare-net",
            "--dev", "/dev", "--proc", "/proc"]

    # 网络隔离：--unshare-net 模式（参考 koda-sandbox）
    # Agent 在独立网络命名空间，内核层面无路由
    # UDS 文件 bind-mount 到沙箱内，stage2 TCP→UDS 转发到宿主机 NetworkProxy

    # env：bwrap 0.4.1 不支持 --clearenv，用 --unsetenv 逐个清 + --setenv 白名单
    # 从 proc_env 和 os.environ 合并生成 unsetenv 列表（防止 proc_env 变量泄漏）
    # safe_env 中的 key 也要先 unset 再 set（bwrap 0.4.1 --setenv 不覆盖已有值）
    _all_env_keys = set(os.environ.keys())
    if proc_env:
        _all_env_keys |= set(proc_env.keys())
    # 逐个清除所有变量（包括白名单变量——先 unset 再 set）
    for k in _all_env_keys:
        opts += ["--unsetenv", k]
    # 白名单变量
    # 注意：bwrap 0.4.1 --setenv 不覆盖 Popen env 已有的值
    # 从 proc_env 删掉 safe_env 的 key，让 bwrap --setenv 成为唯一来源
    if proc_env is not None:
        for k in safe_env:
            proc_env.pop(k, None)
    for k, v in safe_env.items():
        opts += ["--setenv", k, v]

    # seccomp fd
    if seccomp_fd >= 0:
        opts += ["--seccomp", str(seccomp_fd)]

    # UDS 桥接：见下方 --tmpfs /tmp 之后的 bind-mount
    # bwrap 0.4.1 不支持 --bind 直接挂载 socket 文件（报 No such device or address）
    # 改为 bind-mount socket 所在目录——但必须在 --tmpfs /tmp 之后，否则会被覆盖
    uds_path = getattr(network, '_socket_path', None)

    # 注意：stage2 脚本在 cwd --bind 之后挂载（cwd 在 L174 才 --bind）

    # 基础系统 RO
    from src.kernel.config import BWRAP_RO_SYSTEM, BWRAP_RO_ENGINE_COMMON
    for p in BWRAP_RO_SYSTEM:
        if p and os.path.isdir(p):
            opts += ["--ro-bind", p, p]

    # 引擎白名单——用 engine_type 查 backend（不靠实例名）
    from src.domains.runtime.agents.executor import get_backend_or_none
    backend = get_backend_or_none(engine_type) if engine_type else None
    ro_paths = [os.path.expanduser(p) for p in getattr(backend, "sandbox_ro_paths", ())]
    rw_paths = [os.path.expanduser(p) for p in getattr(backend, "sandbox_rw_paths", ())]

    # 自动解析引擎运行时路径（软链 → 真实安装前缀）
    # 解决：/usr/local/bin/opencode → /root/node-v24.../bin/opencode 软链目标不在白名单
    _engine_binary = getattr(backend, "binary", "") or exec_backend
    if _engine_binary:
        _auto_ro = resolve_engine_runtime_paths(_engine_binary)
        ro_paths.extend(_auto_ro)

    # codearts enc:v3 config 覆盖：用解密版本 --ro-bind 覆盖原 config 路径
    # 必须在引擎 RO 挂载之后——否则被 --ro-bind ~/.codeartsdoer 覆盖
    # 原文件不改——非沙箱模式 codearts 自己解密 enc:v3
    # （已删除——codearts 自己能解密 enc:v3，不需要 grape 辅助）

    # DB 追加路径——通过 API 获取（远程 worker 无 DB 连接）
    try:
        server_url = os.environ.get("GRAPE_SERVER_URL", "") or os.environ.get("RUNTIME_SERVER_URL", "")
        token = os.environ.get("RUNTIME_TOKEN", "")
        if server_url and token:
            import requests as _requests
            resp = _requests.get(
                f"{server_url}/api/runtime/sandbox-paths",
                params={"backend_name": engine_type or exec_backend},
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if resp.status_code == 200:
                for item in resp.json().get("paths", []):
                    expanded = os.path.expanduser(item.get("path", ""))
                    if item.get("access") == "ro":
                        ro_paths.append(expanded)
                    else:
                        rw_paths.append(expanded)
        else:
            raise Exception("no server_url/token, fallback to DB")
    except Exception:
        # API 不可用时 fallback 到本地 DB（仅本机 worker 有 DB）
        try:
            # 表归 mgmt：SQL 在 mgmt public 一份
            from src.domains.mgmt import public as _mg_paths
            rows = _mg_paths.engine_sandbox_paths_of(exec_backend)
            for path, access in rows:
                expanded = os.path.expanduser(path)
                if access == 'ro':
                    ro_paths.append(expanded)
                else:
                    rw_paths.append(expanded)
        except Exception:
            pass

    # 引擎通用 RO + 引擎专用 RO
    _ro = [os.path.expanduser(x) for x in BWRAP_RO_ENGINE_COMMON] + ro_paths
    # 沙箱内 HOME：从 safe_env 读实际值（per-task 持久路径）
    # 引擎通过 HOME 找 config（如 ~/.config/opencode），HOME 前缀路径映射到沙箱 HOME
    # 但 --tmpfs /tmp 会覆盖 /tmp 下的所有 bind，所以 HOME 前缀路径必须在 --tmpfs /tmp 之后 bind
    _sandbox_home_path = safe_env.get("HOME", "") or _SANDBOX_HOME
    _real_home = os.path.expanduser("~")
    _post_tmpfs_binds: list[tuple[str, str, str]] = []  # (源, 目标, mode) 在 --tmpfs /tmp 之后执行
    # 先处理目录——记录已挂载的目录前缀
    _seen = set()
    _mounted_dirs: list[str] = []
    for p in _ro:
        if not p or p in _seen:
            continue
        if os.path.isdir(p):
            _seen.add(p)
            _mounted_dirs.append(p)
            _target = p
            if p.startswith(_real_home + "/"):
                # 目录 bind 也不做 HOME 前缀映射——直接 bind 到宿主机路径
                # 原因：hermes 内部硬编码 /root/.hermes/tools/...，不走 $HOME
                # opencode/codearts 的 config 也通过绝对路径可见（/root/.config/opencode）
                # HOME 前缀映射（$HOME/.config → sandbox_home/.config）在 _post_tmpfs_binds 中单独处理
                opts += ["--ro-bind", p, p]
                continue
            opts += ["--ro-bind", p, _target]
        elif os.path.isfile(p):
            _covered = False
            for d in _mounted_dirs:
                if p.startswith(d + "/"):
                    _covered = True
                    break
            if _covered:
                continue
            _seen.add(p)
            # 单文件 bind 不做 HOME 前缀映射——直接 bind 到宿主机路径
            # 原因：单文件（如 binary）通过 PATH 或硬编码路径引用，不走 $HOME
            # 父目录在 _post_tmpfs_binds 处理时逐层 --dir 创建
            _post_tmpfs_binds.append((p, p, "ro"))
        # 不存在的路径跳过（hermes 未安装等）

    # DEBUG: 打印完整 bwrap 命令（临时调试用，正式环境可删）
    # 日志在 RW 处理后打印——见下方

    # 引擎缓存 RW
    # RW 路径中的 ~ 展开为宿主机路径，但沙箱内 HOME 是 _SANDBOX_HOME（非真实 HOME）
    # 并发隔离：HOME 前缀的 RW 路径不 bind 宿主机源目录（多个任务共享同一个源目录会冲突）
    # 改为在 tmpfs 上创建空目录——每个任务有独立的 RW 数据，互不影响
    # 例外：引擎通过 GRAPE_TASK_RW_DIRS 声明 per-task RW 目录（源路径=沙箱内目标路径）
    # 这些目录由引擎 prepare_task_env 创建，包含 per-task 初始数据，bind 到沙箱内 HOME 路径
    # 非 HOME 前缀的 RW 路径（如 /var/cache/bun-compile）仍 bind 宿主机目录（全局共享缓存，无冲突风险）
    _task_rw_dirs = {}  # {沙箱内目标路径: (宿主机源路径, mode)}
    _task_rw_raw = (proc_env or {}).get("GRAPE_TASK_RW_DIRS", "")
    if _task_rw_raw:
        import json as _json
        try:
            for pair in _json.loads(_task_rw_raw):
                _mode = pair.get("mode", "rw")  # 默认 rw（向后兼容）
                _task_rw_dirs[pair["target"]] = (pair["source"], _mode)
            log.info(f"GRAPE_TASK_RW_DIRS: {_task_rw_dirs}")
        except Exception as _e:
            log.warning(f"GRAPE_TASK_RW_DIRS parse failed: {_e}")
    for p in rw_paths:
        if p and os.path.isdir(p):
            if p.startswith(_real_home + "/"):
                # RW 路径也不做 HOME 前缀映射——直接 bind 到宿主机路径
                # 原因：hermes 内部硬编码安装时的 HOME 路径，不走 $HOME
                # 并发隔离由 per-task 沙箱 HOME 处理，共享 RW 路径接受并发风险
                # 父目录可能不存在，放到 _post_tmpfs_binds 里逐层 --dir 后再 bind
                _post_tmpfs_binds.append((p, p, "rw"))
                continue
            opts += ["--bind", p, p]

    # GRAPE_TASK_RW_DIRS 中未被 rw_paths 覆盖的 target——子路径级别的 per-task bind
    # 例如 codearts 声明 ~/.codeartsdoer 为 RW（rw_paths 处理时走 --dir 或 shared bind），
    # 但 GRAPE_TASK_RW_DIRS 额外映射 ~/.codeartsdoer/cli-data 到 per-task 目录，
    # 需要额外 bind 这个子路径，否则 codearts 在空目录里找不到数据
    _handled_targets = set()
    for p in rw_paths:
        if p and p.startswith(_real_home + "/"):
            # rw_paths 中 HOME 前缀路径 bind target 是宿主机路径自身（L374-379），
            # 不是沙箱 HOME 路径。用实际 bind target 防止 GRAPE_TASK_RW_DIRS
            # 的 per-task bind 被误跳。
            _handled_targets.add(p)
    for target, (source, mode) in _task_rw_dirs.items():
        if target not in _handled_targets:
            _post_tmpfs_binds.append((source, target, mode))
            log.info(f"Task bind (sub-path): {source} -> {target} ({mode})")

    # 沙箱内 HOME 目录——per-task 持久路径，RW bind
    # 统一所有引擎：~/.cache/grape-sandbox/<engine_type>/<task_id>/
    # 必须在 _post_tmpfs_binds 之前 bind——GRAPE_TASK_RW_DIRS 的 target 在 HOME 下，
    # 需要先 --bind HOME 使其可写，否则 --dir 创建子目录时 HOME 父路径是 RO（/root/.cache 被 --ro-bind）
    _home = sandbox_home_for(engine_type, task_id)
    os.makedirs(_home, exist_ok=True)
    opts += ["--bind", _home, _home]

    # /tmp tmpfs + 引擎 overlay（hermes ACP 需要持久化 home）
    opts += ["--tmpfs", "/tmp"]
    # HOME 前缀的 RO/RW 路径在 --tmpfs /tmp 之后 bind——否则会被 tmpfs 覆盖
    for src, target, mode in _post_tmpfs_binds:
        if mode == "ro":
            # bwrap --ro-bind 单文件要求父目录在沙箱里已存在
            # 逐层 --dir 创建缺失的父目录（--dir 不递归创建）
            _parent = os.path.dirname(target)
            _dirs_to_create = []
            _p = _parent
            while _p and _p != "/" and _p not in _dirs_to_create:
                _dirs_to_create.append(_p)
                _p = os.path.dirname(_p)
            for _d in reversed(_dirs_to_create):
                opts += ["--dir", _d]
            opts += ["--ro-bind", src, target]
        elif mode == "dir":
            opts += ["--dir", target]
        else:
            # RW bind 也需要先 --dir 父目录
            _parent = os.path.dirname(target)
            _dirs_to_create = []
            _p = _parent
            while _p and _p != "/" and _p not in _dirs_to_create:
                _dirs_to_create.append(_p)
                _p = os.path.dirname(_p)
            for _d in reversed(_dirs_to_create):
                opts += ["--dir", _d]
            opts += ["--bind", src, target]
    # UDS 目录 bind-mount——必须在 --tmpfs /tmp 之后
    # bwrap 0.4.1 不支持 --bind socket 文件，改为 bind 目录
    # 放在 --tmpfs /tmp 之后避免被 tmpfs 覆盖（UDS 目录在 HOME 下，不在 /tmp 下）
    if uds_path and os.path.exists(uds_path):
        _uds_dir = os.path.dirname(uds_path)
        if _uds_dir and os.path.isdir(_uds_dir):
            opts += ["--bind", _uds_dir, _uds_dir]

    # DEBUG: 打印完整 binds（临时调试用，正式环境可删）
    _all_binds = []
    for i in range(len(opts)-1):
        if opts[i] in ('--bind','--ro-bind','--dir','--tmpfs') and i+1 < len(opts):
            _all_binds.append(f"{opts[i]} {opts[i+1]}")
    log.info(f"BWRAP all binds: {_all_binds}")

    # /proc /dev 已在初始 opts 中设置，不重复

    # .git 危险路径挂载屏蔽
    _add_git_shields(opts, cwd)

    # codebase worktree
    _issues_idx = cwd.find("/issues/")
    if _issues_idx > 0:
        _codebase = os.path.join(cwd[:_issues_idx], "codebase")
        if os.path.isdir(_codebase):
            opts += ["--bind", _codebase, _codebase]

    # 工位 cwd
    opts += ["--bind", cwd, cwd, "--chdir", cwd]

    # stage2 TCP→UDS 桥接脚本——在 cwd --bind 之后挂载（cwd 可写）
    stage2_src = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stage2.py"))
    if os.path.exists(stage2_src):
        stage2_mount = os.path.join(cwd, "_grape_stage2.py")
        opts += ["--ro-bind", stage2_src, stage2_mount]

    return opts


def _add_git_shields(opts: list[str], cwd: str):
    """挂载 /dev/null 屏蔽 .git 危险路径。"""
    git_dir = _resolve_git_dir(cwd)
    if git_dir and os.path.isdir(git_dir):
        for sub in ("hooks", "config"):
            path = os.path.join(git_dir, sub)
            if os.path.exists(path):
                opts += ["--ro-bind", "/dev/null", path]
    gitmodules = os.path.join(cwd, ".gitmodules")
    if os.path.exists(gitmodules):
        opts += ["--ro-bind", "/dev/null", gitmodules]

    _issues_idx = cwd.find("/issues/")
    if _issues_idx > 0:
        _codebase = os.path.join(cwd[:_issues_idx], "codebase")
        if os.path.isdir(_codebase):
            cb_git = os.path.join(_codebase, ".git")
            if os.path.isdir(cb_git):
                for sub in ("hooks", "config"):
                    path = os.path.join(cb_git, sub)
                    if os.path.exists(path):
                        opts += ["--ro-bind", "/dev/null", path]


def _resolve_git_dir(cwd: str) -> str | None:
    """解析 cwd 下的真实 .git 目录。"""
    git_path = os.path.join(cwd, ".git")
    if not os.path.exists(git_path):
        return None
    if os.path.isdir(git_path):
        return git_path
    try:
        with open(git_path) as f:
            line = f.read().strip()
        if line.startswith("gitdir: "):
            real = line[8:]
            if not os.path.isabs(real):
                real = os.path.join(cwd, real)
            return os.path.normpath(real)
    except Exception:
        pass
    return None


class BwrapSandbox:
    """bubblewrap 沙箱实例。

    状态机：
    - __enter__：PREPARED → 启动组合件（network.start / limiter.prepare），失败抛异常
    - wrap：纯命令构造，可多次调用（缓存 opts/env，每次重新组装 cmd）
    - cleanup：清理组合件，不关外部依赖（audit/credential）
    """

    execution_mode = ExecutionMode.LOCAL_BWRAP

    def __init__(self, cwd, exec_backend, task_id="", runtime_id=None,
                 network: NetworkIsolation = NullNetworkIsolation(),
                 limiter: ResourceLimiter = NullResourceLimiter(),
                 audit: AuditListener = NullAuditLogger(),
                 credential: CredentialStrategy = NullCredentialStrategy(),
                 seccomp: SeccompProgram = NullSeccompProgram(),
                 alert_engine: AlertEngine | NullAlertEngine | None = None,
                 engine_type: str = ""):
        self._network = network
        self._limiter = limiter
        self._audit = audit
        self._credential = credential
        self._seccomp = seccomp
        self._alert_engine = alert_engine
        self._cwd = cwd
        self._exec_backend = exec_backend
        self._engine_type = engine_type
        self._task_id = task_id
        self._runtime_id = runtime_id
        self._state = SandboxState.PREPARED
        self._cleanup_paths: list[str] = []
        self._opts_cache: list[str] | None = None

    @property
    def state(self) -> SandboxState:
        return self._state

    def __enter__(self) -> "BwrapSandbox":
        if self._state == SandboxState.CLEANED:
            raise RuntimeError("Sandbox already cleaned")
        try:
            self._network.start(self._task_id)
        except Exception as e:
            self._network.stop()
            raise SandboxUnavailableError(f"network isolation failed: {e}") from e
        try:
            self._limiter.prepare()
        except Exception as e:
            self._network.stop()
            raise SandboxUnavailableError(f"resource limiter failed: {e}") from e
        self._state = SandboxState.ACTIVE
        return self

    def prepare_env(self, env: dict[str, str], engine_backend: "Any", resource_stack: "Any" = None) -> None:
        """bwrap 隔离：注入 per-task HOME + 调 engine prepare_task_env。

        在 __enter__ 之后、wrap 之前调用一次。
        NullSandbox 的 prepare_env 是空操作——非 bwrap 模式 HOME 保持宿主机路径。

        根因：v5 把 HOME 注入放在 agent.py 无条件执行，非 bwrap 模式下 codearts installer
        用 per-task HOME 拼接 KERNEL_DATA_DIR → 空目录 → database migration → text_len=0。
        v6 把 HOME 注入移到 BwrapSandbox.prepare_env，NullSandbox.prepare_env 空操作。
        设计文档：docs/weekly-05/bwrap-sandbox-abstraction-v6.md
        """
        if self._state == SandboxState.CLEANED:
            raise RuntimeError("Sandbox already cleaned")
        _home = sandbox_home_for(self._engine_type, self._task_id)
        os.makedirs(_home, exist_ok=True)
        env["HOME"] = _home
        if engine_backend and hasattr(engine_backend, "prepare_task_env"):
            try:
                engine_backend.prepare_task_env(self._task_id, env, resource_stack)
            except Exception as e:
                log.warning(f"engine prepare_task_env failed: {e}")

    def wrap(self, cmd: list[str], proc_env: dict[str, str] | None = None) -> list[str]:
        if self._state == SandboxState.CLEANED:
            raise RuntimeError("Sandbox already cleaned")

        # seccomp fd 幂等 open
        seccomp_fd = self._seccomp.open()

        # 缓存不依赖 cmd 的中间产物（proc_env 第一次传入后缓存）
        if self._opts_cache is None:
            self._opts_cache = build_bwrap_argv(
                self._cwd, self._exec_backend, self._network,
                self._build_env(proc_env), seccomp_fd, proc_env,
                engine_type=self._engine_type, task_id=self._task_id)

        # BUG-1 修复：build_bwrap_argv 返回 None 说明 bwrap 不可用（二进制缺失或 cwd 非法）。
        # 不能静默裸执行——审计会记 "allow" 但实际无沙箱包装，安全控制失效且审计误导。
        # fail-closed：raise SandboxUnavailableError，让 manager 的 PREFERRED 降级或 REQUIRED 拒绝。
        if self._opts_cache is None:
            self._audit.log(AuditEvent(
                ts=self._now(), task_id=self._task_id,
                kind=AuditKind.SANDBOX_ACCESS, outcome="deny",
                detail={"provider": "bwrap", "reason": "build_bwrap_argv returned None (cwd invalid or bwrap binary missing)"}))
            raise SandboxUnavailableError(
                f"bwrap 不可用：cwd='{self._cwd}' 非法或 bwrap 二进制缺失，拒绝裸执行")

        # 每次重新组装
        final_cmd = self._limiter.wrap_self_attach(cmd)
        if self._opts_cache:
            # --unshare-net 模式：沙箱内需要 stage2 TCP→UDS 桥接
            # 参考 koda-sandbox：沙箱内本地 bridge 把 TCP 转发到 UDS
            # bash wrapper：先启动 stage2，再 exec Agent
            import shlex
            uds_path = getattr(self._network, '_socket_path', '')
            if uds_path and os.path.exists(uds_path):
                # stage2：监听 127.0.0.1:8888（loopback），转发到 UDS
                # --unshare-net 下 loopback 可用（lo 接口自动创建），但无外部路由
                # stage2 stderr 写到 cwd（沙箱内可写），供调试
                _stage2_log = os.path.join(self._cwd, f'_grape_stage2_{self._task_id or "default"}.log')
                stage2_script = (
                    f"python3 {shlex.quote(os.path.join(self._cwd, '_grape_stage2.py'))} 8888 {shlex.quote(uds_path)} >/dev/null 2>{shlex.quote(_stage2_log)} & "
                    f"sleep 1; "
                    # exec Agent（shlex.quote 转义防注入）
                    f"exec {' '.join(shlex.quote(c) for c in final_cmd)}"
                )
                final_cmd = self._opts_cache + ["--", "bash", "-c", stage2_script]
            else:
                final_cmd = self._opts_cache + ["--"] + final_cmd

        self._audit.log(AuditEvent(
            ts=self._now(), task_id=self._task_id,
            kind=AuditKind.SANDBOX_ACCESS, outcome="allow",
            detail={"provider": "bwrap", "exec_backend": self._exec_backend}))

        return final_cmd

    def popen_kwargs(self) -> dict:
        """返回 Popen 需要的额外参数（pass_fds）。"""
        fd = self._seccomp.open()
        if fd >= 0:
            return {"pass_fds": (fd,)}
        return {}

    def cleanup(self) -> None:
        if self._state == SandboxState.CLEANED:
            return
        # 清理 per-task 沙箱 HOME 目录
        _home = sandbox_home_for(self._engine_type, self._task_id)
        if _home and os.path.isdir(_home):
            import shutil as _shutil
            try:
                _shutil.rmtree(_home, ignore_errors=True)
                log.info(f"cleaned sandbox home: {_home}")
            except Exception as _e:
                log.warning(f"failed to clean sandbox home {_home}: {_e}")
        self._network.stop()
        self._limiter.release()
        self._audit.log(AuditEvent(
            ts=self._now(), task_id=self._task_id,
            kind=AuditKind.SANDBOX_ACCESS, outcome="stopped",
            detail={"action": "cleanup"}))

        # P1-5/P1-9: 任务结束后扫描审计序列
        # 异常隔离——告警扫描失败不影响资源清理和 state 转换
        if self._alert_engine is not None:
            try:
                # flush 异步写入队列，确保所有审计记录落盘后再读取
                if hasattr(self._audit, "flush"):
                    try:
                        self._audit.flush()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                events = self._get_task_events()
                if events:
                    alerts = self._alert_engine.analyze(self._task_id, events)
                    for alert in alerts:
                        self._audit.log(AuditEvent(
                            ts=self._now(), task_id=self._task_id,
                            kind=AuditKind.SECURITY_ALERT,
                            outcome=alert.severity.value,
                            detail={
                                "rule": alert.rule,
                                "description": alert.description,
                                "fingerprint": alert.fingerprint,
                                "related_events": len(alert.events),
                            }))
                        if alert.severity == AlertSeverity.CRITICAL:
                            log.error(
                                f"SECURITY ALERT [CRITICAL] task={self._task_id} "
                                f"rule={alert.rule} desc={alert.description}")
            except Exception as e:
                log.error(f"alert engine scan failed (suppressed): {e}")

        for path in self._cleanup_paths:
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
        self._state = SandboxState.CLEANED

    def is_active(self) -> bool:
        return self._state == SandboxState.ACTIVE

    def kill(self) -> bool:
        """空操作——bwrap 进程随子进程退出，无需显式 kill。"""
        return True

    def __exit__(self, *exc) -> None:
        self.cleanup()

    def _build_env(self, proc_env: dict[str, str] | None = None) -> dict[str, str]:
        """构建安全 env：白名单变量 + 代理 env + 凭据保护 + 引擎必需变量。

        proc_env 是 executor.py 传给 Popen 的完整 env。
        --clearenv 后只透传安全变量，引擎必需变量从 proc_env 中补充。
        """
        env = {}
        # v4: HOME 由 agent.py _execute_task 注入 env（per-task 沙箱路径），bwrap 只读不写
        # proc_env 中的 HOME 已是 per-task 路径（agent.py → env → stream_agent → _proc_env）
        env["HOME"] = (proc_env or {}).get("HOME", "") or os.environ.get("HOME", "/")
        # 基础系统变量
        for k in ("PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING"):
            if k in os.environ:
                env[k] = os.environ[k]
        # 代理 env（NetworkProxy）
        env.update(self._network.env())
        # 禁用 OpenTelemetry 遥测导出——沙箱内通过代理发 OTLP 请求到 collector
        # 每次 13-20 秒（collector 503 + 代理转发开销），严重拖慢 codearts
        # 设 none 让 SDK 不初始化导出器，跳过所有 OTLP 请求
        env["OTEL_TRACES_EXPORTER"] = "none"
        env["OTEL_LOGS_EXPORTER"] = "none"
        env["OTEL_METRICS_EXPORTER"] = "none"
        # 系统注入变量透传——默认全部透传，只排除黑名单
        # proc_env 由 _assemble_env + env_extra 构建，全部是 grape 系统注入的变量
        # 用黑名单而非白名单：新增系统变量自动透传，无需改代码
        if proc_env:
            # 黑名单：不应透传到沙箱的变量
            _BLOCKED_VARS = {
                "GIT_SSH_COMMAND",  # 由 network.env() 设置 /bin/false
                # HOME 由 agent.py 注入 per-task 沙箱路径，proc_env 里的 HOME 是 per-task 路径
                # bwrap._build_env 从 proc_env 读 HOME，设到 safe_env 供 bwrap --setenv
                "HOME",
                # GRAPE_TASK_RW_DIRS 是 bwrap bind 配置，不是引擎环境变量。
                # 如果不 block，_build_env 会把它复制到 safe_env，
                # 然后 build_bwrap_argv L223-225 把它从 proc_env 删掉，
                # L362 从 proc_env 读到空 → per-task bind 不执行。
                "GRAPE_TASK_RW_DIRS",
            }
            for k, v in proc_env.items():
                if k not in _BLOCKED_VARS:
                    env[k] = v
        # 凭据保护
        env = self._credential.protect(env, self._get_credentials())
        # secret suffix deny——双保险，防止遗漏
        for k in list(env.keys()):
            if k.endswith(("_TOKEN", "_SECRET", "_KEY", "_PASSWORD", "_CREDENTIALS")):
                # 引擎白名单中的 token 保留（P0-3 凭据保护策略已处理）
                # 这里只剥非白名单的
                if k not in env:  # 已被 protect 移除
                    continue
                # 外部 API key 不进沙箱 env——通过引擎配置文件传入（--ro-bind 只读）
                # 只保留 grape 内部 token（不是外部 API key）
                _SAFE_TOKENS = {"GRAPE_API_KEY", "GRAPE_SESSION_TOKEN",
                                "GRAPE_PROXY_TOKEN",
                                # GitCode token——agent 需要 git push / API 操作
                                "BOT_GITCODE_TOKEN", "USER_GITCODE_TOKEN",
                                # hermes LLM Key——config.yaml 用 ${ENV_VAR} 引用，必须保留
                                "HERMES_API_KEY",
                                "HERMES_CUSTOM_OPENGW_CLOUDDEVELOPER_CLUB_API_KEY",
                                # opencode LLM Key——config.json 用 {env:VAR} 引用
                                "OPENCODE_API_KEY",
                                # codearts LLM Key——env 字段从环境变量读取
                                "CODEARTS_API_KEY",
                                # codearts AK/SK——dummy 值绕过检查，走自定义 provider
                                "CODEARTS_CLI_AK", "CODEARTS_CLI_SK",
                                # 华为云 AK/SK——agent 需要 OBS 操作
                                "HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK"}
                # 平台派生 token 键（BOT_GITEE_TOKEN / BOT_GITLAB_TOKEN …）同样放行：
                # 白名单无法枚举未来平台，命名约定 BOT_/USER_ + _TOKEN 才是判据。
                _is_platform_token = (
                    (k.startswith("BOT_") or k.startswith("USER_")) and k.endswith("_TOKEN"))
                if k not in _SAFE_TOKENS and not _is_platform_token:
                    env.pop(k, None)
        return env

    def _get_credentials(self) -> dict:
        """从 DB / settings 获取凭据名列表（不含值——NullCredentialStrategy 只需要 key 名）。"""
        return {}  # P0 不注入凭据

    def _get_task_events(self) -> list[dict]:
        """从审计日志加载当前 task 的全部审计记录。

        只有 FileAuditLogger 实现了 get_events_by_task，
        NullAuditLogger 返回空列表。
        """
        if hasattr(self._audit, "get_events_by_task"):
            return self._audit.get_events_by_task(self._task_id)  # type: ignore[attr-defined]
        return []

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


class BwrapProvider:
    """bubblewrap 沙箱 Provider——抽象工厂。

    Provider 全局可复用（无状态），每次 create_sandbox 创建独立的 BwrapSandbox。
    audit/credential/seccomp/alert_engine 由 manager 通过 registry factory 注入。
    """

    name = "bwrap"

    def __init__(self, audit: AuditListener = NullAuditLogger(),
                 credential: CredentialStrategy = NullCredentialStrategy(),
                 seccomp: SeccompProgram = NullSeccompProgram(),
                 alert_engine=None):
        self._audit = audit
        self._credential = credential
        self._seccomp = seccomp
        self._alert_engine = alert_engine

    def is_available(self) -> bool:
        from src.domains.runtime.agents.sandbox.detect import has_bwrap_binary
        return has_bwrap_binary()

    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.PROCESS

    def create_sandbox(
        self, cwd: str, exec_backend: str, task_id: str = "",
        runtime_id: int | None = None, **kwargs,
    ) -> BwrapSandbox:
        # P0-1: 激活 NetworkProxy（白名单代理 + Unix socket 桥接）
        from ..isolation.network import NetworkProxy
        from src.domains.runtime.agents.executor import get_backend_or_none
        engine_type = kwargs.get("engine_type", "")
        whitelist = self._get_whitelist(exec_backend, engine_type=engine_type)
        # 从引擎 backend 读 local_gateway（codex 声明 127.0.0.1:40001）
        _backend = get_backend_or_none(engine_type) if engine_type else None
        _gw = getattr(_backend, "local_gateway", None) if _backend else None
        # skill egress 策略——经 PolicyEngine 审批（Layer 2），不直读 frontmatter
        # feature flag SANDBOX_EGRESS_ENABLED 默认关闭，关闭时返回 None
        skill_name = kwargs.get("skill_name", "")
        skill_id = kwargs.get("skill_id", 0)
        egress_policy = None
        rate_limiter = None
        if skill_name:
            from ..isolation.policy import load_skill_egress
            egress_policy = load_skill_egress(skill_name, skill_id, cwd=cwd)
            # 有 egress 策略时创建速率限制器
            if egress_policy:
                from ..isolation.rate_limiter import RateLimiter
                rate_limiter = RateLimiter(task_id)
        network = NetworkProxy(whitelist=whitelist, audit_log=self._audit,
                               local_gateway=_gw, egress_policy=egress_policy,
                               rate_limiter=rate_limiter)
        return BwrapSandbox(
            cwd=cwd, exec_backend=exec_backend, task_id=task_id,
            runtime_id=runtime_id,
            network=network,
            limiter=NullResourceLimiter(),  # cgroup v1 非 root 下不稳定，暂时禁用
            audit=self._audit,
            credential=self._credential,
            seccomp=self._seccomp,
            alert_engine=self._alert_engine,
            engine_type=engine_type,
        )

    @staticmethod
    def _get_whitelist(exec_backend: str, engine_type: str = "") -> list[str]:
        """NetworkProxy 白名单：grape host + 引擎 LLM API 域名。

        五维白名单从服务端 DB（grape_t_engine_sandbox_whitelist 表）通过 API 获取，
        动态可配置。bwrap 在本机 worker 进程中，不直接查 DB。
        """
        server_url = os.environ.get("GRAPE_SERVER_URL", "http://127.0.0.1:3000")
        from urllib.parse import urlparse
        host = urlparse(server_url).hostname or "127.0.0.1"
        wl = [host]
        # 引擎 LLM API 域名（从配置文件自动推导）
        try:
            from src.domains.runtime.agents.executor import get_backend_or_none
            backend = get_backend_or_none(engine_type) if engine_type else None
            if backend:
                for domain in backend.llm_api_domains:
                    if domain and domain not in wl:
                        wl.append(domain)
        except Exception:
            pass
        return wl


class BwrapAdapter:
    """Bwrap 本地沙箱适配器——无配置文件、无 DB 行、无落盘。
    is_cloud=False → 不出现在 UI 云沙箱平台列表中。
    所有 UI 入口必须过滤 is_cloud。"""
    platform = "bwrap"
    display_name = "Bwrap (本地)"
    priority = 999
    is_cloud = False

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config=None):
        return BwrapProvider(
            audit=audit, credential=credential,
            seccomp=seccomp, alert_engine=alert_engine)

    def parse_config_file(self, raw: dict):
        from .api import ParsedConfig
        return ParsedConfig(endpoint="", api_key="")
    def normalize_db_config(self, db_row: dict): return {}
    def prepare_artifacts(self, config, workdir, provider_id) -> None: pass
    def validate_extra_config(self, raw: dict): return {}
    def health_check(self, config: dict) -> tuple: return (True, "")
    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool: return True
    def ui_schema(self):
        from .api import FormSchema
        return FormSchema()
    def cleanup(self, provider_id: int, workdir: str) -> None: pass


PROVIDER = BwrapProvider
ADAPTER = BwrapAdapter()
