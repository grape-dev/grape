"""A2A 链路补偿器（Phase 2 执行者，D20 责任链+策略）。

结构（docs/weekly-09/a2a-chain-patrol-design.md D20）：
- 责任链（Chain of Responsibility）：补偿请求依次流过六个处理器，任一拒绝即短路
  落状态返回，绝不继续——
    GateHandler（开关+白名单门卫）→ ClaimHandler（状态原子流转抢占，防双发）
    → RecheckHandler（补偿前终检 L1+L2 全状态）→ IdentityHandler（身份策略）
    → ModeHandler（模式恢复策略）→ DispatchHandler（A2A 派发适配器）
- 策略族（Strategy）：身份解析（路径 B 用户 PAT / 路径 A 托管用户映射 / 无主体降级）、
  模式恢复（普通链 / 子任务链）。
- 巡检器（patrol.py）保持"观察者"职责，本模块独立为"执行者"——职责分离即
  最坏伤害半径的分界线。
- D18：本模块同样零 agent_tasks 终态化写路径。
"""
from __future__ import annotations

import asyncio
import json
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config import log
from src.session_manager import db, db_lock

# 评审八轮修正①：claiming 孤儿租约（分钟）。派发是秒级；10 分钟仍未回写=进程崩溃
# 或异常路径漏写，回收为 open 下轮重试（幂等键保证回收重试不会双发）。
_CLAIMING_LEASE_MINUTES = 10

# 评审十六轮修正①/S29：open 失败冷却（分钟）。失败回挂 open 的 finding（auth_unavailable
# 等可自愈类 / dispatch 瞬态失败）在冷却窗内不再进批处理——否则每 5 分钟对同一条
# 卡死 finding 重走全链（action_meta 反复追加截断 + 日志刷屏），且 FIFO 队头被
# 永不自愈类永久占据，饿死后面真正可补偿的 finding。首轮即失败（meta 已写、
# updated_at 刚刷新）也会等满冷却，属可接受的一次延迟换全局降噪。
_RETRY_COOLDOWN_MINUTES = 30

# 评审十七轮修正/S33：race 对齐收敛的延迟二次扫（秒）。下游任务由事件总线异步
# 创建——派发返回 ≠ 任务已诞生，首轮 _sync 可能扫空；延迟后幂等再扫一次
# （_sync 谓词只动 queued/dispatched/running，重复调用零副作用）。
_RACE_ALIGN_DELAY_SECONDS = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CompensateContext:
    """补偿请求上下文：责任链各处理器读写，单一数据载体。"""
    finding_id: str
    kind: str = ""
    task_id: str = ""
    repo_key: str = ""
    issue_id: str = ""
    status: str = ""
    # 终检/解析出的判定输入
    completed_at: str = ""
    agent_type: str = ""
    target_agent: str = ""
    result_text: str = ""      # 评审五轮修正②：原任务结论，派发时随 context 下发（等价性）
    # 处理器产出
    caller_user: Optional[dict] = None
    mode_payload: dict = field(default_factory=dict)
    # 结果
    verdict: str = ""          # 最终处置标签（compensated/false_positive/auth_unavailable/...）
    action_meta: str = ""      # 落库回执


# ====== findings 读写（与 patrol.py 表共享，动作侧语义） ======


def _load_finding(fid: str) -> Optional[CompensateContext]:
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, kind, task_id, repo_key, issue_id, status FROM grape_t_patrol_findings WHERE id=?",
            (fid,))
        row = c.fetchone()
    if not row:
        return None
    return CompensateContext(finding_id=row[0], kind=row[1] or "", task_id=row[2] or "",
                             repo_key=row[3] or "", issue_id=row[4] or "", status=row[5] or "")


def _set_finding(fid: str, status: str, meta: str) -> None:
    # 仓库规范：UPDATE 禁 SQL CASE/条件逻辑——先读现值，Python 拼接，普通 UPDATE
    with db_lock:
        c = db.cursor()
        c.execute("SELECT action_meta FROM grape_t_patrol_findings WHERE id=?", (fid,))
        row = c.fetchone()
        merged = (row[0] or "") if row else ""
        merged = (merged + " | " + meta) if merged else meta
        c.execute(
            "UPDATE grape_t_patrol_findings SET status=?, action_meta=?, updated_at=? WHERE id=?",
            (status, merged[:2000], _now_iso(), fid))
        db.commit()


class Handler:
    """责任链处理器基类：handle 返回 True=放行，False=已短路（verdict 已写入 ctx）。"""
    name: str = "handler"

    def handle(self, ctx: CompensateContext) -> bool:
        raise NotImplementedError


# ====== 1. 门卫（D19）：开关 + 白名单 ======


class GateHandler(Handler):
    name = "gate"

    def handle(self, ctx: CompensateContext) -> bool:
        from src.api.patrol import compensate_enabled, compensate_repo_blocked
        if not compensate_enabled():
            ctx.verdict, ctx.action_meta = "disabled", "补偿开关被紧急关闭（meta）"
            return False
        if compensate_repo_blocked(ctx.repo_key):
            ctx.verdict, ctx.action_meta = "repo_blocklisted", "仓库在补偿黑名单"
            return False
        return True


# ====== 2. 抢占（R4）：状态原子流转，防双发 ======


class ClaimHandler(Handler):
    name = "claim"

    def handle(self, ctx: CompensateContext) -> bool:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_t_patrol_findings SET status='claiming', updated_at=? WHERE id=? AND status='open'",
                (_now_iso(), ctx.finding_id))
            db.commit()
            if c.rowcount != 1:
                ctx.verdict, ctx.action_meta = "not_open", f"状态={ctx.status}（已被消费/收敛）"
                return False
        ctx.status = "claiming"
        return True


# ====== 3. 终检（D11②）：L1（全状态）+ L2 再查一次 ======


class RecheckHandler(Handler):
    name = "recheck"

    def handle(self, ctx: CompensateContext) -> bool:
        from src.api import patrol as _p
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT completed_at, agent_type, result_text FROM agent_tasks WHERE id=?",
                (ctx.task_id,))
            row = c.fetchone()
        if not row:
            # 评审十六轮修正①/S29：原任务行已删=不自愈，终态 needs_human（不进批处理），
            # 不再回挂 open 每 5 分钟空转重试
            _set_finding(ctx.finding_id, "needs_human", "task_missing：原任务行不存在")
            ctx.verdict, ctx.action_meta = "task_missing", "原任务行不存在，转人工"
            return False
        ctx.completed_at, ctx.agent_type, result_text = row[0] or "", row[1] or "", row[2] or ""
        ctx.result_text = result_text          # 评审五轮修正②：结论随补偿下发（等价性）
        next_step = _p.extract_next_step(result_text or "")
        ctx.target_agent = _p.parse_target_agent(next_step)
        # 评审五轮修正①（终检版）：issue 已被人收敛 → 误报，放弃补偿。
        # L1/L2 只找"交棒证据"，找不到"人已处置证据"；端点只拦 cancelled，
        # 不拦 completed/pending_review——此处必须自守，否则补偿复活已关 issue。
        _term = _p.issue_terminal(ctx.repo_key, ctx.issue_id or "")
        if _term:
            _set_finding(ctx.finding_id, "false_positive", f"issue 已终态（{_term}），用户已收敛，不补")
            ctx.verdict, ctx.action_meta = "false_positive", f"终检 issue 终态命中（{_term}）"
            return False
        # 终检：任一证据出现 → 误报，放弃补偿
        if _p.l1_event_exists(ctx.repo_key, ctx.issue_id, ctx.completed_at, ctx.target_agent):
            _set_finding(ctx.finding_id, "false_positive", "终检 L1 命中（交棒已发生，入库慢）")
            ctx.verdict, ctx.action_meta = "false_positive", "终检 L1 命中"
            return False
        if _p.l2_downstream_exists(ctx.repo_key, ctx.issue_id, ctx.target_agent, ctx.completed_at,
                                   _p._subtask_parent(ctx.repo_key, ctx.issue_id or "")):
            _set_finding(ctx.finding_id, "false_positive", "终检 L2 命中（下游任务已出现）")
            ctx.verdict, ctx.action_meta = "false_positive", "终检 L2 命中"
            return False
        if not ctx.target_agent:
            # 评审十六轮修正①/S29：next_step 不可解析=数据不自愈，终态 needs_human
            _set_finding(ctx.finding_id, "needs_human", "target_unresolvable：下一棒意图不可解析，人工核对")
            ctx.verdict, ctx.action_meta = "target_unresolvable", "下一棒意图不可解析，转人工"
            return False
        return True


# ====== 4. 身份解析（D14 策略族） ======


class IdentityStrategy:
    """身份策略接口：返回 user dict 或 None（降级）。"""
    def resolve(self, repo_key: str) -> Optional[dict]:
        raise NotImplementedError


class HostUserStrategy(IdentityStrategy):
    """路径 B：用户 PAT 托管（host_user_id>0）→ 托管用户平台身份。"""
    def resolve(self, repo_key: str):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT host_user_id FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
        if row and row[0]:
            return {"id": int(row[0]), "auth_type": "jwt", "username": f"host-user-{row[0]}"}
        return None


class ManagedUserFallbackStrategy(IdentityStrategy):
    """路径 A：bot token 托管 → 托管用户映射（user_repos 管理员）。"""
    def resolve(self, repo_key: str):
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT user_id FROM user_repos WHERE repo_key=? AND is_admin=1 "
                "ORDER BY created_at LIMIT 1", (repo_key,))
            row = c.fetchone()
        if row and row[0]:
            return {"id": int(row[0]), "auth_type": "jwt", "username": f"managed-user-{row[0]}"}
        return None


class IdentityHandler(Handler):
    """身份策略编排（D14 顺序）：路径 B → 路径 A 映射 → 无主体降级（绝不冒用）。"""
    name = "identity"
    strategies = None  # 可注入（测试替身）

    def handle(self, ctx: CompensateContext) -> bool:
        chain = self.strategies or [HostUserStrategy(), ManagedUserFallbackStrategy()]
        for s in chain:
            user = s.resolve(ctx.repo_key)
            if user:
                ctx.caller_user = user
                return True
        _set_finding(ctx.finding_id, "open", "auth_unavailable")
        ctx.verdict, ctx.action_meta = "auth_unavailable", "无托管主体，降级挂起"
        return False


# ====== 5. 模式恢复（D15 策略族） ======


class ModeStrategy:
    """模式恢复策略：返回 payload 模式字段 dict；不可恢复抛 ModeUnrecoverable。"""
    def recover(self, repo_key: str, issue_id: str) -> dict:
        raise NotImplementedError


class ModeUnrecoverable(Exception):
    pass


class PlainChainMode(ModeStrategy):
    def recover(self, repo_key: str, issue_id: str) -> dict:
        return {"is_subtask_mode": False, "parent_issue": ""}


class SubtaskChainMode(ModeStrategy):
    def recover(self, repo_key: str, issue_id: str) -> dict:
        from src.api.session_store import get_issue_stage, get_carried_context
        st = get_issue_stage(repo_key, issue_id) or {}
        if st.get("is_subtask_mode"):
            parent = (st.get("parent_issue_id") or "").strip()
            if not parent:
                raise ModeUnrecoverable("子任务模式但 parent_issue 缺失")
            return {"is_subtask_mode": True, "parent_issue": parent}
        return {"is_subtask_mode": False, "parent_issue": ""}


class ModeHandler(Handler):
    name = "mode"

    def handle(self, ctx: CompensateContext) -> bool:
        from src.api.session_store import get_carried_context
        try:
            st = SubtaskChainMode().recover(ctx.repo_key, ctx.issue_id)
        except ModeUnrecoverable as e:
            # 评审十六轮修正①/S29：模式字段缺失=数据不自愈，终态 needs_human
            _set_finding(ctx.finding_id, "needs_human", f"mode_unrecoverable: {e}")
            ctx.verdict, ctx.action_meta = "mode_unrecoverable", str(e)
            return False
        ctx.mode_payload = dict(st)
        # carried_context 一并恢复（在 issue 表，D15）
        try:
            num = int(str(ctx.issue_id).lstrip("#"))
            ctx.mode_payload["carried_context"] = get_carried_context(ctx.repo_key, num)
        except (ValueError, TypeError):
            ctx.mode_payload["carried_context"] = {}
        return True


# ====== 6. 派发（D5 适配器）：直调 a2a_endpoint，compensated_by 必传 ======


def _conclusion_snippet(result_text: str) -> str:
    """评审七轮修正④：结论优先取结构化 conclusion 字段。

    result_text 是 JSON（D6），整段下发等于让下游自己再解析一遍；
    优先取 conclusion 字段（人类可读结论），解析失败/无该字段回落原文。
    """
    try:
        data = json.loads(result_text or "")
        if isinstance(data, dict):
            c = data.get("conclusion")
            if isinstance(c, str) and c.strip():
                return c.strip()
    except Exception:
        pass
    return (result_text or "").strip()


# ====== S33【评审十七轮修正】派发后终态回查 + 对齐收敛 ======


def _align_convergence_once(repo_key: str, issue_id: str) -> int:
    """对齐收敛（S33）：复用 completed 路径的官方收敛动作 _sync_issue_pipeline_terminal。

    实核修正（评审十七轮）：confirm_issue_done（用户主路径，677-703）只置
    issue.status='completed'，**不清扫 agent_tasks/events**——收敛晚于派发的
    任意时点，孤儿下游任务都存活（cancel_issue / admin-confirm 两条路才扫）。
    对齐收敛 = 把用户已表达的收敛意图补扫到迟到的下游任务上：
    - 参照系是 _sync_issue_pipeline_terminal（admin-confirm 同款）：任务置
      cancelled、事件置 completed、**不做运行时 kill**（其 docstring 明示
      "避免误杀仍在写回的引擎"——completed 路径的既定取舍，巡检沿用，
      引擎继续写回的残留由 S33 观测层留痕）；
    - patrol 侧零 agent_tasks SQL（D18/UC7-5 结构断言保住）：语义单 owner，
      调用而非复写；
    - 幂等（谓词只动 queued/dispatched/running，已终态不动）。
    返回收敛任务数；-1=调用失败（只记日志，不重试不 claiming）。
    """
    num = str(issue_id or "").lstrip("#")
    if not repo_key or not num.isdigit():
        return 0
    try:
        from src.api.routes.issues import _sync_issue_pipeline_terminal
        return _sync_issue_pipeline_terminal(repo_key, int(num)) or 0
    except Exception as e:
        log.warning(f"[patrol-comp] align convergence failed (repo={repo_key} issue={issue_id}): {e}")
        return -1


async def _align_convergence_delayed(repo_key: str, issue_id: str,
                                      delay: int = _RACE_ALIGN_DELAY_SECONDS) -> int:
    """延迟二次扫：下游任务异步诞生晚于首轮 _sync 时兜底收回（幂等）。"""
    await asyncio.sleep(delay)
    return _align_convergence_once(repo_key, issue_id)


def _post_dispatch_race_check(ctx: "CompensateContext") -> str:
    """派发成功后的终态回查（S33 第 1 层观测 + 第 2 层对齐收敛触发）。

    命中终态（completed/pending_review/cancelled）：说明派发与用户收敛赛跑
    （终检时 issue 尚非终态）。动作：
    1. 立即对齐收敛一次（扫除已诞生的迟到任务）；
    2. 排延迟二次扫（异步晚生任务）；
    3. race_detected 回执进 action_meta（观测数据源——毫秒窗在真实流量的
       发生频率由此说话，也是第 3 层零窗根治（FOR UPDATE 行锁事务）的立项
       触发条件）。
    不撤销派发本身（零破坏：不补偿补偿、不删事件——收回动作只走 _sync）。
    """
    from src.api import patrol as _p
    try:
        term = _p.issue_terminal(ctx.repo_key, ctx.issue_id or "")
    except Exception as e:
        log.warning(f"[patrol-comp] post-dispatch terminal check failed: {e}")
        return ""
    if not term:
        return ""
    aligned = _align_convergence_once(ctx.repo_key, ctx.issue_id or "")
    try:
        asyncio.get_running_loop().create_task(
            _align_convergence_delayed(ctx.repo_key, ctx.issue_id or ""))
    except RuntimeError:
        pass  # 无运行循环（同步调用语境）：只做首轮对齐
    return f"race_detected:{term}→aligned:{aligned}+delayed-resweep"


class DispatchHandler(Handler):
    name = "dispatch"

    async def handle(self, ctx: CompensateContext) -> bool:
        from src.api.routes.webhook import A2AHandoffBody, a2a_endpoint
        from src.api import patrol as _p
        marker = f"patrol:{ctx.task_id}:{_now_iso()}"   # D5 硬约束：补偿标记必传
        repository = ctx.repo_key.split(":", 1)[1] if ":" in ctx.repo_key else ctx.repo_key
        # 决策 A（2026-11-02 负责人拍板）：幂等键改规范交棒键 handoff:{task_id}:{target_agent}
        # （单源派生自 patrol.handoff_idempotency_key，护栏①；非空强校验护栏②）——
        # 迟到原交棒撞同键被端点幂等命中，残余竞态降级为良性先到先得；skill 侧
        # 落地前行为与旧 patrol-comp 键等价（单调不劣）。
        _idem_key = _p.handoff_idempotency_key(ctx.task_id, ctx.target_agent or "")
        # 评审五轮修正②：原任务结论摘要随 context 下发——手工 a2a-redo 带
        # "基于原事件结论继续"指针，补偿不带则下游缺上游结论，等价性不成立。
        # 评审七轮修正④：优先取结构化 conclusion 字段，非整段 JSON。
        _concl = _conclusion_snippet(ctx.result_text)
        if len(_concl) > 600:
            _concl = _concl[:600] + "…(截断)"
        _concl_part = (f" 原任务结论：{_concl}" if _concl else
                       " 原任务无可读结论（result_text 空），仅按自报 next_step 补发。")
        payload = {
            "repository": repository,
            "account": repository.split("/", 1)[0],
            "issue_number": str(ctx.issue_id).lstrip("#"),
            "pr_number": "",
            # context 会被 a2a_endpoint 组装进事件 payload——marker 必须出现在 context
            # 里才能落事件（事件可审计：机器补偿 vs 手工重派可区分）。
            "context": (f"巡检器补偿交棒[compensated_by={marker}]：原任务 {ctx.task_id}"
                        f"（{ctx.agent_type or 'agent'}）已完成但未交下游，按其自报 next_step 补发。"
                        f"{_concl_part} finding={ctx.finding_id}"),
            "compensated_by": marker,                 # 载荷层冗余标记（调用方可读）
            "idempotency_key": _idem_key,             # 决策 A：规范交棒键（同任务同目标恰好一次）
        }
        payload.update(ctx.mode_payload)
        try:
            res = await a2a_endpoint(
                None, A2AHandoffBody(agent=ctx.target_agent, payload=payload,
                                     idempotency_key=_idem_key),
                ctx.caller_user)
        except Exception as e:
            _set_finding(ctx.finding_id, "open", f"dispatch_error: {e}")
            ctx.verdict, ctx.action_meta = "dispatch_error", str(e)
            return False
        ok, summary = self._summarize(res)
        if ok:
            # 评审八轮修正③：审计键退役——compensated:{task_id} 曾再写一行 meta
            # （无 TTL 无清理，随补偿量线性膨胀且 set_meta_value 是 check-then-act 两步）。
            # 追溯链由 finding.action_meta（回执全文）+ 事件 context 的 compensated_by
            # 标记承载，双通道已足够，meta 键不再写。
            # 决策 A 护栏③：端点幂等命中（idempotent:true = 原生交棒先到或本键先补过）
            # 显式记 idempotent_hit——区分"我补发的"与"发现已被原生交棒命中"，
            # Phase 2 灰度误报统计的干净口径。
            _hit = "idempotent_hit" if (isinstance(res, dict) and res.get("idempotent")) or \
                   "duplicate idempotency_key" in summary else ""
            # S33 第 1+2 层：派发后终态回查——命中即对齐收敛（收回迟到任务）+
            # race_detected 回执（观测）。不撤销派发本身（零破坏）。
            _race = _post_dispatch_race_check(ctx)
            _meta = (f"idempotent_hit | {marker} → {summary}" if _hit else f"{marker} → {summary}")
            if _race:
                _meta = f"{_race} | {_meta}"
            _set_finding(ctx.finding_id, "compensated", _meta)
            ctx.verdict, ctx.action_meta = "compensated", (f"{_race} | " if _race else "") + summary
            return True
        # 评审八轮修正②：duplicate_handoff（同 issue 目标场景已有活跃事件）是
        # "交棒实际已发生"的正向证据——终检与派发间的新事件到达，正确分桶是
        # false_positive（检测误报）而非 permanent_failure（机器失败），否则
        # 误报率统计口径被污染。
        if isinstance(res, dict) and "duplicate_handoff" in str(res.get("error") or "") + summary:
            _set_finding(ctx.finding_id, "false_positive", "派发撞端点 duplicate_handoff：交棒已发生（终检后到达）")
            ctx.verdict, ctx.action_meta = "false_positive", "端点 duplicate_handoff（交棒已发生）"
            return False
        # 评审六轮修正①：永久失败终态化——4xx/端点语义拒绝（agent disabled 403、
        # not_in_scene 409、PR 订阅禁用、skipped 类）不会自愈，回挂 open 只会
        # 每周期重试到永远（action_meta 反复截断、日志刷屏）。瞬态失败（5xx/
        # 网络异常）才回 open 下轮再试。幂等键已保证不重复建事件，本修治"吵"。
        if self._is_permanent_failure(res):
            _set_finding(ctx.finding_id, "permanent_failure", f"dispatch_permanent: {summary}")
            ctx.verdict, ctx.action_meta = "dispatch_permanent_failure", summary
            return False
        _set_finding(ctx.finding_id, "open", f"dispatch_failed: {summary}")
        ctx.verdict, ctx.action_meta = "dispatch_failed", summary
        return False

    @staticmethod
    def _is_permanent_failure(res) -> bool:
        """永久失败判定：端点语义拒绝（dict 带 skipped/非 ok 状态）或 HTTP 4xx。"""
        if isinstance(res, dict):
            if res.get("skipped"):
                return True
            return res.get("status") not in ("ok", None)
        code = getattr(res, "status_code", 0)
        return 400 <= code < 500

    @staticmethod
    def _summarize(res) -> tuple[bool, str]:
        """直调返回 dict（成功/拦截）或 JSONResponse（拦截类 skipped 透传）。"""
        if isinstance(res, dict):
            status = res.get("status")
            return status == "ok", json.dumps(res, ensure_ascii=False)[:300]
        code = getattr(res, "status_code", 0)
        try:
            body = getattr(res, "body", b"") or b""
            if isinstance(body, memoryview):
                body = body.tobytes()
            summary = body.decode("utf-8", "replace")[:300]
        except Exception:
            summary = "<opaque response>"
        return code == 200, f"HTTP {code} {summary}"


# ====== 责任链编排 ======

def build_chain() -> list[Handler]:
    """同步责任链五处理器（D20）：门卫→抢占→终检→身份→模式。

    第六个处理器 DispatchHandler 为 async 接口（直调 a2a_endpoint），
    与同步 handle(ctx) 签名不兼容，在 compensate_finding 里链跑完后单独
    await——六段编排顺序不变（门卫→抢占→终检→身份→模式→派发），
    任一同步处理器拒绝即短路、派发永不执行。
    """
    return [GateHandler(), ClaimHandler(), RecheckHandler(), IdentityHandler(), ModeHandler()]


async def compensate_finding(finding_id: str) -> str:
    """对单条 finding 执行补偿责任链，返回最终处置标签。

    D23 收尾（S53/R24，外部复审发现 2）：载入与五个同步 handler 的持锁 DB
    查询经 asyncio.to_thread 卸载出事件循环（量小但与 D23 的"扫描/回收
    不裸跑循环"同口径）；派发处理器为 async 接口（直调 a2a_endpoint），
    保持 await 语义不变——同步链跑完未短路才派发的编排不变。
    """
    def _sync_chain():
        ctx = _load_finding(finding_id)
        if ctx is None:
            return None, False
        for handler in build_chain():
            if not handler.handle(ctx):
                log.info(f"[patrol-comp] {handler.name} short-circuit "
                         f"finding={finding_id} → {ctx.verdict}")
                return ctx, False
        return ctx, True
    ctx, dispatch = await asyncio.to_thread(_sync_chain)
    if ctx is None:
        return "missing"
    if not dispatch:
        return ctx.verdict
    # 尾处理器为异步派发
    dispatcher = DispatchHandler()
    ok = await dispatcher.handle(ctx)
    log.info(f"[patrol-comp] dispatch finding={finding_id} verdict={ctx.verdict} ok={ok}")
    return ctx.verdict


def reclaim_stale_claimings() -> int:
    """claiming 孤儿租约回收（评审八轮修正①；部署复验修正：无条件执行，不受补偿开关门控）。

    进程在终检/派发途中崩溃（kill -9/OOM）会把 finding 永久留在 claiming——
    回收是卫生操作：即使补偿总闸关闭也要回收（否则孤儿在开关重开后前永远滞留）。
    租约窗（10 分钟，复用 updated_at）内未回写的 claiming 回收为 open。
    """
    _lease = datetime.now(timezone.utc) - timedelta(minutes=_CLAIMING_LEASE_MINUTES)
    with db_lock:
        c = db.cursor()
        # 评审十轮修正①：禁用 SQL 内 || 拼接——MySQL 默认 sql_mode 下 || 是逻辑 OR
        # （静默把 action_meta 写成 '0'/'1'，审计回执丢失且无告警；兼容层只翻译
        # 'gitcode:'||col 形态）。改读-改-写两段普通 UPDATE（_set_finding 同款模式）。
        c.execute(
            "SELECT id, action_meta FROM grape_t_patrol_findings "
            "WHERE kind='broken_chain' AND status='claiming' AND (updated_at='' OR updated_at < ?)",
            (_lease.isoformat(),))
        recycled = 0
        for (fid, meta) in c.fetchall():
            c.execute(
                "UPDATE grape_t_patrol_findings SET status='open', action_meta=? WHERE id=?",
                (((meta or "") + " | claiming 租约超时回收")[:2000], fid))
            recycled += 1
        db.commit()
        if recycled:
            log.info(f"[patrol] claiming 孤儿回收 {recycled} 条（租约 {_CLAIMING_LEASE_MINUTES}min）")
    return recycled


async def compensate_open_findings(limit: int = 20) -> dict:
    """补偿本轮 open 的断链 finding（开关在门卫处理器里判，关=空转零动作）。

    评审十六轮修正①/S29 批处理捞单谓词：
    - 不自愈类已转 needs_human 终态，天然不进批处理（task_missing /
      target_unresolvable / mode_unrecoverable）；
    - 失败回挂 open 的（auth_unavailable 可自愈 / dispatch 瞬态失败）按
      updated_at 冷却排除（_RETRY_COOLDOWN_MINUTES）——action_meta 为空
      （从未尝试过）的捞取不受冷却限制，新 finding 首轮即处理；
    - 不再无条件 FIFO：卡死 finding 无法永久占据队头饿死后来者。
    """
    cooldown = datetime.now(timezone.utc) - timedelta(minutes=_RETRY_COOLDOWN_MINUTES)
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM grape_t_patrol_findings WHERE kind='broken_chain' AND status='open' "
            "AND (action_meta='' OR updated_at < ?) "
            "ORDER BY created_at LIMIT ?", (cooldown.isoformat(), limit))  # 评审六轮小项：按 created_at FIFO（id 是 UUID，按 id 排序=随机挑）
        ids = [r[0] for r in c.fetchall()]
    counts: dict[str, int] = {}
    for fid in ids:
        verdict = await compensate_finding(fid)
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts
