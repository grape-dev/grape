"""Issue #993：分支级写租约 + push 前置守卫。

背景：同一 PR head 分支上 merge-bot 与任务 Agent 并发写，导致重复解冲突；
PR 已合并后继续向 head 分支 push 无任何提示，修复静默丢失在基线之外。

设计（docs/issue-993/spec.md）：
- 写租约：lock key = ``branch_guard:<repo_key>:<head_branch>``，复用
  ``src.kernel.lock`` 的 ``distributed_locks`` 表，跨实例互斥；租约期望的远端 head sha
  （expected_remote_sha）与租约同源存 DB ``branch_guard_state`` 表，跨 worker 可见。
- push 前置守卫：PR 已 merged/closed → fail-closed 阻断并提示新建分支/PR；
  远端 head 与租约记录不一致 → fail-closed 阻断并提示重新 fetch/rebase。
  守卫自身查询异常 → fail-open + 告警，避免阻断正常交付。
  守卫自身**依赖**异常（如租约 lock 模块缺失，Issue #2706）同样 fail-open：
  租约三函数经 ``_lock_module()`` 收敛点惰性导入 ``src.kernel.lock``，缺模块/
  初始化异常打可定位 WARNING 后按「无租约」放行，push 通道不被守卫自身缺陷阻断。
- pre-push 钩子：由 ``repogit.ensure_issue_worktree`` 安装；git 的 hooks 目录是
  仓库共享目录，因此钩子**不固化分支**，运行时从 stdin 的远程 ref 动态判定每个被推
  分支，覆盖同一仓库所有 worktree 的 Agent 手工 push。
"""
from __future__ import annotations

import contextvars
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import contextlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

class _LazyLog:
    """惰性日志代理（Issue #993 R8）。

    ``src.kernel.config`` 的 import 链约 74~113ms（urllib3/urllib.request 等）。pre-push 钩子是
    每次 push 起一个新进程，若在模块顶层 import config，即使本次没有任何 refs/heads/
    （tag 推送、删分支、空 stdin）也要付这笔钱。改成按需取 logger 后，钩子的快路径
    不再触发 config 链；真正要判分支时 ``src.kernel.lock`` 仍会导入 config，那是必要成本。
    """

    def __getattr__(self, name):
        from src.kernel.config import log as _log
        return getattr(_log, name)


log = _LazyLog()

# Issue #2890：pre-push 钩子内守卫自身异常聚合。
# 各环节独立失败时不再各自 log.warning（缺 branch_guard_state 表时一次 push 会刷出
# 「state read ×3 / PR query / state write」共 5 条 WARNING JSON，上层 Agent 易误判为
# 推送失败而重试）。改由 hook_main 建立收集器，结束时统一输出唯一一条结构化告警
# （含 attempts/reason/fail_open=true）；单点异常降 debug 保留排障明细。
# 无收集器（服务端 preflight_push 等）时维持原有 warning 语义。
_guard_issue_sink: contextvars.ContextVar = contextvars.ContextVar(
    "branch_guard_issue_sink", default=None)


def _safe_debug(message: str) -> None:
    """写 debug 明细；日志后端异常不得影响守卫主流程（Issue #2923）。

    显式返回而非 ``pass``：日志写入失败时直接返回，不改变调用方的 fail-open
    语义，也不触发静默 except ratchet。
    """
    try:
        log.debug(message)
    except Exception:
        return


def _guard_stage_failed(stage: str, message: str) -> None:
    """记录单点守卫异常：钩子内降 debug，服务端 preflight 维持 warning。

    Issue #2923：钩子作用域（收集器在场）只保留 ``stage``，明细写 debug，最终由
    ``hook_main`` 统一输出一条可读中文结论（``_emit_fail_open_alert``）；无收集器
    路径（服务端 ``preflight_push`` 等，没有统一聚合输出）仍按原有 WARNING 语义
    记录，避免服务端异常完全不可见。
    """
    _safe_debug(message)
    sink = _guard_issue_sink.get()
    if sink is not None:
        sink.append({"stage": stage})
        return
    try:
        log.warning(message)
    except Exception:
        return


LEASE_PREFIX = "branch_guard:"
DEFAULT_TTL = 1800  # 30 分钟；租约仅覆盖一次写窗口，过期自动释放
HOOK_MARKER = "# grape-branch-guard (Issue #993)"

# Issue #2706（第 8 次同类复发终修）：租约 lock 导入的单一收敛点与降级语义。
# 此前三处租约函数各自裸 ``from src.kernel import lock``（#2292 迁移后），
# 缺模块时 ImportError 直接向上抛：arch-weekly-11 布局靠 ``hook_main`` 最外层
# fail-open 才放行（WARNING 定位不到租约环节），``guarded_push``/``branch_lease``
# 等服务端路径直接炸；旧布局（线上 #7415 实测）则确定性阻断全部 push。
LEASE_LOCK_MODULE = "src.kernel.lock"
# 降级放行体 reason=guard_lock_unavailable（可观测；真实阻断 reason 不会取该值）。
# Issue #2028：钩子模板版本指纹。install_pre_push_hook 依据它识别存量旧模板并重装；
# 模板内容（fail-open/加载路径/自检）变化时必须递增，否则现网存量 worktree 永不升级。
# 2028-03：固化元数据改为单行 marker 注释（hook-interpreter/hook-module-path/
# hook-repo-key），修复 _existing_hook_usable 解析多行内联代码恒 False 的缺陷。
# 2028-04：spec 构建并入钩子 try/except（I-3），_spec is None 时不再 fail-closed。
# 2028-05：shell 包装层 stdin 暂存失败改 fail-open（TMPDIR 不可写不再 exit 1），
# 告警单行含 --no-verify 指引（Issue #2933）。
# 2028-06：并入 #2292 修复指引（HOOK_REPAIR_HINT：重装/升级 worker 包），
# 模板文案变化必须递增版本号，否则存量 worktree 永不升级。
# 2028-07：Issue #2923 顶层兜底不再向 stderr 打印原始异常 repr（去噪），
# 依赖自检返回值仅含模块名；模板文案变化必须递增版本号。
# 2028-08（Issue #3579 QA 复审 Required/Optional）：加载优先级反转为
# 「部署态守卫模块优先，worktree 兜底」——安装时记录守卫模块稳定来源根
# （# hook-module-root:，issue 工作树内安装不记录），运行时按
# <module_root>/<rel> → <push_root>/<rel> → 部署态包导入（导入前移除
# sys.path 的 ''/cwd，避免 -c 模式下又解析回 worktree 同一棵树）；
# 解释器解析与 worker 同源（GRAPE_PYTHON → VIRTUAL_ENV → push 树 .venv → PATH）。
# 2028-09（Issue #2923 rebase）：#3579 运行时解析模板（_hook_runtime_entry_code）
# 并入 #2923 去噪语义——兜底输出不再 %r 打印原始异常；模板文案变化必须递增版本号。
HOOK_TEMPLATE_VERSION = "2028-09"
HOOK_VERSION_LINE = f"# hook-template-version: {HOOK_TEMPLATE_VERSION}"

# Issue #3579：钩子只记录仓库相对模块路径，运行时由当前 worktree 顶层解析。
# 安装发生在 in-flight issue worktree 时，任何绝对路径都会随 worktree 回收而失效
# （解释器 → exit 127；模块 → 导入失败），且 shell 层 127 早于内联 Python 的
# try/except，fail-open 无法执行。
HOOK_MODULE_REL_PATH = "src/domains/runtime/engine/branch_guard.py"
# Issue #3579 QA 复审：安装进程自身可能跑在会随任务回收的 issue 工作树内
# （{WORKSPACE}/{provider}/{owner}/{repo}/issues/{n}/{agent_type}，即
# repogit.issue_dir_for 的布局）。该树不得作为守卫模块稳定来源根写入 marker——
# 回收后失效正是本 Issue 要根治的形态；仅匹配路径尾部三段，不误伤同名子目录。
_ISSUE_WORKTREE_PATH_RE = re.compile(r"(?:^|/)issues/\d+/[^/]+$")
# 钩子运行时自检依赖：缺失时 fail-open 并输出可定位告警（避免 Agent 反复盲重试）。
# Issue #2292（同 #2147）：lock 随 #2010 布局收敛到 ``src.kernel.lock``，部署树不再保证
# 顶层 ``src/lock.py``（现存别名仅由 ``src/__init__.py`` 一跳解析）。自检必须直接校验
# canonical 路径，否则真缺模块时自检恒通过、租约处才抛 ImportError。
# Issue #3414：自检依赖保持 canonical 路径（旧 ``src.lock`` 别名依赖部署树
# src/__init__.py 垫片保持最新，过期部署树会在 pre-push 钩子抛 ImportError）。
HOOK_DEP_MODULES = ("src.kernel.lock", "src.kernel.session_manager")
# Issue #2292：fail-open 告警必须带可执行修复动作。此前只打印异常 repr，Agent 只能
# 手工 ``--no-verify`` 绕行并丢失租约/漂移校验，且每回合重复踩同一个包缺陷。
# 指引须声明 ``--no-verify`` 仅限故障期临时使用，避免被当成日常推送方式。
HOOK_REPAIR_HINT = ("请重装/升级 worker 发行包（scripts/install-worker.sh）补齐 "
                    "src/kernel/lock.py 等关键模块后重试；仅在确认钩子守卫故障时"
                    "临时用 grape git push --no-verify 推送（分支租约/漂移校验会失效），"
                    "不可日常化使用，推送后必须尽快重装/升级 worker 包。")


def lease_key(repo_key: str, head_branch: str) -> str:
    """分支级租约 key（distributed_locks.lock_key 上限 128）。

    超长时对完整 ``repo_key+head_branch`` 取 sha1，避免截断造成不同分支碰撞。
    """
    raw = f"{LEASE_PREFIX}{repo_key}:{head_branch or 'default'}"
    if len(raw) <= 128:
        return raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"{LEASE_PREFIX}sha1:{digest}"


# ===================== 租约状态（expected_remote_sha） =====================
#
# 期望 sha 必须与租约同源存储（同一 DB，跨 worker 可见）。早前落在本机 workspace
# 文件里，同一分支调度到另一 worker 时读不到期望值，漂移检测直接 fail-open。
# 使用独立表 branch_guard_state：租约释放会删除 distributed_locks 行，状态需比
# 单次租约生命周期更长（供下一次任务比对远端漂移）。


def _warn_lease_fallback(exc: Exception) -> None:
    """租约降级告警（Issue #2706）：可定位（模块名 + 异常）+ 可执行修复指引。

    走 stderr 与任务日志双通道（与 ``_fail_open`` 同口径）：只留裸异常时
    执行者唯一出路是 ``--no-verify``，分支租约/漂移校验全部丢失。
    """
    reason = f"branch lease lock module unavailable: {LEASE_LOCK_MODULE} " \
             f"({exc.__class__.__name__}: {exc})"
    sys.stderr.write(f"\n[branch-guard] WARNING {reason}；本次按无租约放行（fail-open）\n")
    sys.stderr.write(f"[branch-guard] WARNING 修复指引: {HOOK_REPAIR_HINT}\n")
    try:
        log.warning(f"branch guard lease fallback (fail-open): {reason}；"
                    f"修复指引: {HOOK_REPAIR_HINT}")
    except Exception as e:
        # 日志通道不可用不吞掉已写出的 stderr 告警；此处无动作可做，
        # 以显式注释说明意图（静默 except ratchet 禁止 bare pass）。
        sys.stderr.write(f"[branch-guard] WARNING lease fallback log write failed: {e}\n")


def _lock_module(quiet: bool = False):
    """租约 lock 模块惰性导入 + fail-open 兜底（Issue #2706 终修收敛点，反馈 #7415）。

    三处租约函数共享：``src.kernel.lock`` 缺失/初始化任何异常（不止
    ImportError——模块顶层 socket/secrets 初始化、别名注册链异常同样兜住）
    都打可定位 WARNING（模块名 + 可执行修复指引）后返回 None，调用方按
    「无租约」放行——守卫自身依赖缺陷不得阻断 push 通道（守卫文档契约：
    守卫查询异常 → fail-open + 告警；#7415 实机复现，同类第 8 次复发）。

    - 用 ``importlib.import_module`` 而非 import 语句：把「导入失败」收敛成
      单一可观测点；进程内 ``sys.modules`` 未命中时行为与函数内 import 一致
      （首次真正执行导入），不改变正常环境的模块缓存语义；
    - ``quiet=True`` 供内部组合路径复用（如 ``_lease_holder`` 在 acquire 内被
      二次调用）避免同一次 push 打重复告警。
    """
    try:
        return importlib.import_module(LEASE_LOCK_MODULE)
    except Exception as e:
        if not quiet:
            _warn_lease_fallback(e)
        return None


def load_lease_state(repo_key: str, head_branch: str) -> dict:
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT expected_remote_sha, pr_number, base_branch, updated_at, "
                "pending_expected_sha, pending_prev_sha "
                "FROM branch_guard_state WHERE repo_key=? AND head_branch=?",
                (repo_key, head_branch)).fetchone()
        if not row:
            return {}
        return {
            "repo_key": repo_key,
            "head_branch": head_branch,
            "expected_remote_sha": str(row[0] or ""),
            "pr_number": int(row[1] or 0),
            "base_branch": str(row[2] or ""),
            "updated_at": str(row[3] or ""),
            "pending_expected_sha": str(row[4] or ""),
            "pending_prev_sha": str(row[5] or ""),
        }
    except Exception as e:
        _guard_stage_failed(
            "state_read", f"branch guard state read failed {repo_key}:{head_branch}: {e}")
        return {}


def save_lease_state(repo_key: str, head_branch: str, **fields) -> dict:
    """合并写租约状态（expected_remote_sha / pr_number / base_branch / pending 两字段）。"""
    state = load_lease_state(repo_key, head_branch)
    state.update({k: v for k, v in fields.items() if v is not None})
    state["repo_key"] = repo_key
    state["head_branch"] = head_branch
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            exists = db.cursor().execute(
                "SELECT 1 FROM branch_guard_state WHERE repo_key=? AND head_branch=?",
                (repo_key, head_branch)).fetchone()
            vals = (str(state.get("expected_remote_sha") or ""),
                    int(state.get("pr_number") or 0),
                    str(state.get("base_branch") or ""),
                    state["updated_at"],
                    str(state.get("pending_expected_sha") or ""),
                    str(state.get("pending_prev_sha") or ""))
            if exists:
                db.execute(
                    "UPDATE branch_guard_state SET expected_remote_sha=?, pr_number=?, "
                    "base_branch=?, updated_at=?, pending_expected_sha=?, pending_prev_sha=? "
                    "WHERE repo_key=? AND head_branch=?",
                    (*vals, repo_key, head_branch))
            else:
                db.execute(
                    "INSERT INTO branch_guard_state "
                    "(repo_key, head_branch, expected_remote_sha, pr_number, base_branch, "
                    "updated_at, pending_expected_sha, pending_prev_sha) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (repo_key, head_branch, *vals))
            db.commit()
    except Exception as e:
        _guard_stage_failed(
            "state_write", f"branch guard state write failed {repo_key}:{head_branch}: {e}")
    return state


def prune_guard_state(older_than_days: int = 90) -> int:
    """清理长期未更新的分支守卫状态行（Issue #993 R9）。

    ``branch_guard_state`` 每个 (repo_key, head_branch) 一行、只增不减：实测 171 字节/行，
    1 万用户按每月 10 个分支估算 ≈10 万行/月（约 17MB/月）。按 ``updated_at`` 做 TTL 清理，
    有 pending 候选的行不删（那是 pre-push 钩子等待 reconcile 的状态，删了会漏一次漂移比对）。

    返回删除行数；任何异常只告警，不影响 sweeper。
    """
    if older_than_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM branch_guard_state WHERE updated_at != '' AND updated_at < ? "
                "AND COALESCE(pending_expected_sha, '') = ''",
                (cutoff,))
            n = c.rowcount if isinstance(c.rowcount, int) and c.rowcount > 0 else 0
            db.commit()
        if n:
            log.info(f"branch guard state pruned: {n} row(s) older than {older_than_days}d")
        return n
    except Exception as e:
        # worker 本地 SQLite 没有 branch_guard_state 是常态（agent 不读主控表）：
        # 缺表/缺列只记 debug，别让 sweeper 每 15 分钟刷一条 WARNING。
        from src.kernel.db_errors import missing_schema
        if missing_schema(e):
            log.debug(f"branch guard state prune skipped (no local table): {e}")
        else:
            log.warning(f"branch guard state prune failed: {e}")
        return 0


def record_remote_sha(repo_key: str, head_branch: str, remote_sha: str) -> dict:
    """记录本次 push 成功后远端将前进到的 head sha（供下一次 push 做漂移比对）。

    注意：必须是**本次推上去的 local sha**，不是 push 前的旧 remote sha；否则同一
    任务第二次合法 push 会把自己上一次推送误判为他人漂移。成功后落定即清空 pending。
    """
    return save_lease_state(repo_key, head_branch, expected_remote_sha=remote_sha or "",
                            pending_expected_sha="", pending_prev_sha="")


def record_pending_push(repo_key: str, head_branch: str, local_sha: str,
                        prev_remote_sha: str = "") -> dict:
    """pre-push 钩子记录「候选」期望 sha（钩子退出后才真正传输，可能失败）。

    钩子无法感知传输结果，因此不直接落定 expected：记录候选 local sha + 推送前远端
    sha，下一次守卫查询时由 ``reconcile_pending`` 按远端实际值判定是否落地。
    """
    return save_lease_state(repo_key, head_branch,
                            pending_expected_sha=local_sha or "",
                            pending_prev_sha=prev_remote_sha or "")


def reconcile_pending(repo_key: str, head_branch: str, remote_sha: str,
                      repo_dir: str = "") -> str:
    """用远端实际 head 收敛 pre-push 钩子留下的候选 sha，返回应比对的期望 sha。

    - remote == 候选 → 上次手工 push 已落地，expected 前进到候选并清空 pending；
    - remote == 推送前旧值 → 上次 push 未落地，丢弃候选，保持旧 expected
      （避免下一次合法 push 被误判 remote_drift）；
    - 远端未知（查询失败/分支不存在）→ 保留候选不清空（fail-open，交由后续守卫
      在能查到远端时再收敛，避免凭未知信息误丢候选）；
    - 其它值 → 传输窗口内有他人写入，保留候选让 ``check_push_allowed`` 判漂移阻断。

    Issue #2314：``pending`` 为空时不再直接返回存量 expected。若 expected 是远端当前
    head 的祖先，说明「上一次 push 已落地、远端已前进」而租约期望陈旧（本次故障现场：
    历史提交 8868f61a 被当作期望，远端实际已是 b22b43a6），此处先验自愈推进到远端，
    避免下一轮判定把普通 fast-forward 误判成 ``remote_drift``。无法证明祖先关系
    （``repo_dir`` 缺失、对象不可达、git 异常）时保持原值，交由判定层 fail-closed 阻断。
    """
    state = load_lease_state(repo_key, head_branch)
    expected = state.get("expected_remote_sha", "")
    pending = state.get("pending_expected_sha", "")
    prev = state.get("pending_prev_sha", "")
    if not pending:
        if expected and remote_sha and expected != remote_sha \
                and _ancestor_relation(expected, remote_sha, repo_dir) is True:
            save_lease_state(repo_key, head_branch, expected_remote_sha=remote_sha,
                             pending_expected_sha="", pending_prev_sha="")
            return remote_sha
        return expected
    if remote_sha and remote_sha == pending:
        save_lease_state(repo_key, head_branch, expected_remote_sha=pending,
                         pending_expected_sha="", pending_prev_sha="")
        return pending
    if not remote_sha:
        return pending
    if remote_sha == prev:
        save_lease_state(repo_key, head_branch, pending_expected_sha="", pending_prev_sha="")
        return expected
    return pending


def _peek_holder(lock_key: str) -> tuple[str, int]:
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?",
                (lock_key,)).fetchone()
        if row:
            return str(row[0] or ""), int(row[1] or 0)
    except Exception as e:
        log.warning(f"branch guard peek lock failed {lock_key}: {e}")
    return "", 0


def _lease_holder(task_id: str) -> str | None:
    """租约 holder：``task_id:进程 holder``。

    只把 ``task_id`` 当 holder 会让同一任务在不同 worker 上并发写时互相通过
    ``renew_lock``（holder 相等即续期成功），跨实例互斥失效；拼接进程级 holder
    后才能同时满足「同任务同进程可重入」与「同任务跨实例互斥」。

    lock 不可用时返回 None（无租约降级，Issue #2706 fail-open），不上抛。
    """
    if not task_id:
        return None
    _lock = _lock_module(quiet=True)  # Issue #3414: canonical src.kernel.lock via fail-open convergence point
    if _lock is None:
        return None
    return f"{task_id}:{_lock._holder}"


def acquire_branch_lease(repo_key: str, head_branch: str,
                         expected_remote_sha: str | None = None,
                         ttl: int = DEFAULT_TTL, task_id: str = "") -> dict:
    """获取分支写租约；同 holder 重入视为成功（一次任务内多次 push）。

    ``task_id`` 非空时与进程级 holder 组合作为租约 holder 写入 DB，争用提示可直接
    定位到任务；缺省用进程级 holder（``src.kernel.lock._holder``），兼容手工 push 钩子等
    无任务上下文场景。
    返回 ``{"ok": bool, "lock_key", "holder", "reason", "message", ...}``。

    Issue #2706（守卫文档契约）：lock 模块缺失/初始化异常 → fail-open：打可
    定位 WARNING 后按「无租约」放行（``ok=True, degraded=True,
    reason=guard_lock_unavailable``）——push 通道不得被守卫自身依赖缺陷阻断；
    真实租约争用（branch_locked）与业务 verdict 不受降级影响。
    """
    if not repo_key or not head_branch:
        return {"ok": False, "reason": "invalid_target",
                "message": "分支租约参数缺失（repo_key/head_branch）"}
    _lock = _lock_module()  # Issue #3414: canonical src.kernel.lock via fail-open convergence point
    key = lease_key(repo_key, head_branch)
    if _lock is None:
        # Issue #2706：lock 模块不可用属守卫自身异常——fail-open 按无租约放行
        # （ok=True + degraded），不得让 ImportError 阻断 push 交付通道；
        # 状态表读取自身已 fail-open，能读则尽量保留漂移校验。
        state = load_lease_state(repo_key, head_branch)
        if expected_remote_sha is not None:
            state = record_remote_sha(repo_key, head_branch, expected_remote_sha)
        return {"ok": True, "degraded": True, "lock_key": key, "holder": None,
                "reason": "guard_lock_unavailable",
                "message": ("分支租约模块不可用，已按无租约放行（fail-open）；"
                            "分支互斥/漂移校验降级失效，请尽快重装/升级 worker 包"),
                "expected_remote_sha": state.get("expected_remote_sha", "")}
    holder = _lease_holder(task_id)
    # 新获取或同 holder 重入（renew）均视为成功；仅在两者都失败时判定他人持有。
    if not (_lock.acquire_lock(key, ttl=ttl, holder=holder)
            or _lock.renew_lock(key, ttl=ttl, holder=holder)):
        current, _expires = _peek_holder(key)
        return {
            "ok": False,
            "lock_key": key,
            "holder": current,
            "reason": "branch_locked",
            "message": (f"分支 {head_branch} 正被任务 {current or 'unknown'} 占用，"
                        f"请等待其完成后再推送（禁止并发重建同分支）"),
        }
    state = load_lease_state(repo_key, head_branch)
    if expected_remote_sha is not None:
        state = record_remote_sha(repo_key, head_branch, expected_remote_sha)
    return {"ok": True, "lock_key": key, "holder": holder or _lock._holder,
            "expected_remote_sha": state.get("expected_remote_sha", "")}


def release_branch_lease(repo_key: str, head_branch: str, task_id: str = "") -> None:
    """释放分支写租约（Issue #2706：lock 缺失时降级告警后静默返回）。

    release 无可恢复动作（租约 TTL 1800s 自然过期兜底），缺模块只打 WARNING，
    不向上抛——否则 ``_hook_main`` 的 finally 会把真实拦截的 return 1 顶掉
    （#2028 教训）。
    """
    _lock = _lock_module()  # Issue #3414: canonical src.kernel.lock via fail-open convergence point
    if _lock is None:
        return
    _lock.release_lock(lease_key(repo_key, head_branch), holder=_lease_holder(task_id))


@contextmanager
def branch_lease(repo_key: str, head_branch: str, expected_remote_sha: str | None = None,
                 ttl: int = DEFAULT_TTL, task_id: str = ""):
    """持有分支租约的上下文管理器：进入获取、退出释放（未获取不释放）。

    worker 端 ``_submit_linked_repo_prs`` 与 direct 端 ``_submit_direct_linked_prs``
    共用，避免两侧租约生命周期逻辑重复漂移。
    """
    lease = acquire_branch_lease(repo_key, head_branch,
                                 expected_remote_sha=expected_remote_sha,
                                 ttl=ttl, task_id=task_id)
    try:
        yield lease
    finally:
        if lease.get("ok"):
            release_branch_lease(repo_key, head_branch, task_id=task_id)


# ===================== push 前置判定 =====================

def _ancestor_relation(ancestor: str, descendant: str, repo_dir: str = "") -> bool | None:
    """判定 ``ancestor`` 是否为 ``descendant`` 的祖先（``git merge-base --is-ancestor``）。

    Issue #2314：漂移判定从「sha 等值比较」改为「祖先关系」，必须能区分三种结果，
    且**拿不准就不能放行**：

    - True：祖先关系成立（remote 只是落后于本地 → 合法 fast-forward）；
    - False：两者线性无关（真分叉 → 阻断）；
    - None：无法判定（无 ``repo_dir``、对象不在本地对象库、git 异常/超时）→ 调用方
      按 fail-closed 处理，等价于旧行为「不一致即阻断」，不放大风险。
    """
    if not ancestor or not descendant or not repo_dir:
        return None
    try:
        from src.domains.runtime.engine.repogit import _run_git
        r = _run_git(["-C", repo_dir, "merge-base", "--is-ancestor",
                      str(ancestor), str(descendant)], timeout=15)
    except Exception as e:
        log.warning(f"branch guard merge-base failed "
                    f"{str(ancestor)[:12]}..{str(descendant)[:12]}: {e}")
        return None
    if r is None:
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    # rc 128：非 git 仓库 / 对象不可达 / 参数异常——无法证明祖先关系。
    log.warning(f"branch guard merge-base undetermined (rc={r.returncode}) "
                f"{str(ancestor)[:12]}..{str(descendant)[:12]}: {(r.stderr or '')[:200]}")
    return None


def _resolve_local_head(repo_dir: str, token: str = "") -> str:
    """取工作树当前检出提交（``guarded_push`` 推的就是 ``HEAD``）。失败返回空串。"""
    if not repo_dir:
        return ""
    try:
        from src.domains.runtime.engine.repogit import _run_git
        r = _run_git(["-C", repo_dir, "rev-parse", "HEAD"], token=token, timeout=15)
    except Exception as e:
        log.warning(f"branch guard rev-parse HEAD failed {repo_dir}: {e}")
        return ""
    if r is None or r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _hook_repo_dir() -> str:
    """钩子运行目录（git 以工作树顶层为 cwd 执行 pre-push）——祖先关系判定用对象库。"""
    try:
        return os.getcwd()
    except Exception:
        return ""


def _drift_log_fields(verdict: dict) -> str:
    """``remote_drift`` 日志定位字段（Issue #2314）。

    现场只记得住「lease expected X, current Y」，无法区分「租约陈旧」与「真有人并发写」；
    补上这三项后，运维看日志即可判断是旧租约还是真漂移。
    """
    def _short(v) -> str:
        return str(v or "")[:12] or "-"

    return (f"expected_updated_at={verdict.get('expected_updated_at') or '-'} "
            f"pending_expected_sha={_short(verdict.get('pending_expected_sha'))} "
            f"pending_prev_sha={_short(verdict.get('pending_prev_sha'))}")


def check_push_allowed(repo_key: str, head_branch: str, *, remote_sha: str = "",
                       expected_remote_sha: str | None = None, pr_state: str = "",
                       pr_number: int = 0, base_branch: str = "",
                       local_sha: str = "", repo_dir: str = "") -> dict:
    """纯判定：PR 已合并/关闭优先阻断，其次远端漂移阻断，其余放行。

    返回 ``{"allowed": bool, "reason": ..., "message": ...}``；
    reason 取值：pr_merged / pr_closed / remote_drift / fast_forward / ok。

    Issue #2314：`expected != remote_sha` 的等值比较会把「上一次 push 已落地、远端前进到
    新 head」误判为漂移。判定改为祖先关系（与 git 对非 force 更新 ``refs/heads/*`` 的
    fast-forward 语义一致）：

    - expected 为空或 == remote → 放行（快路径，行为不变）；
    - 判据 B（主）：``local_sha`` 存在且 remote 是其祖先 → 合法 fast-forward 放行，
      ``reason=fast_forward`` 并回带 ``refresh_expected=remote_sha`` 供调用方前移租约；
    - 判据 A（兜底）：无 ``local_sha``（服务端 preflight 路径）时 expected 是 remote 的
      祖先 → 只是租约落后、未分叉，同样放行并回带 ``refresh_expected``；
    - 其余（真分叉，或 merge-base 无法判定）→ ``remote_drift`` 阻断（fail-closed），
      返回体补 ``expected_updated_at`` / ``pending_expected_sha`` / ``pending_prev_sha``。
    """
    state = pr_state or ""
    if state == "merged":
        msg = (f"PR #{pr_number or '?'} 已合并，继续向 {head_branch} push 的修复不会进入基线；"
               f"请从 {base_branch or '目标基线'} 新建分支并开新 PR")
        return {"allowed": False, "reason": "pr_merged", "message": msg,
                "pr_number": pr_number, "base_branch": base_branch}
    if state == "closed":
        msg = (f"PR #{pr_number or '?'} 已关闭，继续向 {head_branch} push 不会进入基线；"
               f"请从 {base_branch or '目标基线'} 新建分支并开新 PR")
        return {"allowed": False, "reason": "pr_closed", "message": msg,
                "pr_number": pr_number, "base_branch": base_branch}
    state_row = {}
    expected = expected_remote_sha
    if expected is None:
        state_row = load_lease_state(repo_key, head_branch)
        expected = state_row.get("expected_remote_sha", "")
    if not expected or not remote_sha or expected == remote_sha:
        return {"allowed": True, "reason": "ok", "message": ""}

    if local_sha:
        forward = _ancestor_relation(remote_sha, local_sha, repo_dir)
        basis = "remote_is_ancestor_of_local"
    else:
        forward = _ancestor_relation(expected, remote_sha, repo_dir)
        basis = "expected_is_ancestor_of_remote"
    if forward is True:
        msg = (f"远端 {head_branch} 仅在合法 fast-forward 上前进：租约期望 "
               f"{str(expected)[:12]} → 当前 {str(remote_sha)[:12]}，"
               f"按 fast-forward 放行并前移租约期望")
        return {"allowed": True, "reason": "fast_forward", "message": msg,
                "expected_remote_sha": expected, "remote_sha": remote_sha,
                "refresh_expected": remote_sha, "ancestor_basis": basis}

    if not state_row:
        state_row = load_lease_state(repo_key, head_branch)
    relation = "unrelated" if forward is False else "undetermined"
    msg = (f"远端 {head_branch} 已漂移：租约期望 {str(expected)[:12]}，"
           f"当前 {str(remote_sha)[:12]}；请先 fetch/rebase 重新解冲突，"
           f"禁止基于旧基线盲目二次解冲突")
    if relation == "undetermined":
        msg += "（无法确认 fast-forward：本地缺少远端对象或 git 查询失败，按漂移阻断）"
    return {"allowed": False, "reason": "remote_drift", "message": msg,
            "expected_remote_sha": expected, "remote_sha": remote_sha,
            "ancestor_basis": basis, "drift_relation": relation,
            "expected_updated_at": state_row.get("updated_at", ""),
            "pending_expected_sha": state_row.get("pending_expected_sha", ""),
            "pending_prev_sha": state_row.get("pending_prev_sha", "")}


# ===================== Issue #3386：GitHub workflow scope 推送预检 =====================
#
# 背景：GitHub 对「改动 .github/workflows/**」的 push 强制要求 token 具备 workflow
# scope，否则远端直接 403/rejected。等推送失败再暴露成本高，因此在 pre-push 阶段
# （真正传输之前）做一次本地能力判定：
#   - 只拦 github 且本次 diff 命中 .github/workflows/**；
#   - 显式能力位 True → 放行；显式 False → 阻断并给修复指引；
#   - scope 未知（无 X-OAuth-Scopes 头 / 探测失败）→ fail-open + 提示，不误判阻断；
#   - 仅当 scope 明确缺少 workflow 时阻断。

WORKFLOW_PREFIX = ".github/workflows/"


def _env_seconds(name: str, default: float) -> float:
    """可选秒级超时环境变量：缺省/非法回退默认值并显式告警（fail-open 语义不变）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0.05, float(raw))
    except ValueError as e:
        with contextlib.suppress(Exception):
            log.debug(f"invalid {name} {raw!r}, fallback to {default}: {e}")
        return default


# workflow 预检的 git 子进程超时：rev-list/diff 在万级文件仓库仍需秒级，
# 15s（短查询）与 30s（rev-list --not --remotes）为量级估算，无 spec 数值来源，
# 按 L2 登记为待测量项；可用下面两个 env 覆盖。
_WORKFLOW_GIT_TIMEOUT = _env_seconds("GRAPE_WORKFLOW_GIT_TIMEOUT", 15)
_WORKFLOW_GIT_LONG_TIMEOUT = _env_seconds("GRAPE_WORKFLOW_GIT_LONG_TIMEOUT", 30)


def decide_workflow_scope_push(provider: str, workflow_files: list,
                               capability, scopes) -> dict:
    """纯判定：给定平台/变更文件/显式能力位/探测到的 scope，返回是否允许推送。"""
    files = [
        str(f).replace("\\", "/")
        for f in (workflow_files or [])
        if str(f).replace("\\", "/").startswith(WORKFLOW_PREFIX)
    ]
    if provider != "github" or not files:
        return {"allowed": True, "reason": "ok", "message": ""}
    if capability is True:
        return {"allowed": True, "reason": "ok", "message": ""}
    if capability is False:
        return {
            "allowed": False,
            "reason": "workflow_scope_missing",
            "message": (
                f"本次改动包含 {len(files)} 个 .github/workflows 文件，但当前 GitHub "
                "凭据被标记为缺少 workflow 权限，推送会被远端拒绝。请重新创建 PAT 并勾选 "
                "workflow（及 repo）权限后重新绑定；GitHub App token 需在 App 安装配置中"
                "开启 Workflows 写权限。"
            ),
            "workflow_files": files,
        }
    if scopes is None:
        return {
            "allowed": True,
            "reason": "workflow_scope_unknown",
            "message": (
                "无法确认 GitHub Token 是否具备 workflow scope（X-OAuth-Scopes 缺失，"
                "常见于 fine-grained token / App token）；本次放行，若远端拒绝请改用"
                "勾选 workflow 的 classic PAT。"
            ),
            "workflow_files": files,
        }
    if "workflow" in scopes:
        return {"allowed": True, "reason": "ok", "message": ""}
    return {
        "allowed": False,
        "reason": "workflow_scope_missing",
        "message": (
            f"本次改动包含 {len(files)} 个 .github/workflows 文件，但 GitHub Token 的 "
            "X-OAuth-Scopes 不含 workflow，推送会被远端拒绝。请重新创建 PAT 并勾选 "
            "workflow（及 repo）权限后重新绑定。"
        ),
        "workflow_files": files,
    }


def _workflow_files_changed(wt_dir: str, base_sha: str, local_sha: str) -> list[str]:
    """取 base_sha..local_sha 之间变更的工作流文件（失败返回空=不阻断）。

    base_sha 为空（新分支首次推送）时依次尝试 origin/HEAD、枚举所有未推送提交
    （rev-list --not --remotes=origin 逐提交 diff-tree）——只退回 local_sha^ 会
    漏判多提交新分支中早先提交的 workflow 改动（Issue #3386 复审）。
    """
    if not local_sha:
        return []
    try:
        from src.domains.runtime.engine.repogit import _run_git
        base = str(base_sha or "").strip()
        if not base:
            r_sym = _run_git(
                ["-C", wt_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                timeout=_WORKFLOW_GIT_TIMEOUT)
            if r_sym is not None and r_sym.returncode == 0 and (r_sym.stdout or "").strip():
                base = (r_sym.stdout or "").strip()
        if not base:
            # origin/HEAD 不可用：枚举所有未推送提交，逐提交收集 workflow 改动
            r_rev = _run_git(["-C", wt_dir, "rev-list", local_sha,
                              "--not", "--remotes=origin"], timeout=_WORKFLOW_GIT_LONG_TIMEOUT)
            if r_rev is None or r_rev.returncode != 0:
                return []
            out = []
            for c in (r_rev.stdout or "").splitlines():
                c = c.strip()
                if not c:
                    continue
                r_dt = _run_git(["-C", wt_dir, "diff-tree", "--no-commit-id",
                                 "--name-only", "-r", "--root", c], timeout=_WORKFLOW_GIT_TIMEOUT)
                if r_dt is None or r_dt.returncode != 0:
                    continue
                for line in (r_dt.stdout or "").splitlines():
                    f = line.strip().replace("\\", "/")
                    if f.startswith(WORKFLOW_PREFIX):
                        out.append(f)
            return sorted(set(out))
        r = _run_git(["-C", wt_dir, "diff", "--name-only", f"{base}..{local_sha}"],
                     timeout=_WORKFLOW_GIT_LONG_TIMEOUT)
        if r is None or r.returncode != 0:
            return []
        out = []
        for line in (r.stdout or "").splitlines():
            f = line.strip().replace("\\", "/")
            if f.startswith(WORKFLOW_PREFIX):
                out.append(f)
        return sorted(set(out))
    except Exception as e:
        with contextlib.suppress(Exception):
            log.debug(f"workflow files diff failed {wt_dir}: {e}")
        return []


def check_workflow_scope_push(repo_key: str, wt_dir: str, local_sha: str,
                              remote_sha: str = "", token: str = "",
                              user_id: str = "") -> dict:
    """推送前 workflow scope 预检（fail-open：判定所需信息缺失时不阻断）。"""
    provider = ""
    try:
        from src.kernel.repo_key import parse_repo_key
        provider, _owner, _repo = parse_repo_key(repo_key)
    except Exception:
        provider = ""
    if provider != "github":
        return {"allowed": True, "reason": "ok", "message": ""}
    files = _workflow_files_changed(wt_dir, remote_sha, local_sha)
    if not files:
        return {"allowed": True, "reason": "ok", "message": ""}

    capability = None
    try:
        if str(user_id or "") in ("", "0"):
            from src.domains.mgmt.public import get_host_user_id
            user_id = get_host_user_id(repo_key)
        if str(user_id or "") not in ("", "0"):
            from src.domains.mgmt.public import credential_workflows_write
            capability = credential_workflows_write(user_id, provider)
    except Exception as e:
        with contextlib.suppress(Exception):
            log.debug(f"workflow capability read failed {repo_key}: {e}")

    if capability is True:
        return {"allowed": True, "reason": "ok", "message": ""}

    scopes = None
    if capability is not False:  # 显式受限无需再探测
        try:
            if not token:
                from src.domains.mgmt.public import get_github_token
                token = get_github_token(repo_key=repo_key) or ""
            if token:
                from src.domains.mgmt.public import github_token_scopes
                scopes = github_token_scopes(token)
        except Exception as e:
            with contextlib.suppress(Exception):
                log.debug(f"workflow scope probe failed {repo_key}: {e}")

    return decide_workflow_scope_push(provider, files, capability, scopes)


def preflight_push(repo_key: str, head_branch: str, token: str = "",
                   worktree_dir: str = "", local_sha: str = "") -> dict:
    """服务端 push 前置检查：查远端 head + PR 状态后调用 ``check_push_allowed``。

    两次上游查询互不依赖（ls-remote / PR 列表），并发执行把 push 前置延迟从
    「a+b」降到「max(a,b)」——实测打桩 100ms+200ms 时 300ms → ~201ms。
    两者异常各自 fail-open（``resolve_remote_head`` 返回空、``fetch_pr_state`` 返回空状态）。

    Issue #2314：服务端路径同样需要祖先关系判定，否则陈旧租约在这里仍会把普通
    fast-forward 判成漂移。``worktree_dir`` 给出时以本地对象库判定，``local_sha``
    缺省取该工作树的 ``HEAD``（``guarded_push`` 推的就是 ``HEAD``）；两者都缺时退化为
    判据 A（expected 是远端祖先），无法判定则 fail-closed 阻断。
    """
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bg-preflight") as pool:
        f_remote = pool.submit(resolve_remote_head, repo_key, head_branch, token)
        f_pr = pool.submit(fetch_pr_state, repo_key, head_branch, token)
        try:
            remote_sha = f_remote.result()
        except Exception as e:
            log.warning(f"branch guard ls-remote task failed {repo_key}:{head_branch}: {e}")
            remote_sha = ""
        try:
            pr = f_pr.result()
        except Exception as e:
            log.warning(f"branch guard PR task failed {repo_key}:{head_branch}: {e}")
            pr = {"state": ""}
    if worktree_dir and not local_sha:
        local_sha = _resolve_local_head(worktree_dir, token=token)
    expected = reconcile_pending(repo_key, head_branch, remote_sha, repo_dir=worktree_dir)
    verdict = check_push_allowed(
        repo_key, head_branch,
        remote_sha=remote_sha,
        expected_remote_sha=expected,
        pr_state=pr.get("state", ""),
        pr_number=pr.get("number", 0),
        base_branch=pr.get("base", ""),
        local_sha=local_sha,
        repo_dir=worktree_dir,
    )
    if verdict.get("allowed") and verdict.get("refresh_expected"):
        # 放行即前移租约期望，避免下一次 push 继续走同一兜底分支。
        record_remote_sha(repo_key, head_branch, verdict["refresh_expected"])
    verdict["remote_sha"] = remote_sha
    verdict["pr"] = pr
    return verdict


def guarded_push(repo_key: str, head_branch: str, wt_dir: str, token: str = "",
                 run_git=None) -> dict:
    """自动推送路径统一入口：preflight → push --no-verify → 记录已推 local sha。

    调用方须已持有分支租约（写入窗口从 ``ensure_feature_branch`` 前开始，不能只包
    push）。``--no-verify`` 是因为本进程已持租约且完成等价 preflight；钩子是独立
    进程、holder 不同，再走钩子会被自身租约判 branch_locked。
    返回 ``{"ok", "stage", "reason", "message", "sha"}``；阻断同时写任务日志与
    stderr（与手工 push 钩子统一双通道）。
    """
    pre = preflight_push(repo_key, head_branch, token=token, worktree_dir=wt_dir)
    if not pre.get("allowed"):
        log_msg = f"branch guard blocked push {repo_key}:{head_branch}: {pre.get('reason')}"
        if pre.get("reason") == "remote_drift":
            log_msg += " " + _drift_log_fields(pre)
        _block(log_msg, pre.get("message") or "push blocked")
        return {"ok": False, "stage": "preflight",
                "reason": pre.get("reason", "guard_blocked"),
                "message": pre.get("message", "")}
    if run_git is None:
        from src.domains.runtime.engine.repogit import _run_git as run_git
    r = run_git(["-C", wt_dir, "push", "--no-verify", "origin",
                 f"HEAD:refs/heads/{head_branch}"], token=token, timeout=120,
              repo_key=repo_key)
    if r is None or r.returncode != 0:
        err = (r.stderr[:300] if r is not None and r.stderr else "git exec failed")
        return {"ok": False, "stage": "push", "reason": "push_failed",
                "message": f"git push failed: {err}"}
    r_head = run_git(["-C", wt_dir, "rev-parse", "HEAD"], token=token, timeout=15)
    sha = ((r_head.stdout or "").strip()
           if r_head is not None and r_head.returncode == 0 else "")
    if sha:
        record_remote_sha(repo_key, head_branch, sha)
    return {"ok": True, "stage": "pushed", "reason": "ok", "message": "", "sha": sha}


def resolve_remote_head(repo_key: str, head_branch: str, token: str = "") -> str:
    """``git ls-remote`` 取远端 head sha；失败/不存在返回空串让守卫 fail-open。"""
    try:
        from src.domains.runtime.engine.repogit import _run_git, clone_url_for
        r = _run_git(["ls-remote", clone_url_for(repo_key),
                      f"refs/heads/{head_branch}"], token=token, timeout=30)
        if r is None or r.returncode != 0:
            return ""
        out = (r.stdout or "").strip()
        return out.split()[0] if out else ""
    except Exception as e:
        log.warning(f"branch guard ls-remote failed {repo_key}:{head_branch}: {e}")
        return ""


def _match_pr(items, head_branch: str) -> dict:
    """从 PR 列表里按 head 分支匹配，取编号最大的一个（最近一次 PR）。

    不能只看首个命中：分页后同一 head 可能有多条历史 PR，若最近一条已 merged，
    取首个（更旧）会漏掉 merged 而 fail-open。
    """
    if not isinstance(items, list):
        return {}
    matched = []
    for item in items:
        if not isinstance(item, dict):
            continue
        head = item.get("head") if isinstance(item.get("head"), dict) else {}
        ref = head.get("ref") or item.get("source_branch") or ""
        if ref != head_branch:
            continue
        raw_state = str(item.get("state") or "").lower()
        merged = bool(item.get("merged") or item.get("merged_at")
                      or raw_state == "merged")
        if merged:
            state = "merged"
        elif raw_state in ("closed", "close"):
            state = "closed"
        elif raw_state in ("open", "opened"):
            state = "open"
        else:
            state = raw_state
        base = item.get("base") if isinstance(item.get("base"), dict) else {}
        try:
            number = int(item.get("number") or item.get("iid") or 0)
        except (TypeError, ValueError):
            number = 0
        matched.append({"state": state, "number": number,
                        "base": base.get("ref") or item.get("target_branch") or ""})
    if not matched:
        return {}
    return max(matched, key=lambda m: m["number"])


def fetch_pr_state(repo_key: str, head_branch: str, token: str = "") -> dict:
    """按 head 分支查询 PR 状态（merged/closed/open）；异常 fail-open 返回空。

    全量翻页查询后再匹配，避免 PR 超过单页上限时漏掉 merged/closed 而 fail-open。
    runner 侧 token 为空时尝试 grape CLI。
    """
    if not token:
        return _fetch_pr_state_via_grape(repo_key, head_branch)
    try:
        from src.kernel.repo_key import parse_repo_key
        from src.domains.runtime.vcs.api_client import VcsApiClient
        provider, owner, repo = parse_repo_key(repo_key)
        client = VcsApiClient(provider=provider, token=token, owner=owner, repo=repo)
        items = client.list_prs(state="all", head=head_branch)
        if isinstance(items, dict) and items.get("error"):
            _guard_stage_failed(
                "pr_query",
                f"branch guard PR query failed (fail-open) {repo_key}: {items.get('error')}")
            return {"state": "", "error": items.get("error")}
        return _match_pr(items, head_branch)
    except Exception as e:
        _guard_stage_failed(
            "pr_query", f"branch guard PR query failed (fail-open) {repo_key}: {e}")
        return {"state": "", "error": str(e)}


def _fetch_pr_state_via_grape(repo_key: str, head_branch: str) -> dict:
    """pre-push 钩子无 VCS token 时，通过 grape CLI（会话 token）查 PR 状态。"""
    try:
        from src.kernel.repo_key import parse_repo_key
        from src.domains.runtime.vcs.api_client import paginate_pages
        provider, owner, repo = parse_repo_key(repo_key)

        def _fetch(page):
            cmd = ["grape", "git-repo", "pulls", "list", "--owner", owner, "--repo", repo,
                   "--state", "all", "--per_page", "100", "--page", str(page), "--json-out"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                _guard_stage_failed(
                    "pr_query",
                    f"branch guard grape PR query failed (fail-open) {repo_key}: "
                    f"{(r.stderr or '')[:200]}")
                return {"error": (r.stderr or "")[:200]}
            return json.loads(r.stdout or "[]")

        items = paginate_pages(_fetch, page_size=100, max_pages=20)
        if isinstance(items, dict) and items.get("error"):
            return {"state": ""}
        return _match_pr(items, head_branch)
    except Exception as e:
        _guard_stage_failed(
            "pr_query", f"branch guard grape PR query failed (fail-open) {repo_key}: {e}")
        return {"state": ""}


# ===================== pre-push 钩子 =====================

def _hooks_dir(wt_dir: str) -> str:
    from src.domains.runtime.engine.repogit import _run_git
    r = _run_git(["-C", wt_dir, "rev-parse", "--git-path", "hooks"], timeout=15)
    if r is None or r.returncode != 0:
        return ""
    path = (r.stdout or "").strip()
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(wt_dir, path)


def _hook_module_path() -> str:
    """当前 canonical 守卫模块绝对路径（随 #699 迁移到 src/domains/runtime/engine/）。"""
    return os.path.abspath(__file__)


def _hook_module_relpath() -> str:
    """仓库相对守卫模块路径（写入钩子，运行时按当前 worktree 顶层解析）。"""
    return HOOK_MODULE_REL_PATH


def _hook_root() -> str:
    """仓库/安装根目录：``<root>/src/domains/runtime/engine/branch_guard.py`` 上溯 5 层。"""
    root = _hook_module_path()
    for _ in range(5):
        root = os.path.dirname(root)
    return root


def _resolve_hook_interpreter() -> str:
    """钩子运行时解释器：与 shell 模板同一解析顺序（Issue #3579 QA 复审 Optional）。

    ``$GRAPE_PYTHON``（worker 启动解释器，scripts/start-worker.sh 同名变量）→
    ``$VIRTUAL_ENV/bin/python3``（激活中的 venv）→ ``<根>/.venv/bin/python3``
    （校验侧以安装进程根近似运行时的 push worktree 根）→ PATH 上的 python3。
    全部为运行时解析、缺失自动降级，不固化绝对路径；最终权威口径以 shell 模板为准。
    """
    cand = os.environ.get("GRAPE_PYTHON", "").strip()
    if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if venv:
        cand = os.path.join(venv, "bin", "python3")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    cand = os.path.join(_hook_root(), ".venv", "bin", "python3")
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return shutil.which("python3") or ""


def _stable_module_root(wt_dir: str) -> str:
    """安装进程守卫模块根：issue 工作树（随任务回收）不记录，返回空串。

    Issue #3579 QA 复审 Required：运行时优先加载部署态守卫模块，部署态来源 =
    安装进程自身加载本模块的树（``_hook_root()``，worker 从部署安装根运行时即
    部署根）。但该根若本身就是 issue 工作树（开发期 dogfood：安装进程跑在
    ``issues/<n>/<agent_type>`` 检出内，或恰好就是本次 push 的 worktree），
    随任务回收后必失效，记录它等于复发 #3579 原始缺陷——返回空串，
    运行时自动降级为 worktree/部署态包导入兜底链。
    """
    try:
        root = os.path.realpath(_hook_root())
    except Exception:
        return ""
    try:
        wt = os.path.realpath(wt_dir or "")
    except Exception:
        wt = ""
    if wt and (root == wt or root.startswith(wt + os.sep) or wt.startswith(root + os.sep)):
        return ""
    if _ISSUE_WORKTREE_PATH_RE.search(root):
        return ""
    return root


def _existing_hook_usable(existing: str, repo_key: str) -> bool:
    """版本指纹命中后校验钩子运行时依赖是否可用（Issue #2028 / #3579）。

    2028-08 模板固化：仓库相对模块路径（``# hook-module-rel:``）、守卫模块稳定
    来源根（``# hook-module-root:``，issue 工作树安装时为空）与 repo_key；解释器
    运行时按 ``_resolve_hook_interpreter`` 顺序解析。可用性校验与运行时加载链路
    一致（不再用安装进程的树校验另一棵树的文件）：
    - repo_key 一致；
    - 相对模块路径与当前 canonical 布局一致；
    - 记录了稳定来源根：``<根>/<rel>`` 真实存在（运行时优先加载它）；未记录
      （issue 工作树安装，运行时走 worktree 兜底）时退回校验安装树自身
      （``_hook_root()``），与运行时兜底候选同构；
    - 当前环境仍能解析到 python3 解释器。

    任一项缺失或失效即返回 False，触发重写自愈（存量旧模板的绝对
    ``# hook-interpreter:`` / ``# hook-module-path:`` marker 同样视为不可用）。
    """
    markers = {}
    for line in existing.splitlines():
        for key in ("hook-module-rel", "hook-module-root", "hook-repo-key"):
            prefix = f"# {key}: "
            if line.startswith(prefix):
                markers[key] = line[len(prefix):].strip()
    if len(markers) != 3:
        return False
    if markers["hook-repo-key"] != repo_key:
        return False
    if markers["hook-module-rel"] != _hook_module_relpath():
        return False
    if not _resolve_hook_interpreter():
        return False
    root = markers["hook-module-root"]
    if root:
        return os.path.isfile(os.path.join(root, markers["hook-module-rel"]))
    return os.path.isfile(os.path.join(_hook_root(), markers["hook-module-rel"]))


def _hook_entry_code(root: str, module_path: str) -> str:
    """生成钩子内联 Python 代码：加载守卫模块并执行 hook_main。

    Issue #2028：模块加载与 ``hook_main()`` 调用一并纳入 try/except，
    守卫自身异常一律 fail-open（stderr 明确告警 + exit 0），真实违规仍由
    ``hook_main`` 显式 return 1 阻断。

    说明：``module_path`` 为显式文件路径，仅供单元测试/诊断内联生成复用；
    生产钩子使用 ``_hook_runtime_entry_code`` 在运行时解析仓库相对路径。

    Issue #2923：兜底输出只给可读中文结论，不再用 ``%r`` 打印原始异常——
    模块加载期 ``ModuleNotFoundError`` / ImportError 原文会污染任务 stderr。
    """
    return (
        "import sys, importlib.util; "
        f"sys.path.insert(0, {json.dumps(root)}); "
        "\ntry:\n"
        # Issue #2028 I-3：spec 构建也纳入 try——`_spec is None`（扩展名异常等）
        # 会在 module_from_spec 抛未捕获 AttributeError → 非零退出 fail-closed；
        # 与 hook_main 统一 fail-open。
        f"    _spec = importlib.util.spec_from_file_location('grape_branch_guard', {json.dumps(module_path)}); "
        "    _m = importlib.util.module_from_spec(_spec); "
        "    _spec.loader.exec_module(_m)\n"
        "    _rc = _m.hook_main()\n"
"except Exception:\n"
        "    sys.stderr.write('\\n[branch-guard] WARNING 守卫自身异常，"
        "已放行本次 push（fail-open）；如需跳过本钩子可加 --no-verify\\n')\n"
        # Issue #2292：同一条 WARNING 通道给出可执行修复动作，避免只留裸异常 repr。
        f"    sys.stderr.write('[branch-guard] WARNING 修复指引: %s\\n' "
        f"% {json.dumps(HOOK_REPAIR_HINT)})\n"
        "    _rc = 0\n"
        "sys.exit(_rc)"
    )


def _hook_runtime_entry_code() -> str:
    """生成生产钩子内联代码：运行时解析守卫模块并执行 hook_main（#3579）。

    钩子 shell 层把当前 push worktree 顶层（argv[2]）与安装时记录的守卫模块
    稳定来源根（argv[3]，issue 工作树安装时为空串）传入。加载优先级
    （Issue #3579 QA 复审 Required：部署态优先，worktree 降为兜底）：
    1. ``<guard_root>/<rel>``：部署态守卫——语义由部署版本决定，不受 worktree
       检出的旧基线（缺 #2314/#2890 等修复）或工作树内篡改影响；
    2. ``<push_root>/<rel>``、``<cwd>/<rel>``：worktree 兜底（未记录稳定根，
       与 2028-07 行为一致）；
    3. 部署态包导入 ``src.domains.runtime.engine.branch_guard``：导入前移除
       ``sys.path`` 中的 ``''``/cwd——``-c`` 模式下它们指向 worktree，会再次
       解析回同一棵树，部署安装根反而永远不可达。
    按文件路径加载时把所属树插回 ``sys.path`` 首位，保证模块自身的
    ``src.*`` 依赖与模块来自同一棵树。解析/导入/执行任一失败都 fail-open
    （stderr 单行告警 + exit 0），真实违规仍由 ``hook_main`` 显式返回 1 阻断。
    """
    rel = _hook_module_relpath()
    return (
        "import sys, os, importlib, importlib.util\n"
        "_root = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "_guard_root = sys.argv[3] if len(sys.argv) > 3 else ''\n"
        f"_rel = {json.dumps(rel)}\n"
        "_candidates = []\n"
        "if _guard_root:\n"
        "    _candidates.append((_guard_root, os.path.join(_guard_root, _rel)))\n"
        "if _root:\n"
        "    _candidates.append((_root, os.path.join(_root, _rel)))\n"
        "_candidates.append((os.getcwd(), os.path.join(os.getcwd(), _rel)))\n"
        "_hit = next(((r, p) for r, p in _candidates if os.path.isfile(p)), None)\n"
        "_module_root = _hit[0] if _hit else ''\n"
        "_module_path = _hit[1] if _hit else ''\n"
        "try:\n"
        "    if _module_path:\n"
        "        if _module_root and _module_root not in sys.path:\n"
        "            sys.path.insert(0, _module_root)\n"
        "        _spec = importlib.util.spec_from_file_location('grape_branch_guard', _module_path)\n"
        "        if _spec is None or _spec.loader is None:\n"
        "            raise ImportError('cannot load guard module: %s' % _module_path)\n"
        "        _m = importlib.util.module_from_spec(_spec)\n"
        "        _spec.loader.exec_module(_m)\n"
        "    else:\n"
        "        _path = [p for p in sys.path if p not in ('', os.getcwd())]\n"
        "        if _guard_root and _guard_root not in _path:\n"
        "            _path.insert(0, _guard_root)\n"
        "        sys.path[:] = _path\n"
        "        _m = importlib.import_module('src.domains.runtime.engine.branch_guard')\n"
        "    _rc = _m.hook_main()\n"
        "except Exception:\n"
        "    sys.stderr.write('\\n[branch-guard] WARNING 守卫自身异常，已放行本次 push "
        "（fail-open）；如需跳过本钩子可加 --no-verify\\n')\n"
        "    sys.stderr.write('[branch-guard] WARNING 修复指引: %s\\n' "
        f"% {json.dumps(HOOK_REPAIR_HINT)})\n"
        "    _rc = 0\n"
        "sys.exit(_rc)"
    )


def _missing_hook_deps() -> list:
    """钩子依赖自检：返回无法导入的模块名列表（空=齐全）。

    原始异常文案只写 debug 日志（供排障），返回值仅含模块名，避免经
    ``_fail_open`` 回流到用户侧 stderr（Issue #2923 AC2/AC3）。
    """
    missing = []
    for name in HOOK_DEP_MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:
            missing.append(name)
            _safe_debug(
                f"branch guard dependency import failed: {name} "
                f"({e.__class__.__name__}: {e})")
    return missing


def _fail_open(reason: str, user_hint: str = "分支守卫降级运行，推送已放行（fail-open）") -> None:
    """守卫自身异常/依赖缺失：放行本次 push，stderr 只给可读中文结论 + 修复指引。

    原始 ``reason`` 仅写日志（任务日志/debug）便于定位，不再原样写入 stderr
    （Issue #2923 AC2/AC3：缺 lock/session_manager 或模块加载失败时也不泄漏
    ImportError 原文）；Issue #2292：告警仍带可执行修复动作与 --no-verify 指引。
    """
    sys.stderr.write(f"\n[branch-guard] WARNING {user_hint}；如需跳过本钩子可加 --no-verify\n")
    sys.stderr.write(f"[branch-guard] 修复指引: {HOOK_REPAIR_HINT}\n")
    try:
        log.warning(f"branch guard fail-open: {reason}；修复指引: {HOOK_REPAIR_HINT}")
    except Exception:
        return


def _hook_not_upgraded(repo_key: str, wt_dir: str, reason: str) -> None:
    """Issue #2933：钩子安装/升级失败必须可观测。

    只读 gitdir（容器只读挂载）下 hooks 目录无法重写时，旧模板（2028-04 及更早）
    不含导入期保护；本轮由 ``src.kernel.config`` 导入期降级兜底，但「守卫未升级」
    本身必须留下可定位告警，禁止静默 ``return False``。

    告警文案按真实 ``reason`` 拼接（安装上下文缺失 / hooks 目录不可解析 /
    写权限不足 / 其它异常），不恒定声明「只读 hooks 目录」；logger 自身失败时
    兜底写 stderr；两者都不可写时放弃告警（环境已无可用输出通道），但不阻断安装。
    """
    message = f"branch guard hook not upgraded {repo_key} {wt_dir}: {reason}"
    try:
        log.warning(message)
    except Exception as e:
        message = f"{message} (log failed: {e.__class__.__name__}: {e})"
    try:
        sys.stderr.write(f"[branch-guard] WARNING {message}\n")
        sys.stderr.flush()
    except OSError:
        return


def install_pre_push_hook(wt_dir: str, repo_key: str) -> bool:
    """安装 pre-push 守卫（幂等）。

    注意：``git rev-parse --git-path hooks`` 在 linked worktree 里返回的是**仓库共享
    hooks 目录**（common dir），不是 worktree 私有目录。因此钩子不得固化任何分支，
    运行时从 stdin 读取本次被推的 ref 动态判定——同一仓库所有 worktree 共用一份钩子
    也能正确守卫各自的手工 push。

    已存在非本守卫的钩子时保留为 ``pre-push.grape-local`` 并在守卫前执行，
    不覆盖用户既有逻辑；stdin（本次被推 ref 列表）先暂存到临时文件，分别喂给
    本地钩子与本守卫，避免本地钩子消费 stdin 后守卫读到空串而静默放行。
    """
    if not wt_dir or not repo_key:
        # 调用方漏传安装上下文同样是失败路径，必须留下可定位告警（Issue #2933 复审）：
        # 由安装函数统一负责告警，调用方不必再重复上报。
        _hook_not_upgraded(
            repo_key, wt_dir, "missing wt_dir/repo_key for hook install")
        return False
    try:
        hooks_dir = _hooks_dir(wt_dir)
        if not hooks_dir:
            _hook_not_upgraded(repo_key, wt_dir, "hooks dir not resolvable")
            return False
        os.makedirs(hooks_dir, exist_ok=True)
        hook_path = os.path.join(hooks_dir, "pre-push")
        preserved = os.path.join(hooks_dir, "pre-push.grape-local")
        if os.path.isfile(hook_path):
            try:
                with open(hook_path, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
                if HOOK_MARKER not in existing:
                    if os.path.isfile(preserved):
                        os.remove(preserved)
                    os.replace(hook_path, preserved)
                elif HOOK_VERSION_LINE in existing:
                    # 存量钩子已是当前模板版本：再校验钩子内固化的解释器/模块路径/
                    # repo_key 是否仍可用（Issue #2028：解释器升级、目录搬迁后旧路径
                    # 失效会让钩子 127 退出 → 所有 push 被 git 拒绝，fail-closed），
                    # 任一失效即落到下方重写逻辑自愈；全部可用才幂等跳过。
                    if _existing_hook_usable(existing, repo_key):
                        return True
            except Exception as e:  # noqa: BLE001 - 探针失败按「不可用」继续重写
                log.debug(f"existing hook probe failed {repo_key}: {e}")
        # Issue #3579：模板不再固化解释器/模块绝对路径（worktree 回收后 127/导入失败）。
        # Issue #3579 QA 复审：运行时加载优先级反转为「部署态守卫优先，worktree 兜底」。
        # 解释器按 worker 同源顺序运行时解析；模块来源只记录稳定安装根（部署态优先），
        # issue 工作树内安装不记录（_stable_module_root），避免回收后路径失效复发。
        # Issue #993 R8：钩子按文件路径直接加载本模块，绕过运行时包 __init__ 的重型依赖链。
        code = _hook_runtime_entry_code()
        module_root = _stable_module_root(wt_dir)
        lines = [
            "#!/bin/sh",
            HOOK_MARKER,
            HOOK_VERSION_LINE,
            # Issue #2028/#3579：固化元数据单行 marker，供 _existing_hook_usable 解析
            # （2028-02 解析多行内联代码恒 False 的教训，勿再回退）。
            f"# hook-module-rel: {_hook_module_relpath()}",
            f"# hook-module-root: {module_root}",
            f"# hook-repo-key: {repo_key}",
            'STDIN_TMP="$(mktemp "${TMPDIR:-/tmp}/grape-pre-push.XXXXXX" 2>/dev/null)" || STDIN_TMP=""',
            'if [ -z "$STDIN_TMP" ]; then STDIN_TMP="${TMPDIR:-/tmp}/grape-pre-push.$$"; fi',
            "trap 'rm -f \"$STDIN_TMP\"' EXIT INT TERM HUP",
            # Issue #2933：stdin 暂存失败（TMPDIR 只读/不可写）属环境问题，不得 exit 1
            # 拒推；告警单行且含 --no-verify 指引，之后 fail-open 退出。
            'if ! cat > "$STDIN_TMP" 2>/dev/null; then',
            ('    echo "[branch-guard] WARNING 无法暂存 pre-push stdin（临时目录不可写）；'
             '守卫已放行本次 push（fail-open）。如需跳过本钩子可加 --no-verify" >&2'),
            '    rm -f "$STDIN_TMP" 2>/dev/null || true',
            "    exit 0",
            "fi",
            'LOCAL="$(dirname "$0")/pre-push.grape-local"',
            'if [ -x "$LOCAL" ]; then "$LOCAL" "$@" < "$STDIN_TMP" || exit $?; fi',
            # Issue #3579：模块来源运行时按「部署态稳定根 → 当前 worktree 顶层」
            # 解析，不引用安装时 in-flight issue worktree 的绝对路径。
            'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"',
            # Issue #3579 QA 复审 Optional：解释器与 worker 实际解释器同源
            # （scripts/start-worker.sh 的 GRAPE_PYTHON / .venv 前置）——
            # GRAPE_PYTHON → 激活 venv → push worktree .venv → PATH python3；
            # 全部运行时解析，缺失自动降级，不再因固化失效路径 exit 127。
            'PY=""',
            'if [ -n "$GRAPE_PYTHON" ] && [ -x "$GRAPE_PYTHON" ]; then PY="$GRAPE_PYTHON"; fi',
            'if [ -z "$PY" ] && [ -n "$VIRTUAL_ENV" ] && [ -x "$VIRTUAL_ENV/bin/python3" ]; then '
            'PY="$VIRTUAL_ENV/bin/python3"; fi',
            'if [ -z "$PY" ] && [ -n "$ROOT" ] && [ -x "$ROOT/.venv/bin/python3" ]; then '
            'PY="$ROOT/.venv/bin/python3"; fi',
            'if [ -z "$PY" ]; then PY="$(command -v python3 2>/dev/null || true)"; fi',
            'if [ -z "$PY" ]; then',
            ('    echo "[branch-guard] WARNING 未找到可用的 python3 解释器'
             '（GRAPE_PYTHON/VIRTUAL_ENV/.venv/PATH 均未解析到）；守卫已放行本次 push（fail-open）。'
             '如需跳过本钩子可加 --no-verify" >&2'),
            "    exit 0",
            "fi",
            # Issue #3579 QA 复审 Required：部署态守卫模块优先（module_root 稳定时），
            # push worktree 顶层降为兜底；module_root 为空串即未记录稳定根。
            f'"$PY" -c {shlex.quote(code)} {shlex.quote(repo_key)} "$ROOT" '
            f'{shlex.quote(module_root)} < "$STDIN_TMP"',
            "",
        ]
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        try:
            os.chmod(hook_path, 0o755)
        except Exception as e:  # noqa: BLE001 - 钩子已写出，权限失败只记日志
            log.debug(f"chmod branch-guard hook failed {hook_path}: {e}")
        return True
    except Exception as e:
        _hook_not_upgraded(repo_key, wt_dir, f"{e.__class__.__name__}: {e}")
        return False


def _emit_fail_open_alert(repo_key: str, issues: list) -> None:
    """把钩子内收集到的守卫异常聚合为唯一一条用户可读中文提示（Issue #2923）。

    Issue #2890 已把单点 WARNING 收敛为一条聚合告警，但仍把 ``attempts`` 里的
    原始异常文案（如 ``No module named src.vcs``）写进 stderr，形成误导性噪声。
    Issue #2923 改为：明细只进 debug 日志，用户侧只输出一条中文结论，保持
    fail-open 放行语义。输出本身必须可失败：stderr 写入异常不得阻断 push。
    """
    stages = []
    for issue in issues:
        stage = str((issue or {}).get("stage") or "")
        if stage and stage not in stages:
            stages.append(stage)
    if "pr_query" in stages:
        hint = "分支基线核验暂不可用，推送已放行（fail-open）"
    else:
        hint = "分支守卫降级运行，推送已放行（fail-open）"
    _safe_debug(f"branch guard fail-open stages={stages} repo_key={repo_key}")
    try:
        sys.stderr.write(f"[branch-guard] {hint}\n")
        sys.stderr.flush()
    except Exception:
        return


def hook_main(argv: list | None = None) -> int:
    """pre-push 钩子入口（fail-open 兜底）。

    Issue #2028：守卫自身任何未预期异常（含依赖 ImportError）都不得阻断交付，
    统一 stderr 告警 + 返回 0；真实拦截由 ``_hook_main`` 显式返回 1。

    Issue #2890：守卫各环节的单点异常在此聚合——收集后仅输出一条结构化
    WARNING JSON（attempts/reason/fail_open=true），不再逐条刷 WARNING。
    """
    args = argv if argv is not None else sys.argv[1:]
    repo_key = args[0] if len(args) > 0 else ""
    issues: list = []
    token = _guard_issue_sink.set(issues)
    try:
        try:
            rc = _hook_main(argv)
        except Exception as e:
            # 顶层兜底告警本身已是唯一结论行，避免与聚合行重复输出。
            issues.clear()
            _fail_open(
                f"branch guard runtime error {e.__class__.__name__}: {e}",
                "分支守卫运行异常，推送已放行（fail-open）")
            rc = 0
    finally:
        _guard_issue_sink.reset(token)
    # 真实拦截（rc=1）已有独立阻断提示，不再向 stderr 附加 fail-open 聚合告警；
    # 但守卫异常明细不能因此丢失（同一 push 里可能既有阻断分支又有守卫异常分支），
    # 此时降为一条 WARNING 日志留痕，不进 stderr 结论行。
    if issues:
        if rc == 0:
            _emit_fail_open_alert(repo_key, list(issues))
        else:
            _stages = ", ".join(sorted({str((i or {}).get("stage") or "") for i in issues} - {""}))
            try:
                log.warning(f"branch guard errors aggregated (push blocked separately) "
                            f"{repo_key}: {_stages}")
            except Exception as e:
                # logger 自身失败兜底写 stderr（#2933 同口径）：守卫异常明细不能因
                # 日志通道故障而整体丢失。
                sys.stderr.write(f"\n[branch-guard] WARNING guard errors aggregated "
                                 f"{repo_key}: {_stages} ({e.__class__.__name__}: {e})\n")
    return rc


def _initial_expected(repo_key: str, head_branch: str, remote_sha: str) -> str | None:
    """分支首次无状态时以推送时实时远端 head 初始化租约期望（Issue #2314 缺口 ③）。

    ``_hook_main`` 调 ``acquire_branch_lease`` 从来不传 ``expected_remote_sha``，而
    ``acquire_branch_lease`` 仅在非 None 时写库 → 分支首次 push 的期望永远是空值，
    下一次守卫只能靠 pending 收敛，历史 sha 一旦落库就再没有校正入口。

    - 状态表已有 expected 或 pending → 返回 None（**不改写**，交由 ``reconcile_pending``
      按远端实际值收敛，避免钩子把正在比对的候选覆盖掉）；
    - 全空 → 返回实时远端 head（newline 分支 / 远端无此分支时为空串，保持 None 语义不写库）。
    """
    state = load_lease_state(repo_key, head_branch)
    if state.get("expected_remote_sha") or state.get("pending_expected_sha"):
        return None
    return remote_sha or None


def _hook_main(argv: list | None = None) -> int:
    """pre-push 钩子主逻辑：stdin 为 ``<local_ref> <local_sha> <remote_ref> <remote_sha>``。

    不固化分支：逐条解析 stdin 的 remote ref，对本次实际被推的每个 head 分支独立
    获取租约、查 PR/漂移并在全部通过后记录 local sha。返回 0=放行；1=阻断
    （stderr 提示 + 任务日志告警双通道）。查询异常 fail-open。
    """
    args = argv if argv is not None else sys.argv[1:]
    repo_key = args[0] if len(args) > 0 else ""
    if not repo_key:
        return 0
    try:
        data = sys.stdin.read()
    except Exception:
        data = ""
    refs = []
    for raw in (data or "").splitlines():
        parts = raw.split()
        if len(parts) < 4:
            continue
        local_sha, remote_ref, remote_sha = parts[1], parts[2], parts[3]
        if not remote_ref.startswith("refs/heads/"):
            continue
        pushed = remote_ref[len("refs/heads/"):]
        if not pushed:
            continue
        if set(local_sha) <= {"0"}:
            continue  # 删除分支不写内容，无需租约
        if set(remote_sha) <= {"0"}:
            remote_sha = ""
        refs.append((pushed, local_sha, remote_sha))
    if not refs:
        return 0

    # 依赖自检放在快路径（tag 推送/删分支/空 stdin）之后：#993 R8 的快路径不触发
    # config 链，自检的 import 开销只在确有分支要判定时才支付（Issue #2028 复审）。
    missing = _missing_hook_deps()
    if missing:
        _fail_open(
            "branch guard dependency missing: " + "; ".join(missing),
            "分支守卫依赖缺失，推送已放行（fail-open）")
        return 0

    held: list[str] = []
    # Issue #2314：祖先关系判定需要本地对象库——git 以工作树顶层为 cwd 执行 pre-push。
    repo_dir = _hook_repo_dir()
    try:
        for pushed, _local_sha, remote_sha in refs:
            # Issue #2314 缺口 ③：分支首次无状态时以推送时实时远端 head 初始化期望，
            # 不再让第一次 push 的期望永远缺失、历史 sha 无人校正。
            lease = acquire_branch_lease(
                repo_key, pushed,
                expected_remote_sha=_initial_expected(repo_key, pushed, remote_sha))
            if not lease.get("ok"):
                _block(f"branch lease rejected {repo_key}:{pushed}: "
                       f"{lease.get('message', 'branch locked')}",
                       lease.get("message", "branch locked"))
                return 1
            held.append(pushed)
        for pushed, local_sha, remote_sha in refs:
            # Issue #3386：涉及 .github/workflows/** 的 GitHub 推送在真正传输前先做
            # workflow scope 预检，避免远端 403/rejected 后才发现。守卫自身异常 fail-open。
            try:
                _wf = check_workflow_scope_push(
                    repo_key, repo_dir or os.getcwd(), local_sha, remote_sha=remote_sha)
                if not _wf.get("allowed"):
                    _block(f"workflow scope blocked push {repo_key}:{pushed}: "
                           f"{_wf.get('reason')}",
                           _wf.get("message") or "workflow scope missing")
                    return 1
                # Spec：未知 scope 仅 fail-open 放行不够——必须可见提示（不阻断）
                _advise_workflow_scope_unknown(_wf, repo_key, pushed)
            except Exception as _wf_e:
                _guard_stage_failed(
                    "workflow_scope",
                    f"branch guard workflow scope check failed {repo_key}:{pushed}: {_wf_e}")
            # Issue #2314 缺口 ②：陈旧 expected 在这里按远端实际值先验收敛。
            expected = reconcile_pending(repo_key, pushed, remote_sha, repo_dir=repo_dir)
            pr = _fetch_pr_state_via_grape(repo_key, pushed)
            # Issue #2314 缺口 ①：把 local_sha 带进判定，远端只是祖先时按 fast-forward 放行。
            verdict = check_push_allowed(
                repo_key, pushed, remote_sha=remote_sha,
                expected_remote_sha=expected,
                pr_state=pr.get("state", ""), pr_number=pr.get("number", 0),
                base_branch=pr.get("base", ""),
                local_sha=local_sha, repo_dir=repo_dir)
            if not verdict.get("allowed"):
                log_msg = (f"branch guard blocked push {repo_key}:{pushed}: "
                           f"{verdict.get('reason')}")
                if verdict.get("reason") == "remote_drift":
                    # Issue #2314 缺口 ④：日志补齐定位字段，区分「旧租约」与「真漂移」。
                    log_msg += " " + _drift_log_fields(verdict)
                _block(log_msg, verdict.get("message") or "push blocked")
                return 1
            if verdict.get("refresh_expected"):
                record_remote_sha(repo_key, pushed, verdict["refresh_expected"])
        # pre-push 钩子退出后 git 才真正传输，无法在钩子内确认成功：这里只记录候选
        # sha + 推送前远端，下一次守卫用 reconcile_pending 按远端实际值收敛，避免
        # push 失败（网络/non-fast-forward）后留下未落地的 expected 导致误报漂移。
        for pushed, local_sha, remote_sha in refs:
            if local_sha:
                record_pending_push(repo_key, pushed, local_sha, prev_remote_sha=remote_sha)
        return 0
    finally:
        for pushed in held:
            try:
                release_branch_lease(repo_key, pushed)
            except Exception as e:
                # Issue #2028：释放租约的 DB 异常不得覆盖待返回的退出码——真实拦截
                # （return 1）若被 finally 异常顶掉，会被 hook_main 的 fail-open 兜底
                # 翻转成放行。这里只告警，不改变既有判定结果。
                with contextlib.suppress(Exception):
                    log.warning(f"branch guard release lease failed {repo_key}:{pushed}: {e}")
                sys.stderr.write(f"\n[branch-guard] WARNING release lease failed "
                                 f"{pushed}: {e}\n")


def _block(log_message: str, user_message: str) -> None:
    """阻断提示双通道：git push stderr（用户可见）+ 任务日志（结构化可追溯）。"""
    with contextlib.suppress(Exception):
        log.warning(log_message)
    sys.stderr.write(f"\n[branch-guard] {user_message}\n")


def _warn(log_message: str, user_message: str) -> None:
    """非阻断告警双通道：stderr 提示 + 任务日志，不改变放行判定（fail-open 提示用）。"""
    with contextlib.suppress(Exception):
        log.warning(log_message)
    sys.stderr.write(f"\n[branch-guard] WARNING {user_message}\n")


def _advise_workflow_scope_unknown(verdict: dict, repo_key: str, pushed: str) -> None:
    """Issue #3386：workflow scope 未知时放行但不静默（Spec：fail-open 仅提示）。

    stderr / 任务日志双通道输出告警文案；判定为其它结果（含明确缺失已阻断）不做任何事。
    """
    if not verdict or verdict.get("reason") != "workflow_scope_unknown":
        return
    _warn(f"workflow scope unknown push allowed {repo_key}:{pushed}",
          verdict.get("message") or
          "无法确认 GitHub Token 是否具备 workflow scope，本次放行；"
          "若远端拒绝请改用勾选 workflow 的 classic PAT")


if __name__ == "__main__":
    sys.exit(hook_main())
