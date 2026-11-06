"""Issue #993：分支级写租约 + push 前置守卫。

背景：同一 PR head 分支上 merge-bot 与任务 Agent 并发写，导致重复解冲突；
PR 已合并后继续向 head 分支 push 无任何提示，修复静默丢失在基线之外。

设计（docs/issue-993/spec.md）：
- 写租约：lock key = ``branch_guard:<repo_key>:<head_branch>``，复用
  ``src.lock`` 的 ``distributed_locks`` 表，跨实例互斥；租约记录本机首次看到的
  远端 head sha（expected_remote_sha），供后续 push 做漂移检测。
- push 前置守卫：PR 已 merged/closed → fail-closed 阻断并提示新建分支/PR；
  远端 head 与租约记录不一致 → fail-closed 阻断并提示重新 fetch/rebase。
  守卫自身查询异常 → fail-open + 告警，避免阻断正常交付。
- pre-push 钩子：由 ``repogit.ensure_issue_worktree`` 安装到每个 worktree，
  覆盖 Agent 手工 push；钩子只做本地租约/漂移判定 + 通过 grape CLI 查 PR 状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from src.config import log

LEASE_PREFIX = "branch_guard:"
DEFAULT_TTL = 1800  # 30 分钟；租约仅覆盖一次写窗口，过期自动释放
HOOK_MARKER = "# grape-branch-guard (Issue #993)"
STATE_DIR_SUFFIX = ".branch_guard"
STATE_SUFFIX = ".json"


def lease_key(repo_key: str, head_branch: str) -> str:
    """分支级租约 key（distributed_locks.lock_key 上限 128）。"""
    raw = f"{LEASE_PREFIX}{repo_key}:{head_branch or 'default'}"
    if len(raw) <= 128:
        return raw
    digest = hashlib.sha1((head_branch or "default").encode("utf-8")).hexdigest()
    return f"{LEASE_PREFIX}{repo_key}:sha1:{digest}"[:128]


# ===================== 租约状态（expected_remote_sha） =====================

def _state_dir(repo_key: str) -> str:
    try:
        from src.repo_key import workspace_repo_dir
        base = workspace_repo_dir(repo_key)
    except Exception:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", repo_key or "unknown")
        base = os.path.join(os.environ.get("TMPDIR", "/tmp"), "grape-branch-guard", safe)
    return os.path.join(base, STATE_DIR_SUFFIX)


def _state_path(repo_key: str, head_branch: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "__", head_branch or "default")
    return os.path.join(_state_dir(repo_key), f"{safe}{STATE_SUFFIX}")


def load_lease_state(repo_key: str, head_branch: str) -> dict:
    try:
        with open(_state_path(repo_key, head_branch), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_lease_state(repo_key: str, head_branch: str, **fields) -> dict:
    """合并写租约状态（expected_remote_sha / pr_number / base_branch）。"""
    state = load_lease_state(repo_key, head_branch)
    state.update({k: v for k, v in fields.items() if v is not None})
    state.setdefault("repo_key", repo_key)
    state.setdefault("head_branch", head_branch)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        os.makedirs(_state_dir(repo_key), exist_ok=True)
        with open(_state_path(repo_key, head_branch), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"branch guard state write failed {repo_key}:{head_branch}: {e}")
    return state


def record_remote_sha(repo_key: str, head_branch: str, remote_sha: str) -> dict:
    """记录本次 push 时看到的远端 head（供下一次 push 做漂移比对）。"""
    return save_lease_state(repo_key, head_branch, expected_remote_sha=remote_sha or "")


def _peek_holder(lock_key: str) -> tuple[str, int]:
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?",
                (lock_key,)).fetchone()
        if row:
            return str(row[0] or ""), int(row[1] or 0)
    except Exception as e:
        log.warning(f"branch guard peek lock failed {lock_key}: {e}")
    return "", 0


def acquire_branch_lease(repo_key: str, head_branch: str,
                         expected_remote_sha: str | None = None,
                         ttl: int = DEFAULT_TTL) -> dict:
    """获取分支写租约；同 holder 重入视为成功（一次任务内多次 push）。

    返回 ``{"ok": bool, "lock_key", "holder", "reason", "message", ...}``。
    """
    if not repo_key or not head_branch:
        return {"ok": False, "reason": "invalid_target",
                "message": "分支租约参数缺失（repo_key/head_branch）"}
    from src import lock as _lock
    key = lease_key(repo_key, head_branch)
    if _lock.acquire_lock(key, ttl=ttl):
        pass
    elif _lock.renew_lock(key, ttl=ttl):
        pass  # 同 holder 重入
    else:
        holder, _expires = _peek_holder(key)
        return {
            "ok": False,
            "lock_key": key,
            "holder": holder,
            "reason": "branch_locked",
            "message": (f"分支 {head_branch} 正被任务 {holder or 'unknown'} 占用，"
                        f"请等待其完成后再推送（禁止并发重建同分支）"),
        }
    state = load_lease_state(repo_key, head_branch)
    if expected_remote_sha is not None:
        state = record_remote_sha(repo_key, head_branch, expected_remote_sha)
    return {"ok": True, "lock_key": key, "holder": _lock._holder,
            "expected_remote_sha": state.get("expected_remote_sha", "")}


def renew_branch_lease(repo_key: str, head_branch: str, ttl: int = DEFAULT_TTL) -> bool:
    from src import lock as _lock
    return _lock.renew_lock(lease_key(repo_key, head_branch), ttl=ttl)


def release_branch_lease(repo_key: str, head_branch: str) -> None:
    from src import lock as _lock
    _lock.release_lock(lease_key(repo_key, head_branch))


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
    """服务端 push 前置检查：查远端 head + PR 状态后调用 ``check_push_allowed``。"""
    remote_sha = resolve_remote_head(repo_key, head_branch, token=token)
    pr = fetch_pr_state(repo_key, head_branch, token=token)
    verdict = check_push_allowed(
        repo_key, head_branch,
        remote_sha=remote_sha,
        pr_state=pr.get("state", ""),
        pr_number=pr.get("number", 0),
        base_branch=pr.get("base", ""),
    )
    verdict["remote_sha"] = remote_sha
    verdict["pr"] = pr
    return verdict


def resolve_remote_head(repo_key: str, head_branch: str, token: str = "") -> str:
    """``git ls-remote`` 取远端 head sha；失败/不存在返回空串让守卫 fail-open。"""
    try:
        from src.runtime.repogit import _run_git, clone_url_for
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
    """从 PR 列表里按 head 分支匹配，归一为 {state, number, base}。"""
    if not isinstance(items, list):
        return {}
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
        elif raw_state in ("merged",):
            state = "merged"
        elif raw_state in ("open", "opened"):
            state = "open"
        else:
            state = raw_state
        base = item.get("base") if isinstance(item.get("base"), dict) else {}
        return {"state": state,
                "number": item.get("number") or item.get("iid") or 0,
                "base": base.get("ref") or item.get("target_branch") or ""}
    return {}


def fetch_pr_state(repo_key: str, head_branch: str, token: str = "") -> dict:
    """按 head 分支查询 PR 状态（merged/closed/open）；异常 fail-open 返回空。

    复用 ``VcsApiClient`` 的平台端点；runner 侧 token 为空时尝试 grape CLI。
    """
    if not token:
        return _fetch_pr_state_via_grape(repo_key, head_branch)
    try:
        from src.repo_key import parse_repo_key
        from src.vcs.api_client import VcsApiClient
        provider, owner, repo = parse_repo_key(repo_key)
        client = VcsApiClient(provider=provider, token=token, owner=owner, repo=repo)
        if provider == "github":
            items = client._github_request(
                "GET", f"/repos/{owner}/{repo}/pulls?state=all&head={owner}:{head_branch}"
                       f"&per_page=100")
        elif provider == "gitlab":
            project_id = f"{owner}%2F{repo}"
            items = client._gitlab_request(
                "GET", f"/projects/{project_id}/merge_requests?state=all&per_page=100")
        else:
            items = client._gitcode_request(
                "GET", f"/repos/{owner}/{repo}/pulls?state=all&per_page=100")
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
        from src.repo_key import parse_repo_key
        provider, owner, repo = parse_repo_key(repo_key)
        cmd = ["grape", "git-repo", "pulls", "list", "--owner", owner, "--repo", repo,
               "--state", "all", "--per_page", "100", "--json-out"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log.warning(f"branch guard grape PR query failed (fail-open) {repo_key}: "
                        f"{(r.stderr or '')[:200]}")
            return {"state": ""}
        return _match_pr(json.loads(r.stdout or "[]"), head_branch)
    except Exception as e:
        log.warning(f"branch guard grape PR query failed (fail-open) {repo_key}: {e}")
        return {"state": ""}


# ===================== pre-push 钩子 =====================

def _current_branch(wt_dir: str) -> str:
    try:
        from src.runtime.repogit import _run_git
        r = _run_git(["-C", wt_dir, "rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
        return (r.stdout or "").strip() if r is not None and r.returncode == 0 else ""
    except Exception:
        return ""


def _hooks_dir(wt_dir: str) -> str:
    from src.runtime.repogit import _run_git
    r = _run_git(["-C", wt_dir, "rev-parse", "--git-path", "hooks"], timeout=15)
    if r is None or r.returncode != 0:
        return ""
    path = (r.stdout or "").strip()
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(wt_dir, path)


def install_pre_push_hook(wt_dir: str, repo_key: str, head_branch: str = "") -> bool:
    """在每个 worktree 的 git hooks 目录安装 pre-push 守卫（幂等）。

    已存在非本守卫的钩子时保留为 ``pre-push.grape-local`` 并在守卫前执行，
    不覆盖用户既有逻辑。
    """
    if not wt_dir or not repo_key:
        return False
    try:
        hooks_dir = _hooks_dir(wt_dir)
        if not hooks_dir:
            return False
        os.makedirs(hooks_dir, exist_ok=True)
        branch = head_branch or _current_branch(wt_dir)
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
            except Exception:
                pass
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        code = (f"import sys; sys.path.insert(0, {json.dumps(root)}); "
                f"from src.runtime.branch_guard import hook_main; sys.exit(hook_main())")
        lines = [
            "#!/bin/sh",
            HOOK_MARKER,
            'LOCAL="$(dirname "$0")/pre-push.grape-local"',
            'if [ -x "$LOCAL" ]; then "$LOCAL" "$@" || exit $?; fi',
            f"python3 -c {shlex.quote(code)} {shlex.quote(repo_key)} {shlex.quote(branch)}",
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


def remove_pre_push_hook(wt_dir: str) -> bool:
    """移除本守卫安装的 pre-push 钩子（仅当内容含本守卫标记）。"""
    try:
        hooks_dir = _hooks_dir(wt_dir)
        hook_path = os.path.join(hooks_dir, "pre-push") if hooks_dir else ""
        if not hook_path or not os.path.isfile(hook_path):
            return False
        with open(hook_path, "r", encoding="utf-8", errors="replace") as f:
            if HOOK_MARKER not in f.read():
                return False
        os.remove(hook_path)
        preserved = os.path.join(hooks_dir, "pre-push.grape-local")
        if os.path.isfile(preserved):
            os.replace(preserved, hook_path)
        return True
    except Exception:
        return False


def hook_main(argv: list | None = None) -> int:
    """pre-push 钩子入口：stdin 为 ``<local_ref> <local_sha> <remote_ref> <remote_sha>``。

    返回 0=放行；1=阻断（stderr 输出可执行提示）。查询异常 fail-open。
    """
    args = argv if argv is not None else sys.argv[1:]
    repo_key = args[0] if len(args) > 0 else ""
    branch = args[1] if len(args) > 1 else ""
    if not repo_key or not branch:
        return 0
    try:
        data = sys.stdin.read()
    except Exception:
        data = ""
    lease = acquire_branch_lease(repo_key, branch)
    if not lease.get("ok"):
        sys.stderr.write(f"\n[branch-guard] {lease.get('message', 'branch locked')}\n")
        return 1
    try:
        for raw in (data or "").splitlines():
            parts = raw.split()
            if len(parts) < 4:
                continue
            remote_ref, remote_sha = parts[2], parts[3]
            pushed = remote_ref[len("refs/heads/"):] if remote_ref.startswith("refs/heads/") else remote_ref
            if pushed != branch:
                continue
            if set(remote_sha) <= {"0"}:
                remote_sha = ""
            state = load_lease_state(repo_key, branch)
            pr = _fetch_pr_state_via_grape(repo_key, branch)
            verdict = check_push_allowed(
                repo_key, branch, remote_sha=remote_sha,
                expected_remote_sha=state.get("expected_remote_sha", ""),
                pr_state=pr.get("state", ""), pr_number=pr.get("number", 0),
                base_branch=pr.get("base", ""))
            if not verdict.get("allowed"):
                sys.stderr.write(f"\n[branch-guard] {verdict.get('message')}\n")
                return 1
            if remote_sha:
                record_remote_sha(repo_key, branch, remote_sha)
        return 0
    finally:
        release_branch_lease(repo_key, branch)


if __name__ == "__main__":
    sys.exit(hook_main())