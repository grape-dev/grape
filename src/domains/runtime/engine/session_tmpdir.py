"""Issue #2225：会话级唯一暂存目录。

背景：共享 worker 上多个 Agent 会话曾共用 `/tmp/opencode/body.md` 等固定名文件，
并发会话互相覆写，导致「把其他 Issue 的正文写进线上 Issue/PR」。

本模块提供四件事：
- 平台侧下发：`inject_session_tmpdir_env` 只把会话唯一路径写入 env（不建目录，
  worker 可能在远端，服务端建目录只会留下本地空目录）。
- worker 侧 ensure：`ensure_session_tmpdir` 创建并校验目录；平台变量缺失或不可写时
  回退 `tempfile.mkdtemp` 并告警，禁止静默退回固定名。
- 会话结束清理：`cleanup_session_tmpdir` 只删自身会话子目录，带根目录白名单校验。
- 存量治理：sweeper 通过 `is_staging_path` 识别可清理的暂存路径（见 cleanup.py）。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile

from src.kernel.config import log

ENV_TASK_TMPDIR = "TASK_TMPDIR"
ENV_SESSION_DIR = "SESSION_DIR"
ENV_SESSION_KEY = "GRAPE_SESSION_TMP_KEY"
STAGING_DIR_NAME = "opencode"
_MARKER_NAME = ".grape-session-tmpdir"
# 本进程内已创建的会话目录（task key → 实际路径）。worker 结束清理通常直接读 env；
# direct 执行路径的 finally 可能拿不到 env，用该登记表兜底清理，避免回退 mktemp 目录泄漏。
_CREATED: dict[str, str] = {}


def _worker_temp_root(root: str | None = None) -> str:
    if root:
        return root
    try:
        from src.domains.runtime.engine.os_adapter import temp_dir
        return temp_dir()
    except Exception:
        return tempfile.gettempdir()


def staging_base(root: str | None = None) -> str:
    """会话暂存根目录（worker 本机临时目录下的 opencode/）。"""
    return os.path.join(_worker_temp_root(root), STAGING_DIR_NAME)


def _safe_name(task_id: str) -> str:
    raw = str(task_id or "").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw).strip("._-")
    return safe or f"anon-{os.getpid()}"


def platform_tmpdir(task_id: str, root: str = "/tmp") -> str:
    """平台下发路径（POSIX 约定）；worker 不可用时自行回退。"""
    return os.path.join(root, STAGING_DIR_NAME, _safe_name(task_id))


def inject_session_tmpdir_env(env: dict, task_id: str, *, root: str | None = None) -> dict:
    """平台侧：把会话级路径写入 env（不创建目录）。"""
    if env is None:
        env = {}
    path = platform_tmpdir(task_id, root=root or "/tmp")
    env[ENV_TASK_TMPDIR] = path
    env[ENV_SESSION_DIR] = path
    env[ENV_SESSION_KEY] = _safe_name(task_id)
    return env


def _declared_path(env: dict) -> str:
    for key in (ENV_TASK_TMPDIR, ENV_SESSION_DIR):
        val = str((env or {}).get(key) or "").strip()
        if val:
            return val
    return ""


def _try_prepare(path: str) -> bool:
    """创建目录并做一次可写探针（原子、失败即返回 False）。"""
    if not path:
        return False
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".grape-write-probe-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        marker = os.path.join(path, _MARKER_NAME)
        if not os.path.isfile(marker):
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("issue-2225\n")
        return True
    except Exception:
        return False


def ensure_session_tmpdir(env: dict, task_id: str, *, root: str | None = None) -> dict:
    """worker 侧：保证会话暂存目录存在且可写，返回回填后的 env。

    优先级（与实现逐档对应，QA 复审：原 docstring 声明三档而实现只有两档）：
    平台注入路径 → worker 本地 `{临时目录}/opencode/{task_id}` → `mkdtemp`。
    平台变量缺失或路径不可写时告警，绝不静默退回固定名。
    """
    env = env if isinstance(env, dict) else {}
    key = _safe_name(task_id)
    declared = _declared_path(env)

    if declared and _try_prepare(declared):
        env[ENV_TASK_TMPDIR] = declared
        env[ENV_SESSION_DIR] = declared
        env[ENV_SESSION_KEY] = key
        env["GRAPE_SESSION_TMPDIR_FALLBACK"] = "0"
        _CREATED[key] = declared
        return env

    if not declared:
        log.warning(
            "session tmpdir missing: TASK_TMPDIR/SESSION_DIR not injected, "
            "falling back to worker-local session dir (task=%s, issue #2225)", task_id)
    else:
        log.warning(
            "session tmpdir fallback: declared %s unusable, falling back to "
            "worker-local session dir (task=%s, issue #2225)", declared, task_id)

    # 第二档：worker 本地确定性会话目录 `{临时目录}/opencode/{task_id}`——会话唯一
    # （不与固定名平铺文件共享），可被 sweeper 覆盖、也可按 task key 清理。
    local = platform_tmpdir(key, root=_worker_temp_root(root))
    if _try_prepare(local):
        env[ENV_TASK_TMPDIR] = local
        env[ENV_SESSION_DIR] = local
        env[ENV_SESSION_KEY] = key
        env["GRAPE_SESSION_TMPDIR_FALLBACK"] = "1"
        _CREATED[key] = local
        log.warning("session tmpdir fallback to worker-local dir: %s (task=%s, issue #2225)",
                    local, task_id)
        return env

    # 第三档：worker 临时目录不可写时用 mkdtemp（唯一路径仍然满足并发隔离）。
    try:
        fallback = tempfile.mkdtemp(prefix=f"opencode-{key}-")
    except Exception as e:
        log.error("session tmpdir mktemp failed (task=%s): %s", task_id, e)
        return env
    if _try_prepare(fallback):
        env[ENV_TASK_TMPDIR] = fallback
        env[ENV_SESSION_DIR] = fallback
        env[ENV_SESSION_KEY] = key
        env["GRAPE_SESSION_TMPDIR_FALLBACK"] = "1"
        _CREATED[key] = fallback
        log.warning("session tmpdir fallback to mktemp: %s (task=%s, issue #2225)",
                    fallback, task_id)
    return env


def _staging_roots(root: str | None = None) -> list[str]:
    roots = {
        os.path.realpath(staging_base(root)),
        os.path.realpath(os.path.join("/tmp", STAGING_DIR_NAME)),
    }
    return [r for r in roots if r]


def is_staging_path(path: str, *, root: str | None = None) -> bool:
    """路径是否属于本模块创建/下发的暂存目录（且不是根本身）。

    只允许两类：`{临时目录}/opencode/...`（含 /tmp/opencode），以及 mktemp 回退
    直接落在临时目录下的 `opencode-*`。其它路径一律拒绝，避免误删系统文件。
    """
    if not path:
        return False
    real = os.path.realpath(path)
    for base in _staging_roots(root):
        if real == base:
            return False
        if real.startswith(base + os.sep):
            return True
    tmp_root = os.path.realpath(tempfile.gettempdir())
    if os.path.dirname(real) == tmp_root and os.path.basename(real).startswith("opencode-"):
        return True
    return False


def cleanup_session_tmpdir(env: dict, *, root: str | None = None) -> bool:
    """会话结束清理自身暂存子目录；非白名单路径拒绝删除。"""
    env = env if isinstance(env, dict) else {}
    path = _declared_path(env)
    if not path:
        return False
    if not is_staging_path(path, root=root):
        log.warning("session tmpdir cleanup refused outside staging roots: %s (issue #2225)", path)
        return False
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        log.warning("session tmpdir cleanup failed %s: %s (issue #2225)", path, e)
        return False
    _CREATED.pop(str(env.get(ENV_SESSION_KEY) or _safe_name(os.path.basename(path))), None)
    env.pop(ENV_TASK_TMPDIR, None)
    env.pop(ENV_SESSION_DIR, None)
    env.pop(ENV_SESSION_KEY, None)
    return True


def cleanup_session_tmpdir_by_key(task_id: str, *, root: str | None = None) -> bool:
    """按 task key 清理（供拿不到 env 的执行路径 finally 兜底）。"""
    key = _safe_name(task_id)
    path = _CREATED.pop(key, None)
    if not path:
        return False
    if not is_staging_path(path, root=root):
        log.warning("session tmpdir cleanup refused outside staging roots: %s (issue #2225)", path)
        return False
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        log.warning("session tmpdir cleanup failed %s: %s (issue #2225)", path, e)
        return False
    return True
