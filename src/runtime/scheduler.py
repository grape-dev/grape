"""Autopilot 定时调度引擎 v3.0

重构要点（v3.0）：
- 不再创建 Issue 触发完整流水线，改为直接执行单个 agent
- 不走 run_pipeline / build_devops_agent_prompt / 事件系统
- prompt 在 _build_autopilot_prompt 中直接构建（巡检指令 + findings 格式模板）
- 底层复用 pick_engine / get_agent_workdir / _materialize_direct_workdir / stream_devops_agent
- 执行结果独立存储在 autopilot_executions，不写 issue 表 / events 表
- agent 产出中解析问题列表(findings)，用户可手动派发为真实 Issue 走完整流水线
- event_seq 用 autopilot_executions.id（非0，避免多任务并发覆盖）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.config import log
from src.session_manager import db, db_lock

_SCHEDULER_INTERVAL = 60  # 秒：调度器扫描间隔
_BASE_RETRY_DELAY = 10    # 秒：指数退避基数
_MAX_RETRY_DELAY = 300    # 秒：退避上限
_RUNNING_TASKS: set[int] = set()  # 内存互斥锁：正在执行的 task_id 集合

# Issue #537：pending_self_report 超时（worker 未自报）→ timeout_failed 兜底阈值；
# zombie 判死离线宽限（停双主/重启 master 的短暂离线不误判 failed）。
# Issue #1071：默认 1800→5400——部署类任务（打包+生产部署全流程约 40-50 分钟）按设计
# 在执行中重启主控，重启瞬间 pending_since 重置，1800s 窗口实际退化为「重启后 30 分钟」，
# 剩余工序经常超 30 分钟被误判 timeout_failed；配合判死前活跃流豁免（见 sweep）双保险。
PENDING_SELF_REPORT_TIMEOUT_SECONDS = int(os.environ.get("PENDING_SELF_REPORT_TIMEOUT_SECONDS", "5400"))
ZOMBIE_OFFLINE_GRACE_SECONDS = int(os.environ.get("ZOMBIE_OFFLINE_GRACE_SECONDS", "300"))
# Issue #1546：派发协程尚未 enqueue_task 落库时回收器已开跑，年轻执行（<120s）本轮跳过回收，
# 避免「回收器抢在真任务落库前判定终态」的竞态；时区解析规则同 _get_exec_agent_task。
# 窗口来源：Issue #1546 中派发链路（pick_engine → enqueue_task → 回写 execution）的落库时延，
# 120s 为留足余量的默认值；不同部署的派发链耗时差异用 env 覆盖，与相邻两个宽限常量口径一致。
ZOMBIE_YOUNG_GRACE_SECONDS = int(os.environ.get("ZOMBIE_YOUNG_GRACE_SECONDS", "120"))

# agent_type 固定为 'issue'，不再由用户选择
_FIXED_AGENT_TYPE = "issue"

# ====== findings 解析 ======

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


# ====== Issue #697：dedup 判定级别枚举 + 门禁判定（代码级强制，单一事实源） ======
# prompt_builder / routes/autopilot.py 均从这里取常量与判定函数，禁止各自复制规则文本。

DEDUP_LEVEL_EXACT = "exact_duplicate"
DEDUP_LEVEL_SUSPECTED = "suspected_duplicate"
DEDUP_LEVEL_NOT = "not_duplicate"
DEDUP_LEVELS = (DEDUP_LEVEL_EXACT, DEDUP_LEVEL_SUSPECTED, DEDUP_LEVEL_NOT)

# 写入公开 Issue 描述 / 确认人字段的限长（防 LLM 长文注入公开面）
DEDUP_NOTE_MAX_CHARS = 200
DEDUP_CONFIRMED_BY_MAX_CHARS = 64

# 注：判重规则的 prompt 文本已按运维决定移除（不再改自动化任务的 prompt）；
# 这里只保留代码侧门禁所需的级别枚举与判定函数。


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
            from src.agent.hermes_acp import _fix_json_newlines, _ensure_closed
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


# ====== prompt 构建 ======

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

    parts.append("\n请执行以上任务指令，分析仓库代码/依赖/文档/配置等。")
    parts.append("\n## 输出格式要求\n")
    parts.append(_FINDINGS_FORMAT_TEMPLATE)
    parts.append("\n## 项目知识\n")
    parts.append("项目知识文档位于工作目录中，存在则用 read tool 读取。")
    parts.append("\n执行以上指令，不要操作 GitCode Issue（不需要回帖/打标签/关 Issue），结果直接输出。")
    return "\n".join(parts)


# ====== agent 执行 ======

def _execute_autopilot_agent(repo_key: str, task: dict, exec_id: int):
    """执行单个 autopilot agent（在线程池中运行）。

    不走 _execute_agent_inner / build_devops_agent_prompt / 事件系统。
    直接构建 prompt → 物化 workdir → 调 stream_devops_agent。
    """
    from src.repo_key import from_repo_key, split_owner_repo
    from src.runtime.engine_select import pick_engine, engine_name
    from src.runtime.instances import runtime_instance_name
    from src.api.event_pipeline import (
        get_agent_workdir, _materialize_direct_workdir, get_agent_display_name,
        _create_direct_task,
    )

    agent_type = _FIXED_AGENT_TYPE  # 固定 'issue'，不再从 task 读取
    skill_name = task.get("skill_name", "")
    repository = from_repo_key(repo_key) or repo_key

    # 1. 选引擎
    engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id=0)
    if engine_id is None:
        log.warning(f"Autopilot exec {exec_id}: no engine for {agent_type}")
        _update_execution_result(exec_id, "failed", error="无可用引擎")
        return

    inst_name = runtime_instance_name(engine_id) or engine_name(engine_id) or ""
    backend = inst_name

    # 判断执行模式：DIRECT（本机）或 REMOTE（远端 worker）/ 广播
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, get_execution_mode
    from src.api.event_pipeline import is_plugin_mode
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

    # 3. bot token（按 provider 分流）
    repo_token = ""
    try:
        if repo_key.startswith("github:"):
            from src.user.db import get_github_token
            repo_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            repo_token = get_gitcode_token(repo_key=repo_key) or ""
    except Exception as e:
        log.warning(f"Autopilot exec {exec_id}: get bot token failed: {e}")

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
    from src.prompt_builder import resolve_task_server_env
    # Issue #1660：REPO_OWNER/REPO_NAME 由 REPO_FULL_NAME 派生（缺失回退 repo_key）。
    from src.repo_key import derive_repo_identity
    _full_name, _repo_owner, _repo_name = derive_repo_identity(repository, repo_key)
    env_extra = {
        "REPO_FULL_NAME": _full_name or repository,
        "REPO_OWNER": _repo_owner,
        "REPO_NAME": _repo_name,
        "ACCOUNT": repository,
        # Issue #1577：与事件/chat 同源注入两项服务基址，远端 autopilot worker
        # 的业务命令免 --base-url 可用（direct 模式注入本机回环同样正确）。
        **resolve_task_server_env(engine_id),
    }
    # 注入 custom_fields（自定义环境变量）和 work_branch
    _task_custom_fields = task.get("custom_fields") or ""
    if _task_custom_fields:
        try:
            _cf_list = json.loads(_task_custom_fields) if isinstance(_task_custom_fields, str) else _task_custom_fields
            from src.scene_custom_env import is_upper_name, is_reserved_name
            for _f in _cf_list:
                _fname = (_f.get("name") or "").strip()
                if not _fname or not is_upper_name(_fname) or is_reserved_name(_fname):
                    log.warning(f"Autopilot exec {exec_id}: custom env '{_fname}' rejected")
                    continue
                env_extra[_fname] = str(_f.get("value", ""))
        except Exception as _e:
            log.warning(f"Autopilot exec {exec_id}: inject custom_fields failed: {_e}")
    _task_work_branch = task.get("work_branch") or ""
    if _task_work_branch:
        env_extra["GRAPE_WORK_BRANCH"] = _task_work_branch

    # Issue #1811：定时调度与 event_pipeline 同源注入 bot 身份，单点覆盖 DIRECT/REMOTE。
    # 函数内延迟 import，避免 scheduler ↔ event_pipeline 模块级循环依赖。
    from src.api.event_pipeline import _inject_bot_agent_env
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
    from src.api.event_pipeline import _create_direct_task

    # 创建 agent_tasks 记录（用 exec_id 作为 event_seq，skip_event_config=True 不写 events 表）
    task_id = _create_direct_task(
        repo_key, exec_id, agent_type, backend, skill_name,
        "", engine_id, {"repo_key": repo_key, "issue_id": "", "payload": {}},
        issue_id="", skill_id=0, skip_event_config=True,
    )
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
        from src.runtime.skill_materialize import (SKILL_CONSISTENCY_KEY,
                                                   skill_mismatch_error)
        from src.runtime.task_log_stream import build_skill_source_message
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
    from src.observability.master_usage import for_task as _master_for_task
    _mu = _master_for_task(task_id, backend, cwd=ws, session_id="", seq=exec_id,
                           repo_key=repo_key, agent_type=agent_type,
                           engine_id=int(engine_id or 0))
    _mu.start()

    try:
        from src.agent.executor import stream_devops_agent
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
    from src.runtime.service import enqueue_task
    from src.api.event_pipeline import get_agent_workdir as _gaw
    from src.api.session_store import get_or_create_agent_session, get_or_create_issue_session
    from src.prompt_builder import _resolve_server_url

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

    # 任务级引擎限定：task 有 engine_runtime_ids 时用它限定广播范围
    # Issue #259：写入前按健康过滤（复用 engine_select 的 _engines_by_ids + _health_ok）
    _pool_engine_ids = ""
    if engine_id == 0:
        _task_engine_ids = task.get("engine_runtime_ids") or ""
        if _task_engine_ids:
            try:
                _raw_ids = json.loads(_task_engine_ids) if isinstance(_task_engine_ids, str) else _task_engine_ids
                if isinstance(_raw_ids, list) and _raw_ids:
                    from src.runtime.engine_select import _engines_by_ids, _health_ok, load_engine_selection, _norm_repo
                    _members = _engines_by_ids([int(x) for x in _raw_ids if str(x).strip().isdigit()],
                                               _norm_repo(repo_key), load_engine_selection())
                    _healthy_ids = [m["runtime_id"] for m in _members if _health_ok(m)]
                    if _healthy_ids:
                        _pool_engine_ids = json.dumps(_healthy_ids)
                        log.info(f"Autopilot exec {exec_id}: task 级限定 engines={_pool_engine_ids}")
                    else:
                        # 方案A：任务级引擎全灭 → 明确报错，不广播
                        log.warning(f"Autopilot exec {exec_id}: task engines {_raw_ids} all offline (Issue #259)")
                        _update_execution_result(exec_id, "failed", error="任务绑定的引擎全部离线")
                        return
            except Exception as _pe:
                log.warning(f"Autopilot exec {exec_id}: parse engine ids failed: {_pe}")
                _pool_engine_ids = ""

    log.info(f"Autopilot exec {exec_id}: enqueuing remote agent={agent_type} engine={inst_name} pool_tag={_pool_tag}")

    # Issue #287：入队前按任务创建者预解析 skill_id（user scope skill 的 owner_id 匹配 created_by）。
    # user_id 保持 0（_collect_server_env 凭据注入行为零变化）；解析不中时降级 skill_id=0 + warning，不阻断入队。
    _skill_id = 0
    if skill_name:
        try:
            from src.user.db import resolve_skill_id
            _skill_id = resolve_skill_id(skill_name, repo_key=repo_key,
                                         user_id=task.get("created_by") or 0)
            if not _skill_id:
                log.warning(f"Autopilot exec {exec_id}: skill '{skill_name}' not resolved for "
                            f"creator={task.get('created_by')}, enqueue without skill files")
        except Exception as _se:
            log.warning(f"Autopilot exec {exec_id}: pre-resolve skill '{skill_name}' failed: {_se}")

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
            repo_gitcode_token=repo_token,
            user_gitcode_token="",
            env_extra=env_extra,
            event_seq=exec_id,
            issue_id="",  # 保持空：issue_id 会驱动场景/成员知识/工作目录按 issue 号解析，
                          # 合成 id 含数字会被 _issue_num 误挂到真实 issue；串行锁只看 issue_session_id
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
        log.error(f"Autopilot exec {exec_id}: enqueue failed: {e}")
        _update_execution_result(exec_id, "failed", error=f"入队异常: {e}")


def _finalize_autopilot_task(task_id: str, exec_id: int, exit_code: int, final_text: str):
    """执行完成后更新 agent_tasks + autopilot_executions + 解析 findings。"""
    # 更新 agent_tasks 状态（用 task_id 精确更新）
    # L1（2026-10-17）：改为统一终态入口 update_task_status，其内部完成
    #   release_busy_slot（防 busy_slots 泄漏 → 本机引擎误判满载）＋ events 状态同步。
    # 直接 UPDATE agent_tasks 会绕过统一递减（见 local-engine-busy-slots-zombie-fix-design.md §九）。
    try:
        from src.runtime.service import update_task_status
        update_task_status(
            task_id,
            "completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
            result_text=final_text[:5000],
            completed_at=datetime.now(timezone.utc).isoformat(),
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
    _ev_error = ""
    if status == "success":
        # Issue #537 P1-1：部署类任务收口前做落地证据校验（版本指纹/健康检查）。
        # 校验失败 → 收口 failed 带 evidence 详情（纵深防御，防 false-success 也防 false-failed）。
        # QA R1 三表终态一致性取舍：仅 autopilot_executions 判 failed；agent_tasks/events 保留
        # completed——worker 确实完成执行，部署落地矛盾由 exec.error 承载；不回翻任务/事件侧
        # 终态（update_task_status 终态幂等，也不应把已完成任务后改为 failed）。
        try:
            from src.runtime.service import get_task
            from src.runtime.deploy_evidence import check_deploy_evidence
            _evd = check_deploy_evidence(get_task(task_id) or {})
        except Exception:
            _evd = {"checked": False}
        if _evd.get("checked") and not _evd.get("passed"):
            status = "failed"
            _ev_error = _evd.get("reason", "部署证据校验未通过")
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


# ====== 飞书订阅通知（autopilot-feishu-subscription-design.md §4.4）======

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
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT s.user_id, t.name, t.repo_key FROM autopilot_subscriptions s "
                "JOIN autopilot_tasks t ON s.task_id=t.id WHERE s.task_id=?",
                (task_id,)).fetchall()
        if not rows:
            return
        _task_name = rows[0][1] or f"task-{task_id}"
        _repo_key = rows[0][2] or ""
        # Web 详情页 URL（前端 autopilot 页路由）
        _web_url = ""
        try:
            from src.prompt_builder import _resolve_server_url
            _web_url = f"{_resolve_server_url(0)}/autopilot"
        except Exception:
            pass
        card = build_exec_card(_task_name, _repo_key, exec_id, status,
                               result_summary, findings_result, _web_url)
        # 触达链：本人 bot + open_id（复用 issues_center 既有函数，不回落他人 bot）
        from src.api.issues_center import _get_feishu_open_id_by_user_id, _resolve_user_chat_bot
        from src.api import main as api_main
        bm = getattr(api_main, "bot_manager", None)
        _loop = asyncio.get_event_loop()
        for user_id, _, _ in rows:
            try:
                open_id = _get_feishu_open_id_by_user_id(user_id) or ""
                ctx = _resolve_user_chat_bot(bm, user_id) if bm else None
                api = bm.api if bm else None
                _loop.create_task(_run_autopilot_notify(ctx, api, user_id, open_id,
                                                        card, task_id, exec_id))
            except Exception as e:
                log.warning(f"[autopilot-notify] task={task_id} exec={exec_id} user={user_id} "
                            f"resolve failed: {e}")
    except Exception as e:
        log.warning(f"[autopilot-notify] task={task_id} exec={exec_id} notify failed: {e}")


def _update_execution_result(exec_id: int, status: str, result: str = "",
                             error: str = "", findings_json: str = ""):
    """更新 autopilot_executions 执行结果。"""
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
    if status in ("success", "failed", "cancelled"):
        fields.append("completed_at=?")
        vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(exec_id)
    try:
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
    except Exception as e:
        log.warning(f"Autopilot: update execution {exec_id} failed: {e}")


# ====== 调度器 ======

def _get_enabled_tasks() -> list[dict]:
    """获取所有启用的 autopilot 任务。"""
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, created_by, engine_runtime_ids, custom_fields, work_branch FROM autopilot_tasks WHERE enabled=1"
        ).fetchall()
    tasks = []
    for r in rows:
        tasks.append({
            "id": r[0], "name": r[1], "repo_key": r[2],
            "agent_type": r[3], "instruction": r[4], "skill_name": r[5],
            "cron": r[6], "max_retry": r[7], "created_by": r[8] if len(r) > 8 else 0,
            "engine_runtime_ids": r[9] if len(r) > 9 else "",
            "custom_fields": r[10] if len(r) > 10 else "",
            "work_branch": r[11] if len(r) > 11 else "",
        })
    return tasks


def _is_cron_due(cron_expr: str, last_run: str) -> bool:
    """判断 cron 表达式是否在当前时间窗口（最近 60s）内到期。"""
    try:
        from croniter import croniter
        now = datetime.now().astimezone()
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
        now_local = datetime.now().astimezone()
        cron = croniter(cron_expr, now_local)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.astimezone()
        return nxt.isoformat()
    except Exception:
        return ""


def _create_execution(task_id: int, agent_type: str) -> int:
    """创建执行记录，返回 execution_id。"""
    now_str = datetime.now().astimezone().isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_executions "
            "(task_id, status, retry_count, started_at, created_at, agent_type) "
            "VALUES (?, 'running', 0, ?, ?, ?)",
            (task_id, now_str, now_str, agent_type)
        )
        db.commit()
        return c.lastrowid


def _update_execution(exec_id: int, status: str, result: str = "", error: str = "",
                      retry_count: Optional[int] = None):
    """更新执行记录状态（兼容旧调用，内部转 _update_execution_result）。"""
    _update_execution_result(exec_id, status, result=result, error=error)


def _update_task_run_info(task_id: int, next_run: str):
    """更新任务最近运行时间和下次预计运行时间。"""
    now_str = datetime.now().astimezone().isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE autopilot_tasks SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?",
            (now_str, next_run, now_str, task_id)
        )
        db.commit()


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
                    from src.runtime.deploy_evidence import check_deploy_evidence
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

        for attempt in range(max_retry + 1):
            exec_id = _create_execution(task_id, agent_type)
            log.info(f"Autopilot: executing task {task_id} ('{task['name']}'), exec_id={exec_id}, attempt={attempt}")

            # 在线程池中执行 agent（不阻塞事件循环）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _execute_autopilot_agent, task["repo_key"], task, exec_id)

            # 检查执行结果
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT status, error FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()
            exec_status = row[0] if row else "unknown"

            # REMOTE 模式下状态为 "dispatched"，需要轮询等待 worker 完成
            if exec_status == "dispatched":
                log.info(f"Autopilot: task {task_id} exec {exec_id} dispatched, polling worker completion...")
                exec_status = await _poll_dispatched_completion(exec_id, timeout=3600)

            if exec_status == "success":
                log.info(f"Autopilot: task {task_id} exec {exec_id} success (attempt {attempt})")
                break
            if exec_status == "cancelled":
                # 用户杀死：立即停止，不再重试
                log.info(f"Autopilot: task {task_id} exec {exec_id} cancelled by user, stop")
                break
            if attempt < max_retry:
                error_msg = row[1] if row else "unknown"
                delay = min(_BASE_RETRY_DELAY * (2 ** attempt), _MAX_RETRY_DELAY)
                log.warning(f"Autopilot: task {task_id} exec {exec_id} attempt {attempt} failed ({exec_status}), "
                            f"retrying in {delay}s: {error_msg}")
                await asyncio.sleep(delay)
            else:
                log.error(f"Autopilot: task {task_id} exec {exec_id} failed after {max_retry} retries ({exec_status})")

        next_run = _compute_next_run(task["cron"])
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
        from src.runtime.health import parse_dt
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
        from src.runtime.health import parse_dt
        _ts = parse_dt(row[0])
        if _ts is None:
            return False
        _age = datetime.now(timezone.utc).timestamp() - float(_ts)
        return _age < _PENDING_STREAM_ACTIVE_WINDOW_SECONDS
    except Exception as _e:
        log.warning(f"pending stream activity check failed for task {task_id}: {_e}")
        return False


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
    from src.runtime.service import update_task_status
    from src.api.event_store import update_event_complete
    _cutoff_ts = datetime.now(timezone.utc).timestamp() - PENDING_SELF_REPORT_TIMEOUT_SECONDS
    _now = datetime.now(timezone.utc).isoformat()
    _reason = (f"[PENDING_SELF_REPORT_TIMEOUT] worker 在 {PENDING_SELF_REPORT_TIMEOUT_SECONDS}s 内未自报完成/失败"
               f"且执行流静默超 {_PENDING_STREAM_ACTIVE_WINDOW_SECONDS}s"
               f"（pending_self_report 超时），判 timeout_failed")
    marked = 0
    try:
        _pending_rows = []
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, event_seq, runtime_id, pending_since FROM agent_tasks "
                "WHERE status='pending_self_report' AND pending_since IS NOT NULL "
                "AND pending_since != ''"
            )
            # 数值比较（parse_dt→epoch）：兼容历史写入的无时区 "%Y-%m-%d %H:%M:%S" 与
            # 带时区 ISO-8601 UTC（Issue #588 起新写入）混合格式——字符串比较会因
            # ' '(0x20) < 'T'(0x54) 把停机写入的 pending 误判为已过期（超时窗口形同虚设）。
            # 带时区值写入/解析同源（UTC），MySQL 后端不再被 parse_dt 的 naive-CST 口径
            # （Issue #512）误加 28800s；无时区历史值的兼容恢复按 Spec 单独评估，不在此改。
            from src.runtime.health import parse_dt
            for _r in c.fetchall():
                try:
                    _ts = parse_dt(_r[3])
                except Exception:
                    _ts = None
                if _ts and float(_ts) <= _cutoff_ts:
                    # 活跃流豁免（Issue #1071）：执行流 <600s 内仍在写入 → 任务在跑，
                    # 跳过判死等下轮（同一事务内查询，不加额外锁、每候选一条主键查询开销小）。
                    if _pending_self_report_stream_active(c, _r[0]):
                        continue
                    _pending_rows.append(_r)
            rows = _pending_rows
        for tid, evt_seq, _rt_id, _ps in rows:
            try:
                update_task_status(tid, "timeout_failed", exit_code=-1,
                                   errors=json.dumps([_reason], ensure_ascii=False),
                                   completed_at=_now)
                if evt_seq:
                    try:
                        update_event_complete(evt_seq, "failed", exit_code=-1,
                                              completed_at=_now, result=_reason[:200])
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
                    from src.runtime.deploy_evidence import check_deploy_evidence
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
                            from src.runtime.service import update_task_status
                            update_task_status(_at_id, "failed", exit_code=-1,
                                errors=json.dumps([f"worker 引擎离线超过 {_offline_secs:.0f}s，任务无法完成"],
                                                  ensure_ascii=False))
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
    from src.runtime.task_lock import run_with_distributed_lock
    log.info("Autopilot scheduler loop started (60s interval)")
    while True:
        try:
            async with run_with_distributed_lock("autopilot_scheduler", ttl=120):
                tasks = _get_enabled_tasks()
                for task in tasks:
                    task_id = task["id"]
                    if task_id in _RUNNING_TASKS:
                        continue
                    with db_lock:
                        c = db.cursor()
                        row = c.execute("SELECT last_run_at FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()
                        last_run = row[0] if row else ""
                    if _is_cron_due(task["cron"], last_run):
                        log.info(f"Autopilot: cron due for task {task_id} ('{task['name']}')")
                        asyncio.ensure_future(_execute_task(task))
        except Exception as e:
            log.warning(f"Autopilot scheduler loop error: {e}")

        # 僵尸执行回收：主控重启后 _poll_dispatched_completion 协程被杀，
        # autopilot_executions 卡在 running/dispatched，但 agent_tasks 可能已完成。
        # 每轮扫描顺带检查并同步终态（幂等自愈）。
        try:
            _reap_zombie_executions()
        except Exception as _ze2:
            log.warning(f"Autopilot zombie recovery error: {_ze2}")
        # pending_self_report 超时兜底（Issue #537 P0-1）：worker 未在阈值内自报 → timeout_failed
        try:
            _sweep_pending_self_report_timeouts()
        except Exception as _se2:
            log.warning(f"Autopilot pending timeout sweep error: {_se2}")

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


async def trigger_task_now(task_id: int) -> dict:
    """手动触发任务执行（异步入口）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, created_by, engine_runtime_ids, custom_fields, work_branch FROM autopilot_tasks WHERE id=?",
            (task_id,)
        ).fetchone()
    if not row:
        return {"ok": False, "message": "task not found"}
    task = {
        "id": row[0], "name": row[1], "repo_key": row[2],
        "agent_type": row[3], "instruction": row[4], "skill_name": row[5],
        "cron": row[6], "max_retry": row[7], "created_by": row[8] if len(row) > 8 else 0,
        "engine_runtime_ids": row[9] if len(row) > 9 else "",
        "custom_fields": row[10] if len(row) > 10 else "",
        "work_branch": row[11] if len(row) > 11 else "",
    }
    if task["id"] in _RUNNING_TASKS:
        return {"ok": False, "message": "task already running"}
    asyncio.create_task(_execute_task(task))
    return {"ok": True, "message": "task triggered"}
