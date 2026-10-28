"""反馈 → Issue 转换器 + reconcile worker（feedback-openapi-expose-design.md §1.3/§四.5/§四.6）。

职责：把 `source='agent'` 且未转换（issue_url=''）的反馈，按 skill.scope 路由到目标仓库并创建 Issue，
成功后回写 issue_url / issue_number / converted_at。

路由规则：
- skill.scope='official' → 官方技能仓库（GRAPE_SKILLS_REPO 配置，以全局 bot token 身份）
- skill.scope='repo'/'user' → 反馈所属 repo_key 的 <owner>/<repo>（以仓库 bot token 身份）

幂等：以 (task_id, skill_name, title) 为组，同组只建一个 Issue；组内全部回写同一 issue_url。
失败处理：异常仅记日志、不回写，下轮重试；连续失败可标记 skipped 供 admin 感知。
"""
import os
import asyncio

from src.session_manager import db, db_lock
from src.config import log

# 官方技能仓库（平台配置，格式 <owner>/<repo>；缺省空 = 官方反馈不转换，标记 skipped）
GRAPE_SKILLS_REPO = os.environ.get("GRAPE_SKILLS_REPO", "").strip()
# 官方技能仓库 provider（默认 gitcode；github 可配置）
GRAPE_SKILLS_PROVIDER = os.environ.get("GRAPE_SKILLS_PROVIDER", "gitcode").strip() or "gitcode"

FEEDBACK_CATEGORY_OTHER = "other"


def _provider_module(provider: str):
    from src.vcs.providers import _MODULES
    mod = _MODULES.get(provider)
    if mod is None:
        raise ValueError(f"Git provider 未支持: {provider}")
    return mod


def _official_skills_target():
    """官方技能仓库目标：返回 (provider, owner, repo, global_bot_token)；未配置返回 None。"""
    repo = GRAPE_SKILLS_REPO
    if not repo or "/" not in repo:
        return None
    owner, rname = repo.split("/", 1)
    if not owner or not rname:
        return None
    mod = _provider_module(GRAPE_SKILLS_PROVIDER)
    try:
        if GRAPE_SKILLS_PROVIDER == "github":
            from src.user.db import get_global_github_bot_token
            token = get_global_github_bot_token()
        else:
            from src.user.db import get_global_gitcode_bot_token
            token = get_global_gitcode_bot_token()
    except Exception:
        token = ""
    return (GRAPE_SKILLS_PROVIDER, owner, rname, token)


def _repo_target(repo_key: str):
    """反馈所属仓库目标：返回 (provider, owner, repo)。"""
    from src.repo_key import parse_repo_key, split_owner_repo
    provider, _ = parse_repo_key(repo_key) or ("gitcode", "")
    provider = provider or "gitcode"
    owner, repo = split_owner_repo(repo_key)
    return (provider, owner or "", repo or "")


def _repo_bot_token(repo_key: str):
    """反馈所属仓库 bot token（provider 模块解析）。"""
    from src.repo_key import parse_repo_key
    provider, _ = parse_repo_key(repo_key) or ("gitcode", "")
    provider = provider or "gitcode"
    try:
        return _provider_module(provider).bot_token(repo_key) or ""
    except Exception:
        return ""


def _create_provider_issue(provider: str, owner: str, repo: str, token: str,
                           title: str, body: str) -> dict:
    """调用 git provider 创建 Issue，返回 {number, url}。

    provider 调用方式与 git_repo_api 一致（provider 中性翻译 + 原生 URL + bot token），
    但走服务端身份（bot token），不走 session_only 透传端点。
    """
    import urllib.parse
    from src.vcs.providers import translate_inbound
    mod = _provider_module(provider)
    url = f"{mod.API_BASE}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/issues"
    neutral = {"title": title, "body": body, "assignee": ""}
    payload = translate_inbound(provider, "issues", "create", neutral,
                                {"owner_repo": f"{owner}/{repo}"})
    resp = mod.request("POST", url, token, payload)
    number = resp.get("number") or resp.get("iid") or 0
    issue_url = _issue_url(provider, owner, repo, number)
    return {"number": number, "url": issue_url}


def _issue_url(provider: str, owner: str, repo: str, number) -> str:
    """拼 Issue 公开页 URL（与 issues_center._vcs_issue_url 同构）。"""
    if not number:
        return ""
    if provider == "github":
        return f"https://github.com/{owner}/{repo}/issues/{number}"
    if provider == "gitlab":
        return f"https://gitlab.com/{owner}/{repo}/-/issues/{number}"
    return f"https://gitcode.com/{owner}/{repo}/issues/{number}"


def _fetch_pending(limit: int):
    """取待转换行：source='agent' AND issue_url='' AND is_deleted=0。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, repo_key, task_id, agent_type, skill_name, skill_scope, "
            "title, description, fail_summary, event_seq, issue_id, issue_number "
            "FROM user_feedback WHERE source='agent' AND issue_url='' AND is_deleted=0 "
            "ORDER BY id LIMIT ?",
            (limit,))
        rows = c.fetchall()
    return [{
        "id": r[0], "repo_key": r[1] or '', "task_id": r[2] or '',
        "agent_type": r[3] or '', "skill_name": r[4] or '', "skill_scope": r[5] or '',
        "title": r[6] or '', "description": r[7] or '', "fail_summary": r[8] or '',
        "event_seq": r[9], "issue_id": r[10], "issue_number": r[11] or '',
    } for r in rows]


def convert_pending_feedback(limit: int = 20) -> dict:
    """扫描并转换待处理的 agent 反馈，返回统计 {converted, skipped, failed}。"""
    result = {"converted": 0, "skipped": 0, "failed": 0}
    rows = _fetch_pending(limit)
    if not rows:
        return result
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()

    # 幂等组：同 (task_id, skill_name, title)，只建一个 Issue，组内全部回写
    groups = {}
    for row in rows:
        key = (row["task_id"], row["skill_name"], row["title"])
        groups.setdefault(key, []).append(row)

    for key, group in groups.items():
        first = group[0]
        fid = first["id"]
        try:
            provider, owner, repo, token = _resolve_target(first)
        except Exception as e:
            log.warning(f"feedback→issue: skip fid={fid} (target resolve failed): {e}")
            result["skipped"] += 1
            continue
        if not owner or not repo:
            result["skipped"] += 1
            continue
        if not token:
            # 无 bot token：官方仓库无全局 token / 仓库未托管 → 本轮跳过（下轮重试）
            log.warning(f"feedback→issue: no bot token for fid={fid} (route={provider}:{owner}/{repo})")
            result["skipped"] += 1
            continue
        body = _build_issue_body(first)
        try:
            issue = _create_provider_issue(provider, owner, repo, token, first["title"], body)
        except Exception as e:
            result["failed"] += 1
            log.warning(f"feedback→issue: create failed fid={fid}: {e}")
            continue
        number = issue.get("number")
        url = issue.get("url") or _issue_url(provider, owner, repo, number)
        with db_lock:
            c = db.cursor()
            for row in group:
                c.execute(
                    "UPDATE user_feedback SET issue_url=?, issue_number=?, converted_at=? "
                    "WHERE id=? AND issue_url=''",
                    (url, number, _now, row["id"]))
            db.commit()
        result["converted"] += 1
        log.info(f"feedback→issue: converted fid={fid} -> {provider}:{owner}/{repo}#{number}")
    return result


def _resolve_target(row):
    """路由：official → 官方技能仓库；repo/user → 反馈所属仓库。返回 (provider, owner, repo, token)。"""
    scope = row.get("skill_scope") or ""
    if scope == "official":
        target = _official_skills_target()
        if target is None:
            return ("", "", "", "")
        provider, owner, repo, token = target
        return (provider, owner, repo, token)
    provider, owner, repo = _repo_target(row.get("repo_key") or "")
    token = _repo_bot_token(row.get("repo_key") or "")
    return (provider, owner, repo, token)


def _build_issue_body(row: dict) -> str:
    """Issue 正文组装 + 消毒（长度/空行/URL 化）。

    M-1：正文可能发布到公开仓库（skill.scope=official → GRAPE_SKILLS_REPO），Agent 输出的
    fail_summary / description 可能含内网 IP、绝对路径、凭据串 —— 出转发前统一脱敏
    （docs/weekly-08/feedback-issue-body-redact-m1-design.md）。
    """
    from src.observability.redact_issue_body import redact_issue_text
    lines = [
        f"> 来源：Agent 自动反馈（source=agent）",
        f"- 反馈ID：{row['id']}",
        f"- 仓库：{row['repo_key'] or '-'}",
        f"- 任务：{row['task_id'] or '-'}",
        f"- Agent：{row['agent_type'] or '-'}",
        f"- 技能：{row['skill_name'] or '-'}（scope={row['skill_scope'] or '-'}）",
        f"- 事件：{row['event_seq'] or '-'} (issue {row['issue_number'] or '-'})",
        "",
        "## 反馈内容",
    ]
    desc = (row.get("description") or "").strip()
    if desc:
        lines.append(redact_issue_text(desc, 2000))
    summary = (row.get("fail_summary") or "").strip()
    if summary:
        lines += ["", "## 失败摘要", "```", redact_issue_text(summary, 2000), "```"]
    return "\n".join(lines)


# ===================== reconcile worker =====================

async def feedback_reconcile_loop(interval: float = 300.0):
    """周期性扫描转换（与 skill-sync serial worker 同套路：分布式锁 + 崩溃安全）。"""
    from src.runtime.task_lock import run_with_distributed_lock
    _fail = 0
    while True:
        try:
            async with run_with_distributed_lock("feedback_reconcile", ttl=300):
                await asyncio.to_thread(convert_pending_feedback, 50)
            _fail = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            _fail += 1
            if _fail >= 3:
                log.error(f"feedback reconcile: {_fail} consecutive failed cycles")
                log.exception("feedback reconcile run failed")
            else:
                log.exception("feedback reconcile run failed")
        await asyncio.sleep(interval)


_bg_tasks = set()


def start_feedback_reconcile_scheduler():
    """启动反馈→Issue reconcile 调度（与 skill-sync scheduler 同款）。"""
    interval = max(float(os.environ.get("FEEDBACK_RECONCILE_INTERVAL", "300") or 300), 60)
    t = asyncio.create_task(feedback_reconcile_loop(interval), name="feedback-reconcile")
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    log.info(f"feedback reconcile scheduler started (interval={interval}s)")


async def stop_feedback_reconcile_scheduler():
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    log.info("feedback reconcile scheduler stopped")