"""Autopilot 定时调度引擎 v3.0

重构要点（v3.0）：
- 不再创建 Issue 触发完整流水线，改为直接执行单个 agent
- 不走 run_pipeline / build_devops_agent_prompt / 事件系统
- prompt 在 _build_autopilot_prompt 中直接构建（巡检指令 + findings 格式模板）
- 底层复用 pick_engine / get_agent_workdir / _materialize_direct_workdir / stream_devops_agent
- 执行结果独立存储在 autopilot_executions，不写 issue 表 / events 表
- agent 产出中解析问题列表(findings)，用户可手动派发为真实 Issue 走完整流水线
- event_seq 用 900_000_000 + autopilot_executions.id（高位命名空间，避开真实 events 序号；见 _autopilot_event_seq）
"""

from __future__ import annotations

import asyncio

import json

import os

import re

import time

from datetime import datetime, timezone, timedelta

from typing import Optional

from src.kernel.config import log

from src.kernel.session_manager import db, db_lock

_SCHEDULER_INTERVAL = 60  # 秒：调度器扫描间隔

_BASE_RETRY_DELAY = 10    # 秒：指数退避基数

_MAX_RETRY_DELAY = 300    # 秒：退避上限

# Issue #3897：失败 exec 的 cause 里 agent 输出尾行截断长度（单行日志可读、可 grep）
_AUTOPILOT_CAUSE_TAIL_MAX = 300

_RUNNING_TASKS: set[int] = set()  # 内存互斥锁：正在执行的 task_id 集合

# ── Issue #3800：环境性失败（绑定引擎离线/悬空）分类 + 退避 + 熔断 ────────────
#
# 缺陷：任务绑定的引擎全部离线时，健康过滤为空的路径直接写 failed 返回，而
# `_execute_task` 之后无条件推进 next_run_at —— 60s cron 下形成
# 「建 exec → 秒失败 → 再建一条」的空转（生产实测 1 小时 60 条 failed exec +
# 60+ 条 ERROR/WARNING，且没有任何告警）。
# 修复：环境性失败单独归类，走「指数退避 → 连续同因熔断 → 一次性告警 → 恢复自愈」。
ERROR_ENGINE_OFFLINE = "ENGINE_OFFLINE"   # 绑定引擎全部离线（环境性，重试无意义）
ERROR_BINDING_STALE = "BINDING_STALE"     # 绑定 uid/id 在 agent_runtimes 中查无此行（需重绑）
_ENV_FAILURE_CODES = (ERROR_ENGINE_OFFLINE, ERROR_BINDING_STALE)

# ── Issue #3963：repo-dispatch 租约争用（同仓 enqueue busy）归类为「瞬态」──────────
#
# 语义与 `_ENV_FAILURE_CODES` 严格分开：环境性失败要 60s 起指数退避 + 连续 3 次熔断
# （任务停用），而 busy 是**秒级**争用，等/重试几秒即可。若复用环境通道，会把一次
# 排队冲突升级成任务被暂停，恰是本单要消灭的「定时任务丢失」。
# 瞬态通道：独立预算（不消耗 max_retry 的 attempt）、短退避 1/2/4s、预算耗尽 requeue
# 下一 tick（execution 记 skipped + error_code，不置 failed、不推进失败计数）。
# 复审（#3963 Optional 落地）：错误码与文案指纹改为从 service 共享（同源防漂移）——
# service 抛出方（RepoDispatchBusyError）与本模块判定方各自维护字面量时，文案
# 漂移会让「存量库缺 error_code 列」的指纹回推静默失效。本地名保持不变，引用零改动。
from src.domains.runtime.engine.service import (
    REPO_DISPATCH_BUSY as ERROR_REPO_DISPATCH_BUSY,
    REPO_DISPATCH_BUSY_LOG_FINGERPRINT as _BUSY_LOG_FINGERPRINT)
_BUSY_RETRY_BUDGET = 3                                 # 同 tick 内的瞬态重试次数
_BUSY_RETRY_DELAYS = (1, 2, 4)                         # 秒：短退避基数
_BUSY_RETRY_JITTER_RATIO = 0.2                         # 抖动：避免多实例同刻齐抢
_BACKOFF_CAP_SECONDS = 1800               # 退避封顶 30min
_BACKOFF_MIN_BASE_SECONDS = 60            # 退避基数下限（cron 周期更短时按 60s 起算）
_CIRCUIT_THRESHOLD = 3                    # 连续同因失败 → 熔断
_CIRCUIT_MIN_OFFLINE_SECONDS = 300        # 熔断附加门槛：离线持续 ≥5min（防健康抖动误停）
# 既有 Loki 查询按该指纹聚合（生产 Grafana：`|= "all offline (Issue #259)"`），
# ERROR 行内必须保留这段字面子串——改名会让历史看板/告警规则失明。
_OFFLINE_LOG_FINGERPRINT = "all offline (Issue #259)"
# Issue #3827：`autopilot_executions.error_code` 落库失败（存量库缺列 1054）时的
# 分类兜底。本条与 `_binding_failure_code` 的 BINDING_STALE 文案同源（本模块常量），
# 仅在「列读不到 -> error_code 为空」时按稳定指纹回推分类，不让缺列把退避链路整条断掉。
_STALE_LOG_FINGERPRINT = "not found in agent_runtimes"


def _scheduler_now() -> datetime:
    """调度侧「当前时间」唯一 seam（aware local datetime）。

    cron 计算、退避/熔断判定、运行时间回写全部走这里——假时钟测试只 patch 这一个
    函数，就能把 1 小时的空转窗口压成毫秒级回归（Issue #3800）。
    """
    return datetime.now().astimezone()


# Issue #537：pending_self_report 超时（worker 未自报）→ timeout_failed 兜底阈值；
# zombie 判死离线宽限（停双主/重启 master 的短暂离线不误判 failed）。
# Issue #1071：默认 1800→5400——部署类任务（打包+生产部署全流程约 40-50 分钟）按设计
# 在执行中重启主控，重启瞬间 pending_since 重置，1800s 窗口实际退化为「重启后 30 分钟」，
# 剩余工序经常超 30 分钟被误判 timeout_failed；配合判死前活跃流豁免（见 sweep）双保险。
PENDING_SELF_REPORT_TIMEOUT_SECONDS = int(os.environ.get("PENDING_SELF_REPORT_TIMEOUT_SECONDS", "5400"))

# 「认领了但从没执行」的短阈值（默认 10 分钟）：pending_self_report 且 runtime_session_id 为空，
# 说明引擎会话根本没建起来，等 90 分钟自报窗口毫无意义。2026-11-15 实测：116 上残留的
# 旧代码 agent 抢到任务后不建会话、不产出消息，事件层 sweeper 30 分钟就判 failed，任务却要
# 挂满 90 分钟，需求在「处理中」多躺一小时，且覆盖 sweeper 的终态归因。
PENDING_NO_SESSION_TIMEOUT_SECONDS = int(
    os.environ.get("PENDING_NO_SESSION_TIMEOUT_SECONDS", "600"))

ZOMBIE_OFFLINE_GRACE_SECONDS = int(os.environ.get("ZOMBIE_OFFLINE_GRACE_SECONDS", "300"))

# Issue #1546：派发协程尚未 enqueue_task 落库时回收器已开跑，年轻执行（<120s）本轮跳过回收，
# 避免「回收器抢在真任务落库前判定终态」的竞态；时区解析规则同 _get_exec_agent_task。
# 窗口来源：Issue #1546 中派发链路（pick_engine → enqueue_task → 回写 execution）的落库时延，
# 120s 为留足余量的默认值；不同部署的派发链耗时差异用 env 覆盖，与相邻两个宽限常量口径一致。
ZOMBIE_YOUNG_GRACE_SECONDS = int(os.environ.get("ZOMBIE_YOUNG_GRACE_SECONDS", "120"))

# agent_type 固定为 'issue'，不再由用户选择
_FIXED_AGENT_TYPE = "issue"

# 组织仓库 AI 分析受管任务使用的 skill（与 mgmt.org_analysis.ANALYSIS_SKILL 同值）。
# 跨域不直接 import mgmt 常量，避免 runtime→mgmt 依赖；仅用于 prompt 输出要求判定。
_ORG_ANALYSIS_SKILL = "org-repo-analyzer"


def _autopilot_issue_ref(task_id) -> str:
    """P8-consistency：autopilot 任务（agent_type='issue'）的 issue 统计回链键。

    autopilot 巡检任务没有真实 issue 关联，但 agent_tasks.issue_id 是 dashboard issue
    统计/时间线的分组键（src/domains/insight/routes/dashboard.py:565 按 issue_id IS NOT
    NULL 回链分组）——创建入队时不落 issue_id，失败任务就整体丢出 issue 统计，导致三页面
    数字对不上。本函数在任务创建时即产出确定性合成键（同一 autopilot 任务每次触发相同），
    使直接模式与远端入队模式（含失败路径）的 agent_tasks 行都带回链。

    为什么必须无数字：issue_id 的下游解析链
    - engine/service.py `_issue_num`（re 搜任意数字）→ collect_knowledge_files 按真实
      issue 号过滤成员知识；
    - engine/agent.py / repogit.py worktree 分支派生。
    含数字的合成 id（如 autopilot-12）会被 `_issue_num` 误挂到真实 issue #12。纯字母键使
    `_issue_num`→0、get_issue_scene 查不到场景行（回退 default），worktree 门
    （issue 型非 _GIT_AGENTS）天然短路——合成键只作为统计分组键生效，不驱动任何 issue
    号解析行为。串行锁继续走 issue_session_id（合成会话 autopilot-<id>），与本键无关。
    """
    try:
        n = int(task_id or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("a") + r) + s
    return f"autopilot-{s}"


def _skip_autopilot_for_takeover(repo_key, task, exec_id) -> bool:
    """Issue #3201：接管 active 期间 autopilot 派发同样统一拦截。

    autopilot 巡检任务没有真实 Issue 维度（`_autopilot_issue_ref` 是纯字母统计合成键，
    与真实接管会话的 `#<数字>` 永远不可能命中），因此按**仓库**判定：该仓库存在任一
    active 接管（`active_takeover_in_repo`）即冻结自动派发。命中后执行记录置
    ``skipped``、不建 task、不消耗重试次数，并日志留痕。
    返回 True 表示本次派发已被拦截，调用方须立即 return。
    """
    if not repo_key:
        return False
    try:
        from src.domains.runtime.engine.takeover import active_takeover_in_repo
        sess = active_takeover_in_repo(repo_key)
    except Exception as e:
        log.warning(f"Autopilot exec {exec_id}: takeover guard check failed (allow): {e}")
        return False
    if not sess:
        return False
    log.info(f"Autopilot exec {exec_id}: skipped — takeover {sess.get('id')} active in repo "
             f"{repo_key} for {sess.get('issue_id')} (agent={sess.get('agent_type')})")
    _update_execution_result(exec_id, "skipped", error="接管进行中，自动派发已拦截")
    return True


_FINDINGS_PATTERN = re.compile(
    r'<!--\s*autopilot-findings\s*(.*?)\s*-->',
    re.DOTALL
)

# 宽松匹配：不要求结尾完整 -->，-- 即可（chunk 可能拆断 --> 为 --\n>）
_FINDINGS_PATTERN_LOOSE = re.compile(
    r'<!--\s*autopilot-findings\s*(.*?)(?:\s*-->|\s*--\s*$)',
    re.DOTALL
)

_FINDINGS_FORMAT_TEMPLATE = """\
请在执行结果的末尾输出任务产出列表，用 HTML 注释标记包裹（必须严格按以下格式，不要在标记头或 JSON 的 key 中插入换行符）：

<!-- autopilot-findings
{"result":"ok","summary":"执行结论：整体判定 + 关键产出和需关注事项，2-4 句","findings":[{"id":"f1","title":"问题标题","severity":"high","category":"security","description":"问题描述和建议","suggested_assignee":"coding","suggested_skill":"implementation"}]}
-->

规则：
- 标记头必须是 <!-- autopilot-findings，中间不能有空格或换行
- JSON 必须是单行紧凑格式，key 和 value 之间不要加换行符
- result: ok/warning/error 三档整体判定 —— ok=任务目标达成且无需关注事项；warning=任务完成但存在需要关注的事项；error=任务未能达成目标或发现需立即处理的问题
- summary 是结论性概览：先给整体判定（对应 result 的中文描述），再概括关键产出和需要关注的事项，2-4 句，不要罗列所有 finding
- severity: high/medium/low
- category: security/quality/documentation/performance/other
- findings 为空数组表示未发现问题
- 即使未发现问题也要包含空数组：{"result":"ok","summary":"任务执行完成，未发现问题","findings":[]}"""

def _strip_trailing_garbage(raw: str) -> str:
    """剥离 JSON 尾部非 JSON 残余字符。

    ACP 流式 chunk 边界可能在 --> 前留下 - 或 -- 残片，
    _ensure_closed 只补闭合括号不剥这类垃圾 → json.loads 报 Extra data。
    策略：从尾部往前扫，跳过空白和 - 字符，找到最后一个 } 为止。
    """
    s = raw.rstrip()
    while s and s[-1] in ('-', ' ', '\t', '\n', '\r'):
        s = s[:-1].rstrip()
    return s

def _heal_findings_marker(text: str) -> str:
    """修复 ACP 流式输出在标记头中插入的换行符。

    流式 chunk 边界可能在 <!-- autopilot-findings 中插入 \\n，
    如 <!-- autop\\nilot-findings，导致正则匹配失败。
    """
    idx = text.find('<!-- autop')
    if idx < 0:
        return text
    fidx = text.find('findings', idx)
    if fidx < 0:
        return text
    header_end = fidx + len('findings')
    header = text[idx:header_end]
    if '\n' in header:
        # 标记头被插换行（如 <!-- autop\nilot-findings），拼回去
        text = text[:idx] + header.replace('\n', '') + text[header_end:]
        header_end = idx + len('<!-- autopilot-findings')

    # 修复流式 chunk 边界拆断结尾 --> 的情况（如 --\n> / --\n\n>）：
    # 仅当整个文本找不到完整 --> 时才归一，避免误伤正文里恰好出现的 --\n>
    if '-->' not in text:
        _TAIL_BROKEN = re.compile(r'--[ \t]*\n+[ \t]*>')
        m = _TAIL_BROKEN.search(text, header_end)
        if m:
            text = text[:m.start()] + '-->' + text[m.end():]
    return text

DEDUP_LEVEL_EXACT = "exact_duplicate"

DEDUP_LEVEL_SUSPECTED = "suspected_duplicate"

DEDUP_LEVEL_NOT = "not_duplicate"

DEDUP_LEVELS = (DEDUP_LEVEL_EXACT, DEDUP_LEVEL_SUSPECTED, DEDUP_LEVEL_NOT)

# 写入公开 Issue 描述 / 确认人字段的限长（防 LLM 长文注入公开面）
DEDUP_NOTE_MAX_CHARS = 200

DEDUP_CONFIRMED_BY_MAX_CHARS = 64

def normalize_dedup_level(raw) -> str:
    """判定级别归一（fail-closed）。

    合法枚举原样返回；空串=LLM 未分类（保持既有放行行为）；
    非法/拼错的非空级别一律降级为 suspected_duplicate，杜绝漏发/拼错绕过 409 门禁。
    """
    level = str(raw or "").strip().lower()
    if not level:
        return ""
    return level if level in DEDUP_LEVELS else DEDUP_LEVEL_SUSPECTED

def sanitize_dedup_note(raw) -> str:
    """dedup_note 压平换行 + 限长（写入公开 Issue 描述前调用）。"""
    return " ".join(str(raw or "").split())[:DEDUP_NOTE_MAX_CHARS]

def sanitize_dedup_confirmed_by(raw) -> str:
    """dedup_confirmed_by 压平 + 限长（确认人身份字段，非自由文本）。"""
    return " ".join(str(raw or "").split())[:DEDUP_CONFIRMED_BY_MAX_CHARS]

def dedup_auto_merge_allowed(dedup_level, dedup_confirmed: bool = False) -> bool:
    """并入挂靠门禁：exact 允许；未分类（空）保持既有行为允许；疑似/未知需 owner 确认。"""
    level = normalize_dedup_level(dedup_level)
    if level in ("", DEDUP_LEVEL_NOT, DEDUP_LEVEL_EXACT):
        return True
    return bool(dedup_confirmed)

def parse_findings(agent_output: str) -> dict:
    """从 agent 产出中解析问题列表。

    返回: {"summary": str, "findings": list, "parse_error": str}
    解析失败时 findings 为空列表。
    """
    # 修复 ACP 流式输出在标记头中插入的换行符（如 autop\nilot → autopilot）
    _text = _heal_findings_marker(agent_output or "")

    # 宽松匹配：允许 autopilot 和 -findings 之间有换行/空格，标记头后换行或同行均接受
    # 先尝试严格匹配（结尾 -->），再降级宽松匹配（结尾 --，chunk 可能拆断 --> 为 --\n>）
    match = re.search(
        r'<!--\s*autopilot[\s\-]*findings\s*(.*?)\s*-->',
        _text,
        re.DOTALL
    )
    if not match:
        match = re.search(
            r'<!--\s*autopilot[\s\-]*findings\s*(.*?)(?:\s*-->|\s*--\s*$)',
            _text,
            re.DOTALL
        )
    if not match:
        return {"summary": "", "findings": [], "parse_error": "no findings block found"}

    try:
        raw = match.group(1).strip()
        # 复用 hermes_acp 的 JSON 修复函数（兜底：hermes ACP flush 边界导致字段名断裂等）
        try:
            from src.domains.runtime.agents.hermes_acp import _fix_json_newlines, _ensure_closed
            raw = _fix_json_newlines(raw)
            raw = _ensure_closed(raw)
        except ImportError:
            pass  # hermes_acp 不可用时回退原有容错
        # 剥离 JSON 尾部残余（流式 chunk 可能在 --> 前留下 - 或 -- 残片，
        # _ensure_closed 不处理非括号垃圾 → json.loads 报 Extra data）
        raw = _strip_trailing_garbage(raw)
        data = json.loads(raw)
        # 修复流式输出在 JSON key 中插入的换行符（如 "description\n" → "description"）
        data = {k.replace('\n', '') if isinstance(k, str) else k: v for k, v in data.items()}
        findings = data.get("findings", [])
        valid = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            # 同上，修复 finding 对象 key 中的换行符
            f = {k.replace('\n', '') if isinstance(k, str) else k: v for k, v in f.items()}
            if not f.get("id") or not f.get("title") or not f.get("description"):
                continue
            f.setdefault("severity", "medium")
            f.setdefault("suggested_assignee", "coding")
            f.setdefault("suggested_skill", "implementation")
            # Issue #697：判定级别与差异说明透传（exact_duplicate/suspected_duplicate/not_duplicate）；
            # 级别在解析层即归一 fail-closed（漏发/拼错的非空级别按疑似重复处理，杜绝绕过 409 门禁）
            f["dedup_level"] = normalize_dedup_level(f.get("dedup_level"))
            f["dedup_note"] = sanitize_dedup_note(f.get("dedup_note"))
            valid.append(f)
        return {
            "result": data.get("result", ""),
            "summary": data.get("summary", ""),
            "findings": valid,
            "parse_error": "" if valid else "no valid findings"
        }
    except json.JSONDecodeError as e:
        return {"summary": "", "findings": [], "parse_error": str(e)}

def _build_autopilot_prompt(task: dict, repo_key: str, repository: str) -> str:
    """构建定时任务的 prompt。不依赖 issue/scene/pipeline 上下文。"""
    parts = []
    parts.append("## 定时任务\n")
    parts.append(f"任务名称: {task['name']}\n")
    parts.append(f"仓库: {repository}\n")
    parts.append(f"任务指令:\n{task.get('instruction', '')}\n")

    # skill 注入（可选）
    skill_name = task.get("skill_name", "")
    if skill_name:
        parts.append(f"\n## Skill: {skill_name}\n")
        parts.append(f"Skill 文件：{{{{SKILL_DIR}}}}/{skill_name}/SKILL.md\n")
        parts.append("技能内容位置：环境变量 DEVOPS_SKILLS_DIR 与 {{SKILL_DIR}} 指向同一目录；"
                     f"布局为 {skill_name}/SKILL.md（主文件）与 {skill_name}/references/ 参考文件（同目录下）。\n")
        parts.append("**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令。\n")
        parts.append("若技能目录为空或文件缺失（降级下发，见 [Skill Materialization Notice]），"
                     "改用 `grape skills detail` 或平台 skills API 取回技能内容，禁止无技能继续执行。\n")

    # 环境变量提示（custom_fields + work_branch）
    env_lines = []
    wb = task.get("work_branch", "")
    if wb:
        env_lines.append("- GRAPE_WORK_BRANCH")
    cf_raw = task.get("custom_fields", "")
    if cf_raw:
        try:
            cf_list = json.loads(cf_raw) if isinstance(cf_raw, str) else cf_raw
            for f in cf_list:
                fname = (f.get("name") or "").strip()
                if fname:
                    env_lines.append(f"- {fname}")
        except Exception:
            pass
    if env_lines:
        parts.append("\n## 环境变量\n")
        parts.append("以下环境变量已注入，执行前请先读取（printenv）:")
        parts.extend(env_lines)
        parts.append("")

    _run_params = task.get("_run_params") or {}
    # 组织分析任务不再用 custom_fields 里的 GRAPE_MANAGED_BY 标记（GRAPE_* 是保留
    # 环境变量，前端不可保存、执行侧也不注入）。受管身份改由 skill 判定。
    _is_org_analysis = str(task.get("skill_name") or "") == _ORG_ANALYSIS_SKILL
    if _run_params:
        parts.append("\n## 本次运行参数（JSON）\n")
        parts.append("```json")
        parts.append(json.dumps(_run_params, ensure_ascii=False, indent=2))
        parts.append("```")
    parts.append("\n请执行以上任务指令，分析仓库代码/依赖/文档/配置等。")
    if _is_org_analysis:
        parts.append("\n## 输出要求\n")
        parts.append("严格按 org-repo-analyzer 技能执行：通过平台 API 获取上下文并回传结构化 JSON；"
                     "不要输出 findings 列表，不要修改仓库代码，不要创建 Issue/PR。")
    else:
        parts.append("\n## 输出格式要求\n")
        parts.append(_FINDINGS_FORMAT_TEMPLATE)
    parts.append("\n## 项目知识\n")
    parts.append("项目知识文档位于工作目录中，存在则用 read tool 读取。")
    if not _is_org_analysis:
        parts.append("\n执行以上指令，不要操作 GitCode Issue（不需要回帖/打标签/关 Issue），结果直接输出。")
    return "\n".join(parts)

# Autopilot 不使用 events 表，但 agent_tasks.event_seq 需要非 0 且不能与真实事件
# 序号碰撞（历史实现直接传 exec_id，重放/已有 events 的库会命中旧事件状态，
# 被 executor 误判为 event:failed 而提前 kill）。用高位命名空间隔离。
_AUTOPILOT_EVENT_SEQ_BASE = 900_000_000


def _autopilot_event_seq(exec_id: int) -> int:
    return _AUTOPILOT_EVENT_SEQ_BASE + int(exec_id or 0)


def _execute_autopilot_agent(repo_key: str, task: dict, exec_id: int):
    """执行单个 autopilot agent（在线程池中运行）。

    不走 _execute_agent_inner / build_devops_agent_prompt / 事件系统。
    直接构建 prompt → 物化 workdir → 调 stream_devops_agent。
    """
    from src.kernel.repo_key import from_repo_key, split_owner_repo
    from src.domains.runtime.engine.engine_select import pick_engine, engine_name
    from src.domains.runtime.engine.instances import runtime_instance_name
    from src.domains.runtime.pipeline.event_pipeline import (
        get_agent_workdir, _materialize_direct_workdir, get_agent_display_name,
        _create_direct_task,
    )

    agent_type = _FIXED_AGENT_TYPE  # 固定 'issue'，不再从 task 读取
    skill_name = task.get("skill_name", "")
    repository = from_repo_key(repo_key) or repo_key

    # 1. 选引擎
    engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=0)
    # legacy 模式 / 无场景绑定时，按任务级 engine_runtime_ids 随机选健康引擎。
    # 组织 AI 分析任务通过该路径实现「随机可用运行时」，不依赖场景绑定。
    if engine_id is None:
        try:
            _raw_ids = task.get("engine_runtime_ids") or ""
            _ids = json.loads(_raw_ids) if isinstance(_raw_ids, str) else _raw_ids
            if isinstance(_ids, list) and _ids:
                from src.domains.runtime.engine.engine_select import (
                    _engines_by_ids, _health_ok, _norm_repo, load_engine_selection,
                )
                _members = _engines_by_ids(
                    [int(x) for x in _ids if str(x).strip().isdigit()],
                    _norm_repo(repo_key), load_engine_selection())
                _healthy = [m for m in _members if _health_ok(m)]
                if _healthy:
                    import random as _random
                    engine_id = _random.choice(_healthy)["runtime_id"]
                    log.info(f"Autopilot exec {exec_id}: picked random task engine "
                             f"id={engine_id} from {len(_healthy)} healthy")
        except Exception as _se:
            log.warning(f"Autopilot exec {exec_id}: task engine pick failed: {_se}")
    if engine_id is None:
        # Issue #3800：任务级绑定全灭 / 绑定悬空属于环境性失败——写 error_code 交给收口段
        # 走退避/熔断通道，而不是记一条「无可用引擎」后每 tick 重来一次。
        _probe = _probe_task_binding(task, repo_key)
        _code, _msg = _binding_failure_code(_probe)
        if _code:
            _update_execution_result(exec_id, "failed", error=_msg, error_code=_code)
            return
        log.warning(f"Autopilot exec {exec_id}: no engine for {agent_type}")
        _update_execution_result(exec_id, "failed", error="无可用引擎")
        return

    inst_name = runtime_instance_name(engine_id) or engine_name(engine_id) or ""
    backend = inst_name

    # 判断执行模式：DIRECT（本机）或 REMOTE（远端 worker）/ 广播
    from src.domains.runtime.engine.service import runtime_execution_kind, EXEC_MODE_DIRECT, get_execution_mode
    from src.domains.runtime.pipeline.event_pipeline import is_plugin_mode
    if engine_id:
        _kind = runtime_execution_kind(engine_id)
    else:
        # engine_id=0: plugin 模式下是广播，否则 direct
        _kind = EXEC_MODE_DIRECT if (not is_plugin_mode() or get_execution_mode() == EXEC_MODE_DIRECT) else "broadcast"
    _is_broadcast = (_kind == "broadcast")
    _is_direct = (_kind == EXEC_MODE_DIRECT)

    # 2. 工作目录
    ws = get_agent_workdir(repo_key, agent_type, "")
    os.makedirs(ws, exist_ok=True)

    # 3. bot token 校验（D101 方案二 W5，评审 v2 第 3 点）：传输值退役恒空，
    # 校验只取来源级别（resolve_config_level 纯读，不检查过期不触发 refresh，
    # 禁消费 .token）——空启动守卫语义保留（无任何 bot 配置 → 不启动），
    # token 值传输职责移交 claim 唯一注入点。
    repo_token = ""
    try:
        from src.kernel.token_service import resolve_repo_config_level
        _lvl = resolve_repo_config_level(repo_key).level or ""
        if _lvl == "":
            log.warning(f"Autopilot exec {exec_id}: no bot token configured for {repo_key} (empty short-circuit)")
        elif _lvl == "user":
            log.warning(f"Autopilot exec {exec_id}: repo {repo_key} is user-level (claim-time injection)")
        # repo_token 不再赋值传输——恒空进 enqueue（行值退役）
    except Exception as e:
        log.warning(f"Autopilot exec {exec_id}: token config check failed: {e}")

    # 4. 构建 prompt
    prompt = _build_autopilot_prompt(task, repo_key, repository)

    # 5. 物化 workdir（project-info + skill 如果有）
    # ⚠️ 仅 DIRECT 模式做服务端物化 + {{SKILL_DIR}} 替换（占位符换成服务端本机 ws/.skills 路径）。
    #    REMOTE/广播模式保留 {{SKILL_DIR}} 占位符——worker 认领后在自己的工位物化技能
    #    （agent.py: prompt.replace("{{SKILL_DIR}}", skill_dir)），技能内容则由 enqueue_task
    #    按 skill_name + repo_key 解析打包随任务下发。
    #    历史 bug（2026-10-13 task 16 两次失败根因）：REMOTE 也走了服务端替换，把服务端的
    #    /root/grape-workspaces/... 绝对路径烧进 prompt，远端非 root worker（如 0005 liufei 用户）
    #    读 /root 被拒 → "SKILL.md 无法访问（权限被拒）"。10-12 exec 350 成功纯属 agent 自行
    #    glob 搜到 /home/{user} 副本自救，不可依赖。
    evt = {"repo_key": repo_key, "issue_id": "", "payload": {}}
    if _is_direct:
        prompt = _materialize_direct_workdir(ws, repo_key, agent_type, skill_name, evt, "",
                                             prompt, engine_name=inst_name or backend or "")

    # 6. 环境变量（定时任务不需要场景/issue相关变量）
    agent_display_name = get_agent_display_name(agent_type)
    from src.kernel.prompt_builder import resolve_task_server_env
    # Issue #1660：REPO_OWNER/REPO_NAME 由 REPO_FULL_NAME 派生（缺失回退 repo_key）。
    from src.kernel.repo_key import derive_repo_identity
    _full_name, _repo_owner, _repo_name = derive_repo_identity(repository, repo_key)
    # Issue #1819：autopilot 无事件 actor，不得把仓库全名（owner/repo）注入 $ACCOUNT。
    # 取「任务创建者」账号作来源（autopilot_tasks.created_by → user_accounts.username）；
    # 查不到才留空并告警。语义是创建者而非本次触发人，detail 里标明 account_source。
    from src.domains.runtime.pipeline.account_env import log_account_alert, task_creator_account
    _autopilot_account = task_creator_account(task.get("created_by") or 0)
    if not _autopilot_account:
        # created_by=0 的历史任务属常态，按 info 记录以免稀释真实 warning 信号。
        log_account_alert(
            "ACCOUNT_MISSING", level="info",
            event_type="autopilot",
            repository=repository,
            repo_key=repo_key,
            exec_id=exec_id,
            fallback="autopilot_created_by->none",
            owner_fallback=False,
            account_source="task_creator",
        )
    env_extra = {
        "REPO_FULL_NAME": _full_name or repository,
        "REPO_OWNER": _repo_owner,
        "REPO_NAME": _repo_name,
        "ACCOUNT": _autopilot_account,
        # Issue #1577：与事件/chat 同源注入两项服务基址，远端 autopilot worker
        # 的业务命令免 --base-url 可用（direct 模式注入本机回环同样正确）。
        **resolve_task_server_env(engine_id),
    }
    # 注入 custom_fields（自定义环境变量）和 work_branch
    # Issue #4066：不再逐条 WARNING（一条执行会把整批 custom_fields 刷成多行噪声），
    # 改为一次执行一条 info 汇总（injected/skipped + 变量名）。非法/保留项写侧已 400
    # 拒绝，这里只兜底存量数据；平台已注入的键（如 GRAPE_ORG_ID）不由任务覆盖。
    _task_custom_fields = task.get("custom_fields") or ""
    if _task_custom_fields:
        _cf_injected: list = []
        _cf_skipped: list = []
        try:
            _cf_list = json.loads(_task_custom_fields) if isinstance(_task_custom_fields, str) else _task_custom_fields
            from src.kernel.scene_custom_env import is_upper_name, is_reserved_name
            for _f in (_cf_list or []):
                _fname = (_f.get("name") or "").strip() if isinstance(_f, dict) else ""
                if (not _fname or not is_upper_name(_fname)
                        or is_reserved_name(_fname) or _fname in env_extra):
                    _cf_skipped.append(_fname)
                    continue
                env_extra[_fname] = str(_f.get("value", ""))
                _cf_injected.append(_fname)
        except Exception as _e:
            log.warning(f"Autopilot exec {exec_id}: inject custom_fields failed: {_e}")
        if _cf_injected or _cf_skipped:
            log.info(
                f"Autopilot exec {exec_id}: custom env injected={len(_cf_injected)} "
                f"skipped={len(_cf_skipped)} names={','.join(_cf_injected)} "
                f"skipped_names={','.join(n for n in _cf_skipped if n)}")
    _task_work_branch = task.get("work_branch") or ""
    if _task_work_branch:
        env_extra["GRAPE_WORK_BRANCH"] = _task_work_branch
    # Issue #2225：会话级唯一暂存目录（同一自动化任务每次触发独立目录，避免并发互覆写）。
    try:
        from src.domains.runtime.engine.session_tmpdir import inject_session_tmpdir_env
        inject_session_tmpdir_env(env_extra, f"autopilot-{exec_id}")
    except Exception as _tmp_e:
        log.warning(f"Autopilot exec {exec_id}: inject session tmpdir failed: {_tmp_e}")

    # Issue #1811：定时调度与 event_pipeline 同源注入 bot 身份，单点覆盖 DIRECT/REMOTE。
    # 函数内延迟 import，避免 scheduler ↔ event_pipeline 模块级循环依赖。
    from src.domains.runtime.pipeline.event_pipeline import _inject_bot_agent_env
    _inject_bot_agent_env(env_extra, repo_key, agent_type)
    if not env_extra.get("BOT_USERNAME"):
        log.warning(
            f"Autopilot exec {exec_id}: BOT_USERNAME unresolved for repo={repository}; "
            "skill author filtering will degrade"
        )

    # 7. 分流执行
    if _is_direct:
        # DIRECT 模式：本机执行
        _execute_autopilot_direct(repo_key, task, exec_id, agent_type, skill_name,
                                  engine_id, backend, inst_name, ws, prompt,
                                  repo_token, agent_display_name, env_extra, repository,
                                  evt)
    else:
        # REMOTE / 广播模式：入队让远端 worker 认领
        _execute_autopilot_remote(repo_key, task, exec_id, agent_type, skill_name,
                                  engine_id, backend, inst_name, ws, prompt,
                                  repo_token, agent_display_name, env_extra, repository, evt)

def _execute_autopilot_direct(repo_key, task, exec_id, agent_type, skill_name,
                              engine_id, backend, inst_name, ws, prompt,
                              repo_token, agent_display_name, env_extra, repository,
                              evt=None):
    """DIRECT 模式：本机 stream_devops_agent 执行。"""
    from src.domains.runtime.pipeline.event_pipeline import _create_direct_task

    # Issue #3201：接管 active 期间不建 task、不起 agent（执行记录置 skipped）。
    if _skip_autopilot_for_takeover(repo_key, task, exec_id):
        return

    # 创建 agent_tasks 记录（用 autopilot 专属 seq 命名空间，skip_event_config=True 不写 events 表）
    # P8-consistency：issue 回链键创建即落（无数字合成键，失败路径不丢统计），
    # 串行锁/工作目录/知识过滤均不受影响（见 _autopilot_issue_ref）。
    # Issue #3963（复审 Required）：direct 派发撞 repo-dispatch 租约时
    # _create_direct_task 抛 RepoDispatchBusyError（瞬态）——与 REMOTE 入队同口径
    # 落 skipped + error_code，交给 _execute_task 瞬态通道（不消耗 attempt /
    # 超预算 requeue 下一 tick），不再「创建任务记录失败」记 failed（撞锁即判死）。
    try:
        task_id = _create_direct_task(
            repo_key, _autopilot_event_seq(exec_id), agent_type, backend, skill_name,
            "", engine_id, {"repo_key": repo_key, "issue_id": "", "payload": {}},
            issue_id=_autopilot_issue_ref(task.get("id")), skill_id=0, skip_event_config=True,
        )
    except Exception as _dt_e:
        _dt_code = _enqueue_failure_code(_dt_e)
        _dt_status = "skipped" if _dt_code == ERROR_REPO_DISPATCH_BUSY else "failed"
        if _dt_code == ERROR_REPO_DISPATCH_BUSY:
            log.warning(f"Autopilot exec {exec_id}: direct dispatch busy (transient, retryable): {_dt_e}")
        else:
            log.error(f"Autopilot exec {exec_id}: _create_direct_task failed: {_dt_e}")
        _update_execution_result(exec_id, _dt_status, error=f"直发建任务异常: {_dt_e}",
                                 error_code=_dt_code)
        return
    if not task_id:
        log.warning(f"Autopilot exec {exec_id}: _create_direct_task failed")
        _update_execution_result(exec_id, "failed", error="创建任务记录失败")
        return

    log.info(f"Autopilot exec {exec_id}: starting direct agent={agent_type} engine={inst_name}")

    # Issue #887（QA 修复）：direct 端执行前技能一致性硬校验——复用 _materialize_direct_workdir
    # 物化后写入 evt 的判定结果（SKILL_CONSISTENCY_KEY），与 event_pipeline._execute_agent_inner
    # 口径一致（物化后校验，残留缓存+绑定未物化才拒绝），不再在物化前另起一套 verify
    # （旧实现与物化时序/口径不一致，存在误拒）；拒绝消息经 skill_mismatch_error 单一实现。
    try:
        from src.domains.runtime.engine.skill_materialize import (SKILL_CONSISTENCY_KEY,
                                                   skill_mismatch_error)
        from src.domains.runtime.engine.task_log_stream import build_skill_source_message
        _sv = (evt or {}).get(SKILL_CONSISTENCY_KEY) or {}
        _sk_mat = (evt or {}).get("_skill_materialize") or {}
        if not _sv:
            # 判定缺失（物化异常等）→ 显式降级放行 + 告警，不静默当作通过
            log.warning(f"Autopilot exec {exec_id}: skill consistency verdict missing "
                        f"(materialize degraded); degraded pass")
        else:
            _src_line = build_skill_source_message(
                _sv.get("bound", ""), _sv.get("actual", ""), _sv.get("source", ""),
                ok=_sv.get("ok", True), reason=_sv.get("reason", ""),
                source_info=_sk_mat.get("source") or None,
                drift=_sk_mat.get("drift") or None)["data"]["text"]
            log.info(f"Autopilot exec {exec_id}: skill source {_src_line}")
            if _sv.get("ok") is False:
                _sv_err = skill_mismatch_error(_sv)
                log.error(f"Autopilot exec {exec_id}: skill consistency refused: {_sv_err}")
                _finalize_autopilot_task(task_id, exec_id, 1, _sv_err)
                _update_execution_result(exec_id, "failed", error=_sv_err)
                return
    except Exception as _sc_e:
        log.warning(f"Autopilot exec {exec_id}: skill consistency verify failed: {_sc_e}")

    exit_code = -1
    final_text = ""

    # 用量采集（审计 2026-10-17）：autopilot DIRECT 模式跑在主控进程里，以前不记 token
    from src.domains.insight.public import master_usage_for_task as _master_for_task
    _mu = _master_for_task(task_id, backend, cwd=ws, session_id="", seq=exec_id,
                           repo_key=repo_key, agent_type=agent_type,
                           engine_id=int(engine_id or 0))
    _mu.start()

    try:
        from src.domains.runtime.engine.session_tmpdir import ensure_session_tmpdir
        ensure_session_tmpdir(env_extra, f"autopilot-{exec_id}")
    except Exception as _tmp_e:
        log.warning(f"Autopilot exec {exec_id}: ensure session tmpdir failed: {_tmp_e}")
    try:
        from src.domains.runtime.engine.executor import stream_devops_agent
        for evt_type, data in stream_devops_agent(
            prompt, ws, session_id="",
            event_type="autopilot_event",
            account=repository, bot_token=repo_token,
            agent_name=agent_display_name,
            agent_label=f"[Autopilot:{agent_type}|repo:{repository}|engine:{inst_name}]",
            env_extra=env_extra,
            repo_key=repo_key,
        ):
            _mu.feed(evt_type, data)
            if evt_type == "done":
                d = data
                exit_code = d.get("exit_code", -1)
                final_text = d.get("text", "")
                log.info(f"Autopilot exec {exec_id}: done exit={exit_code} text_len={len(final_text)}")
    except Exception as e:
        log.error(f"Autopilot exec {exec_id}: agent execution crashed: {e}")
        final_text = f"执行异常: {e}"
        exit_code = 1

    # Issue #2225：direct 自动化任务结束清理自身暂存目录。
    try:
        from src.domains.runtime.engine.session_tmpdir import cleanup_session_tmpdir
        cleanup_session_tmpdir(env_extra)
    except Exception as _tmp_e:
        log.warning(f"Autopilot exec {exec_id}: session tmpdir cleanup failed: {_tmp_e}")
    # 更新 agent_tasks 状态（用 task_id 精确更新）
    _finalize_autopilot_task(task_id, exec_id, exit_code, final_text)
    # 用量落台账：放在任务终态之后（引擎已退出、账本已落盘），失败只打日志
    try:
        _mu.finish(session_id="")
    except Exception as _ue:
        log.warning(f"Autopilot exec {exec_id}: usage finish failed: {_ue}")

def _execute_autopilot_remote(repo_key, task, exec_id, agent_type, skill_name,
                              engine_id, backend, inst_name, ws, prompt,
                              repo_token, agent_display_name, env_extra, repository, evt):
    """REMOTE / 广播模式：入队让远端 worker 认领执行。"""
    from src.domains.runtime.engine.service import enqueue_task
    from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir as _gaw
    from src.api.session_store import get_or_create_agent_session, get_or_create_issue_session
    from src.kernel.prompt_builder import _resolve_server_url

    # 获取会话
    session = get_or_create_agent_session(repo_key, agent_type, backend or "")
    agent_session_id = session.get("id", "")
    if not agent_session_id:
        log.warning(f"Autopilot exec {exec_id}: no agent_session_id for {repo_key}/{agent_type}")
        _update_execution_result(exec_id, "failed", error="无 agent 会话")
        return

    # Autopilot 独立串行槽：合成 issue_id（autopilot-<任务id>，同一任务每次触发相同），
    # 复用 issue_agent_session 机制。同任务运行中时 D9 串行锁自动拦截二次触发（防误触）；
    # 不同 autopilot 任务各持独立会话，互不排队。每次触发前清空续接指针 → 全新上下文开始。
    # 注意：清空必须走直接 UPDATE（update_issue_session 的空指针不覆盖），且只影响合成会话行，
    # 不触碰真实 issue 的会话（主流程不受影响）。
    _synthetic_issue = f"autopilot-{task.get('id') or ''}"
    _isess = get_or_create_issue_session(repo_key, agent_type, _synthetic_issue)
    _autopilot_issue_session_id = _isess.get("id") or ""
    if _autopilot_issue_session_id:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE issue_agent_session SET runtime_session_id='' "
                    "WHERE id=? AND issue_id=?",
                    (_autopilot_issue_session_id, _synthetic_issue)
                )
                db.commit()
        except Exception as _ce:
            log.warning(f"Autopilot exec {exec_id}: clear synthetic session pointer failed: {_ce}")

    # 广播模式设 pool_tag
    _pool_tag = agent_type if engine_id == 0 else ""

    # 任务级引擎限定：uid 模式（engine_runtime_uids 非空）优先 → uid 池路径；
    # legacy id 路径（engine_runtime_ids）行为不变——三分支判据不混装（Issue #1011）
    # Issue #259：写入前按健康过滤（复用 engine_select 的 _engines_by_ids + _health_ok）
    # Issue #3800：两条路径共用 `_probe_task_binding` 一次探测，健康过滤为空统一落
    # error_code（ENGINE_OFFLINE / BINDING_STALE），由 `_execute_task` 收口改走退避/熔断，
    # 不再在此处直接写一句中文 failed 让下一 tick 重来。
    _pool_engine_ids = ""
    if engine_id == 0:
        _probe = _probe_task_binding(task, repo_key)
        if _probe.get("mode") and _probe.get("error"):
            # 探测本身失败（DB 抖动）：不分类（避免把瞬时抖动记成「绑定悬空」触发重绑
            # 告警），也绝不回落到广播池——任务级绑定必须被尊重，宁可按普通失败留痕。
            log.warning(f"Autopilot exec {exec_id}: probe task engines failed: {_probe['error']}")
            _update_execution_result(
                exec_id, "failed",
                error=f"任务级引擎探测失败，未派发（{_probe['error']}）")
            return
        if _probe.get("mode"):
            if _probe.get("healthy"):
                _pool_engine_ids = json.dumps([m["runtime_id"] for m in _probe["healthy"]])
                log.info(f"Autopilot exec {exec_id}: task 级限定（{_probe['mode']} 模式）"
                         f"engines={_pool_engine_ids}")
            else:
                _code, _msg = _binding_failure_code(_probe)
                # 单条结构化行由 `_handle_environmental_failure` 在收口段输出（含指纹），
                # 此处不再另发 WARNING/ERROR，避免「ERROR 无因 + WARNING 有因」割裂。
                _update_execution_result(exec_id, "failed", error=_msg, error_code=_code)
                return

    log.info(f"Autopilot exec {exec_id}: enqueuing remote agent={agent_type} engine={inst_name} pool_tag={_pool_tag}")

    # Issue #287：入队前按任务创建者预解析 skill_id（user scope skill 的 owner_id 匹配 created_by）。
    # user_id 保持 0（_collect_server_env 凭据注入行为零变化）；解析不中时降级 skill_id=0 + warning，不阻断入队。
    _skill_id = 0
    if skill_name:
        try:
            from src.domains.mgmt.public import resolve_skill_id  # 跨域走 owner public
            _skill_id = resolve_skill_id(skill_name, repo_key=repo_key,
                                         user_id=task.get("created_by") or 0)
            if not _skill_id:
                log.warning(f"Autopilot exec {exec_id}: skill '{skill_name}' not resolved for "
                            f"creator={task.get('created_by')}, enqueue without skill files")
        except Exception as _se:
            log.warning(f"Autopilot exec {exec_id}: pre-resolve skill '{skill_name}' failed: {_se}")

    # Issue #3201：接管 active 期间不建 task（执行记录置 skipped，不消耗重试）。
    if _skip_autopilot_for_takeover(repo_key, task, exec_id):
        return

    try:
        task_id = enqueue_task(
            agent_session_id=agent_session_id,
            runtime_id=engine_id,
            prompt=prompt,
            cwd=ws,
            runtime_session_id="",
            event_type="autopilot_event",
            account=repository,
            user_id=task.get("created_by") or 0,  # Issue #432: 写任务创建者，user scope 引擎才能认领（原硬编码 0 导致个人级引擎绑定的自动化任务永久 queued）
            repo_gitcode_token="",  # D101: 行值退役恒空，claim 现取（W5）
            user_gitcode_token="",
            env_extra=env_extra,
            event_seq=_autopilot_event_seq(exec_id),
            issue_id=_autopilot_issue_ref(task.get("id")),
            # P8-consistency：issue 回链键创建即落（无数字合成键 autopilot-<letters>）。
            # 旧注释顾虑已由键的形态消除：纯字母键使 service._issue_num→0、
            # get_issue_scene 查不到行（回退 default），不会按 issue 号解析场景/成员知识/
            # 工作目录，也不会误挂真实 issue；串行锁只看 issue_session_id（不变）。
            repo_key=repo_key,
            agent_type=agent_type,
            skill=skill_name,
            skill_path="",
            skill_id=_skill_id,
            server_url=_resolve_server_url(engine_id),
            issue_session_id=_autopilot_issue_session_id,
            pool_agent_type=_pool_tag,
            pool_tag=_pool_tag,
            pool_engine_ids=_pool_engine_ids,
        )
        if not task_id:
            log.warning(f"Autopilot exec {exec_id}: enqueue_task failed")
            _update_execution_result(exec_id, "failed", error="入队失败")
            return

        # Issue #1546：执行记录直接持有 agent_tasks.id，反查不再依赖 event_seq 撞号。
        # 回写失败只告警，不影响派发主流程（历史空值行走受约束兜底）。
        try:
            with db_lock:
                _c = db.cursor()
                _c.execute(
                    "UPDATE autopilot_executions SET agent_task_id=? WHERE id=?",
                    (str(task_id), exec_id))
                db.commit()
        except Exception as _at_e:
            log.warning(f"Autopilot exec {exec_id}: persist agent_task_id failed: {_at_e}")

        log.info(f"Autopilot exec {exec_id}: enqueued task_id={task_id}, waiting for worker...")

        # 远端执行是异步的，worker 认领后执行完会通过 report_messages/task_complete 回调更新 agent_tasks
        # autopilot_executions 状态需要轮询 agent_tasks 的状态来更新
        # 这里标记为 "dispatched"，后续由轮询或手动检查更新终态
        _update_execution_result(exec_id, "dispatched",
                                 result=f"已入队 task_id={task_id}，等待 worker 执行")

    except Exception as e:
        # Issue #3963：入队异常按分类落库。租约争用（busy）是瞬态，记 `skipped` +
        # error_code（非 failed：管理台/统计按 error_code 区分「排队冲突 / 执行失败」），
        # 由 `_execute_task` 的瞬态通道决定「同 tick 重试」还是「requeue 下一 tick」。
        _code = _enqueue_failure_code(e)
        _status = "skipped" if _code == ERROR_REPO_DISPATCH_BUSY else "failed"
        if _code == ERROR_REPO_DISPATCH_BUSY:
            # 瞬态争用不是执行失败：WARNING 级留痕（ERROR 面会被失败率类告警误计）
            log.warning(f"Autopilot exec {exec_id}: enqueue busy (transient, retryable): {e}")
        else:
            log.error(f"Autopilot exec {exec_id}: enqueue failed: {e}")
        _update_execution_result(exec_id, _status, error=f"入队异常: {e}", error_code=_code)

def _autopilot_failure_cause(exit_code: int, final_text: str) -> str:
    """失败 exec 的结构化 cause（恒非空）：`exit_code=<n>; output=<agent 输出尾行>`。

    Issue #3897（生产 Loki：`Autopilot: task 83 exec <id> failed after 0 retries`）：
    此前 `_finalize_autopilot_task` 失败时只写 status、`error` 留空，调度收口日志回落到
    字面量 `failed after N retries` —— 既没有失败原因，也不解释为什么 max_retry=0
    一次都不重试。本函数保证「失败必有 cause」：输出为空时也给出可辨识的空输出标记，
    让日志能直接区分「agent 报错」与「agent 什么都没产出」。
    """
    _lines = [ln.strip() for ln in str(final_text or "").splitlines() if ln.strip()]
    tail = _lines[-1] if _lines else ""
    if not tail:
        tail = "<no output: agent produced no stdout/stderr>"
    if len(tail) > _AUTOPILOT_CAUSE_TAIL_MAX:
        tail = tail[:_AUTOPILOT_CAUSE_TAIL_MAX] + "…"
    return f"exit_code={int(exit_code)}; output={tail}"


def _retry_skip_reason(max_retry: int, attempt: int, error_code: str) -> str:
    """解释「为什么 0 重试」（Issue #3897）：环境性失败 / 任务配置 / 已达上限。

    收口日志用该字段替代原来的裸 `failed after {max_retry} retries` —— 后者在
    max_retry=0 时是一句同义反复，读日志的人无法判断「配置就是 0」还是「代码没重试」。
    """
    if error_code in _ENV_FAILURE_CODES:
        return (f"environmental failure error_code={error_code}: retry is pointless "
                f"(handed to backoff/circuit)")
    _max = int(max_retry or 0)
    if _max <= 0:
        return "max_retry=0 (task config): no retry scheduled"
    if int(attempt) >= _max:
        return f"reached max_retry={_max} after {int(attempt) + 1} attempt(s)"
    return f"retries available ({int(attempt)}/{_max} used)"


def _finalize_autopilot_task(task_id: str, exec_id: int, exit_code: int, final_text: str):
    """执行完成后更新 agent_tasks + autopilot_executions + 解析 findings。"""
    # 更新 agent_tasks 状态（用 task_id 精确更新）
    # L1（2026-10-17）：改为统一终态入口 update_task_status，其内部完成
    #   release_busy_slot（防 busy_slots 泄漏 → 本机引擎误判满载）＋ events 状态同步。
    # 直接 UPDATE agent_tasks 会绕过统一递减（见 local-engine-busy-slots-zombie-fix-design.md §九）。
    try:
        from src.domains.runtime.engine.service import update_task_status
        update_task_status(
            task_id,
            "completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
            result_text=final_text[:5000],
            completed_at=datetime.now(timezone.utc).isoformat(),
            # Issue #1938：补偿归因——该路径由 scheduler 收尾，非任务自报
            cascade_trigger="sweeper",
        )
    except Exception as e:
        log.warning(f"Autopilot exec {exec_id}: update agent_tasks failed: {e}")

    # 解析 findings
    findings_result = parse_findings(final_text)

    # result 只存 summary（不存原始工作日志），解析失败时降级存最后 500 字
    if findings_result["summary"]:
        _result_summary = findings_result["summary"]
    else:
        _result_summary = final_text[-500:] if final_text else ""

    # 更新 autopilot_executions
    status = "success" if exit_code == 0 else "failed"
    # Issue #3897：失败必须落 cause（exit_code + agent 输出尾行）。此前失败行 error 恒为空，
    # 调度收口只能打 `failed after N retries`，生产 task 83 每分钟一条、无法自诊断。
    _ev_error = "" if status == "success" else _autopilot_failure_cause(exit_code, final_text)
    if status == "success":
        # Issue #537 P1-1：部署类任务收口前做落地证据校验（版本指纹/健康检查）。
        # 校验失败 → 收口 failed 带 evidence 详情（纵深防御，防 false-success 也防 false-failed）。
        # QA R1 三表终态一致性取舍：仅 autopilot_executions 判 failed；agent_tasks/events 保留
        # completed——worker 确实完成执行，部署落地矛盾由 exec.error 承载；不回翻任务/事件侧
        # 终态（update_task_status 终态幂等，也不应把已完成任务后改为 failed）。
        try:
            from src.domains.runtime.engine.service import get_task
            from src.domains.runtime.engine.deploy_evidence import check_deploy_evidence
            _evd = check_deploy_evidence(get_task(task_id) or {})
        except Exception:
            _evd = {"checked": False}
        if _evd.get("checked") and not _evd.get("passed"):
            status = "failed"
            # Issue #3897：证据失败也是失败，cause 同时带证据原因与 agent 输出尾行。
            _ev_error = (f"deploy_evidence: {_evd.get('reason', '部署证据校验未通过')}; "
                         f"{_autopilot_failure_cause(exit_code, final_text)}")
            log.info(f"Autopilot exec {exec_id}: deploy evidence failed -> {status} ({_evd.get('reason')})")
    _update_execution_result(exec_id, status,
                             result=_result_summary,
                             error=_ev_error,
                             findings_json=json.dumps(findings_result, ensure_ascii=False) if findings_result["findings"] else "")

    log.info(f"Autopilot exec {exec_id}: completed status={status} findings={len(findings_result['findings'])}")

    # 飞书订阅通知（autopilot-feishu-subscription-design.md §4.4）：终态挂载点①（worker 回调收尾）
    # 与 _poll_dispatched_completion 挂载点②以 notified 列互斥，先到者发
    _notify_subscribers(_exec_task_id(exec_id), exec_id, status,
                        _result_summary, findings_result)

# result 判级 → 卡片模板色（§5.3）；执行失败/取消不查 result 直接红
_RESULT_TEMPLATE = {"ok": "green", "warning": "yellow", "error": "red", "": "grey"}

_RESULT_TITLE = {"ok": "✅ 任务执行完成", "warning": "⚠️ 任务执行完成（有需关注事项）",
                 "error": "❌ 任务执行需处理", "": "✅ 任务执行完成"}

_SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

def _autopilot_result_level(status: str, findings_result: dict) -> str:
    """终态 → 判级：执行失败/取消直接 error；成功取 findings 的 result 字段（旧数据缺省空）。"""
    if status in ("failed", "cancelled"):
        return "error"
    _r = (findings_result or {}).get("result", "")
    return _r if _r in ("ok", "warning", "error") else ""

def build_exec_card(task_name: str, repo_key: str, exec_id: int, status: str,
                    result_summary: str, findings_result: dict, web_url: str) -> dict:
    """构建第一段推送卡（§5.1）。

    纯函数：发卡与（未来的）更新共用同一渲染，输入即 findings_json 同源数据。
    返回 send_card_to_user 参数组（1.0 参数 + 2.0 完整卡，发送侧择优）。
    """
    _lvl = _autopilot_result_level(status, findings_result)
    _findings = (findings_result or {}).get("findings", [])
    _summary = (result_summary or "").strip() or (findings_result or {}).get("summary", "")
    # findings 以列表呈现：每条 severity emoji + 类别 + 标题
    text_parts = []
    if _summary:
        text_parts.append(_summary)
    if _findings:
        text_parts.append("")
        text_parts.append("**产出问题**")
        for f in _findings[:10]:
            _sev = (f.get("severity") or "medium").lower()
            text_parts.append(f"{_SEVERITY_EMOJI.get(_sev, '⚪')} **{f.get('title', '')}** "
                              f"<font color='grey'>{f.get('category', '-')}</font>")
        if len(_findings) > 10:
            text_parts.append(f"<font color='grey'>其余 {len(_findings) - 10} 个见详情</font>")
    notify_text = "\n".join(text_parts) or ("任务执行完成。" if status == "success" else "任务执行失败。")
    fields = [{"is_short": True, "text": {"tag": "lark_md", "content": f"**仓库**\n{repo_key or '-'}"}},
              {"is_short": True, "text": {"tag": "lark_md", "content": f"**产出问题**\n{len(_findings)} 个"}}]
    actions = [{"tag": "button", "text": {"tag": "plain_text", "content": "📋 查看问题详情"},
                "value": {"cmd": "/finding-list", "exec_id": exec_id}}]
    if web_url:
        actions.append({"tag": "button", "text": {"tag": "plain_text", "content": "查看网页详情 ↗"},
                        "url": web_url})
    # 卡片 2.0 完整 JSON（2.0 支持 font 灰字；按钮走 behaviors.callback.value，
    # cmd 同 1.0 解析链）
    body_elements = (
        [{"tag": "div", "text": {"tag": "lark_md", "content": _summary or notify_text[:200]}}]
        + _lark_findings_elements(_findings)
        + [{"tag": "hr"}]
        + [{"tag": "div", "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**仓库**\n{repo_key or '-'}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**产出问题**\n{len(_findings)} 个"}}]}]
        # 有 findings 时才放「查看问题详情」按钮（空 findings 点进去只看到"无产出"，没意义）
        + ([{"tag": "button", "text": {"tag": "plain_text", "content": "查看问题详情"},
             "type": "primary", "behaviors": [{"type": "callback", "value": {
                 "cmd": "/finding-list", "exec_id": exec_id}}]}] if _findings else [])
        + ([{"tag": "button", "text": {"tag": "plain_text", "content": "网页详情 ↗"},
             "behaviors": [{"type": "open_url", "default_url": web_url}]}] if web_url else [])
        # 有 findings 时才放引导脚注
        + ([{"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "<font color='grey'>点击「查看问题详情」可逐条派发为 Issue</font>"}}]
           if _findings else [])
    )
    return {
        "title": f"{_RESULT_TITLE.get(_lvl, _RESULT_TITLE[''])} · {task_name}",
        "template": _RESULT_TEMPLATE.get(_lvl, "grey"),
        "text": notify_text,
        "actions": actions,
        "fields": fields,
        "note": "点击「查看问题详情」可逐条派发为 Issue",
        "card_v2": {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text",
                                 "content": f"{_RESULT_TITLE.get(_lvl, _RESULT_TITLE[''])} · {task_name}"},
                       "template": _RESULT_TEMPLATE.get(_lvl, "grey")},
            "body": {"elements": body_elements},
        },
    }

def _lark_findings_elements(findings: list) -> list:
    """findings → 2.0 卡片元素列表（div 列表——emoji 分级 + 标题 + 灰字类别）。空列表返回空。"""
    if not findings:
        return []
    lines = ["**产出问题**"]
    for f in findings[:10]:
        _sev = (f.get("severity") or "medium").lower()
        _mark = _SEVERITY_EMOJI.get(_sev, "⚪")
        _cat = f.get("category", "-")
        lines.append(f"{_mark} **{f.get('title', '')}** <font color='grey'>{_cat}</font>")
    if len(findings) > 10:
        lines.append(f"<font color='grey'>其余 {len(findings) - 10} 个见详情</font>")
    return [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)[:1800]}}]

async def _run_autopilot_notify(ctx, api, user_id: int, open_id: str,
                                card: dict, task_id: int, exec_id: int):
    """单订阅者发送（仅静态卡片，不触发 ChatAgent/LLM）。"""
    try:
        if open_id and api and ctx:
            # 优先 2.0 卡（card_v2 字段）；无则回退 1.0 参数组
            if card.get("card_v2"):
                await api.send_card_to_user(ctx, open_id, "", card_v2=card["card_v2"])
            else:
                await api.send_card_to_user(
                    ctx, open_id, card["text"], title=card["title"],
                    template=card["template"], actions=card["actions"],
                    fields=card["fields"], note=card.get("note"))
        else:
            log.warning(f"[autopilot-notify] skip task={task_id} exec={exec_id} user={user_id} "
                        f"reason={'no_openid' if not open_id else ('no_ctx' if not ctx else 'no_api')}")
    except Exception as e:
        log.warning(f"[autopilot-notify] task={task_id} exec={exec_id} user={user_id} send failed: {e}")

def _exec_task_id(exec_id: int) -> int:
    """exec_id → task_id 反查（通知挂载点用）；查不到返回 0（_notify_subscribers 内 JOIN 不到行 → noop）。"""
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT task_id FROM autopilot_executions WHERE id=?",
                            (exec_id,)).fetchone()
        return row[0] if row else 0
    except Exception as e:
        log.warning(f"Autopilot: exec task id lookup failed {exec_id}: {e}")
        return 0

def _notify_subscribers(task_id: int, exec_id: int, status: str,
                        result_summary: str, findings_result: dict):
    """终态通知订阅者（§4.4）。全 try 包裹：任何故障不炸调度循环——通知失败不影响执行结果已落库。

    幂等护栏：autopilot_executions.notified 列，UPDATE ... WHERE notified=0 抢占成功才发
    （两个终态挂载点先到者发，后到者 noop）。
    表结构保障：notified/订阅表缺失（迁移失败的极端场景）时 SELECT/UPDATE 抛错 → 记日志跳过，
    不影响主流程。
    """
    try:
        # 抢占通知权（幂等）：失败=已通知或表缺列
        with db_lock:
            c = db.cursor()
            cur = c.execute("UPDATE autopilot_executions SET notified=1 WHERE id=? AND notified=0",
                            (exec_id,)).rowcount
            db.commit()
        if not cur:
            return
        # 查订阅者 + 任务信息（JOIN 取 name/repo_key；订阅表缺失时抛错被外层捕获）
        # mgmt 归属原语：订阅表与任务表同属 mgmt，JOIN 不跨域；SQL 只在归属域留一份，
        # 任意拓扑可用（自有 DSN → mgmt 连接；单库 → 进程主库）。
        from src.domains.mgmt import public as _mgmt_public
        rows = _mgmt_public.autopilot_subscribers_any(task_id)
        if not rows:
            return
        _task_name = rows[0][1] or f"task-{task_id}"
        _repo_key = rows[0][2] or ""
        # Web 详情页 URL（前端 autopilot 页路由）
        _web_url = ""
        try:
            from src.kernel.prompt_builder import _resolve_server_url
            _web_url = f"{_resolve_server_url(0)}/autopilot"
        except Exception:
            pass
        card = build_exec_card(_task_name, _repo_key, exec_id, status,
                               result_summary, findings_result, _web_url)
        # 触达链：本人 bot + open_id（复用 issues_center 既有函数，不回落他人 bot）
        from src.api.issues_center import _get_feishu_open_id_by_user_id, _resolve_user_chat_bot
        from src.domains.runtime.im.engine_wiring import feishu_api
        _loop = None
        try:
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            _loop = None
        for user_id, _, _ in rows:
            try:
                open_id = _get_feishu_open_id_by_user_id(user_id) or ""
                ctx = _resolve_user_chat_bot(None, user_id)
                # 旧 IM 下线 A2 M2：api 不再来自 BotManager，按本人 bot 的 app_id 取共享客户端
                api = feishu_api(ctx.app_id) if ctx is not None else None
                _coro = _run_autopilot_notify(ctx, api, user_id, open_id,
                                              card, task_id, exec_id)
                if _loop is not None:
                    _loop.create_task(_coro)
                else:
                    # 无运行中 loop 时 create_task 的 task 永不执行（通知静默丢失）；
                    # 这条路径常在地线程/同步上下文里被调用，直接跑完再说。
                    asyncio.run(_coro)
            except Exception as e:
                log.warning(f"[autopilot-notify] task={task_id} exec={exec_id} user={user_id} "
                            f"resolve failed: {e}")
    except Exception as e:
        log.warning(f"[autopilot-notify] task={task_id} exec={exec_id} notify failed: {e}")

def _update_execution_result(exec_id: int, status: str, result: str = "",
                             error: str = "", findings_json: str = "", error_code: str = ""):
    """更新 autopilot_executions 执行结果。

    Issue #3800：`error_code` 记录失败分类（`ENGINE_OFFLINE` / `BINDING_STALE` / 空=普通失败），
    调度收口据此改走退避/熔断通道。列缺失（迁移未跑的极端场景）时降级为不写该列，
    保证结果回写本身不因新列而失败。
    """
    def _write(with_code: bool) -> None:
        fields = ["status=?"]
        vals = [status]
        if result:
            fields.append("result=?")
            vals.append(result)
        if error:
            fields.append("error=?")
            vals.append(error)
        if findings_json:
            fields.append("findings_json=?")
            vals.append(findings_json)
        if with_code and error_code:
            fields.append("error_code=?")
            vals.append(error_code)
        if status in ("success", "failed", "cancelled"):
            fields.append("completed_at=?")
            vals.append(datetime.now(timezone.utc).isoformat())
        vals.append(exec_id)
        with db_lock:
            c = db.cursor()
            # 终态幂等：已 success/failed/cancelled 的执行不再覆盖——
            # 用户杀死(cancelled)后，轮询/迟到的完成回写不得把状态洗掉
            _cur = c.execute("SELECT status FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()
            if _cur and _cur[0] in ("success", "failed", "cancelled"):
                return
            c.execute(
                f"UPDATE autopilot_executions SET {', '.join(fields)} WHERE id=?",
                vals
            )
            db.commit()

    try:
        _write(with_code=bool(error_code))
    except Exception as e:
        if error_code:
            try:
                _write(with_code=False)
                log.warning(f"Autopilot: exec {exec_id} error_code column unavailable (1054?), "
                            f"result written without it: {e}")
                return
            except Exception as e2:
                log.warning(f"Autopilot: update execution {exec_id} failed: {e2}")
                return
        log.warning(f"Autopilot: update execution {exec_id} failed: {e}")


def _enqueue_failure_code(exc: BaseException) -> str:
    """入队异常 → 失败分类（Issue #3963）。

    - 结构化异常（`RepoDispatchBusyError.error_code`）优先：显式分类永远可信；
    - 旧写入方/跨进程序列化丢了属性时，按 service 侧稳定文案指纹回推 busy；
    - 其它异常返回空串（普通失败，保持原语义）。
    """
    _code = getattr(exc, "error_code", "") or ""
    if _code:
        return str(_code)
    if _BUSY_LOG_FINGERPRINT in str(exc or ""):
        return ERROR_REPO_DISPATCH_BUSY
    return ""


def _classify_env_error(error_code: str, error: str) -> str:
    """Issue #3827：`error_code` 为空时按稳定指纹回推环境性失败分类。

    仅用于「列缺失/旧写入方没写 error_code」的存量库兜底：显式分类永远优先，
    普通失败（文案不含环境性指纹）保持原样。指纹是本模块 `_binding_failure_code`
    自己产出的字面子串（Loki 看板同源），不是随手解析的第三方文案。
    """
    if error_code:
        return error_code
    _err = error or ""
    if _OFFLINE_LOG_FINGERPRINT in _err:
        return ERROR_ENGINE_OFFLINE
    if _STALE_LOG_FINGERPRINT in _err:
        return ERROR_BINDING_STALE
    # Issue #3963：存量库缺 `error_code` 列时，busy 同样要靠文案指纹回推——否则瞬态
    # 通道会在老库上整条断掉，又退回「0 重试即 failed」。
    if _BUSY_LOG_FINGERPRINT in _err:
        return ERROR_REPO_DISPATCH_BUSY
    return ""


def _read_exec_state(exec_id: int) -> tuple[str, str, str]:
    """读执行记录终态三元组 (status, error, error_code)；老库缺 error_code 列时回落。

    缺列回落路径不再把分类丢弃（Issue #3827）：`error_code` 缺失时按稳定指纹从
    `error` 回推 ENGINE_OFFLINE / BINDING_STALE，保证退避/熔断链路不被缺列整条断掉。
    """
    try:
        with db_lock:
            c = db.cursor()
            try:
                row = c.execute(
                    "SELECT status, error, error_code FROM autopilot_executions WHERE id=?",
                    (exec_id,)).fetchone()
            except Exception:
                row = c.execute(
                    "SELECT status, error FROM autopilot_executions WHERE id=?",
                    (exec_id,)).fetchone()
                _err = (row[1] if row else "") or ""
                return ((row[0] if row else "unknown"), _err,
                        _classify_env_error("", _err))
        _err = (row[1] if row else "") or ""
        _code = (row[2] if len(row) > 2 else "") or ""
        return ((row[0] if row else "unknown"), _err, _classify_env_error(_code, _err))
    except Exception as e:
        log.warning(f"Autopilot: read execution {exec_id} state failed: {e}")
        return ("unknown", "", "")

def _get_enabled_tasks() -> list[dict]:
    """获取所有启用的 autopilot 任务。"""
    # autopilot_tasks 归 mgmt：走归属域任意拓扑原语（列序与原查询逐字一致）
    from src.domains.mgmt import public as _mgmt_public
    rows = _mgmt_public.list_enabled_autopilot_tasks_any()
    tasks = []
    skipped: list[str] = []
    for r in rows:
        # 指令为空的任务不能派发：创建端已经拒收空指令（mgmt 路由 instruction_not_empty），
        # 但存量行（历史数据/直接改库）仍然 enabled，调度器不拦的话会按 cron 反复生成
        # 空壳 execution 与空壳 agent_task（实测每 30 分钟一次，且执行必然空转失败）。
        if not (r[4] or "").strip():
            skipped.append(f"{r[0]}({r[1]})")
            continue
        tasks.append({
            "id": r[0], "name": r[1], "repo_key": r[2],
            "agent_type": r[3], "instruction": r[4], "skill_name": r[5],
            "cron": r[6], "max_retry": r[7], "created_by": r[8] if len(r) > 8 else 0,
            "engine_runtime_ids": r[9] if len(r) > 9 else "",
            "custom_fields": r[10] if len(r) > 10 else "",
            "work_branch": r[11] if len(r) > 11 else "",
            # Issue #1011 姊妹字段：uid 绑定（非空优先，ids legacy 只读回落）
            "engine_runtime_uids": r[12] if len(r) > 12 else "",
            # Issue #3800：退避/熔断状态（列序见 mgmt `autopilot_tasks_enabled_full`）
            "consecutive_failures": int(r[13] or 0) if len(r) > 13 else 0,
            "last_error_code": (r[14] or "") if len(r) > 14 else "",
            "offline_since": (r[15] or "") if len(r) > 15 else "",
            "backoff_until": (r[16] or "") if len(r) > 16 else "",
            "circuit_paused": 1 if (len(r) > 17 and r[17]) else 0,
        })
    if skipped:
        log.warning(
            f"Autopilot: skipped {len(skipped)} enabled task(s) with empty instruction: "
            f"{', '.join(skipped)}（补齐指令或停用后再派发）")
    return tasks

def _is_cron_due(cron_expr: str, last_run: str) -> bool:
    """判断 cron 表达式是否在当前时间窗口（最近 60s）内到期。"""
    try:
        from croniter import croniter
        now = _scheduler_now()
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run.replace(" ", "T"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.astimezone()
            except Exception:
                last_dt = now
            cron = croniter(cron_expr, last_dt)
            next_fire = cron.get_next(datetime)
            if next_fire.tzinfo is None:
                next_fire = next_fire.astimezone()
            return next_fire <= now
        else:
            cron = croniter(cron_expr, now)
            prev_fire = cron.get_prev(datetime)
            if prev_fire.tzinfo is None:
                prev_fire = prev_fire.astimezone()
            delta = (now - prev_fire).total_seconds()
            return delta < _SCHEDULER_INTERVAL
    except Exception as e:
        log.warning(f"Autopilot: cron parse failed '{cron_expr}': {e}")
        return False

def _compute_next_run(cron_expr: str) -> str:
    """计算 cron 表达式的下次触发时间（本地时区，ISO 格式）。"""
    try:
        from croniter import croniter
        now_local = _scheduler_now()
        cron = croniter(cron_expr, now_local)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.astimezone()
        return nxt.isoformat()
    except Exception:
        return ""


def _cron_period_seconds(cron_expr: str) -> int:
    """cron 周期秒数（相邻两次触发差）；解析失败按调度扫描周期兜底。"""
    try:
        from croniter import croniter
        cron = croniter(cron_expr or "", _scheduler_now())
        first = cron.get_next(datetime)
        second = cron.get_next(datetime)
        period = int(round((second - first).total_seconds()))
        if period > 0:
            return max(period, _BACKOFF_MIN_BASE_SECONDS)
    except Exception as e:
        log.warning(f"Autopilot: cron period parse failed '{cron_expr}': {e}")
    return _SCHEDULER_INTERVAL


def _compute_backoff_next_run(cron_expr: str, consecutive_failures, now=None) -> str:
    """环境性失败的指数退避：`now + min(base·2^(n-1), 1800s)`。

    base = max(cron 周期, 60s)：60s cron 下退避序列为 60/120/240/480/960 → 封顶 1800s，
    1 小时内环境性失败最多产生 4 条 exec（Issue #3800 验收），而不是 60 条。
    """
    _now = now if now is not None else _scheduler_now()
    n = max(int(consecutive_failures or 1), 1)
    base = _cron_period_seconds(cron_expr)
    delay = min(base * (2 ** (n - 1)), _BACKOFF_CAP_SECONDS)
    return (_now + timedelta(seconds=delay)).isoformat()


def _json_list(raw) -> list:
    """宽松解析 JSON 数组列（已是 list 则原样返回；非法/空 → 空列表）。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return list(data) if isinstance(data, list) else []


def _probe_task_binding(task: dict, repo_key: str | None = None) -> dict:
    """任务级引擎绑定探测（Issue #3800 唯一 seam，派发侧与自愈侧共用）。

    返回：
      - `mode`：`"uid"`（`engine_runtime_uids` 非空，生态优先）/ `"ids"`（legacy）/ `""`（无任务级绑定）
      - `uids` / `ids`：原绑定值
      - `members`：`agent_runtimes` 中命中的行（**不看健康**——查无此行才是「悬空绑定」）
      - `healthy`：`members` 中健康可达者（复用 engine_select._health_ok 单一真尺子）
      - `engine_ids`：命中的 runtime_id 列表（日志/告警里的 `engine_ids`）

    分叉判据沿用 #1011：uids 非空优先，ids 仅在 uids 为空时回落。
    """
    out = {"mode": "", "uids": [], "ids": [], "members": [], "healthy": [],
           "engine_ids": [], "error": ""}
    if not task:
        return out
    raw_uids = _json_list(task.get("engine_runtime_uids"))
    raw_ids = _json_list(task.get("engine_runtime_ids"))
    uids = [str(u).strip() for u in raw_uids if str(u or "").strip()]
    ids = [int(str(x).strip()) for x in raw_ids if str(x).strip().isdigit()]
    if not uids and not ids:
        return out
    repo = repo_key if repo_key is not None else (task.get("repo_key") or "")
    try:
        from src.domains.runtime.engine.engine_select import (
            _engines_by_ids, _engines_by_uids, _health_ok, _norm_repo, load_engine_selection,
        )
        _repo = _norm_repo(repo)
        if uids:
            out["mode"] = "uid"
            out["uids"] = uids
            out["members"] = _engines_by_uids(uids, _repo) or []
        else:
            out["mode"] = "ids"
            out["ids"] = ids
            out["members"] = _engines_by_ids(ids, _repo, load_engine_selection()) or []
        out["healthy"] = [m for m in out["members"] if _health_ok(m)]
        out["engine_ids"] = [m.get("runtime_id") for m in out["members"]]
    except Exception as e:  # 探测失败不判死：交回原有「无可用引擎」路径
        out["error"] = str(e)
        log.warning(f"Autopilot: probe task binding failed: {e}")
    return out


def _binding_failure_code(probe: dict) -> tuple[str, str]:
    """绑定探测结果 → 环境性失败分类 `(error_code, error_msg)`；可派发时返回 `("", "")`。

    - 绑定存在且至少一个健康引擎 → 可派发；
    - 绑定存在但全不健康 → `ENGINE_OFFLINE`（环境性，重试无意义）；
    - 绑定 uid/id 在 `agent_runtimes` 中查无此行 → `BINDING_STALE`（需重绑，不是等引擎上线）；
    - 探测本身失败（DB 异常，`probe["error"]` 非空）→ 返回 `("", "")` 不分类：
      一次瞬时抖动不该被记成「绑定悬空」并触发重绑告警，交回原有「无可用引擎」路径。
    """
    if not probe or probe.get("error") or not probe.get("mode") or probe.get("healthy"):
        return ("", "")
    if probe.get("members"):
        _is_uid = probe.get("mode") == "uid"
        # 报文里报「用户绑的是什么」（uids/ids 原值），不是命中的 runtime_id——
        # 排查者对着任务配置能立刻对上号。
        label = probe.get("uids") if _is_uid else probe.get("ids")
        kind = "uid" if _is_uid else ""
        return (ERROR_ENGINE_OFFLINE,
                f"task engines{'(' + kind + ')' if kind else ''} {label} {_OFFLINE_LOG_FINGERPRINT}")
    if probe.get("mode") == "uid":
        return (ERROR_BINDING_STALE,
                f"bound engine uids {probe.get('uids')} not found in agent_runtimes; "
                f"please rebind task engines")
    return (ERROR_BINDING_STALE,
            f"bound engine ids {probe.get('ids')} not found in agent_runtimes; "
            f"please rebind task engines")


def _task_binding_label(task: dict) -> str:
    """任务绑定标签（日志用，零 DB 开销）：uid 优先，回落 legacy ids。"""
    uids = _json_list(task.get("engine_runtime_uids")) if task else []
    if uids:
        return json.dumps([str(u) for u in uids if str(u or "").strip()])
    ids = _json_list(task.get("engine_runtime_ids")) if task else []
    return json.dumps(ids) if ids else "[]"


def _backoff_active(task: dict, now=None) -> bool:
    """退避窗口是否仍然有效（`backoff_until` 未到 → 本 tick 不派发）。"""
    until = _parse_iso_aware(task.get("backoff_until") or "")
    if until is None:
        return False
    return (now if now is not None else _scheduler_now()) < until

def _create_execution(task_id: int, agent_type: str, params_json: str = "") -> int:
    """创建执行记录，返回 execution_id。params_json 记录本次一次性触发参数。"""
    now_str = datetime.now().astimezone().isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_executions "
            "(task_id, status, retry_count, started_at, created_at, agent_type, params_json) "
            "VALUES (?, 'running', 0, ?, ?, ?, ?)",
            (task_id, now_str, now_str, agent_type, params_json or "")
        )
        db.commit()
        return c.lastrowid

def _update_execution(exec_id: int, status: str, result: str = "", error: str = "",
                      retry_count: Optional[int] = None):
    """更新执行记录状态（兼容旧调用，内部转 _update_execution_result）。"""
    _update_execution_result(exec_id, status, result=result, error=error)

def _update_task_run_info(task_id: int, next_run: str):
    """更新任务最近运行时间和下次预计运行时间。"""
    now_str = _scheduler_now().isoformat()
    # P3-5d：autopilot_tasks 是 mgmt 基表；runtime 进程写入走 mgmt lib + 幂等意图
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_ri
    _mg_ri = _mgmt_lib_ri(foreign_only=True)
    if _mg_ri is not None:
        try:
            from src.kernel import cross_write as _xw_ri
            _res_ri = _xw_ri.submit(
                "mgmt", "autopilot_run_info_set",
                _xw_ri.unique_key(f"autopilot-run:{task_id}:{now_str}"),
                {"task_id": int(task_id), "last_run_at": now_str, "next_run_at": next_run},
                lambda: _mg_ri.set_autopilot_run_info(int(task_id), now_str, next_run))
            if _res_ri.get("status") == "failed":
                log.warning(f"_update_task_run_info queued for retry: {_res_ri.get('error')}")
            return
        except Exception as _e_ri:
            log.warning(f"_update_task_run_info lib path failed, fallback to local SQL: {_e_ri}")
    # 兜底：归属域任意拓扑原语（三库拓扑 → mgmt 连接；单库 → 进程主库 + db_lock）
    from src.domains.mgmt import public as _mgmt_public
    _mgmt_public.set_autopilot_run_info_any(task_id, now_str, next_run)

def _parse_iso_aware(value):
    """解析 ISO 时间串为 aware datetime（无时区按 UTC 补齐），失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _write_failure_state(task_id: int, consecutive_failures, last_error_code: str,
                         offline_since: str, backoff_until: str, circuit_paused,
                         updated_at: str) -> None:
    """写回「运行时间 + 退避/熔断状态」（Issue #3800，跨域写 mgmt 基表）。

    一次 UPDATE 同时落 `last_run_at/next_run_at` 与失败状态，避免两次跨域写出现
    「状态写了、下次运行时间没写」的半写；通道与 `_update_task_run_info` 一致
    （mgmt lib 意图优先，归属域任意拓扑原语兜底）。
    """
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_fs
    _mg_fs = _mgmt_lib_fs(foreign_only=True)
    if _mg_fs is not None and hasattr(_mg_fs, "set_autopilot_failure_state"):
        try:
            from src.kernel import cross_write as _xw_fs
            _res_fs = _xw_fs.submit(
                "mgmt", "autopilot_failure_state_set",
                _xw_fs.unique_key(f"autopilot-fail:{task_id}:{updated_at}"),
                {"task_id": int(task_id), "consecutive_failures": int(consecutive_failures or 0),
                 "last_error_code": last_error_code or "", "offline_since": offline_since or "",
                 "backoff_until": backoff_until or "",
                 "circuit_paused": 1 if circuit_paused else 0, "updated_at": updated_at},
                lambda: _mg_fs.set_autopilot_failure_state(
                    int(task_id), int(consecutive_failures or 0), last_error_code or "",
                    offline_since or "", backoff_until or "",
                    1 if circuit_paused else 0, updated_at))
            if _res_fs.get("status") == "failed":
                log.warning(f"autopilot failure state queued for retry: {_res_fs.get('error')}")
            return
        except Exception as _e_fs:
            log.warning(f"_write_failure_state lib path failed, fallback to local SQL: {_e_fs}")
    from src.domains.mgmt import public as _mgmt_public_fs
    _mgmt_public_fs.set_autopilot_failure_state_any(
        task_id, consecutive_failures, last_error_code, offline_since, backoff_until,
        circuit_paused, updated_at)


def _clear_failure_state(task_id: int, next_run: str) -> None:
    """清空退避/熔断状态并推进正常 `next_run_at`（环境性失败已恢复）。"""
    now_str = _scheduler_now().isoformat()
    from src.kernel.domain_read import mgmt_lib as _mgmt_lib_cs
    _mg_cs = _mgmt_lib_cs(foreign_only=True)
    if _mg_cs is not None and hasattr(_mg_cs, "clear_autopilot_failure_state"):
        try:
            from src.kernel import cross_write as _xw_cs
            _res_cs = _xw_cs.submit(
                "mgmt", "autopilot_failure_state_clear",
                _xw_cs.unique_key(f"autopilot-clear:{task_id}:{now_str}"),
                {"task_id": int(task_id), "next_run_at": next_run, "updated_at": now_str},
                lambda: _mg_cs.clear_autopilot_failure_state(int(task_id), next_run, now_str))
            if _res_cs.get("status") == "failed":
                log.warning(f"autopilot failure state clear queued for retry: {_res_cs.get('error')}")
            return
        except Exception as _e_cs:
            log.warning(f"_clear_failure_state lib path failed, fallback to local SQL: {_e_cs}")
    from src.domains.mgmt import public as _mgmt_public_cs
    _mgmt_public_cs.clear_autopilot_failure_state_any(task_id, next_run, now_str)


def _alert_task_failure(task: dict, exec_id: int, error_code: str, error: str,
                        consecutive_failures: int, circuit_paused: bool) -> None:
    """一次性结构化告警（Issue #3800）：ERROR 结构化行 + 订阅者通知。

    只在「跨越门槛的那一刻」被调用（`BINDING_STALE` 首派 / `ENGINE_OFFLINE` 熔断），
    因此同一 episode 只发一次；卡片侧幂等仍由 `_notify_subscribers` 的
    `notified=0` 抢占保证（同一 exec 不会重复发送）。
    """
    task_id = task.get("id") or 0
    # 报文里的 engine_ids 一律是「用户绑定了什么」（uid 优先、回落 legacy ids），
    # 与 `_task_binding_label` 同一口径：收到告警的人对着任务配置就能对上号，
    # 而 runtime_id 是命中行才有的内部编号（悬空绑定时根本没有）。
    engine_ids = _json_list(task.get("engine_runtime_uids")) or _json_list(
        task.get("engine_runtime_ids"))
    if error_code == ERROR_BINDING_STALE:
        guidance = ("绑定引擎在引擎列表中已不存在（常见于引擎重注册换了 uid）："
                    "请在自动化任务配置中重新绑定引擎")
    else:
        guidance = ("请恢复绑定引擎（启动 worker / 修复引擎健康）后任务会自动解除暂停并续跑")
    _state = "已自动暂停（熔断）" if circuit_paused else "已进入退避"
    log.error(
        f"[autopilot-alert] task={task_id} error_code={error_code} engine_ids={engine_ids} "
        f"consecutive_failures={consecutive_failures} circuit_paused={int(bool(circuit_paused))} "
        f"error={error or '(no detail)'}")
    summary = (f"错误码 {error_code}；连续失败 {consecutive_failures} 次；绑定引擎 {engine_ids}；"
               f"{_state}。原因：{error or '无'}。{guidance}")
    try:
        _notify_subscribers(task_id, exec_id, "failed", summary, {})
    except Exception as e:  # 通知失败不影响状态已落库
        log.warning(f"[autopilot-alert] task={task_id} exec={exec_id} notify failed: {e}")


def _handle_environmental_failure(task: dict, exec_id: int, error_code: str, error: str) -> dict:
    """环境性失败收口（Issue #3800）：计数 → 退避 → 熔断 → 一次性告警，返回状态快照。

    离线时长取 `offline_since`（落库，进程重启后仍生效）而非引擎 `last_seen_at`：
    熔断判据是「本任务持续撞同一面墙」而不是「某引擎心跳断了多久」。
    """
    task_id = task.get("id") or 0
    now = _scheduler_now()
    prev_code = task.get("last_error_code") or ""
    prev_n = int(task.get("consecutive_failures") or 0)
    same_cause = (prev_code == error_code)
    consecutive = (prev_n + 1) if same_cause else 1
    offline_since = (task.get("offline_since") or "") if same_cause else ""
    offline_since = offline_since or now.isoformat()
    _since = _parse_iso_aware(offline_since)
    offline_seconds = max((now - _since).total_seconds(), 0.0) if _since else 0.0
    was_paused = bool(task.get("circuit_paused"))
    circuit_paused = was_paused or (
        consecutive >= _CIRCUIT_THRESHOLD
        and (error_code == ERROR_BINDING_STALE
             or offline_seconds >= _CIRCUIT_MIN_OFFLINE_SECONDS))
    next_run = _compute_backoff_next_run(task.get("cron") or "", consecutive, now)
    state = {
        "consecutive_failures": consecutive,
        "last_error_code": error_code,
        "offline_since": offline_since,
        "backoff_until": next_run,
        "circuit_paused": 1 if circuit_paused else 0,
    }
    _write_failure_state(task_id, consecutive, error_code, offline_since, next_run,
                         circuit_paused, now.isoformat())
    task.update(state)

    # 告警恰好一次：悬空绑定首派即告警（等下 3 次太晚，用户需要重绑指引）；
    # 全离线则等到「跨越熔断门槛」那一刻，避免抖动刷屏。
    should_alert = ((error_code == ERROR_BINDING_STALE and consecutive == 1)
                    or (error_code == ERROR_ENGINE_OFFLINE
                        and circuit_paused and not was_paused))
    _error_line = error or ""
    if error_code == ERROR_ENGINE_OFFLINE and _OFFLINE_LOG_FINGERPRINT not in _error_line:
        _error_line = f"{_error_line} {_OFFLINE_LOG_FINGERPRINT}".strip()
    log.error(
        f"Autopilot task={task_id} exec={exec_id} status=failed error_code={error_code} "
        f"engine_ids={_task_binding_label(task)} n={consecutive} "
        f"consecutive_failures={consecutive} "
        f"offline_seconds={int(offline_seconds)} circuit_paused={int(bool(circuit_paused))} "
        f"backoff_until={next_run} error={_error_line or '(no detail)'}")
    if should_alert:
        _alert_task_failure(task, exec_id, error_code, _error_line, consecutive, circuit_paused)
    return state


def _try_resume_task(task: dict) -> bool:
    """熔断自愈（Issue #3800）：绑定引擎恢复健康 / 悬空绑定重新存在 → 解除暂停。

    无任务级绑定的任务（广播池）不参与熔断，直接放行（健康由派发侧判定）。
    """
    probe = _probe_task_binding(task)
    if probe.get("mode") and not probe.get("healthy"):
        return False
    task_id = task.get("id") or 0
    _prev_code = task.get("last_error_code") or ""
    next_run = _compute_next_run(task.get("cron") or "")
    try:
        _clear_failure_state(task_id, next_run)
    except Exception as e:
        log.warning(f"Autopilot: task {task_id} clear failure state failed: {e}")
        return False
    task.update({"consecutive_failures": 0, "last_error_code": "", "offline_since": "",
                 "backoff_until": "", "circuit_paused": 0})
    log.info(
        f"Autopilot: task {task_id} engine binding recovered (was error_code={_prev_code}, "
        f"engines={probe.get('engine_ids')}) -> circuit closed, resume on next due tick")
    return True


def _scan_task_once(task: dict, last_run: str) -> str:
    """单任务单轮派发裁决（scheduler_loop 与假时钟测试共用的同一条判定路径）。

    返回：`dispatch` / `paused`（熔断未解除）/ `backoff`（退避窗口内）/ `not-due`。
    """
    task_id = task.get("id") or 0
    if task.get("circuit_paused"):
        if not _try_resume_task(task):
            return "paused"
    if not _is_cron_due(task.get("cron") or "", last_run):
        return "not-due"
    if _backoff_active(task):
        log.info(
            f"Autopilot: task {task_id} in engine-offline backoff until "
            f"{task.get('backoff_until')} (error_code={task.get('last_error_code')}, "
            f"consecutive_failures={task.get('consecutive_failures')}), skip this tick")
        return "backoff"
    log.info(f"Autopilot: cron due for task {task_id} ('{task.get('name')}')")
    return "dispatch"

_AGENT_TASK_SELECT = "id, status, result_text, exit_code, skill, env_extra, runtime_id"

def _agent_task_view(row):
    """将 agent_tasks 行（`_AGENT_TASK_SELECT` 列序）映射为 evidence 校验用 dict。"""
    return {
        "status": row[1] or "",
        "result_text": row[2] or "",
        "skill": row[4] or "",
        "env_extra": row[5] or "",
        "runtime_id": row[6] or 0,
    }

def _get_exec_agent_task(c, exec_id, agent_task_id="", started_at="", created_at=""):
    """按执行记录解析对应 agent_tasks 行（Issue #1546）。

    入参为执行记录的显式字段（调用方按列名取值后传入，避免行内魔数下标契约）：
    `exec_id`（= events 序列旁路关联键）、`agent_task_id`、`started_at`、`created_at`。

    - `agent_task_id` 非空：按任务主键 `id` 直查（唯一权威关联）；
    - 为空（历史行）：受约束兜底 `event_seq=? AND event_type='autopilot_event'
      AND created_at >= exec.started_at`（started_at 空则用 created_at，aware
      比较；started_at/created_at 为空或解析失败均视为不匹配）；
    - 两路皆无 → 返回 None（调用方跳过，禁止退回裸 event_seq）。

    返回列序：id, status, result_text, exit_code, skill, env_extra, runtime_id。
    """
    _atid = str(agent_task_id or "").strip()
    if _atid:
        try:
            return c.execute(
                f"SELECT {_AGENT_TASK_SELECT} FROM agent_tasks WHERE id=? LIMIT 1",
                (_atid,)).fetchone()
        except Exception as e:
            log.warning(f"Autopilot: lookup agent_task {_atid} failed: {e}")
            return None

    if started_at:
        _since = _parse_iso_aware(started_at)
    else:
        _since = _parse_iso_aware(created_at)
    if _since is None:
        return None
    try:
        _rows = c.execute(
            f"SELECT {_AGENT_TASK_SELECT}, created_at FROM agent_tasks "
            "WHERE event_seq=? AND event_type='autopilot_event' "
            "ORDER BY created_at DESC LIMIT 5",
            (exec_id,)).fetchall()
    except Exception as e:
        log.warning(f"Autopilot: legacy agent_task fallback failed for exec {exec_id}: {e}")
        return None
    for _r in _rows:
        _seen = _parse_iso_aware(_r[7])
        if _seen is not None and _seen >= _since:
            return _r
    return None

async def _poll_dispatched_completion(exec_id: int, timeout: int = 3600,
                                     poll_interval: float = 10.0) -> str:
    """轮询 dispatched 状态的 exec 等待 worker 完成执行。

    通过 agent_task_id 直连优先、历史空值按 event_type+started_at 兜底解析任务，
    据其状态推断 autopilot_executions 终态：
    - agent_tasks status=completed/success → exec success（部署类任务先过 evidence 校验）
    - agent_tasks status=failed → exec failed
    - agent_tasks status=timeout_failed → exec failed（带超时说明，不等自身 3600s）
    - agent_tasks status 仍为 queued/dispatched/running/pending_self_report → 继续等
      （pending_self_report 为 Issue #537 主控停机中间态，视同 running 等待 worker 自报）
    - 超时 → exec failed (timeout)
    """
    import time as _time
    _start = _time.time()
    while _time.time() - _start < timeout:
        await asyncio.sleep(poll_interval)
        try:
            with db_lock:
                c = db.cursor()
                _erow = c.execute(
                    "SELECT agent_task_id, started_at, created_at "
                    "FROM autopilot_executions WHERE id=?",
                    (exec_id,)).fetchone()
                if _erow is None:
                    row = None
                else:
                    _atid, _started, _created = _erow
                    # Issue #1546：优先按 agent_task_id 直连，历史行受约束兜底；无命中跳过
                    row = _get_exec_agent_task(c, exec_id, _atid, _started, _created)
            if not row:
                continue
            _at_status = row[1] or ""
            _result_text = row[2] or ""
            _exit_code = row[3]
            _at_task = _agent_task_view(row)

            if _at_status in ("completed", "success"):
                # 部署类任务收口前证据校验（Issue #537 P1-1 纵深防御）
                # QA R1 三表终态一致性取舍：同 finalize——evidence 判失败仅 exec 侧 failed，
                # 任务/事件侧保留 completed（worker 已完成），错误详情写入 exec.error。
                try:
                    from src.domains.runtime.engine.deploy_evidence import check_deploy_evidence
                    _evd = check_deploy_evidence(_at_task)
                except Exception:
                    _evd = {"checked": False}
                if _evd.get("checked") and not _evd.get("passed"):
                    _update_execution_result(exec_id, "failed",
                                             result=_result_text[:5000],
                                             error=f"部署证据校验未通过: {_evd.get('reason', '')}")
                    log.info(f"Autopilot poll exec {exec_id}: -> failed (deploy evidence: {_evd.get('reason')})")
                    return "failed"
                # worker 完成，同步终态到 autopilot_executions
                findings_result = parse_findings(_result_text)
                # result 只存 summary（不存原始工作日志），解析失败时降级存最后 500 字
                if findings_result["summary"]:
                    _result_summary = findings_result["summary"]
                else:
                    _result_summary = _result_text[-500:] if _result_text else ""
                _update_execution_result(exec_id, "success",
                                         result=_result_summary,
                                         findings_json=json.dumps(findings_result, ensure_ascii=False) if findings_result["findings"] else "")
                _notify_subscribers(_exec_task_id(exec_id), exec_id, "success",
                                    _result_summary, findings_result)
                return "success"
            elif _at_status == "cancelled":
                # 用户杀死：原样同步 cancelled（不翻译成 failed）
                _update_execution_result(exec_id, "cancelled",
                                         result=_result_text[:5000],
                                         error="用户手动终止")
                _notify_subscribers(_exec_task_id(exec_id), exec_id, "cancelled",
                                    _result_text[:500], {})
                return "cancelled"
            elif _at_status == "failed":
                _update_execution_result(exec_id, "failed",
                                         result=_result_text[:5000],
                                         error="agent_tasks status=failed")
                _notify_subscribers(_exec_task_id(exec_id), exec_id, "failed",
                                    _result_text[:500], {})
                return "failed"
            elif _at_status == "timeout_failed":
                # Issue #537：pending_self_report 超时兜底终态 → poll 快速收口 failed
                _update_execution_result(exec_id, "failed",
                                         result=_result_text[:5000],
                                         error="agent_tasks status=timeout_failed（worker 未自报超时）")
                return "failed"
            elif _at_status in ("running", "pending_self_report"):
                # worker 已认领并开始执行（pending_self_report = 主控停机等自报），
                # 同步 autopilot_executions 为 running
                _update_execution_result(exec_id, "running")
            # queued/dispatched → 继续等
        except Exception as e:
            log.warning(f"Autopilot poll exec {exec_id}: query failed: {e}")

    # 超时
    _update_execution_result(exec_id, "failed", error=f"worker 执行超时 ({timeout}s)")
    _notify_subscribers(_exec_task_id(exec_id), exec_id, "failed",
                        f"worker 执行超时 ({timeout}s)", {})
    return "failed"

async def _execute_task(task: dict):
    """执行单个 autopilot 任务（含重试逻辑）。"""
    task_id = task["id"]
    if not (task.get("instruction") or "").strip():
        # 手动触发也走这里：空指令不可能产出有意义的任务，直接拒绝并留痕，
        # 不要建 execution/agent_task 再在 agent 侧空转（见 _get_enabled_tasks 同名守卫）。
        log.warning(f"Autopilot: task {task_id} has empty instruction, refuse to dispatch")
        return
    if task_id in _RUNNING_TASKS:
        log.info(f"Autopilot: task {task_id} already running, skip")
        return
    _RUNNING_TASKS.add(task_id)
    # 触发即写 last_run_at/next_run_at（防执行窗口内主控重启导致 cron 重新判定到期：
    # _is_cron_due 用 last_run_at 推算，若只在任务结束后写，执行期间的 20 分钟窗口内
    # 服务重启会丢失 _RUNNING_TASKS 内存互斥 → 同一 cron 被再次触发 → 自触发循环。
    # 2026-09-28 实际发生：8 次重启 + 17 个僵尸 execution + 生产被误停一轮）
    try:
        _early_next = _compute_next_run(task["cron"])
        _update_task_run_info(task_id, _early_next)
    except Exception as _e:
        log.warning(f"Autopilot: task {task_id} early next_run write failed: {_e}")
    try:
        max_retry = task["max_retry"]
        agent_type = _FIXED_AGENT_TYPE

        _run_params = task.get("_run_params") or {}
        _params_json = json.dumps(_run_params, ensure_ascii=False) if _run_params else ""
        # Issue #3800：环境性失败（绑定引擎离线/悬空）单独收口——不消耗重试、不推进 cron，
        # 交给退避/熔断状态机。`_env_failure` 为本次派发的分类结果（空=普通失败路径）。
        _env_failure = None
        # Issue #3963：repo-dispatch 租约争用走独立瞬态通道——`busy_deferred` 非空表示
        # 同 tick 内的瞬态预算已耗尽，本次不判 failed，requeue 到下一 tick 重投。
        _busy_retries = 0
        _busy_deferred = None
        attempt = 0
        while attempt <= max_retry:
            exec_id = _create_execution(task_id, agent_type, _params_json)
            log.info(f"Autopilot: executing task {task_id} ('{task['name']}'), exec_id={exec_id}, attempt={attempt}")

            # 在线程池中执行 agent（不阻塞事件循环）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _execute_autopilot_agent, task["repo_key"], task, exec_id)

            # 检查执行结果（status/error/error_code 一并取出：error_code 是 #3800 的分类依据）
            exec_status, exec_error, exec_error_code = _read_exec_state(exec_id)

            # REMOTE 模式下状态为 "dispatched"，需要轮询等待 worker 完成
            if exec_status == "dispatched":
                log.info(f"Autopilot: task {task_id} exec {exec_id} dispatched, polling worker completion...")
                exec_status = await _poll_dispatched_completion(exec_id, timeout=3600)
                # 轮询收口后重读原因/错误码（轮询侧的失败写可能带 error_code）
                _, exec_error, exec_error_code = _read_exec_state(exec_id)

            if exec_status == "success":
                log.info(f"Autopilot: task {task_id} exec {exec_id} success (attempt {attempt})")
                break
            if exec_error_code == ERROR_REPO_DISPATCH_BUSY:
                # Issue #3963：同仓 enqueue 撞租约——瞬态争用，不消耗 attempt。
                # 预算内按 1/2/4s（+抖动）在同 tick 重试；耗尽则 requeue 下一 tick。
                # 判定顺序在 skipped/cancelled 之前：busy 的 execution 行本身就是 skipped。
                if _busy_retries < _BUSY_RETRY_BUDGET:
                    _delay = _BUSY_RETRY_DELAYS[min(_busy_retries, len(_BUSY_RETRY_DELAYS) - 1)]
                    if _BUSY_RETRY_JITTER_RATIO > 0:
                        import random as _random
                        _delay *= 1.0 + _random.uniform(-_BUSY_RETRY_JITTER_RATIO, _BUSY_RETRY_JITTER_RATIO)
                    _busy_retries += 1
                    log.warning(
                        f"Autopilot: task {task_id} exec {exec_id} {_BUSY_LOG_FINGERPRINT} "
                        f"(transient, attempt={attempt} untouched, retry {_busy_retries}/"
                        f"{_BUSY_RETRY_BUDGET} in {_delay:.1f}s)")
                    await asyncio.sleep(max(0.0, _delay))
                    continue
                _busy_deferred = (exec_id, exec_error)
                break
            if exec_status == "skipped":
                # Issue #3201：接管守门拦截 → 非失败，立即终止，不重试、不消耗执行次数
                log.info(f"Autopilot: task {task_id} exec {exec_id} skipped under active takeover, stop")
                break
            if exec_status == "cancelled":
                # 用户杀死：立即停止，不再重试
                log.info(f"Autopilot: task {task_id} exec {exec_id} cancelled by user, stop")
                break
            if exec_error_code in _ENV_FAILURE_CODES:
                # 环境性失败（D4）：重试无意义（任务 83 即 max_retry=0 仍每 tick 建 exec），
                # 直接跳出重试循环进入退避/熔断裁决。
                log.warning(
                    f"Autopilot: task {task_id} exec {exec_id} environmental failure "
                    f"error_code={exec_error_code}, hand over to backoff/circuit: {exec_error} "
                    f"retries_attempted={attempt + 1}/{max_retry + 1} max_retry={max_retry} "
                    f"retry_skip=\"{_retry_skip_reason(max_retry, attempt, exec_error_code)}\"")
                _env_failure = (exec_id, exec_error_code, exec_error)
                break
            if attempt < max_retry:
                error_msg = exec_error or "unknown"
                delay = min(_BASE_RETRY_DELAY * (2 ** attempt), _MAX_RETRY_DELAY)
                log.warning(f"Autopilot: task {task_id} exec {exec_id} attempt {attempt} failed ({exec_status}), "
                            f"retrying in {delay}s: {error_msg}")
                await asyncio.sleep(delay)
                attempt += 1
            else:
                # Issue #3897：此前是 `error=failed after {max_retry} retries`——max_retry=0 时
                # 既无原因也不解释为何不重试。现在恒带 cause + retries_attempted + retry_skip
                # （环境性失败/任务配置 max_retry=0/已达上限三种可读原因）。
                log.error(
                    f"Autopilot task={task_id} exec={exec_id} status={exec_status} "
                    f"error_code={exec_error_code or ''} engine_ids={_task_binding_label(task)} "
                    f"consecutive_failures={int(task.get('consecutive_failures') or 0)} "
                    f"retries_attempted={attempt + 1}/{max_retry + 1} max_retry={max_retry} "
                    f"retry_skip=\"{_retry_skip_reason(max_retry, attempt, exec_error_code)}\" "
                    f"error={exec_error or '<no cause recorded by executor>'}")
                break

        if _env_failure is not None:
            _env_exec_id, _env_code, _env_error = _env_failure
            _handle_environmental_failure(task, _env_exec_id, _env_code, _env_error)
        else:
            if _busy_deferred is not None:
                # Issue #3963：瞬态预算耗尽 → requeue 下一 tick。execution 行已是
                # skipped（非终态，可二次写入），补一句「已重投」说明供管理台/排查；
                # 不置 failed、不推进 consecutive_failures/backoff/熔断，定时任务不丢。
                _b_exec_id, _b_error = _busy_deferred
                _b_msg = (f"{_BUSY_LOG_FINGERPRINT} {task['repo_key']}（排队冲突，瞬态预算已耗尽，"
                          f"已 requeue 下一 tick 重投）")
                log.warning(
                    f"Autopilot: task {task_id} exec {_b_exec_id} repo dispatch busy after "
                    f"{_BUSY_RETRY_BUDGET} transient retries, requeue next tick: {_b_error}")
                _update_execution_result(_b_exec_id, "skipped", error=_b_msg,
                                         error_code=ERROR_REPO_DISPATCH_BUSY)
            next_run = _compute_next_run(task["cron"])
            # 环境性失败已恢复（本轮成功/跳过/普通失败/busy requeue）→ 清空退避与熔断状态。
            if task.get("circuit_paused") or int(task.get("consecutive_failures") or 0):
                _clear_failure_state(task_id, next_run)
                task.update({"consecutive_failures": 0, "last_error_code": "",
                             "offline_since": "", "backoff_until": "", "circuit_paused": 0})
            _update_task_run_info(task_id, next_run)
    except Exception as e:
        log.error(f"Autopilot: task {task_id} execution exception: {e}")
    finally:
        _RUNNING_TASKS.discard(task_id)

def _offline_duration_seconds(last_seen_at: str):
    """引擎已离线秒数；last_seen_at 缺失/解析失败 → None（信息不足，不判死）。

    parse_dt 返回 epoch 秒（float），兼容 datetime 分支。
    """
    if not last_seen_at:
        return None
    try:
        from src.domains.runtime.engine.health import parse_dt
        _ts = parse_dt(last_seen_at)
        if not _ts:
            return None
        if isinstance(_ts, (int, float)):
            _secs = datetime.now(timezone.utc).timestamp() - float(_ts)
        else:
            _dt = _ts
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            _secs = (datetime.now(timezone.utc) - _dt).total_seconds()
        return max(_secs, 0.0)
    except Exception:
        return None

# Issue #1071：活跃流豁免窗口——pending 任务 task_messages 最新写入距 now < 600s 视为
# 「执行中」（worker 还在产出执行流），跳过本轮判死。600s 与调度扫描周期（60s）量级
# 匹配，避免无限豁免；真沉默任务最多延迟一轮（60s 扫描 + 600s 静默窗）被回收。
_PENDING_STREAM_ACTIVE_WINDOW_SECONDS = 600

def _pending_self_report_stream_active(cursor, task_id: str) -> bool:
    """活跃流豁免判据（Issue #1071）：task_messages 最新一条写入距 now < 600s → True。

    返回 False（回退原判死逻辑，Spec：数据缺失不豁免，宁可误回收不留死任务）：
    - 该任务在 task_messages 无任何记录（执行流从未产出，无法证明在跑）；
    - created_at 缺失/无法解析（parse_dt 失败）；
    - 查询异常（表缺失等）。
    """
    try:
        row = cursor.execute(
            "SELECT created_at FROM task_messages WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        if not row or not (row[0] or "").strip():
            return False
        from src.domains.runtime.engine.health import parse_dt
        _ts = parse_dt(row[0])
        if _ts is None:
            return False
        _age = datetime.now(timezone.utc).timestamp() - float(_ts)
        return _age < _PENDING_STREAM_ACTIVE_WINDOW_SECONDS
    except Exception as _e:
        log.warning(f"pending stream activity check failed for task {task_id}: {_e}")
        return False

def _parse_pending_since(value: str) -> float | None:
    """pending_since → epoch 秒。

    历史坑（#588 / #9495）：executor 曾用 gmtime() 写**无时区**的 "YYYY-MM-DDTHH:MM:SS"，
    而 health.parse_dt 在 MySQL 后端把无时区值按 CST(+08:00) 解释，凭空多 8 小时 →
    刚置 pending 就被判超时。这里单独解析：
    - 带偏移/Z：直接按偏移；
    - 含 "T" 但无偏移：本仓库写入语义是 UTC（executor gmtime）→ 按 UTC；
    - 空格分隔的旧格式：沿用 parse_dt（MySQL=CST 历史口径 / SQLite=UTC）。
    """
    s = (value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        from src.domains.runtime.engine.health import parse_dt
        return parse_dt(s)
    if dt.tzinfo is None:
        if "T" in s:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            from src.domains.runtime.engine.health import parse_dt
            return parse_dt(s)
    return dt.timestamp()

def _sweep_pending_self_report_timeouts() -> int:
    """pending_self_report 超时兜底（Issue #537 P0-1）：worker 未在阈值内自报 → timeout_failed。

    主控停机时 REMOTE 任务被置 pending_self_report；正常路径是 worker 继续执行并
    complete_task/fail_task 自报。若 worker 失联/永远不自报，超过
    PENDING_SELF_REPORT_TIMEOUT_SECONDS（默认 90min，Issue #1071 放宽）→ 判
    timeout_failed（带说明的终态，区别于 plain failed），并同步 events=failed +
    autopilot_executions=failed。

    Issue #1071 活跃流豁免：部署类长任务（打包+生产部署约 40-50 分钟）按设计在执行中
    重启主控，重启瞬间 pending_since 重置；判死前先查 task_messages 最新一条写入时间，
    距 now < 600s 说明 worker 仍在执行（执行流持续产出），跳过本轮判死、下轮再评估；
    无消息/解析失败回退原判死逻辑（数据缺失不豁免）。
    """
    from src.domains.runtime.engine.service import update_task_status
    from src.api.event_store import update_event_complete
    _cutoff_ts = datetime.now(timezone.utc).timestamp() - PENDING_SELF_REPORT_TIMEOUT_SECONDS
    # 「从未真正执行」的短阈值：pending_self_report + runtime_session_id 为空
    # （引擎会话从没建起来）说明任务被认领后根本没跑，不必等 90 分钟自报窗口。
    # 实测：116 上残留的旧代码 agent 抢到任务后既不建会话也不产出消息，事件层 sweeper
    # 30 分钟就判 failed，而任务要挂 90 分钟 —— 需求在「处理中」白白多躺一小时。
    _cutoff_short = datetime.now(timezone.utc).timestamp() - PENDING_NO_SESSION_TIMEOUT_SECONDS
    _now = datetime.now(timezone.utc).isoformat()
    _reason = (f"[PENDING_SELF_REPORT_TIMEOUT] worker 在 {PENDING_SELF_REPORT_TIMEOUT_SECONDS}s 内未自报完成/失败"
               f"且执行流静默超 {_PENDING_STREAM_ACTIVE_WINDOW_SECONDS}s"
               f"（pending_self_report 超时），判 timeout_failed")
    _reason_no_session = (f"[PENDING_NO_SESSION_TIMEOUT] 任务认领后从未建立引擎会话"
                          f"（runtime_session_id 为空、无任务消息），{PENDING_NO_SESSION_TIMEOUT_SECONDS}s 内"
                          f"无任何执行痕迹，判 timeout_failed —— 通常意味着认领方跑的是旧代码或引擎 spawn 失败")
    marked = 0
    _ended = []
    try:
        _pending_rows = []
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, event_seq, runtime_id, pending_since, runtime_session_id FROM agent_tasks "
                "WHERE status='pending_self_report' AND pending_since IS NOT NULL "
                "AND pending_since != ''"
            )
            # 数值比较（parse_dt→epoch）：兼容停机写入的 "%Y-%m-%d %H:%M:%S"（executor）与
            # ISO8601（测试/新写入）混合格式——字符串比较会因 ' '(0x20) < 'T'(0x54)
            # 把停机写入的 pending 误判为已过期（超时窗口形同虚设）。
            from src.domains.runtime.engine.health import parse_dt
            for _r in c.fetchall():
                try:
                    _ts = _parse_pending_since(_r[3])
                except Exception:
                    _ts = None
                if not _ts:
                    continue
                _has_session = bool(str(_r[4] or "").strip())
                _limit = _cutoff_ts if _has_session else max(_cutoff_ts, _cutoff_short)
                if float(_ts) > _limit:
                    continue
                # 活跃流豁免（Issue #1071）：执行流 <600s 内仍在写入 → 任务在跑，
                # 跳过判死等下轮（同一事务内查询，不加额外锁、每候选一条主键查询开销小）。
                if _pending_self_report_stream_active(c, _r[0]):
                    continue
                # 执行流已正常收尾（最后一条是 end_turn）→ agent 干完了，只是那次自报
                # 没送达。判 completed 并保留它原本写入的 result，避免把完成的工作
                # 记成 timeout_failed（2026-11-15 #85/#84：148/27 条消息、结论齐全却判死）。
                try:
                    from src.domains.runtime.engine.stream_ended import stream_ended_normally
                    _ended_ok = stream_ended_normally(c, _r[0])
                except Exception:
                    _ended_ok = False
                if _ended_ok:
                    _ended.append(_r[0])
                    continue
                # 下游按 4 元组解包；把「无会话」标记塞进第 4 位之外的 map
                _pending_rows.append((_r[0], _r[1], _r[2], _r[3], _has_session))
            rows = _pending_rows
        # 先按「执行流正常收尾」收敛：这些任务不需要等自报，直接 completed
        for _etid in _ended:
            try:
                update_task_status(_etid, "completed", exit_code=0,
                                   errors="[STREAM_ENDED_OK] 执行流已正常收尾（end_turn）但自报丢失，按完成收敛",
                                   completed_at=_now, cascade_trigger="sweeper")
                marked += 1
                log.info(f"sweeper: task {_etid} stream ended normally -> completed (self-report was lost)")
            except Exception as _ee:
                log.warning(f"sweeper: converge ended task {_etid} failed: {_ee}")
        rows = [r for r in rows if r[0] not in set(_ended)]
        _reasons = {_r[0]: (_reason if _r[4] else _reason_no_session) for _r in rows}
        for tid, evt_seq, _rt_id, _ps, _hs in rows:
            _why = _reasons.get(tid, _reason)
            try:
                update_task_status(tid, "timeout_failed", exit_code=-1,
                                   errors=json.dumps([_why], ensure_ascii=False),
                                   completed_at=_now,
                                   # Issue #1938：补偿归因（sweeper 超时兜底）
                                   cascade_trigger="sweeper")
                if evt_seq:
                    try:
                        update_event_complete(evt_seq, "failed", exit_code=-1,
                                              completed_at=_now, result=_why[:200])
                    except Exception as _ee:
                        log.warning(f"pending timeout: event sync failed seq={evt_seq}: {_ee}")
                    try:
                        _update_execution_result(evt_seq, "failed", error=_reason)
                    except Exception as _ee2:
                        log.warning(f"pending timeout: autopilot sync failed exec={evt_seq}: {_ee2}")
                log.info(f"pending_self_report timeout: task {tid} -> timeout_failed")
                marked += 1
            except Exception as e:
                log.warning(f"pending timeout: mark timeout_failed failed for task {tid}: {e}")
        return marked
    except Exception as e:
        log.warning(f"sweep pending_self_report timeouts failed: {e}")
        return 0


def _requeue_no_progress(task_id, started_at, diagnostic) -> bool:
    """Issue #2793：原子回炉一次假死任务（仅当仍是同一 running 执行）。

    对齐 transient_retry._do_requeue 的重派复位惯例（service.py 实测教训）：
    归零 runtime_id 后必须回填 pool_tag/pool_agent_type，否则池匹配不命中、
    定向 claim 失配 → 任务永久 queued；同时 attempts+1、复位 stream_ended/
    self_reported/exit_code/result_text，避免旧执行信号污染新一轮。

    - `started_at=?` 条件更新 = 乐观并发：worker 在此期间已自报终态/已被其它路径
      回收时 rowcount=0，不覆盖终态（防新旧执行同时写终态）；
    - 置 started_at=NULL 即「旧执行 superseded」——started_at 是 worker run_token，
      旧实例迟到上报会被 validate_run_token 判 stale 拒收；
    - 释放 busy slot、事件同步回 pending，诊断写入 errors（NO_PROGRESS_REQUEUE）。
    """
    from src.domains.runtime.engine.transient_retry import (
        TASK_ERROR_SLOTS, parse_task_errors,
    )
    try:
        from src.domains.runtime.engine.service import release_busy_slot
    except Exception:
        release_busy_slot = None
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT event_seq, errors, runtime_id, pool_tag, pool_agent_type, agent_type "
                "FROM agent_tasks WHERE id=? AND status='running' AND started_at=?",
                (task_id, started_at)).fetchone()
            if not row:
                db.commit()
                return False
            _evt_seq, _errors, _rt_id, _pool_tag, _pool_agent_type, _agent_type = row
            errs = parse_task_errors(_errors)
            errs.append(diagnostic)
            _tag = _pool_tag or (_agent_type or "")
            _pool_type = _pool_agent_type or (_agent_type or "")
            c.execute(
                "UPDATE agent_tasks SET status='queued', started_at=NULL, runtime_id=0, "
                "attempts=COALESCE(attempts,0)+1, stream_ended=0, self_reported=0, "
                "exit_code=NULL, result_text='', pool_tag=?, pool_agent_type=?, "
                "created_at=?, errors=? "
                "WHERE id=? AND status='running' AND started_at=?",
                (_tag, _pool_type, datetime.now(timezone.utc).isoformat(),
                 json.dumps(errs[-TASK_ERROR_SLOTS:], ensure_ascii=False),
                 task_id, started_at))
            if c.rowcount <= 0:
                db.commit()
                return False
            if _rt_id and release_busy_slot:
                try:
                    release_busy_slot(_rt_id, cursor=c)
                except Exception as _re:
                    log.warning(f"no_progress: release_busy_slot {_rt_id} failed: {_re}")
            if _evt_seq:
                try:
                    c.execute(
                        "UPDATE events SET status='pending', result=? "
                        "WHERE seq=? AND status IN ('running','pending')",
                        ("NO_PROGRESS_REQUEUE: 无进展假死，已自动重派一次"
                         f"（静默 {diagnostic.get('silent_sec')}s / 阈值 {diagnostic.get('threshold')}s）",
                         _evt_seq))
                except Exception as _ee:
                    log.warning(f"no_progress: sync event pending failed seq={_evt_seq}: {_ee}")
            db.commit()
        return True
    except Exception as e:
        log.warning(f"no_progress: requeue task {task_id} failed: {e}")
        return False


def _sweep_no_progress_tasks(now_epoch=None) -> int:
    """Issue #2793：running 无新日志/无进展超时自愈巡检。

    判据：status='running' ∧ started_at 距今 ≥ no_progress_timeout_sec ∧ 窗口内
    无 task_messages 产出（task_activity.has_no_recent_activity，与
    has_recent_activity 共用活跃度口径，SQL 内不对时间列做函数运算）。命中后：
      1. 未达 `no_progress_retries` 上限 → 写 NO_PROGRESS_REQUEUE 诊断并原子回炉一次
         （按 _do_requeue 惯例复位 runtime_id/attempts/信号字段并回填 pool_tag，
         先置旧执行 superseded 再排队，防新旧执行同时写终态）；
      2. 已达上限 → 仅写一次 NO_PROGRESS_EXHAUSTED 诊断，不重派，交由原失败路径处理；
      3. `no_progress_enabled=false`（全局/per_skill/per_repo/任务 env）→ 仅写一次
         NO_PROGRESS_DISABLED 诊断，不重派（文档承诺的「只留诊断」）。
    阈值/次数非法值由 task_timeout 配置链静默回退默认，坏配置不拖垮主链路。
    重派计数存于 errors 槽位，配置上限按 TASK_ERROR_SLOTS 截断，防计数被截断丢失后无限重派。
    返回本轮成功重派次数。
    """
    from src.domains.runtime.engine import task_timeout as tt
    from src.domains.runtime.engine.task_activity import has_no_recent_activity
    from src.domains.runtime.engine.transient_retry import (
        TASK_ERROR_SLOTS, _append_task_error, parse_task_errors,
    )
    now = time.time() if now_epoch is None else float(now_epoch)
    try:
        cfg = tt.load_timeout_config()
    except Exception:
        cfg = {}
    try:
        with db_lock:
            c = db.cursor()
            # 仅按 status 过滤，时间比较全部在 Python 侧（不在 SQL 内对时间列做函数运算）。
            rows = list(c.execute(
                "SELECT id, event_seq, runtime_id, started_at, skill, repo_key, env_extra, errors "
                "FROM agent_tasks WHERE status='running'"))
    except Exception as e:
        log.warning(f"no_progress: scan running tasks failed: {e}")
        return 0
    requeued = 0
    for row in rows or []:
        tid, evt_seq, _rt_id, started_at, skill, repo_key, env_extra, errors = row
        task = {"skill": skill or "", "repo_key": repo_key or "",
                "env_extra": env_extra or "{}"}
        try:
            enabled = tt.no_progress_enabled(task, cfg)
            threshold, _src = tt.no_progress_timeout_sec(task, cfg)
            limit = tt.no_progress_retries(task, cfg)
        except Exception:
            continue
        _started = _parse_iso_aware(started_at)
        if _started is None:
            continue  # 信息不足不判死（started_at 缺失/解析失败）
        silent_sec = now - _started.timestamp()
        if silent_sec < threshold:
            continue
        try:
            with db_lock:
                no_activity = has_no_recent_activity(
                    db.cursor(), tid, threshold, now_epoch=now)
        except Exception:
            continue
        if not no_activity:
            continue
        errs = parse_task_errors(errors)
        if not enabled:
            # Spec-1：开关关闭时文档承诺「只留诊断」——写一次即可，避免每轮刷屏。
            if not any(isinstance(e, dict) and e.get("error_code") == "NO_PROGRESS_DISABLED"
                       for e in errs):
                try:
                    _append_task_error(tid, {
                        "error_code": "NO_PROGRESS_DISABLED",
                        "task_id": tid,
                        "silent_sec": round(silent_sec),
                        "threshold": threshold,
                        "note": "no_progress_enabled=false: diagnostic only, no requeue"})
                except Exception as _de:
                    log.warning(f"no_progress: disabled diagnostic failed for {tid}: {_de}")
            continue
        prior = [e for e in errs if isinstance(e, dict)
                 and e.get("error_code") == "NO_PROGRESS_REQUEUE"]
        # 重派计数存于 errors 槽位：上限按槽位数截断（超配上限后计数会被截断丢失 → 无限重派）
        limit = min(limit, TASK_ERROR_SLOTS)
        if len(prior) >= limit:
            if not any(isinstance(e, dict) and e.get("error_code") == "NO_PROGRESS_EXHAUSTED"
                       for e in errs):
                try:
                    _append_task_error(tid, {
                        "error_code": "NO_PROGRESS_EXHAUSTED",
                        "task_id": tid,
                        "silent_sec": round(silent_sec),
                        "threshold": threshold,
                        "requeues": len(prior),
                        "limit": limit,
                        "note": "no-progress requeue limit reached; no further requeue, "
                                "left to the original failure path"})
                except Exception as _ae:
                    log.warning(f"no_progress: exhausted diagnostic failed for {tid}: {_ae}")
            log.info(f"no_progress: task {tid} requeue limit reached "
                     f"(requeues={len(prior)}, limit={limit}); not requeued")
            continue
        diagnostic = {
            "error_code": "NO_PROGRESS_REQUEUE",
            "task_id": tid,
            "silent_sec": round(silent_sec),
            "threshold": threshold,
            "attempt": len(prior) + 1,
            "limit": limit,
            "superseded_started_at": started_at,
            "note": "running task had no new task_messages within threshold; old execution "
                    "superseded (started_at reset) and requeued once",
        }
        if _requeue_no_progress(tid, started_at, diagnostic):
            requeued += 1
            log.info(f"no_progress: task {tid} silent {silent_sec:.0f}s >= {threshold:.0f}s "
                     f"-> requeued (attempt {len(prior) + 1}/{limit})")
    return requeued


def _reap_zombie_executions() -> None:
    """僵尸执行回收：主控重启后 _poll_dispatched_completion 协程被杀，
    autopilot_executions 卡在 running/dispatched，但 agent_tasks 可能已完成。
    每轮扫描顺带检查并同步终态（幂等自愈）。

    Issue #537 增强（生产 exec incident case 误报根因修复）：
    - engine 离线判死加宽限（ZOMBIE_OFFLINE_GRACE_SECONDS，默认 5min）——
      停双主/重启 master 的短暂离线不误判 failed；last_seen 信息缺失也不判死；
    - pending_self_report 不判死（等 worker 自报，超时由 _sweep_pending_self_report_timeouts 兜底）；
    - 收口 success 前对部署类任务做 evidence 校验（版本指纹/健康检查，见 deploy_evidence.py）。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, agent_task_id, started_at, created_at "
                "FROM autopilot_executions "
                "WHERE status IN ('running', 'dispatched') "
                "ORDER BY id DESC LIMIT 20"
            )
            stuck = c.fetchall()
        for row in stuck:
            _exec_id, _exec_atid, _exec_started, _exec_created = row
            # Issue #1546 D1：年轻度保护——派发协程尚未 enqueue 落库时回收器已开跑，
            # started_at/created_at 距今 < 120s 的执行本轮跳过，避免误判历史同号行。
            # 时区解析规则同 C1：started_at 空则用 created_at，为空或解析失败均跳过。
            if _exec_started:
                _ref_ts = _parse_iso_aware(_exec_started)
            else:
                _ref_ts = _parse_iso_aware(_exec_created)
            if _ref_ts is None:
                continue
            _age = datetime.now(timezone.utc).timestamp() - _ref_ts.timestamp()
            if _age < ZOMBIE_YOUNG_GRACE_SECONDS:
                continue
            with db_lock:
                c2 = db.cursor()
                # Issue #1546：agent_task_id 直连优先，历史空值受约束兜底；无命中跳过
                at_row = _get_exec_agent_task(
                    c2, _exec_id, _exec_atid, _exec_started, _exec_created)
            if not at_row:
                continue
            _at_id = at_row[0]
            _at_status = at_row[1] or ""
            _result_text = at_row[2] or ""
            _at_task = _agent_task_view(at_row)
            if _at_status in ("completed", "success"):
                # 部署类任务收口前证据校验（Issue #537 P1-1）
                # QA R1 三表终态一致性取舍：同 finalize——evidence 判失败仅 exec 侧 failed，
                # 任务/事件侧保留 completed，错误详情写入 exec.error。
                try:
                    from src.domains.runtime.engine.deploy_evidence import check_deploy_evidence
                    _evd = check_deploy_evidence(_at_task)
                except Exception:
                    _evd = {"checked": False}
                if _evd.get("checked") and not _evd.get("passed"):
                    _update_execution_result(_exec_id, "failed",
                        result=_result_text[:5000],
                        error=f"部署证据校验未通过: {_evd.get('reason', '')}")
                    log.info(f"Autopilot zombie recovery: exec {_exec_id} -> failed "
                             f"(deploy evidence: {_evd.get('reason')})")
                    continue
                findings_result = parse_findings(_result_text)
                _summary = findings_result["summary"] or (_result_text[-500:] if _result_text else "")
                _update_execution_result(_exec_id, "success",
                    result=_summary,
                    findings_json=json.dumps(findings_result, ensure_ascii=False) if findings_result["findings"] else "")
                log.info(f"Autopilot zombie recovery: exec {_exec_id} -> success (agent_task completed)")
            elif _at_status == "cancelled":
                _update_execution_result(_exec_id, "cancelled",
                    result=_result_text[:5000], error="用户手动终止")
                log.info(f"Autopilot zombie recovery: exec {_exec_id} -> cancelled")
            elif _at_status == "failed":
                _update_execution_result(_exec_id, "failed",
                    result=_result_text[:5000], error="agent_tasks status=failed")
                log.info(f"Autopilot zombie recovery: exec {_exec_id} -> failed")
            elif _at_status == "timeout_failed":
                # Issue #537：pending 超时兜底终态 → 僵尸 exec 同步 failed（带说明）
                _update_execution_result(_exec_id, "failed",
                    result=_result_text[:5000],
                    error="agent_tasks status=timeout_failed（worker 未自报超时）")
                log.info(f"Autopilot zombie recovery: exec {_exec_id} -> failed (timeout_failed)")
            # running/queued/dispatched -> 引擎离线超阈值才判死，否则跳过，等下一轮。
            # Issue #404 自杀修复配套（2026-10-15）：主控停机不再把 REMOTE 任务标
            # failed（terminate_active_agents 跳过 worker 承载任务）。正常路径是
            # worker 继续跑完 → agent_tasks 变 completed → 上面分支同步 success。
            # Issue #537：离线判死加 5min 宽限（重启窗口短暂离线不误判 failed）；
            # pending_self_report 一律不判死（等 worker 自报，超时由 sweep 兜底）。
            elif _at_status in ("running", "dispatched"):
                try:
                    _rt_row = None
                    with db_lock:
                        c3 = db.cursor()
                        c3.execute(
                            "SELECT t.runtime_id, ar.status, ar.last_seen_at FROM agent_tasks t "
                            "LEFT JOIN agent_runtimes ar ON ar.id = t.runtime_id "
                            "WHERE t.id=? LIMIT 1",
                            (_at_id,)
                        )
                        _rt_row = c3.fetchone()
                    _rt_id = (_rt_row[0] or 0) if _rt_row else 0
                    _rt_status = (_rt_row[1] or "") if _rt_row else ""
                    _rt_last_seen = (_rt_row[2] or "") if _rt_row else ""
                    if _rt_id and _rt_status and _rt_status != "online":
                        _offline_secs = _offline_duration_seconds(_rt_last_seen)
                        if _offline_secs is None or _offline_secs <= ZOMBIE_OFFLINE_GRACE_SECONDS:
                            # 重启窗口内短暂离线 / last_seen 信息缺失 → 不判死，等下一轮
                            continue
                        _update_execution_result(_exec_id, "failed",
                            error=f"worker 引擎离线 (runtime={_rt_id}) 超过 {_offline_secs:.0f}s，任务无法完成")
                        # 同步 agent_tasks 判死（任务实际已不可能自报，终态对齐全链路）
                        try:
                            from src.domains.runtime.engine.service import update_task_status
                            update_task_status(_at_id, "failed", exit_code=-1,
                                errors=json.dumps([f"worker 引擎离线超过 {_offline_secs:.0f}s，任务无法完成"],
                                                  ensure_ascii=False),
                                # Issue #1938：补偿归因（sweeper 僵尸回收判死）
                                cascade_trigger="sweeper")
                        except Exception as _tk:
                            log.warning(f"Autopilot zombie recovery: task {_at_id} mark failed failed: {_tk}")
                        log.info(f"Autopilot zombie recovery: exec {_exec_id} -> failed "
                                 f"(agent_task {_at_status}, engine {_rt_id} offline {_offline_secs:.0f}s)")
                    # 引擎在线 → 活任务，等下一轮
                except Exception as _oe:
                    log.warning(f"Autopilot zombie recovery offline-check failed for exec {_exec_id}: {_oe}")
            # pending_self_report → 等待（不判死）；超时由 _sweep_pending_self_report_timeouts 兜底
    except Exception as _ze:
        log.warning(f"Autopilot zombie recovery error: {_ze}")

async def scheduler_loop():
    """Autopilot 调度主循环——每 60 秒扫描到期任务。

    P1-D (Issue #357): Distributed lock ensures only one worker runs the
    scheduler loop when uvicorn uses --workers N (prevents duplicate task dispatch).
    """
    from src.domains.runtime.engine.task_lock import run_with_distributed_lock
    log.info("Autopilot scheduler loop started (60s interval)")
    while True:
        try:
            async with run_with_distributed_lock("autopilot_scheduler", ttl=120):
                # 这些调用都是同步 pymysql：直接在事件循环里跑会把整个 worker 卡住
                # （health 不响应、心跳落不了库、引擎被判失联）。2026-11-15 实测：
                # MainThread 停在 _reap_zombie_executions，本机 worker 38083 全部端点超时，
                # 远端心跳经网关落到它身上一起挂住，表现为该凭据失联。
                tasks = await asyncio.to_thread(_get_enabled_tasks)
                for task in tasks:
                    task_id = task["id"]
                    if task_id in _RUNNING_TASKS:
                        continue
                    # autopilot_tasks 归 mgmt：last_run 走归属域任意拓扑原语
                    from src.domains.mgmt import public as _mgmt_public
                    row = await asyncio.to_thread(
                        _mgmt_public.autopilot_task_last_run_any, task_id)
                    last_run = row[0] if row else ""
                    # Issue #3800：派发裁决（熔断自愈 / cron 到期 / 退避闸门）抽成
                    # `_scan_task_once`，生产循环与假时钟回归走同一条判定路径。
                    try:
                        decision = await asyncio.to_thread(_scan_task_once, task, last_run)
                    except Exception as _de:
                        log.warning(f"Autopilot: task {task_id} dispatch decision failed: {_de}")
                        continue
                    if decision == "dispatch":
                        asyncio.ensure_future(_execute_task(task))
        except Exception as e:
            log.warning(f"Autopilot scheduler loop error: {e}")

        # 僵尸执行回收：主控重启后 _poll_dispatched_completion 协程被杀，
        # autopilot_executions 卡在 running/dispatched，但 agent_tasks 可能已完成。
        # 每轮扫描顺带检查并同步终态（幂等自愈）。
        try:
            await asyncio.to_thread(_reap_zombie_executions)
        except Exception as _ze2:
            log.warning(f"Autopilot zombie recovery error: {_ze2}")
        # pending_self_report 超时兜底（Issue #537 P0-1）：worker 未在阈值内自报 → timeout_failed
        try:
            await asyncio.to_thread(_sweep_pending_self_report_timeouts)
        except Exception as _se2:
            log.warning(f"Autopilot pending timeout sweep error: {_se2}")
        # Issue #2793：running 无新日志/无进展假死 → 超时自愈重派（有上限，可开关回滚）
        try:
            await asyncio.to_thread(_sweep_no_progress_tasks)
        except Exception as _np:
            log.warning(f"Autopilot no-progress sweep error: {_np}")

        await asyncio.sleep(_SCHEDULER_INTERVAL)

_scheduler_task: Optional[asyncio.Task] = None

def start_scheduler():
    """启动 Autopilot 调度器（在 FastAPI lifespan 中调用）。"""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        log.info("Autopilot scheduler already running")
        return
    _scheduler_task = asyncio.ensure_future(scheduler_loop())
    log.info("Autopilot scheduler started")

async def stop_scheduler():
    """停止 Autopilot 调度器（在 FastAPI lifespan shutdown 中调用）。"""
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await asyncio.wait_for(_scheduler_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _scheduler_task = None
        log.info("Autopilot scheduler stopped")

async def trigger_task_now(task_id: int, params: dict | None = None) -> dict:
    """手动触发任务执行（异步入口）。params 作为本次一次性运行参数注入 prompt。"""
    # autopilot_tasks 归 mgmt：单行全列走归属域任意拓扑原语。
    # 原 lib 分支写的是 `c = lib.read_one(...).fetchone()`（元组再取 fetchone → AttributeError，
    # 且错赋给 c 让 row 未绑定），lib 可用时手动触发恒抛错——本批一并修正。
    from src.domains.mgmt import public as _mgmt_public
    row = _mgmt_public.autopilot_task_row_any(task_id)
    if not row:
        return {"ok": False, "message": "task not found"}
    task = {
        "id": row[0], "name": row[1], "repo_key": row[2],
        "agent_type": row[3], "instruction": row[4], "skill_name": row[5],
        "cron": row[6], "max_retry": row[7], "created_by": row[8] if len(row) > 8 else 0,
        "engine_runtime_ids": row[9] if len(row) > 9 else "",
        "custom_fields": row[10] if len(row) > 10 else "",
        "work_branch": row[11] if len(row) > 11 else "",
        # Issue #1011 姊妹字段：uid 绑定（非空优先）
        "engine_runtime_uids": row[12] if len(row) > 12 else "",
        # Issue #3800：退避/熔断状态（列序见 mgmt `autopilot_task_full_row`）
        "consecutive_failures": int(row[13] or 0) if len(row) > 13 else 0,
        "last_error_code": (row[14] or "") if len(row) > 14 else "",
        "offline_since": (row[15] or "") if len(row) > 15 else "",
        "backoff_until": (row[16] or "") if len(row) > 16 else "",
        "circuit_paused": 1 if (len(row) > 17 and row[17]) else 0,
    }
    if task["id"] in _RUNNING_TASKS:
        return {"ok": False, "message": "task already running"}
    task["_run_params"] = dict(params or {})
    asyncio.create_task(_execute_task(task))
    return {"ok": True, "message": "task triggered"}
