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
- pre-push 钩子：由 ``repogit.ensure_issue_worktree`` 安装；git 的 hooks 目录是
  仓库共享目录，因此钩子**不固化分支**，运行时从 stdin 的远程 ref 动态判定每个被推
  分支，覆盖同一仓库所有 worktree 的 Agent 手工 push。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shlex
import subprocess
import sys
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

LEASE_PREFIX = "branch_guard:"
DEFAULT_TTL = 1800  # 30 分钟；租约仅覆盖一次写窗口，过期自动释放
HOOK_MARKER = "# grape-branch-guard (Issue #993)"
# Issue #2028：钩子模板版本指纹。install_pre_push_hook 依据它识别存量旧模板并重装；
# 模板内容（fail-open/加载路径/自检）变化时必须递增，否则现网存量 worktree 永不升级。
# 2028-03：固化元数据改为单行 marker 注释（hook-interpreter/hook-module-path/
# hook-repo-key），修复 _existing_hook_usable 解析多行内联代码恒 False 的缺陷。
# 2028-04：spec 构建并入钩子 try/except（I-3），_spec is None 时不再 fail-closed。
HOOK_TEMPLATE_VERSION = "2028-04"
HOOK_VERSION_LINE = f"# hook-template-version: {HOOK_TEMPLATE_VERSION}"
# 钩子运行时自检依赖：缺失时 fail-open 并输出可定位告警（避免 Agent 反复盲重试）。
HOOK_DEP_MODULES = ("src.lock", "src.kernel.session_manager")


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
        log.warning(f"branch guard state read failed {repo_key}:{head_branch}: {e}")
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
        log.warning(f"branch guard state write failed {repo_key}:{head_branch}: {e}")
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


def reconcile_pending(repo_key: str, head_branch: str, remote_sha: str) -> str:
    """用远端实际 head 收敛 pre-push 钩子留下的候选 sha，返回应比对的期望 sha。

    - remote == 候选 → 上次手工 push 已落地，expected 前进到候选并清空 pending；
    - remote == 推送前旧值 → 上次 push 未落地，丢弃候选，保持旧 expected
      （避免下一次合法 push 被误判 remote_drift）；
    - 远端未知（查询失败/分支不存在）→ 保留候选不清空（fail-open，交由后续守卫
      在能查到远端时再收敛，避免凭未知信息误丢候选）；
    - 其它值 → 传输窗口内有他人写入，保留候选让 ``check_push_allowed`` 判漂移阻断。
    """
    state = load_lease_state(repo_key, head_branch)
    expected = state.get("expected_remote_sha", "")
    pending = state.get("pending_expected_sha", "")
    prev = state.get("pending_prev_sha", "")
    if not pending:
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
    """
    if not task_id:
        return None
    from src import lock as _lock
    return f"{task_id}:{_lock._holder}"


def acquire_branch_lease(repo_key: str, head_branch: str,
                         expected_remote_sha: str | None = None,
                         ttl: int = DEFAULT_TTL, task_id: str = "") -> dict:
    """获取分支写租约；同 holder 重入视为成功（一次任务内多次 push）。

    ``task_id`` 非空时与进程级 holder 组合作为租约 holder 写入 DB，争用提示可直接
    定位到任务；缺省用进程级 holder（``src.kernel.lock._holder``），兼容手工 push 钩子等
    无任务上下文场景。
    返回 ``{"ok": bool, "lock_key", "holder", "reason", "message", ...}``。
    """
    if not repo_key or not head_branch:
        return {"ok": False, "reason": "invalid_target",
                "message": "分支租约参数缺失（repo_key/head_branch）"}
    from src import lock as _lock
    key = lease_key(repo_key, head_branch)
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
    from src import lock as _lock
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

def check_push_allowed(repo_key: str, head_branch: str, *, remote_sha: str = "",
                       expected_remote_sha: str | None = None, pr_state: str = "",
                       pr_number: int = 0, base_branch: str = "") -> dict:
    """纯判定：PR 已合并/关闭优先阻断，其次远端漂移阻断，其余放行。

    返回 ``{"allowed": bool, "reason": ..., "message": ...}``；
    reason 取值：pr_merged / pr_closed / remote_drift / ok。
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
    expected = expected_remote_sha
    if expected is None:
        expected = load_lease_state(repo_key, head_branch).get("expected_remote_sha", "")
    if expected and remote_sha and expected != remote_sha:
        msg = (f"远端 {head_branch} 已漂移：租约期望 {str(expected)[:12]}，"
               f"当前 {str(remote_sha)[:12]}；请先 fetch/rebase 重新解冲突，"
               f"禁止基于旧基线盲目二次解冲突")
        return {"allowed": False, "reason": "remote_drift", "message": msg,
                "expected_remote_sha": expected, "remote_sha": remote_sha}
    return {"allowed": True, "reason": "ok", "message": ""}


def preflight_push(repo_key: str, head_branch: str, token: str = "") -> dict:
    """服务端 push 前置检查：查远端 head + PR 状态后调用 ``check_push_allowed``。

    两次上游查询互不依赖（ls-remote / PR 列表），并发执行把 push 前置延迟从
    「a+b」降到「max(a,b)」——实测打桩 100ms+200ms 时 300ms → ~201ms。
    两者异常各自 fail-open（``resolve_remote_head`` 返回空、``fetch_pr_state`` 返回空状态）。
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
    expected = reconcile_pending(repo_key, head_branch, remote_sha)
    verdict = check_push_allowed(
        repo_key, head_branch,
        remote_sha=remote_sha,
        expected_remote_sha=expected,
        pr_state=pr.get("state", ""),
        pr_number=pr.get("number", 0),
        base_branch=pr.get("base", ""),
    )
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
    pre = preflight_push(repo_key, head_branch, token=token)
    if not pre.get("allowed"):
        _block(f"branch guard blocked push {repo_key}:{head_branch}: {pre.get('reason')}",
               pre.get("message") or "push blocked")
        return {"ok": False, "stage": "preflight",
                "reason": pre.get("reason", "guard_blocked"),
                "message": pre.get("message", "")}
    if run_git is None:
        from src.domains.runtime.engine.repogit import _run_git as run_git
    r = run_git(["-C", wt_dir, "push", "--no-verify", "origin",
                 f"HEAD:refs/heads/{head_branch}"], token=token, timeout=120)
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
            log.warning(f"branch guard PR query failed (fail-open) {repo_key}: {items.get('error')}")
            return {"state": "", "error": items.get("error")}
        return _match_pr(items, head_branch)
    except Exception as e:
        log.warning(f"branch guard PR query failed (fail-open) {repo_key}: {e}")
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
                log.warning(f"branch guard grape PR query failed (fail-open) {repo_key}: "
                            f"{(r.stderr or '')[:200]}")
                return {"error": (r.stderr or "")[:200]}
            return json.loads(r.stdout or "[]")

        items = paginate_pages(_fetch, page_size=100, max_pages=20)
        if isinstance(items, dict) and items.get("error"):
            return {"state": ""}
        return _match_pr(items, head_branch)
    except Exception as e:
        log.warning(f"branch guard grape PR query failed (fail-open) {repo_key}: {e}")
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


def _hook_root() -> str:
    """仓库/安装根目录：``<root>/src/domains/runtime/engine/branch_guard.py`` 上溯 5 层。"""
    root = _hook_module_path()
    for _ in range(5):
        root = os.path.dirname(root)
    return root


def _existing_hook_usable(existing: str, repo_key: str) -> bool:
    """版本指纹命中后仍校验钩子内固化的运行时路径是否可用（Issue #2028）。

    钩子通过单行 marker 注释（``# hook-interpreter:/hook-module-path:/hook-repo-key:``）
    固化解释器绝对路径、守卫模块路径与 repo_key；解释器升级/目录搬迁后旧路径失效
    会让钩子以 127 退出 → git 拒绝所有 push（fail-closed），fail-open 无从执行。
    任一项缺失或失效即返回 False，触发重写自愈。

    注意不要回退到解析内联 Python 代码（2028-02 的做法）：代码经 shlex.quote 后
    跨物理行，``splitlines()`` 首行引号未闭合，``shlex.split`` 抛 ValueError 被吞，
    校验恒 False，「版本命中→校验→幂等跳过」分支退化为死代码（QA 第 2 轮 M-2 复现）。
    """
    markers = {}
    for line in existing.splitlines():
        for key in ("hook-interpreter", "hook-module-path", "hook-repo-key"):
            prefix = f"# {key}: "
            if line.startswith(prefix):
                markers[key] = line[len(prefix):].strip()
    if len(markers) != 3:
        return False
    if not markers["hook-interpreter"] or not os.path.isfile(markers["hook-interpreter"]):
        return False
    if not markers["hook-module-path"] or not os.path.isfile(markers["hook-module-path"]):
        return False
    return markers["hook-repo-key"] == repo_key


def _hook_entry_code(root: str, module_path: str) -> str:
    """生成钩子内联 Python 代码：加载守卫模块并执行 hook_main。

    Issue #2028：模块加载与 ``hook_main()`` 调用一并纳入 try/except，
    守卫自身异常一律 fail-open（stderr 明确告警 + exit 0），真实违规仍由
    ``hook_main`` 显式 return 1 阻断。
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
        "except Exception as _e:\n"
        f"    sys.stderr.write('\\n[branch-guard] WARNING 守卫自身异常，已放行本次 push "
        f"(fail-open): %r [module=%s]\\n' % (_e, {json.dumps(module_path)}))\n"
        "    _rc = 0\n"
        "sys.exit(_rc)"
    )


def _missing_hook_deps() -> list:
    """钩子依赖自检：返回无法导入的依赖描述列表（空=齐全）。"""
    missing = []
    for name in HOOK_DEP_MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:
            missing.append(f"{name} ({e.__class__.__name__}: {e})")
    return missing


def _fail_open(reason: str) -> None:
    """守卫自身异常/依赖缺失：放行本次 push 并输出可定位告警。"""
    sys.stderr.write(f"\n[branch-guard] WARNING {reason}；守卫已放行本次 push（fail-open）\n")
    try:
        log.warning(f"branch guard fail-open: {reason}")
    except Exception:
        pass


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
        return False
    try:
        hooks_dir = _hooks_dir(wt_dir)
        if not hooks_dir:
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
            except Exception:
                pass
        root = _hook_root()
        # Issue #993 R8：钩子按文件路径直接加载本模块，绕过运行时包 __init__ 的重型依赖链。
        # Issue #2028：模板必须按当前 canonical 布局（src/domains/runtime/engine/）定位，
        # 不能沿用 #699 迁移前的 src/runtime/ 路径，否则守卫模块加载必失败、静默 no-op。
        module_path = _hook_module_path()
        code = _hook_entry_code(root, module_path)
        interpreter = sys.executable or "python3"
        lines = [
            "#!/bin/sh",
            HOOK_MARKER,
            HOOK_VERSION_LINE,
            # Issue #2028：固化元数据单行 marker，供 _existing_hook_usable 解析
            # （2028-02 解析多行内联代码恒 False 的教训，勿再回退）。
            f"# hook-interpreter: {interpreter}",
            f"# hook-module-path: {module_path}",
            f"# hook-repo-key: {repo_key}",
            'STDIN_TMP="$(mktemp "${TMPDIR:-/tmp}/grape-pre-push.XXXXXX" 2>/dev/null)" || STDIN_TMP=""',
            'if [ -z "$STDIN_TMP" ]; then STDIN_TMP="${TMPDIR:-/tmp}/grape-pre-push.$$"; fi',
            "trap 'rm -f \"$STDIN_TMP\"' EXIT INT TERM HUP",
            'cat > "$STDIN_TMP" || exit 1',
            'LOCAL="$(dirname "$0")/pre-push.grape-local"',
            'if [ -x "$LOCAL" ]; then "$LOCAL" "$@" < "$STDIN_TMP" || exit $?; fi',
            f'{shlex.quote(interpreter)} -c {shlex.quote(code)} {shlex.quote(repo_key)} < "$STDIN_TMP"',
            "",
        ]
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        try:
            os.chmod(hook_path, 0o755)
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning(f"branch guard install pre-push hook failed {wt_dir}: {e}")
        return False


def hook_main(argv: list | None = None) -> int:
    """pre-push 钩子入口（fail-open 兜底）。

    Issue #2028：守卫自身任何未预期异常（含依赖 ImportError）都不得阻断交付，
    统一 stderr 告警 + 返回 0；真实拦截由 ``_hook_main`` 显式返回 1。
    """
    try:
        return _hook_main(argv)
    except Exception as e:
        _fail_open(f"branch guard runtime error {e.__class__.__name__}: {e}")
        return 0


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
        _fail_open("branch guard dependency missing: " + "; ".join(missing))
        return 0

    held: list[str] = []
    try:
        for pushed, _local_sha, _remote_sha in refs:
            lease = acquire_branch_lease(repo_key, pushed)
            if not lease.get("ok"):
                _block(f"branch lease rejected {repo_key}:{pushed}: "
                       f"{lease.get('message', 'branch locked')}",
                       lease.get("message", "branch locked"))
                return 1
            held.append(pushed)
        for pushed, _local_sha, remote_sha in refs:
            expected = reconcile_pending(repo_key, pushed, remote_sha)
            pr = _fetch_pr_state_via_grape(repo_key, pushed)
            verdict = check_push_allowed(
                repo_key, pushed, remote_sha=remote_sha,
                expected_remote_sha=expected,
                pr_state=pr.get("state", ""), pr_number=pr.get("number", 0),
                base_branch=pr.get("base", ""))
            if not verdict.get("allowed"):
                _block(f"branch guard blocked push {repo_key}:{pushed}: "
                       f"{verdict.get('reason')}",
                       verdict.get("message") or "push blocked")
                return 1
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
                try:
                    log.warning(f"branch guard release lease failed {repo_key}:{pushed}: {e}")
                except Exception:
                    pass
                sys.stderr.write(f"\n[branch-guard] WARNING release lease failed "
                                 f"{pushed}: {e}\n")


def _block(log_message: str, user_message: str) -> None:
    """阻断提示双通道：git push stderr（用户可见）+ 任务日志（结构化可追溯）。"""
    try:
        log.warning(log_message)
    except Exception:
        pass
    sys.stderr.write(f"\n[branch-guard] {user_message}\n")


if __name__ == "__main__":
    sys.exit(hook_main())
