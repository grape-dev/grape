"""反馈状态口径（Issue #673 落地，与 #22 通知机制共用同一映射）。

统一 user_feedback.status 与 issue（issue_state 阶段 stage / task status）的映射：
- 反馈状态枚举：open / in_progress / resolved / closed（前后端共用，#673 / #22 一套口径）
- issue.stage（vod/issue/coding/qa/sre/done）→ 反馈状态推导
- 联动同步只升不降（open < in_progress < resolved < closed）：
  admin 手工标记的 resolved/closed 不被自动同步回退覆盖。
"""
import re

from src.config import log
from src.session_manager import db, db_lock

# 反馈状态枚举（与 routes/feedback.py update_feedback_status 白名单一致）
FEEDBACK_STATUS_OPEN = "open"
FEEDBACK_STATUS_IN_PROGRESS = "in_progress"
FEEDBACK_STATUS_RESOLVED = "resolved"
FEEDBACK_STATUS_CLOSED = "closed"

# 状态推进序（只升不降判断依据；closed 为人工终态）
FEEDBACK_STATUS_ORDER = {
    FEEDBACK_STATUS_OPEN: 0,
    FEEDBACK_STATUS_IN_PROGRESS: 1,
    FEEDBACK_STATUS_RESOLVED: 2,
    FEEDBACK_STATUS_CLOSED: 3,
}

# issue.stage（vod/issue/coding/qa/sre/done）→ 反馈状态
ISSUE_STAGE_TO_FEEDBACK_STATUS = {
    "vod": FEEDBACK_STATUS_OPEN,
    "issue": FEEDBACK_STATUS_OPEN,
    "coding": FEEDBACK_STATUS_IN_PROGRESS,
    "qa": FEEDBACK_STATUS_IN_PROGRESS,
    "sre": FEEDBACK_STATUS_IN_PROGRESS,
    "done": FEEDBACK_STATUS_RESOLVED,
}

# issue.status（任务态）→ 反馈状态（覆盖 stage 推导）
_ISSUE_TASK_STATUS_TO_FEEDBACK_STATUS = {
    "completed": FEEDBACK_STATUS_RESOLVED,
    "closed": FEEDBACK_STATUS_CLOSED,
    "cancelled": FEEDBACK_STATUS_CLOSED,
}


def derive_feedback_status(stage: str, task_status: str = "") -> str:
    """由 issue 的 (stage, task_status) 推导反馈状态（#673/#22 共用口径）。

    task_status 优先（completed/closed/cancelled 为终态）；
    否则按 stage：分析阶段（vod/issue）=open，执行阶段（coding/qa/sre）=in_progress，
    done=resolved（失败完成的卡片不视为 resolved，由 stage 兜底为 open）。
    """
    ts = (task_status or "").strip().lower()
    if ts in _ISSUE_TASK_STATUS_TO_FEEDBACK_STATUS:
        return _ISSUE_TASK_STATUS_TO_FEEDBACK_STATUS[ts]
    return ISSUE_STAGE_TO_FEEDBACK_STATUS.get((stage or "").strip().lower(),
                                              FEEDBACK_STATUS_OPEN)


# 外部 Issue URL 解析（Issue #673 QA S4：repo_key 为空的历史行按 issue_url 兜底推导）
_EXTERNAL_ISSUE_URL_RE = re.compile(
    r"^https?://(?:www\.)?(github\.com|gitcode\.com|gitlab\.com)"
    r"/([^/\s]+)/([^/\s]+)/(?:-/)?issues/(\d+)/?$")


def _repo_key_from_issue_url(issue_url: str) -> str:
    """issue_url → repo_key（"<provider>:<owner>/<repo>"）；无法解析返回 ''。"""
    m = _EXTERNAL_ISSUE_URL_RE.match((issue_url or "").strip())
    if not m:
        return ""
    host, owner, repo, _num = m.groups()
    provider = ("github" if host.startswith("github")
                else "gitlab" if host.startswith("gitlab") else "gitcode")
    return f"{provider}:{owner}/{repo}"


def _issue_number_from_issue_url(issue_url: str) -> str:
    """issue_url → issue number 字符串；无法解析返回 ''。"""
    m = _EXTERNAL_ISSUE_URL_RE.match((issue_url or "").strip())
    return m.group(4) if m else ""


def _provider_module(provider: str):
    """provider 模块解析（与 feedback_issue_converter 同源取 src.vcs.providers，
    但不反向 import converter 私有实现，避免双向耦合，Issue #673 QA 判断项）。"""
    from src.vcs.providers import _MODULES
    mod = _MODULES.get(provider)
    if mod is None:
        raise ValueError(f"Git provider 未支持: {provider}")
    return mod


def _repo_target(repo_key: str):
    """repo_key → (provider, owner, repo)。"""
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    return (provider or "gitcode", owner or "", repo or "")


def _repo_bot_token(repo_key: str):
    """repo_key → 仓库 bot token（provider 模块解析）。"""
    provider, _, _ = _repo_target(repo_key)
    try:
        return _provider_module(provider).bot_token(repo_key) or ""
    except Exception:
        return ""


def _fetch_external_issue_state(repo_key: str, issue_number, token: str) -> str:
    """拉取外部 VCS Issue 的 state（open/closed）；失败/不支持返回 ''（调用方跳过）。"""
    if not repo_key or not issue_number or not token:
        return ""
    import urllib.parse
    provider, owner, repo = _repo_target(repo_key)
    if not owner or not repo:
        return ""
    try:
        mod = _provider_module(provider)
        url = (f"{mod.API_BASE}/repos/{urllib.parse.quote(owner, safe='')}/"
               f"{urllib.parse.quote(repo, safe='')}/issues/{issue_number}")
        resp = mod.request("GET", url, token, None) or {}
        return (resp.get("state") or "").strip().lower()
    except Exception:
        log.exception(f"feedback status sync: fetch external issue failed "
                      f"({repo_key}#{issue_number})")
        return ""


# 外部同步游标（进程内轮转，Issue #673 QA S3：避免固定窗口下先到的外部 open 行
# 永久占位、后续反馈永不轮询）。取满一页则推进到本页最大 id，不足一页回绕到 0。
_external_sync_cursor = 0


def sync_external_feedback_statuses(limit: int = 200) -> int:
    """user 反馈状态联动（Issue #673 QA S1 修复）。

    user 反馈转换后指向外部 GitCode Issue，内部 issue 表无映射（issue_id 为空），
    `sync_feedback_statuses` 的内部 JOIN 无法覆盖 → 这里按 issue_url/issue_number
    拉取外部 Issue 状态联动：closed → closed（只升不降，open 不回退状态）。
    limit 与 `sync_feedback_statuses` 对齐（默认 200）；按 id 游标轮转取窗，
    每轮覆盖全部待同步行的不同分段，避免外部 open 行永久占窗饿死新反馈。
    repo_key 为空的历史行按 issue_url 兜底推导（S4）。
    由 feedback reconcile 周期与内部同步一起调用。
    """
    global _external_sync_cursor
    updated = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT f.id, f.status, f.repo_key, f.issue_number, f.issue_url "
                "FROM user_feedback f "
                "WHERE f.source='user' AND f.issue_url!='' "
                "AND (f.issue_id IS NULL OR f.issue_id=0) AND f.is_deleted=0 "
                "AND f.status NOT IN ('closed') "
                "AND f.id > ? ORDER BY f.id LIMIT ?",
                (_external_sync_cursor, limit))
            rows = c.fetchall()
        if not rows:
            _external_sync_cursor = 0
            return 0
        tokens = {}
        for fid, _cur, repo_key, number, issue_url in rows:
            repo_key = (repo_key or "").strip() or _repo_key_from_issue_url(issue_url)
            number = number or _issue_number_from_issue_url(issue_url)
            if not repo_key or not number:
                continue
            if repo_key not in tokens:
                tokens[repo_key] = _repo_bot_token(repo_key)
            state = _fetch_external_issue_state(repo_key, number, tokens[repo_key])
            if state != "closed":
                continue
            with db_lock:
                db.execute("UPDATE user_feedback SET status=? WHERE id=? AND is_deleted=0",
                           (FEEDBACK_STATUS_CLOSED, fid))
                db.commit()
            updated += 1
        if len(rows) < limit:
            _external_sync_cursor = 0
        else:
            _external_sync_cursor = rows[-1][0]
        if updated:
            log.info(f"feedback status sync (external): {updated} rows updated")
    except Exception:
        log.exception("feedback status sync (external) failed")
    return updated


def sync_feedback_statuses(limit: int = 200) -> int:
    """联动同步：user_feedback.status ← 关联 issue 的 (stage, status)。

    范围：已转换（issue_url 非空）且带内部 issue_id 的反馈；
    规则：推导状态 rank 高于当前状态才更新（只升不降，不覆盖 admin 手工终态）。
    返回更新条数。由 feedback reconcile 周期调用（#673），#22 通知机制复用本函数同一口径。

    边界：source='user' 反馈转换生成的是外部 GitCode Issue（无内部 issue 行），
    由 `sync_external_feedback_statuses` 拉取外部状态联动，不在本 JOIN 范围内。
    """
    updated = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT f.id, f.status, i.stage, i.status FROM user_feedback f "
                "JOIN issue i ON i.id = f.issue_id "
                "WHERE f.issue_url!='' AND f.issue_id IS NOT NULL AND f.is_deleted=0 "
                "ORDER BY f.id LIMIT ?",
                (limit,))
            rows = c.fetchall()
            for fid, cur, stage, task_status in rows:
                derived = derive_feedback_status(stage or "", task_status or "")
                cur = (cur or "").strip().lower() or FEEDBACK_STATUS_OPEN
                if derived == cur:
                    continue
                if FEEDBACK_STATUS_ORDER.get(derived, 0) <= FEEDBACK_STATUS_ORDER.get(cur, 0):
                    continue
                c.execute("UPDATE user_feedback SET status=? WHERE id=?", (derived, fid))
                updated += 1
            db.commit()
        if updated:
            log.info(f"feedback status sync: {updated} rows updated")
    except Exception:
        log.exception("feedback status sync failed")
    return updated
