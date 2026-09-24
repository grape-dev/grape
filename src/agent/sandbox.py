"""bwrap 文件系统隔离包装（组件2）。

构造 bwrap 白名单式沙箱命令前缀，用于给 agent 子进程套文件系统锁：
只挂「基础系统 + 引擎运行时 + 工位 cwd」，敏感路径（configs/、data/、.env、
/root、~/.grape、WORKSPACE 其他仓库/issue）天然不可见。

默认由 GRAPE_AGENT_SANDBOX 环境变量控制（默认关）。回滚 = 关开关，无需删代码。

关键点：
- `--die-with-parent`：grape 进程退出时沙箱内进程一并回收。
- `--unshare-all`：隔离所有 namespace，仅 `--share-net` 保留网络（A2A/GitCode/OBS/云）。
- `--chdir {cwd}` + `--bind {cwd} {cwd}`：保持 /proc/{pid}/cwd == cwd，_kill_stale_engine
  的 readlink startswith 判活不受影响（§6 #10）。
- `/tmp` 用 tmpfs 隔离；hermes 会话 overlay（/tmp/hermes-acp-homes/{md5(cwd)[:12]}）
  单独 rw-bind 回宿主机（§6 #12：不割断 state.db 续传）。
"""
import os
import shutil
import hashlib


def sandbox_enabled() -> bool:
    """开关（运行时读 config，支持 env 灰度，不重启代码逻辑）。"""
    try:
        from src.config import GRAPE_AGENT_SANDBOX
        return bool(GRAPE_AGENT_SANDBOX)
    except Exception:
        return False


def _bwrap_bin() -> str:
    return shutil.which("bwrap") or ""


def build_bwrap_cmd(cwd: str, backend_name: str = "") -> list[str] | None:
    """构造 bwrap 包装命令前缀；返回 None 表示不包装（关 / 缺 bwrap / cwd 无效 / Windows）。

    返回 [bwrap, ...opts, "--"] 前缀，调用方 append 原命令 argv（Popen 时用）。
    backend_name 用于选择引擎专用白名单（opencode/codearts/hermes）。

    Windows 降级：bwrap 依赖 Linux namespace/cgroup，Windows 无等价物 → 永远返回 None。
    """
    from src.runtime.os_adapter import is_windows
    if is_windows():
        return None
    if not sandbox_enabled():
        return None
    bwrap = _bwrap_bin()
    if not bwrap:
        return None
    if not cwd or not os.path.isdir(cwd):
        return None

    from src.config import (
        BWRAP_RO_SYSTEM, BWRAP_RO_ENGINE_COMMON, BWRAP_RO_ENGINE, BWRAP_RW_ENGINE,
    )

    opts = [bwrap, "--die-with-parent", "--unshare-all", "--share-net"]

    # 基础系统（ro）
    for p in BWRAP_RO_SYSTEM:
        if p and os.path.isdir(p):
            opts += ["--ro-bind", p, p]

    # 引擎运行时（ro）——common + 按 backend_name
    _ro = [os.path.expanduser(x) for x in BWRAP_RO_ENGINE_COMMON]
    _ro += [os.path.expanduser(x) for x in BWRAP_RO_ENGINE.get(backend_name, ())]
    _seen = set()
    for p in _ro:
        if p and os.path.isdir(p) and p not in _seen:
            _seen.add(p)
            opts += ["--ro-bind", p, p]

    # 引擎缓存（rw）
    for x in BWRAP_RW_ENGINE.get(backend_name, ()):
        p = os.path.expanduser(x)
        if p and os.path.isdir(p):
            opts += ["--bind", p, p]

    # /tmp 隔离（tmpfs）；hermes 会话 overlay 单独 rw-bind（在 tmpfs 之上叠加）
    opts += ["--tmpfs", "/tmp"]
    if backend_name == "hermes":
        _h = hashlib.md5((cwd or ".").encode()).hexdigest()[:12]
        _overlay = os.path.join("/tmp", "hermes-acp-homes", _h)
        os.makedirs(_overlay, exist_ok=True)
        opts += ["--bind", _overlay, _overlay]

    # /proc /dev
    opts += ["--proc", "/proc", "--dev", "/dev"]

    # git worktree bare mirror（codebase/）：coding/qa/sre 的 .git 是指向
    # {...}/codebase/worktrees/{agent_type} 的指针文件，git commit/push 要读写
    # codebase 的 objects/refs —— 必须 rw 挂载，否则沙箱内 git 报 not a git
    # repository。cwd 形如 {...}/issues/{n}/{agent_type}，codebase 是其仓库根下的兄弟目录。
    _issues_idx = cwd.find("/issues/")
    if _issues_idx > 0:
        _codebase = os.path.join(cwd[:_issues_idx], "codebase")
        if os.path.isdir(_codebase):
            opts += ["--bind", _codebase, _codebase]

    # 工位 cwd（rw + chdir，保持 /proc/{pid}/cwd 不变）
    opts += ["--bind", cwd, cwd]
    opts += ["--chdir", cwd]

    # hermes 需要写 /tmp 下临时 overlay？ACP 只写 /tmp/hermes-acp-homes/{hash}（已 bind）。
    # 其余引擎的 /tmp 临时文件落沙箱 tmpfs，任务结束丢弃（更干净）。

    opts.append("--")
    return opts