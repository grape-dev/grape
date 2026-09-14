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


def build_bwrap_cmd(cwd: str, backend_name: str = "", sandbox_ctx=None) -> list[str] | None:
    """构造 bwrap 包装命令前缀；返回 None 表示不包装（关 / 缺 bwrap / cwd 无效）。

    返回 [bwrap, ...opts, "--"] 前缀，调用方 append 原命令 argv（Popen 时用）。
    backend_name 用于选择引擎专用白名单（opencode/codearts/hermes）。
    sandbox_ctx 有值时启用网络隔离模式（Issue #139）。
    """
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

    if sandbox_ctx and sandbox_ctx.net_enabled:
        # ===== 网络隔离模式（Issue #139）=====
        opts = [bwrap, "--die-with-parent", "--new-session",
                "--cap-drop", "ALL",  # 丢弃所有 capabilities
                "--unshare-net",      # 完全隔离网络
                "--unshare-pid", "--proc", "/proc",  # PID 隔离
                "--unshare-ipc",      # IPC 隔离（信号量/共享内存侧信道）
                "--unshare-uts"]      # UTS 隔离（主机名）
    else:
        # ===== 原有模式（--share-net）=====
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
    opts += ["--proc", "/proc", "--dev", "/dev"] if not (sandbox_ctx and sandbox_ctx.net_enabled) else ["--dev", "/dev"]

    # git worktree bare mirror（codebase/）：coding/qa/sre 的 .git 是指向
    # {...}/codebase/worktrees/{agent_type} 的指针文件，git commit/push 要读写
    # codebase 的 objects/refs —— 必须 rw 挂载，否则沙箱内 git 报 not a git
    # repository。cwd 形如 {...}/issues/{n}/{agent_type}，codebase 是其仓库根下的兄弟目录。
    _issues_idx = cwd.find("/issues/")
    if _issues_idx > 0:
        _codebase = os.path.join(cwd[:_issues_idx], "codebase")
        if os.path.isdir(_codebase):
            opts += ["--bind", _codebase, _codebase]

    # ===== 网络隔离模式额外参数 =====
    if sandbox_ctx and sandbox_ctx.net_enabled:
        # Unix socket 挂入沙箱（--bind，不是 ro-bind，socket 需要读写）
        opts += ["--bind", sandbox_ctx.socket_path, sandbox_ctx.socket_path]

        # 沙箱内桥接脚本（ro-bind）
        if sandbox_ctx.bridge_script_path and os.path.isfile(sandbox_ctx.bridge_script_path):
            opts += ["--ro-bind", sandbox_ctx.bridge_script_path, sandbox_ctx.bridge_script_path]

        # CA 证书
        if os.path.isdir("/etc/ssl/certs"):
            opts += ["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"]
        if os.path.isdir("/etc/pki"):
            opts += ["--ro-bind", "/etc/pki", "/etc/pki"]

        # 遮蔽敏感文件（--symlink 要求目标不存在，用 --ro-bind /dev/null 替代）
        if os.path.exists("/etc/shadow"):
            opts += ["--ro-bind", "/dev/null", "/etc/shadow"]

        # 代理 env 变量
        from src.agent.proxy import generate_proxy_envs
        for key, value in generate_proxy_envs(3128):
            opts += ["--setenv", key, value]

        # GRAPESOCK — 沙箱内桥接读
        opts += ["--setenv", "GRAPESOCK", sandbox_ctx.socket_path]

        # 凭据遮蔽（P0 默认空列表 — 见 config.py SANDBOX_CREDENTIAL_ENV_VARS）
        from src.config import SANDBOX_CREDENTIAL_ENV_VARS
        for var in SANDBOX_CREDENTIAL_ENV_VARS:
            if var:
                opts += ["--unsetenv", var]

    # 工位 cwd（rw + chdir，保持 /proc/{pid}/cwd 不变）
    opts += ["--bind", cwd, cwd]
    opts += ["--chdir", cwd]

    opts.append("--")
    return opts


# ===== 网络隔离：prepare / cleanup / SandboxContext / 沙箱内脚本 =====

def _write_bridge_script(socket_path: str) -> str:
    """把沙箱内桥接 Python 脚本写到宿主侧临时文件，返回路径。

    调用方需把返回的路径 ro-bind 进沙箱（build_bwrap_cmd 中处理）。
    避免在沙箱内用 heredoc / python3 -c 的引号嵌套问题。
    """
    import tempfile
    bridge_code = f'''import socketserver, socket, threading, sys
SOCK = {socket_path!r}
class H(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            u.connect(SOCK)
        except Exception as e:
            sys.stderr.write('bridge error: ' + str(e) + chr(10))
            return
        def p(s, d):
            try:
                while True:
                    b = s.recv(65536)
                    if not b:
                        break
                    d.sendall(b)
            except:
                pass
            finally:
                try:
                    d.shutdown(socket.SHUT_WR)
                except:
                    pass
        t = threading.Thread(target=p, args=(self.request, u), daemon=True)
        t.start()
        p(u, self.request)
        t.join()
socketserver.ThreadingTCPServer(('127.0.0.1', 3128), H).serve_forever()
'''
    # 写到 home 目录（不能在 /tmp，会被 bwrap tmpfs 覆盖）
    home = os.path.expanduser("~")
    fd, path = tempfile.mkstemp(
        suffix=".py", prefix=".grape-bridge-", dir=home
    )
    with os.fdopen(fd, "w") as f:
        f.write(bridge_code)
    return path


def _build_in_sandbox_script(agent_cmd: str, bridge_script_path: str) -> str:
    """生成沙箱内启动脚本：桥接 + trap + Agent。

    bridge_script_path 是 _write_bridge_script 返回的路径（已 ro-bind 进沙箱）。
    """
    script = (
        f"python3 {bridge_script_path} &\n"
        # 轮询等待桥接端口就绪（最多 5 秒）
        "for i in 1 2 3 4 5 6 7 8 9 10; do\n"
        "  python3 -c \"import socket; s=socket.socket(); s.settimeout(0.5); "
        "s.connect(('127.0.0.1',3128)); s.close()\" 2>/dev/null && break\n"
        "  sleep 0.5\n"
        "done\n"
        "trap 'kill %1 2>/dev/null' EXIT\n"
        f"exec {agent_cmd}"
    )
    return script


class SandboxContext:
    """沙箱资源上下文 — 持有代理/cgroup/socket 的引用，供 executor 管理。"""

    def __init__(self, task_id: str, net_enabled: bool):
        self.task_id = task_id
        self.net_enabled = net_enabled
        self.socket_path: str = ""
        self.proxy_port: int = 0
        self.bridge_script_path: str = ""  # 沙箱内桥接脚本路径（ro-bind 进沙箱）
        self._proxy = None       # type: ignore[type-arg]
        self._cgroup = None      # type: ignore[type-arg]
        self._audit_log = None   # type: ignore[type-arg]

    def cleanup(self):
        """清理所有沙箱资源。幂等。先停代理再销毁 cgroup。"""
        if self._proxy:
            try:
                self._proxy.stop()
            except Exception:
                pass
            self._proxy = None
        if self._cgroup:
            try:
                self._cgroup.destroy(force=True)
            except Exception:
                pass
            self._cgroup = None
        if self.bridge_script_path and os.path.exists(self.bridge_script_path):
            try:
                os.unlink(self.bridge_script_path)
            except Exception:
                pass
            self.bridge_script_path = ""


def prepare_sandbox(task_id: str, cwd: str, backend_name: str = "") -> SandboxContext | None:
    """准备沙箱资源：环境预检 → cgroup → 代理 → 返回 SandboxContext。

    返回 None = 网络隔离不可用（降级到原有 --share-net 模式）。
    """
    from src.config import log

    # 1. 环境预检
    from src.agent.detect import detect_sandbox_env
    from src.config import SANDBOX_CREDENTIAL_ENV_VARS
    can_proceed, results, env_warnings = detect_sandbox_env(SANDBOX_CREDENTIAL_ENV_VARS)

    if not can_proceed:
        hard_fails = [r for r in results if not r.passed]
        log.warning(
            f"sandbox net: hard failure, falling back to --share-net: "
            f"{[r.name for r in hard_fails]}"
        )
        return None

    # 软失败告警
    for r in results:
        if not r.passed:
            log.info(f"sandbox net: degraded — {r.name}: {r.reason}")

    # 敏感 env 告警
    for var in env_warnings:
        log.warning(
            f"sandbox net: sensitive env var '{var}' not masked "
            f"(consider adding to SANDBOX_CREDENTIAL_ENV_VARS)"
        )

    ctx = SandboxContext(task_id, net_enabled=True)

    # 2. cgroup
    from src.config import (
        GRAPE_SANDBOX_MEM_LIMIT_MB,
        GRAPE_SANDBOX_CPU_QUOTA,
        GRAPE_SANDBOX_MAX_PIDS,
    )
    from src.agent.cgroup import CgroupSlice
    ctx._cgroup = CgroupSlice(
        task_id,
        mem_limit_mb=GRAPE_SANDBOX_MEM_LIMIT_MB,
        cpu_quota=GRAPE_SANDBOX_CPU_QUOTA,
        max_pids=GRAPE_SANDBOX_MAX_PIDS,
    )
    ctx._cgroup.create()

    # 3. 审计日志
    from src.config import SANDBOX_AUDIT_LOG, SANDBOX_AUDIT_HMAC_KEY
    audit_path = SANDBOX_AUDIT_LOG
    if not audit_path:
        from src.config import CHAT_CWD_BASE
        audit_path = os.path.join(
            os.path.dirname(CHAT_CWD_BASE) if CHAT_CWD_BASE else os.path.expanduser("~"),
            "logs", "sandbox-audit.jsonl",
        )
    from src.agent.audit import RotatingAuditLog
    ctx._audit_log = RotatingAuditLog(audit_path, SANDBOX_AUDIT_HMAC_KEY)

    # 4. 代理 + 桥接 — 白名单从 DB 查询（5 分钟缓存，DB 不可用降级到环境变量）
    from src.agent.proxy import SandboxProxy, get_dynamic_whitelist
    whitelist = _get_whitelist_from_db(backend_name)

    # 凭据泄露检测：读取被注入的 token 值
    leaked_tokens = []
    for var in ("BOT_GITCODE_TOKEN", "USER_GITCODE_TOKEN", "BOT_TOKEN", "BOT_GITHUB_TOKEN"):
        val = os.environ.get(var, "")
        if val:
            leaked_tokens.append((var, val))

    ctx._proxy = SandboxProxy(
        task_id=task_id,
        whitelist=whitelist,
        audit_log=ctx._audit_log,
        leaked_tokens=leaked_tokens,
    )
    info = ctx._proxy.start()
    ctx.socket_path = info["socket_path"]
    ctx.proxy_port = info["proxy_port"]

    # 4.5 沙箱内桥接脚本（写到宿主侧文件，ro-bind 进沙箱）
    ctx.bridge_script_path = _write_bridge_script(ctx.socket_path)

    log.info(
        f"sandbox prepared: task={task_id} "
        f"socket={ctx.socket_path} proxy_port={ctx.proxy_port} "
        f"cgroup={ctx._cgroup.created}"
    )
    return ctx


# ===== 白名单 DB 查询 + 5 分钟缓存 =====

_whitelist_cache: dict[tuple[str, str], list[str]] = {}
_whitelist_cache_ts: dict[tuple[str, str], float] = {}
_whitelist_cache_lock = __import__("threading").Lock()
_WHITELIST_CACHE_TTL = 300  # 5 分钟


def _get_whitelist_from_db(backend_name: str = "", repo_key: str = "") -> list[str]:
    """从 DB 查询白名单（5 分钟缓存，按 backend_name + repo_key 区分）。"""
    global _whitelist_cache, _whitelist_cache_ts
    import time

    cache_key = (backend_name, repo_key)

    with _whitelist_cache_lock:
        now = time.monotonic()
        cached = _whitelist_cache.get(cache_key)
        if cached is not None and (now - _whitelist_cache_ts.get(cache_key, 0)) < _WHITELIST_CACHE_TTL:
            from src.agent.proxy import get_dynamic_whitelist
            return get_dynamic_whitelist(cached)

    # 查 DB
    domains: list[str] = []
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # 先检查表是否存在
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sandbox_whitelist'")
            if c.fetchone():
                c.execute(
                    "SELECT DISTINCT domain FROM sandbox_whitelist "
                    "WHERE enabled=1 AND ("
                    "  scope='global' OR "
                    "  (scope='engine' AND scope_value=?) OR "
                    "  (scope='repo' AND scope_value=?)"
                    ")",
                    (backend_name, repo_key),
                )
                domains = [r[0] for r in c.fetchall()]
    except Exception as e:
        from src.config import log
        log.warning(f"sandbox whitelist: DB query failed, using fallback: {e}")

    if not domains:
        # DB 无数据或不可用 — 降级到环境变量
        from src.config import SANDBOX_WHITELIST_FALLBACK
        domains = list(SANDBOX_WHITELIST_FALLBACK)

    # 更新缓存
    with _whitelist_cache_lock:
        _whitelist_cache[cache_key] = domains
        _whitelist_cache_ts[cache_key] = time.monotonic()

    # 加入动态白名单
    from src.agent.proxy import get_dynamic_whitelist
    return get_dynamic_whitelist(domains)


def invalidate_whitelist_cache() -> None:
    """手动失效白名单缓存（API 增删白名单后调用）。"""
    with _whitelist_cache_lock:
        _whitelist_cache.clear()
        _whitelist_cache_ts.clear()


def cleanup_sandbox(ctx: SandboxContext | None):
    """清理沙箱资源（prepare_sandbox 的配对）。"""
    if ctx:
        ctx.cleanup()