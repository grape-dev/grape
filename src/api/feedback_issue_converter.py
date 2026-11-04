"""反馈 → Issue 转换器 + reconcile worker（feedback-openapi-expose-design.md §1.3/§四.5/§四.6）。

职责：把待转换（issue_url='')的反馈按 skill.scope 路由到目标仓库并创建 Issue，
成功后回写 issue_url / issue_number / converted_at。

覆盖范围（Issue #673）：
- source='agent'（自动反馈）；
- source='user' 且带仓库上下文（repo_key 非空）的用户反馈——按 repo_key 路由建 Issue 回写，
  使「我的反馈」用户视图同样能拿到关联 Issue 链接；纯意见类（无 repo_key）跳过不转换。

路由规则：
- skill.scope='official' → 官方技能仓库（GRAPE_SKILLS_REPO 配置，以全局 bot token 身份）
- skill.scope='repo'/'user'/source='user' → 反馈所属 repo_key 的 <owner>/<repo>（以仓库 bot token 身份）

幂等：以 (task_id, skill_name, title) 为组，同组只建一个 Issue；组内全部回写同一 issue_url。
失败处理：异常仅记日志、不回写，下轮重试；连续失败可标记 skipped 供 admin 感知。
"""
import os
import asyncio

from src.session_manager import db, db_lock
from src.config import log

# 官方技能仓库（owner 唯一固定、无其它值；无 env 覆盖——反馈目标恒为官方技能仓。
# 与 src/storage/scene_sync.py 同源：grape-dev/grape-skills）
_OFFICIAL_SKILLS_REPO = "grape-dev/grape-skills"
# 项目自身组件反馈标识（CLI / 平台）→ 一律进官方技能仓（official-repo-config-unify-design.md §3.2）
_OFFICIAL_COMPONENT_KINDS = {"cli", "platform"}
# 是否向官方技能仓提交官方技能 / CLI / 平台反馈 Issue；默认允许（1），私有部署可设 0 关闭
GRAPE_FEEDBACK_OFFICIAL_ISSUE = os.environ.get("GRAPE_FEEDBACK_OFFICIAL_ISSUE", "1").strip()

GRAPE_SKILLS_PROVIDER = os.environ.get("GRAPE_SKILLS_PROVIDER", "gitcode").strip() or "gitcode"

FEEDBACK_CATEGORY_OTHER = "other"


def _provider_module(provider: str):
    from src.vcs.providers import _MODULES
    mod = _MODULES.get(provider)
    if mod is None:
        raise ValueError(f"Git provider 未支持: {provider}")
    return mod


def _official_skills_target():
    """官方技能仓目标：返回 (provider, owner, repo, global_bot_token)；未配置返回 None。

    地址硬编码 `_OFFICIAL_SKILLS_REPO`（owner 唯一固定，无 env 覆盖）。
    """
    repo = _OFFICIAL_SKILLS_REPO
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


def _feedback_official_issue_enabled() -> bool:
    """默认允许向官方技能仓提交 official / CLI / platform 反馈；显式 '0'/'false'/'no' 视为关闭。"""
    return GRAPE_FEEDBACK_OFFICIAL_ISSUE.lower() not in ("0", "false", "no")


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
    """取待转换行：issue_url='' AND is_deleted=0，且
    source='agent'，或 source='user' 且带仓库上下文（repo_key 非空，Issue #673）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, repo_key, task_id, agent_type, skill_name, skill_scope, "
            "title, description, fail_summary, event_seq, issue_id, issue_number, feedback_kind, "
            "source, username "
            "FROM user_feedback WHERE issue_url='' AND is_deleted=0 "
            "AND (source='agent' OR (source='user' AND COALESCE(repo_key,'')!='')) "
            "ORDER BY id LIMIT ?",
            (limit,))
        rows = c.fetchall()
    return [{
        "id": r[0], "repo_key": r[1] or '', "task_id": r[2] or '',
        "agent_type": r[3] or '', "skill_name": r[4] or '', "skill_scope": r[5] or '',
        "title": r[6] or '', "description": r[7] or '', "fail_summary": r[8] or '',
        "event_seq": r[9], "issue_id": r[10], "issue_number": r[11] or '',
        "feedback_kind": r[12] or '',
        "source": r[13] or 'agent', "username": r[14] or '',
    } for r in rows]


def convert_pending_feedback(limit: int = 20) -> dict:
    """扫描并转换待处理的反馈（agent + 带上下文的 user），返回统计 {converted, skipped, failed}。"""
    result = {"converted": 0, "skipped": 0, "failed": 0}
    rows = _fetch_pending(limit)
    if not rows:
        return result
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()

    # 幂等组：agent 反馈同 (task_id, skill_name, title) 共用一个 Issue，组内全部回写；
    # user 反馈按行独立（task_id/skill_name 常为空，跨用户同标题不得并组，Issue #673 QA S2）
    groups = {}
    for row in rows:
        if (row.get("source") or "agent") == "user":
            key = ("user", row["id"])
        else:
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
    """路由：
    - 非官方技能（repo/user 技能类）→ 反馈所属仓库（现状语义，私有 bug 不进公开仓）
    - official 技能 / CLI / platform（项目自身组件）→ 官方技能仓（受 GRAPE_FEEDBACK_OFFICIAL_ISSUE，
      默认允许）。关闭时返回空目标 → convert 走 skipped（表内保留，不丢）。
    见 official-repo-config-unify-design.md §2/§3.2。
    """
    scope = (row.get("skill_scope") or "").lower()
    kind = (row.get("feedback_kind") or "").lower()
    # 用户反馈（Issue #673）：一律按反馈所属仓库路由（带 repo_key 才会进入待转换队列），
    # 与 repo/user 技能类同路（语义一致，合并分支避免路由规则膨胀）
    is_user = (row.get("source") or "agent") == "user"
    if is_user or (scope != "official" and kind not in _OFFICIAL_COMPONENT_KINDS):
        provider, owner, repo = _repo_target(row.get("repo_key") or "")
        token = _repo_bot_token(row.get("repo_key") or "")
        return (provider, owner, repo, token)
    if not _feedback_official_issue_enabled():
        log.info(f"feedback→issue: official/CLI/platform feedback skipped "
                 f"(GRAPE_FEEDBACK_OFFICIAL_ISSUE=0) fid={row.get('id')}")
        return ("", "", "", "")
    target = _official_skills_target()
    if target is None:
        return ("", "", "", "")
    return target


def _build_issue_body(row: dict) -> str:
    """Issue 正文组装 + 消毒（长度/空行/URL 化）。

    M-1：正文可能发布到公开仓库（skill.scope=official → GRAPE_SKILLS_REPO），Agent 输出的
    fail_summary / description 可能含内网 IP、绝对路径、凭据串 —— 出转发前统一脱敏
    （docs/weekly-08/feedback-issue-body-redact-m1-design.md）。
    """
    from src.observability.redact_issue_body import redact_issue_text
    if (row.get("source") or "agent") == "user":
        # 用户反馈（Issue #673）：来源/提交人/仓库/事件 + 反馈内容
        lines = [
            "> 来源：用户反馈（source=user）",
            f"- 反馈ID：{row['id']}",
            f"- 提交人：{row.get('username') or '-'}",
            f"- 仓库：{row['repo_key'] or '-'}",
            f"- 事件：{row['event_seq'] or '-'} (issue {row['issue_number'] or '-'})",
            "",
            "## 反馈内容",
        ]
        desc = (row.get("description") or "").strip()
        if desc:
            lines.append(redact_issue_text(desc, 2000))
        return "\n".join(lines)
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

def _sync_statuses():
    """reconcile 周期内的反馈状态联动同步（导入放函数内避免循环依赖风险）。

    内部 issue 映射同步 + user 反馈外部 Issue 状态拉取（Issue #673 QA S1）。
    """
    try:
        from src.api.feedback_status import (
            sync_feedback_statuses,
            sync_external_feedback_statuses,
        )
        sync_feedback_statuses()
        sync_external_feedback_statuses()
    except Exception:
        log.exception("feedback status sync in reconcile failed")


async def feedback_reconcile_loop(interval: float = 300.0):
    """周期性扫描转换（与 skill-sync serial worker 同套路：分布式锁 + 崩溃安全）。"""
    from src.runtime.task_lock import run_with_distributed_lock
    _fail = 0
    while True:
        try:
            async with run_with_distributed_lock("feedback_reconcile", ttl=300):
                await asyncio.to_thread(convert_pending_feedback, 50)
                # 状态联动同步（Issue #673：反馈状态随关联 issue 推进更新，与 #22 共用口径）
                await asyncio.to_thread(_sync_statuses)
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