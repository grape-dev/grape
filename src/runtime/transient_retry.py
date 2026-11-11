"""模型流瞬时异常自动重试（Issue #403，R50-R53）。

判定层收口：在任务终态收敛点（complete_task / fail_task / event_pipeline
direct 收尾）对失败原因分类，瞬时上游抖动（IAM 瞬断、模型流中断、exit 0
空输出）且无写操作副作用证据时，延迟 requeue 重跑而非直接判死。

设计约束（见 docs/weekly-08/model-stream-retry-design.md）：
- 复用 agent_tasks.attempts 列计数（无 schema 变更），上限默认 3；
- 指数退避经 threading.Timer，窗口 ≤300s，远小于僵尸判死阈值（30min/6h）；
- 写操作证据闸门与假完成硬门槛共用同一证据定义（结构化产出 / tool_uses
  里的 POST comments|pulls / A2A handoff），有证据一律不重试；
- 只拦截 running 状态任务（终态幂等先于本检查，由调用方保证）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from src.config import log
from src.session_manager import db, db_lock

# ── R50 错误分类 ──────────────────────────────────────────────

# 上游瞬时特征（小写匹配）。保守面：未命中任何特征 → 不重试。
# 收窄原则（Issue #403 QA 审查 Suggestion）：裸 timeout/eof/iam 等高频词不单
# 独命中，要求与网络/流/鉴权上下文佐证，避免普通业务失败文本误分类为可重试。
TRANSIENT_PATTERNS = [
    r"connection\s*(reset|refused|closed|aborted)",
    r"econnreset|econnrefused|epipe",
    r"broken\s*pipe",
    r"socket\s*hang\s*up",
    r"stream\s*(error|aborted|closed|interrupted|terminated)",
    r"(connection|socket|stream|read|network|https?|pool)[\s\S]{0,40}(timeout|timed?\s*out)"
    r"|(timeout|timed?\s*out)[\s\S]{0,40}(connection|socket|stream|read|network|https?|pool)",
    # 5xx：裸编号不单独命中（防 issue/PR 编号 502/503/504 误判）——要求 HTTP 上下文佐证
    r"(?:http|https|status[_\s]?code|response\s*(?:code)?|exit)[\s\S]{0,30}\b50[234]\b"
    r"|\b50[234]\b[\s\S]{0,30}(?:bad\s*gateway|service\s*unavailable|server\s*error)"
    r"|bad\s*gateway|service\s*unavailable|gateway\s*timeout",
    # 限流/过载：裸 overloaded/capacity 不单独命中（防业务文案误判）——要求服务端上下文
    r"rate\s*limit|too\s*many\s*requests"
    r"|(upstream|provider|model|engine|api)[\s\S]{0,30}overloaded"
    r"|server\s+overloaded[\s\S]{0,30}(retry|requests|unavailable|please)"
    r"|capacity\s*(exceeded|limit)",
    r"temporarily\s*unavailable|please\s*retry|retryable",
    r"iam\s*(token|auth|service|error|fail|refresh)|token\s*refresh|认证中心|鉴权中心",
]
_TRANSIENT_RE = re.compile("|".join(TRANSIENT_PATTERNS), re.I)

TRANSIENT = "upstream_transient"   # 上游瞬时抖动 → 可重试
EMPTY_OUTPUT = "empty_output"      # exit 0 空输出（假完成降级）→ 可重试
AUTH_CONFIG = "auth_config"        # VCS token/权限配置错误 → 不重试
TASK_FAILURE = "task_failure"      # 其余（默认业务失败）→ 不重试

_RETRYABLE = (TRANSIENT, EMPTY_OUTPUT)


def classify_stream_failure(exit_code, error_text: str, has_output: bool) -> dict:
    """R50：失败分类。返回 {error_class, transient, reason}。"""
    _txt = (error_text or "").strip()
    # 配置类错误优先（401/token 过期）——即便文本同时带 retry 字样也不盲目重试
    try:
        from src.token_service import is_auth_401_error
        if _txt and is_auth_401_error(_txt):
            return {"error_class": AUTH_CONFIG, "transient": False, "reason": _txt[:300]}
    except Exception:
        pass
    if _txt and _TRANSIENT_RE.search(_txt):
        return {"error_class": TRANSIENT, "transient": True, "reason": _txt[:300]}
    # exit 0 但无可信产出（假完成硬门槛降级路径）→ 模型流静默中断
    if exit_code == 0 and not has_output:
        return {"error_class": EMPTY_OUTPUT, "transient": True,
                "reason": f"exit 0 with no verified output (exit_code={exit_code})"}
    return {"error_class": TASK_FAILURE, "transient": False, "reason": _txt[:300]}


# ── R52 副作用闸门（与假完成硬门槛共用证据定义）──────────────

def has_write_evidence(tool_uses, result_text: str) -> bool:
    """任务失败前是否产生过外部写操作（评论/PR/A2A）或结构化产出。

    有证据 → 重跑可能重复提交，一律不自动重试（留给人工）。
    """
    if result_text:
        try:
            from src.api.event_store import parse_agent_output_json
            _out = parse_agent_output_json(result_text)
            if _out and _out.get("actions") and _out.get("conclusion"):
                return True
        except Exception:
            pass
    # 复用 event_store._extract_tool_command（读 input.command），与假完成硬门槛
    # 共用同一套证据提取（R52「同一套证据检测」严格成立）。
    try:
        from src.api.event_store import _extract_tool_command
    except Exception:
        _extract_tool_command = None
    for _tu in (tool_uses or []):
        if not isinstance(_tu, dict):
            continue
        _cmd = ""
        if _extract_tool_command:
            try:
                _cmd = _extract_tool_command(_tu) or ""
            except Exception:
                _cmd = ""
        if not _cmd:
            _cmd = _tu.get("cmd", "") or ""
        _up = (_cmd or "").upper()
        if (("POST" in _up and any(k in _up for k in ("/COMMENTS", "/PULLS", "/MERGE_REQUESTS")))
                or ("A2A HANDOFF" in _up) or ("POST" in _up and "/API/A2A" in _up)):
            return True
    return False


def _collect_tool_uses_from_agent_logs(event_seq) -> list:
    """从 events.agent_logs 回查 tool_use 条目（worker 上报路径证据补全）。

    worker 上报路径（fail_task / complete_task）调用方无 tool_uses 变量，但
    worker 执行期间的 tool_use 消息已由 report_messages 落入 events.agent_logs。
    当调用方未传 tool_uses 且任务有 event_seq 时，回查该字段以补全写操作证据，
    防止已 POST 评论/PR/A2A 后的瞬时失败被自动重跑造成重复副作用。
    """
    if not event_seq:
        return []
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT agent_logs FROM events WHERE seq=?", (event_seq,)).fetchone()
        if not row or not row[0]:
            return []
        _logs = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
        if not isinstance(_logs, list):
            return []
        _tus = []
        for _entry in _logs:
            if not isinstance(_entry, dict):
                continue
            if _entry.get("type") != "tool_use":
                continue
            _data = _entry.get("data")
            if isinstance(_data, dict):
                _tus.append(_data)
        return _tus
    except Exception as e:
        log.warning(f"transient_retry: collect tool_uses from agent_logs seq={event_seq} failed: {e}")
        return []


# ── R51 退避与上限（常量集中在 src/config.py，借鉴 PR #573 惯例）──

def retry_max() -> int:
    try:
        from src.config import GRAPE_TRANSIENT_RETRY_MAX
        return GRAPE_TRANSIENT_RETRY_MAX
    except Exception:
        return 3


def retry_base_delay() -> int:
    try:
        from src.config import GRAPE_TRANSIENT_RETRY_BASE
        return GRAPE_TRANSIENT_RETRY_BASE
    except Exception:
        return 10


_RETRY_DELAY_CAP = 300  # 对齐 autopilot/scheduler 退避上限


def _backoff_delay(attempts_before: int) -> int:
    """指数退避：base × 2^attempts，封顶 300s。"""
    return min(retry_base_delay() * (2 ** max(0, attempts_before)), _RETRY_DELAY_CAP)


# ── R53 分类失败摘要（借鉴 PR #573 format_stream_failure_result）──

_CLASS_LABELS = {
    TRANSIENT: "上游瞬时抖动",
    EMPTY_OUTPUT: "exit 0 空输出（模型流静默中断）",
    AUTH_CONFIG: "配置类错误（鉴权/凭据）",
    TASK_FAILURE: "任务执行失败",
}


def format_stream_failure_result(error_class: str, attempts: int, max_retries: int,
                                 reason: str = "", exit_code=None,
                                 errors=None) -> str:
    """模型流最终失败 → 分类失败摘要（区分上游抖动 vs 配置错误）。

    供终态入口（fail_task / event_pipeline 收尾）写 result_text，用户侧
    可感知错误分类 + 重试次数 + 原始错误摘要（#403 验收第 2/4 条）。
    """
    parts = []
    _label = _CLASS_LABELS.get(error_class, "模型流异常")
    if error_class == AUTH_CONFIG:
        parts.append(f"{_label}，未自动重试")
    elif attempts:
        parts.append(f"上游瞬时抖动（{_label}），自动重试 {attempts}/{max_retries} 次后仍失败")
    else:
        parts.append(_label)
    if reason:
        parts.append(f"原因: {reason[:200]}")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    _err_brief = "; ".join(str(e)[:160] for e in (errors or [])[:3])[:500]
    if _err_brief:
        parts.append(f"原始错误: {_err_brief}")
    return "❌ " + "；".join(parts)


def _notify_retry(task_id, attempt: int, max_retries: int, error_class: str, reason: str):
    """R53：task:retry 事件透出（借鉴 PR #573）→ agent_logs/SSE 前端实时日志面板。

    走 report_messages 既有通道（task_messages 落库 + events.agent_logs 追加 +
    SSE fanout），前端 ↻ 橙色图标渲染。
    """
    try:
        from src.runtime.service import report_messages
        report_messages(task_id, [{
            "type": "task:retry",
            "data": f"上游瞬时抖动（{error_class}），正在自动重试（第 {attempt}/{max_retries} 次）；"
                    f"原因: {reason[:150]}",
        }])
    except Exception as _e:
        log.warning(f"transient_retry: task:retry notify failed for {task_id}: {_e}")


# ── R51/R53 重试执行 ─────────────────────────────────────────

def _append_task_error(task_id, entry: dict):
    """R53：agent_tasks.errors 追加结构化条目（保留最近 5 条）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        errs = []
        if row and row[0]:
            try:
                errs = json.loads(row[0])
                if not isinstance(errs, list):
                    errs = [str(errs)]
            except Exception:
                errs = [str(row[0])]
        errs.append(entry)
        c.execute("UPDATE agent_tasks SET errors=? WHERE id=?", (json.dumps(errs[-5:]), task_id))
        db.commit()


def _do_requeue(task_id):
    """R51：重置回 queued（含信号/产出字段复位，防 cleanup 双信号判死误收）。

    与 requeue_task 的差异：requeue_task 不复位 stream_ended/self_reported/
    exit_code/result_text（面向客户端冲突回滚）；本函数面向整轮重跑。
    运行在 Timer 线程（外审 P1）：主事务体全程 try/except——任何 DB 异常
    记录 TRANSIENT_RETRY_SKIPPED 证据后由既有僵尸回收兜底（30min 引擎离线/
    6h 绝对上限），不留静默卡 running。
    """
    from src.runtime.service import release_busy_slot
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT runtime_id, event_seq, pool_tag, pool_agent_type, agent_type, attempts, errors "
                      "FROM agent_tasks WHERE id=?", (task_id,))
            row = c.fetchone()
            if not row:
                log.warning(f"transient_retry: task {task_id} vanished, skip requeue")
                return
            runtime_id, event_seq, pool_tag, pool_agent_type, agent_type, attempts_hint, _prior_errors = row
            # 归档旧 errors 到事件侧 agent_logs（第 9 轮外审：note 声称 archived 但无实现）——
            # 旧诊断（NO_TOKEN/COMMENT_POST_FAILED 等）不随 5 槽清空丢失，任务详情外仍可查
            _archived = []
            try:
                _pe = json.loads(_prior_errors) if _prior_errors else []
                if isinstance(_pe, list):
                    _archived = [e for e in _pe if not (
                        isinstance(e, dict) and str(e.get("error_code", "")).startswith("TRANSIENT_RETRY"))]
            except Exception:
                pass
            # 归零 runtime_id 时回填 pool_tag/pool_agent_type（claim 兜底回收同款，
            # service.py L1046-1072 实测教训：归零后 pool_tag 为空 → 池匹配不命中、
            # 定向也失配 → 永久死任务）。空则取 agent_type，非空保持原值（回填 no-op）。
            _tag = pool_tag or (agent_type or "")
            _pool_agent_type = pool_agent_type or (agent_type or "")
            c.execute(
                "UPDATE agent_tasks SET status='queued', started_at=NULL, runtime_id=0, "
                "attempts=attempts+1, stream_ended=0, self_reported=0, "
                "exit_code=NULL, result_text='', pool_tag=?, pool_agent_type=?, created_at=?, "
                "errors=? "
                "WHERE id=? AND status='running'",
                (_tag, _pool_agent_type,
                 datetime.now(timezone.utc).isoformat(),
                 json.dumps([{"error_code": "TRANSIENT_RETRY",
                              "attempt": (attempts_hint or 0) + 1,
                              "note": "prior-round errors archived to event agent_logs; "
                                      "cleared to keep the 5-slot list from filling with "
                                      "retry records and feeding stale transient keywords "
                                      "back into classification"}]),
                 task_id))
            ret = c.rowcount > 0
            if ret:
                if runtime_id:
                    try:
                        release_busy_slot(runtime_id, cursor=c)
                    except Exception as _e:
                        log.warning(f"transient_retry: release_busy_slot {runtime_id} failed: {_e}")
                # 事件同步回 pending + 重试提示（R53，用户可见）
                if event_seq:
                    try:
                        c.execute(
                            "UPDATE events SET status='pending', result=? "
                            "WHERE seq=? AND status IN ('running','pending')",
                            (f"上游瞬时抖动，正在自动重试（重试计数见任务 errors）", event_seq))
                    except Exception as _e:
                        log.warning(f"transient_retry: sync event pending failed {task_id}: {_e}")
            db.commit()
    except Exception as _exc:
        # Timer 线程内 DB 异常：不留静默卡 running——记录证据，僵尸回收兜底。
        # 不走 _append_task_error（它用同一把 db_lock，锁级故障时同样失败），
        # 直接裸 UPDATE + 新游标，最小依赖。
        log.warning(f"transient_retry: requeue DB error for task {task_id}: {_exc}", exc_info=True)
        try:
            _entry = json.dumps([{"error_code": "TRANSIENT_RETRY_SKIPPED",
                                  "reason": f"requeue transaction failed: {str(_exc)[:150]}"}])
            c2 = db.cursor()
            c2.execute("UPDATE agent_tasks SET errors=? WHERE id=?", (_entry, task_id))
            db.commit()
        except Exception as _pe:
            log.warning(f"transient_retry: skipped-retry record failed for {task_id}: {_pe}")
        return
    if ret:
        log.info(f"transient_retry: task {task_id} requeued for retry")
        # 归档：旧诊断落事件侧 agent_logs（report_messages 真实链路，任务详情外可查）
        if _archived:
            try:
                from src.runtime.service import report_messages
                report_messages(task_id, [{
                    "type": "log",
                    "data": "重试轮次归档（前轮诊断）: " + json.dumps(_archived, ensure_ascii=False)[:600],
                }])
            except Exception as _ae:
                log.warning(f"transient_retry: archive to agent_logs failed for {task_id}: {_ae}")
    else:
        # 降级记录（外部审查发现 1）：退避窗口内任务已被并发路径写终态
        # （人工取消/回收判死/停机标记）→ requeue 原子 no-op，但用户此前已看到
        # 「正在自动重试」提示。补一条可查证据：log + errors 追加。
        # 安全论据（对终态任务裸 UPDATE errors 列，此路径为新开先例，非既有模式）：
        # - _append_task_error 不经 update_task_status 终态幂等门（该门会拦终态
        #   任务的一切状态更新，但 errors 是纯记录字段，不迁移状态）；
        # - db_lock 内读改写 + [-5:] 截断，与 _append_task_error 既有结构一致；
        # - 不碰 events——update_event_complete 终态互斥无追加通道，且不绕过。
        try:
            with db_lock:
                _cur = db.cursor().execute(
                    "SELECT status, attempts FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            _final = (_cur[0] if _cur else "vanished") or "unknown"
            _sched_attempt = ((_cur[1] if _cur and len(_cur) > 1 else 0) or 0) + 1
            log.warning(f"transient_retry: requeue skipped for task {task_id} "
                        f"(terminal during backoff, status={_final})")
            _append_task_error(task_id, {
                "error_code": "TRANSIENT_RETRY_SKIPPED",
                "scheduled_attempt": _sched_attempt,
                "reason": f"scheduled retry attempt {_sched_attempt} did not run: task "
                          f"reached terminal state '{_final}' during backoff window"})
        except Exception as _e:
            log.warning(f"transient_retry: skipped-retry record failed for {task_id}: {_e}")


def retry_transient_task(task_id, exit_code, error_text: str, has_output: bool = False,
                          tool_uses=None, result_text: str = "") -> bool:
    """判定层收口入口：可重试则安排退避 requeue 并返回 True。

    调用契约：仅在任务仍为 running、且调用方终态幂等检查之后调用
    （失败终态写入之前）。
    """
    if not task_id:
        return False
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, attempts, errors, event_seq FROM agent_tasks WHERE id=?",
                            (task_id,)).fetchone()
        if not row:
            return False
        status, attempts, _stored_errors, event_seq = row
        with db_lock:
            engine = db.cursor().execute(
                "SELECT ar.name FROM agent_tasks t JOIN agent_runtimes ar ON ar.id=t.runtime_id WHERE t.id=?",
                (task_id,)).fetchone()
        if engine and (engine[0] or "").lower().startswith("atomcode"):
            return False
        if status != "running":
            return False
    except Exception as e:
        log.warning(f"transient_retry: load task {task_id} failed: {e}")
        return False
    # 调用方未给错误文本时，用任务表已存 errors 兜底（worker 上报路径）。
    # 过滤本机制自身的 TRANSIENT_RETRY*/SKIPPED 记录：旧重试记录的 reason
    # 含瞬时特征词（如 "connection reset"），拼进兜底文本会把第二轮普通
    # 业务失败误分类为瞬时 → 盲重试到耗尽（外审 P1-1，已复现）。
    if not (error_text or "").strip() and _stored_errors:
        try:
            _se = json.loads(_stored_errors)
            if isinstance(_se, list):
                _se = [e for e in _se if not (
                    isinstance(e, dict) and str(e.get("error_code", "")).startswith("TRANSIENT_RETRY"))]
            error_text = " ".join(str(e) for e in _se) if isinstance(_se, list) else str(_se)
        except Exception:
            error_text = str(_stored_errors)

    _cls = classify_stream_failure(exit_code, error_text, has_output)
    if not _cls["transient"]:
        # 非瞬时失败（auth_config/task_failure）：不重试，但把分类结果记进
        # errors——fail_task 终态摘要按 TRANSIENT_RETRY* 记录触发，配置类错误
        # 直接判死路径无记录会导致摘要回落原文（部署实测 D2 抓到）。
        # 仅 auth_config 记录（task_failure 是默认分类，记了是噪音）。
        if _cls["error_class"] == AUTH_CONFIG:
            try:
                _append_task_error(task_id, {
                    "error_code": "TRANSIENT_RETRY",
                    "error_class": AUTH_CONFIG,
                    "reason": _cls["reason"][:200],
                    "note": "classified non-transient; not retried"})
            except Exception:
                pass
        return False
    # R52 副作用闸门：调用方未传 tool_uses 且有 event_seq 时，回查 agent_logs
    # 补全写操作证据（worker 上报路径：tool_use 已由 report_messages 落库）
    _effective_tool_uses = tool_uses
    if not _effective_tool_uses and event_seq:
        _effective_tool_uses = _collect_tool_uses_from_agent_logs(event_seq)
    if has_write_evidence(_effective_tool_uses, result_text):
        log.info(f"transient_retry: task {task_id} {_cls['error_class']} but has write "
                 f"evidence, no auto-retry")
        return False
    _attempts = attempts or 0
    if _attempts >= retry_max():
        # R53：耗尽 → 记录诊断（分类 + 次数 + 摘要），交由调用方判死
        _append_task_error(task_id, {
            "error_code": "TRANSIENT_RETRY_EXHAUSTED",
            "attempts": _attempts, "max": retry_max(),
            "error_class": _cls["error_class"],
            "reason": _cls["reason"][:200]})
        log.info(f"transient_retry: task {task_id} retry exhausted "
                 f"(attempts={_attempts}, class={_cls['error_class']})")
        return False

    _append_task_error(task_id, {
        "error_code": "TRANSIENT_RETRY", "attempt": _attempts + 1,
        "max": retry_max(), "error_class": _cls["error_class"],
        "reason": _cls["reason"][:200]})
    # R53：task:retry 实时透出（agent_logs/SSE → 前端 ↻ 图标）
    _notify_retry(task_id, _attempts + 1, retry_max(),
                  _cls["error_class"], _cls["reason"])
    _delay = _backoff_delay(_attempts)
    log.info(f"transient_retry: task {task_id} {(_cls['error_class'])} -> retry in {_delay}s "
             f"(attempt {_attempts + 1}/{retry_max()})")
    t = threading.Timer(_delay, _do_requeue, args=(task_id,))
    t.daemon = True
    t.start()
    return True
