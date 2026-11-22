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
失败处理：异常仅记日志、不回写，下轮重试；连续失败轮次持久化到
`user_feedback.convert_fail_count`（Issue #3731），达阈值（默认 3，`FEEDBACK_CONVERT_FAIL_ESCALATE`）
升级 ERROR 供 admin 感知，建单成功后清零。
"""

import os

import asyncio
import time
import uuid

from src.domains.mgmt import public as _mgmt_public

from src.kernel.config import log

# Issue #4064：去重探测日志的脱敏出口（与 VcsApiClient 共用同一 helper）
from src.domains.runtime.vcs.redact import redact_text

# 官方技能仓库（owner 唯一固定、无其它值；无 env 覆盖——反馈目标恒为官方技能仓。
# 与 src/domains/runtime/storage/scene_sync.py 同源：grape-dev/grape-skills）
_OFFICIAL_SKILLS_REPO = "grape-dev/grape-skills"

# 项目自身组件反馈标识（CLI / 平台）→ 一律进官方技能仓（official-repo-config-unify-design.md §3.2）
_OFFICIAL_COMPONENT_KINDS = {"cli", "platform"}

# 是否向官方技能仓提交官方技能 / CLI / 平台反馈 Issue；默认允许（1），私有部署可设 0 关闭
GRAPE_FEEDBACK_OFFICIAL_ISSUE = os.environ.get("GRAPE_FEEDBACK_OFFICIAL_ISSUE", "1").strip()

GRAPE_SKILLS_PROVIDER = os.environ.get("GRAPE_SKILLS_PROVIDER", "gitcode").strip() or "gitcode"

FEEDBACK_CATEGORY_OTHER = "other"

def _provider_module(provider: str):
    from src.domains.runtime.vcs.providers import _MODULES
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
        # S87/D63：统一 get_global_bot_token_for（github 文件 fallback 语义保留）
        from src.domains.mgmt.user.db import get_global_bot_token_for
        token = get_global_bot_token_for(GRAPE_SKILLS_PROVIDER)
    except Exception:
        token = ""
    return (GRAPE_SKILLS_PROVIDER, owner, rname, token)

def _feedback_official_issue_enabled() -> bool:
    """默认允许向官方技能仓提交 official / CLI / platform 反馈；显式 '0'/'false'/'no' 视为关闭。"""
    return GRAPE_FEEDBACK_OFFICIAL_ISSUE.lower() not in ("0", "false", "no")

def _repo_target(repo_key: str):
    """反馈所属仓库目标：返回 (provider, owner, repo)。

    Issue #3731：`parse_repo_key()` 契约是三元组，(provider, _) 二元解包必抛
    `ValueError: too many values to unpack`；一次全解包同时拿到 owner/repo，
    不再用 `split_owner_repo()` 二次解析（两处口径分叉正是本 bug 成因）。
    空键时 parse_repo_key 已返回 ("gitcode","","")，保留 `or "gitcode"` 默认语义。
    """
    from src.kernel.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    return (provider or "gitcode", owner or "", repo or "")

def _repo_bot_token(repo_key: str):
    """反馈所属仓库 bot token（provider 模块解析，provider 取自三元组首段）。"""
    from src.kernel.repo_key import parse_repo_key
    provider = parse_repo_key(repo_key)[0] or "gitcode"
    try:
        return _provider_module(provider).bot_token(repo_key) or ""
    except Exception:
        return ""

def _create_provider_issue(provider: str, owner: str, repo: str, token: str,
                           title: str, body: str) -> dict:
    """调用 git provider 创建 Issue，返回 {number, url}。

    provider 调用方式与 git_repo_api 一致（provider 中性翻译 + 原生 URL + bot token），
    但走服务端身份（bot token），不走 session_only 透传端点。

    Issue #3731：平台标题上限 200 字符（超限直接 400 `out of max length`，
    该反馈随之每轮必败永久卡死），投递前统一按 `_clip_title()` 截断。
    """
    import urllib.parse
    from src.domains.runtime.vcs.providers import translate_inbound
    mod = _provider_module(provider)
    url = f"{mod.API_BASE}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/issues"
    neutral = {"title": _clip_title(title), "body": body, "assignee": ""}
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
    # D16/S26：host/路径统一从平台卡派生（新平台自动正确）
    from src.kernel.repo_key import issue_web_url
    return issue_web_url(f"{provider}:{owner}/{repo}", number)

_CLAIM_PREFIX = "pending:"
_CLAIM_TTL = 600
_CLAIM_ON = os.environ.get("FEEDBACK_CLAIM", "1") != "0"
_DEDUP_ON = os.environ.get("FEEDBACK_TITLE_DEDUP", "1") != "0"

# 平台标题上限（取保守上限 200 字符：GitCode 实测 400 `out of max length`；GitHub/GitLab
# 亦在该量级，未逐一考证——截断只是防 400 兜底，不是精确契约）。
_MAX_TITLE_LEN = 200
_TITLE_ELLIPSIS = "…"


def _clip_title(title) -> str:
    """按平台标题上限截断（Issue #3731 根因 B）。

    超限时截掉尾部并补一个省略号，保证 `len(结果) <= _MAX_TITLE_LEN`：
    建单侧与查重侧共用本函数，截断后同键才能命中复用，不会重复建单。
    """
    t = (title or "").strip()
    if len(t) <= _MAX_TITLE_LEN:
        return t
    return t[: _MAX_TITLE_LEN - len(_TITLE_ELLIPSIS)] + _TITLE_ELLIPSIS


def _norm_title(t):
    """标题归一：去全部空白 + 全角括号转半角 + 小写，用于跨任务/跨来源的同名单比对。"""
    return "".join((t or "").split()).replace("（", "(").replace("）", ")").lower()


def _claim_value():
    return f"{_CLAIM_PREFIX}{int(time.time())}:{uuid.uuid4().hex[:8]}"


def _claim(row_id) -> bool:
    """抢先占位：只有一个 worker 能把该行空 issue_url 改成占位值（共享库下原子）。"""
    if not _CLAIM_ON:
        return True
    return _mgmt_public.feedback_claim_row(_claim_value(), row_id)


def _release(row_id) -> None:
    """建单失败时清掉占位，留给下一轮重试（少了这步该反馈会永久卡死）。"""
    _mgmt_public.feedback_release_claim(row_id, _CLAIM_PREFIX)


def _reclaim_stale_claims() -> int:
    """把进程崩溃 / 超时留下的过期占位清回待转换状态。"""
    now = int(time.time())
    stale = []
    for rid, url in _mgmt_public.feedback_list_claims(_CLAIM_PREFIX):
        try:
            ts = int((url or "").split(":")[1])
        except Exception:
            ts = 0
        if ts == 0 or ts < now - _CLAIM_TTL:
            stale.append(rid)
    for rid in stale:
        _mgmt_public.feedback_release_claim(rid, _CLAIM_PREFIX)
    if stale:
        log.warning(f"feedback→issue: reclaimed {len(stale)} stale claim(s)")
    return len(stale)


# 连续失败升级 ERROR 的默认阈值（Issue #3731：每 tick 一条 WARNING 会淹没真实故障）
_FAIL_ESCALATE_DEFAULT = 3

def _fail_escalate_at() -> int:
    """连续失败升级阈值（`FEEDBACK_CONVERT_FAIL_ESCALATE`，默认 3，最小 1）。"""
    try:
        return max(int(os.environ.get("FEEDBACK_CONVERT_FAIL_ESCALATE", "")
                       or _FAIL_ESCALATE_DEFAULT), 1)
    except Exception:
        return _FAIL_ESCALATE_DEFAULT


def _fail_is_escalation(times: int, prev: int) -> bool:
    """本轮是否要发 ERROR：只在「跨越阈值」那一轮发（Issue #3731 QA Required）。

    - 达阈值前（times < threshold）：WARNING；
    - 恰好跨过阈值那一轮（prev < threshold <= times）：ERROR（admin 感知点）；
    - 其后（prev >= threshold，即上一轮已在 ERROR 区间）：静默回 WARNING，
      避免单行永久卡死时每轮一条 ERROR（阈值 3 + interval 300s → 288 条/天），
      与「消除 20.7 万条噪声」的目标同向（计数仍照常落库可查）。
    """
    threshold = _fail_escalate_at()
    if times < threshold:
        return False
    if prev < threshold:
        return True
    # 上一轮已在 ERROR 区间（prev >= threshold）：静默 WARNING（2 的幂降采样留作
    # 后续调整，本期阈值后不再逐轮 ERROR，直接 False，保证「只在跨越那轮报一次」）
    return False


def _note_failure(fid, reason: str, provider: str = "", owner: str = "",
                  repo: str = "", prev_count: int = 0) -> None:
    """记一次转换失败：持久化连续轮次，跨阈值那轮升级 ERROR（Issue #3731）。

    - 计数落 `user_feedback.convert_fail_count`（跨 worker 共享库可见、admin 可查），
      写库失败不影响主流程（只丢可观测性，下一轮照常重试）；
    - ERROR 只在「上一轮还未达阈值、本轮恰好达阈值」时发一次（`_fail_is_escalation`），
      其后各轮回 WARNING，不逐轮刷屏；
    - 日志保留原有可检索子串（`target resolve failed` / `create failed`），
      Grafana 现有查询口径不变。

    `prev_count` 为本轮自增**前**的持久化计数（调用方从 `_fetch_pending` 读回的
    `convert_fail_count`），仅用于判定「跨越阈值」；拿不到（如首轮并发）按 0 处理。
    """
    try:
        times = _mgmt_public.feedback_bump_convert_fail(fid)
    except Exception as e:
        times = 0
        log.warning(f"feedback→issue: failure counter bump failed fid={fid}: {e}")
    threshold = _fail_escalate_at()
    route = f"{provider}:{owner}/{repo}" if owner and repo else "-"
    msg = (f"feedback→issue: {reason} "
           f"(fid={fid}, route={route}, consecutive={times}/{threshold})")
    if _fail_is_escalation(times, prev_count):
        log.error(msg)
    else:
        log.warning(msg)


def _find_existing_open_issue(provider, owner, repo, token, title):
    """目标仓里已存在同标题的开放 issue 就复用，返回单号或 None。

    跨机各库时这是唯一有效的防重复手段（目标仓是共享的，占位只在本库可见）。
    翻页翻到短页为止（上限 10 页），避免固定页数漏掉更早的同标题单。
    """
    if not _DEDUP_ON:
        return None
    import urllib.parse
    from src.domains.runtime.vcs.providers import translate_query_state
    mod = _provider_module(provider)
    # 查重键与建单侧同一入参（截断后标题），否则截断后长标题还会重复建单（Issue #3731）
    want = _norm_title(_clip_title(title))
    # 开放态参数按平台卡方言取值（中性 open → GitCode/GitLab opened，GitHub/Gitee open；
    # S87：不用平台字面量比较，与 git_repo_api.list_issues 同一口径）
    _state = translate_query_state(provider, "open")
    for page in range(1, 11):
        url = (f"{mod.API_BASE}/repos/{urllib.parse.quote(owner, safe='')}/"
               f"{urllib.parse.quote(repo, safe='')}/issues"
               f"?state={_state}&per_page=100&page={page}"
               f"&access_token={urllib.parse.quote(token, safe='')}")
        try:
            items = mod.request("GET", url, "")
        except Exception as e:
            # Issue #4064：异常串常回带含 access_token 的 URL，日志出口统一脱敏
            log.warning(f"feedback→issue: title probe failed {provider}:{owner}/{repo}: "
                        f"{redact_text(e)}")
            return None
        if not isinstance(items, list):
            return None
        for it in items:
            if not isinstance(it, dict):
                continue
            if _norm_title(it.get("title")) == want:
                return it.get("number")
        if len(items) < 100:
            break
    return None


def _fetch_pending(limit: int):
    """取待转换行：issue_url='' AND is_deleted=0，且
    source='agent'，或 source='user' 且带仓库上下文（repo_key 非空，Issue #673）。

    SQL 与入选口径都在 mgmt owner 域（`user_feedback` 归 mgmt，本文件属 api 进程）：
    `feedback_pending_conversions` 走任意拓扑原语，单库 / 三库同一份语句。
    """
    _reclaim_stale_claims()
    rows = _mgmt_public.feedback_pending_conversions(limit)
    return [{
        "id": r[0], "repo_key": r[1] or '', "task_id": r[2] or '',
        "agent_type": r[3] or '', "skill_name": r[4] or '', "skill_scope": r[5] or '',
        "title": r[6] or '', "description": r[7] or '', "fail_summary": r[8] or '',
        "event_seq": r[9], "issue_id": r[10], "issue_number": r[11] or '',
        "feedback_kind": r[12] or '',
        "source": r[13] or 'agent', "username": r[14] or '',
        "convert_fail_count": int(r[15] or 0),
    } for r in rows]

def _feedback_routeable(row, provider: str, owner: str, repo: str) -> bool:
    """该行本轮是否「可路由」：区分策略跳过 / 不可路由与真失败（Issue #3731 QA Required）。

    - 官方目标为空（`GRAPE_FEEDBACK_OFFICIAL_ISSUE=0` 或官方仓未配置）→ 策略跳过，
      不是失败（Spec Out of Scope：开关关闭 = 有意不建单，行保留待开关打开）；
    - repo/user 技能类或 user 反馈走仓库路由，但行上 `repo_key` 为空 → 不可路由
      数据缺失（入选口径对 source='agent' 不看 repo_key，这类行永远解析不出
      owner/repo），计连败只会永久 ERROR 刷屏——同样只跳过、不计数。
    其余（路由解析出了目标）走正常建单链路，失败才计连败。
    """
    scope = (row.get("skill_scope") or "").lower()
    kind = (row.get("feedback_kind") or "").lower()
    is_user = (row.get("source") or "agent") == "user"
    to_repo = is_user or (scope != "official" and kind not in _OFFICIAL_COMPONENT_KINDS)
    if to_repo:
        # 仓库路由：目标必须由行上的 repo_key 解出（缺 repo_key = 不可路由）
        if not (row.get("repo_key") or "").strip():
            log.info(f"feedback→issue: fid={row.get('id')} unroutable "
                     f"(empty repo_key), skip without counting")
            return False
        return bool(owner and repo)
    # 官方路由：空目标 = 策略关闭/未配置（_resolve_target 已记过 info 日志）
    return bool(owner and repo)


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
        prev_count = int(first.get("convert_fail_count") or 0)
        try:
            provider, owner, repo, token = _resolve_target(first)
        except Exception as e:
            # 路由解析异常（如 parse_repo_key 解包崩溃）是真失败，计入连败，达阈值升级 ERROR
            _note_failure(fid, f"target resolve failed: {e}", prev_count=prev_count)
            result["skipped"] += 1
            continue
        if not _feedback_routeable(first, provider, owner, repo):
            # 策略跳过 / 不可路由（GRAPE_FEEDBACK_OFFICIAL_ISSUE=0 的官方目标、
            # 或 agent 反馈缺 repo_key）：不是转换失败，不计连败、不升级 ERROR，
            # 保持 Spec「Out of Scope：不改开关语义」（Issue #3731 QA Required）。
            result["skipped"] += 1
            continue
        if not token:
            # 无 bot token：官方仓库无全局 token / 仓库未托管 → 本轮跳过（下轮重试）
            _note_failure(fid, "no bot token", provider, owner, repo,
                          prev_count=prev_count)
            result["skipped"] += 1
            continue
        # ① 抢先占位：抢不到说明别的 worker 正在处理这一行，本轮跳过
        if not _claim(fid):
            log.info(f"feedback→issue: fid={fid} claimed by another worker, skip")
            result["skipped"] += 1
            continue

        # ② 目标仓已有同标题开放单就复用（跨任务、跨人的同一毛病不再各建一条）
        existing = _find_existing_open_issue(provider, owner, repo, token, first["title"])
        if existing:
            url = _issue_url(provider, owner, repo, existing)
            _mgmt_public.feedback_mark_converted_from_claim(
                [(url, existing, _now, row["id"]) for row in group], _CLAIM_PREFIX)
            result["converted"] += 1
            log.info(f"feedback→issue: reused existing #{existing} for fid={fid}")
            continue

        body = _build_issue_body(first)
        try:
            issue = _create_provider_issue(provider, owner, repo, token, first["title"], body)
        except Exception as e:
            _release(fid)  # ③ 失败必须清占位，否则该反馈永久卡死
            result["failed"] += 1
            _note_failure(fid, f"create failed: {e}", provider, owner, repo,
                          prev_count=prev_count)
            continue
        number = issue.get("number")
        url = issue.get("url") or _issue_url(provider, owner, repo, number)
        # 组内多行同一事务回写（同组共用同一个 Issue，半批会让同组行分属不同 Issue）。
        written = _mgmt_public.feedback_mark_converted_from_claim(
            [(url, number, _now, row["id"]) for row in group], _CLAIM_PREFIX)
        if written < len(group):
            # 守卫 `issue_url=''` 未命中 = 并发进程已回写该行；统计口径仍按组 +1（原口径）
            log.info(f"feedback→issue: group fid={fid} guarded "
                     f"({written}/{len(group)} rows updated)")
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
    from src.domains.insight.observability.redact_issue_body import redact_issue_text
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
        # 外部拉取单独限批：单请求 30s 超时，20×30s=600s 仍小于 feedback_status_sync 的
        # 900s 锁 TTL（架构审查 M-4：原 200 条最坏 6000s，锁先过期导致并发重入）。
        sync_external_feedback_statuses(limit=20)
    except Exception:
        log.exception("feedback status sync in reconcile failed")

async def feedback_reconcile_loop(interval: float = 300.0):
    """周期性扫描转换（与 skill-sync serial worker 同套路：分布式锁 + 崩溃安全）。

    Issue #3742：锁 CM 不跳过 body，两段都必须消费 `acquired` 跳过本轮，
    否则多实例部署下每个实例各转换一遍、各拉一遍外部 Issue 状态。
    """
    from src.domains.runtime.engine.task_lock import run_with_distributed_lock
    _fail = 0
    while True:
        try:
            async with run_with_distributed_lock("feedback_reconcile", ttl=300) as acquired:
                if acquired:
                    await asyncio.to_thread(convert_pending_feedback, 50)
                else:
                    log.debug("feedback reconcile: skipped, another instance holds the lock")
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
        # 状态联动同步走独立的锁与独立预算：外部 Issue 状态拉取是慢路径
        # （单请求 timeout=30s），放在 reconcile 的 300s 锁内会把锁撑过期，
        # 导致其它进程重入并重复发起外部请求（架构审查 M-4）。
        try:
            async with run_with_distributed_lock("feedback_status_sync", ttl=900) as acquired:
                if acquired:
                    await asyncio.to_thread(_sync_statuses)
                else:
                    log.debug("feedback status sync: skipped, another instance holds the lock")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("feedback status sync run failed")
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
