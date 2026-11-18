"""IM 通知扇出（2026-11-10 A4）—— 任务分配 / 待澄清 / 待审核 / 完成 主动触达所有已连接渠道。

背景：旧实现只认「用户本人的飞书 bot」（issues_center._resolve_user_chat_bot 写死
channel_type="feishu"），用户没绑飞书时这类通知被静默跳过（reason=no_installation）。

本模块做的事：
  1. 目标 = grape_t_channel_installation(scope=user, status=active) ∩ Supervisor 活渠道；
     未连上的渠道跳过并计数（不静默）。
  2. 会话 = grape_t_channel_chat_session_binding 里该安装的 p2p 行（用户需先私聊过机器人）；
     没有绑定的渠道跳过并计数。
  3. 内容 = 单份 Card IR → 各渠道 renderer 渲染；无 RICH_CARD 的渠道降级 render_text
     （标题+正文+编号选项），并登记 pending selection → 回数字即把该渠道接入该任务会话
     （复用 handler_async._handle_selection 的 "chat" 动作，不新增命令）。
  4. 发送 = 每渠独立、失败重试 RETRY 次、一渠失败不影响其他渠；计数走 stats()。
  5. 幂等 = (user, kind, repo, issue) 在 _DEDUPE_TTL 内只发一轮（agent 重试风暴兜底）。

Issue #2261（2026-11-12）：
  6. 引擎解析 = resolve_engine() 多源（engine_wiring 注册点 → grape_server → main →
     app.state），修三进程下恒 None → engine_down 的根因；
  7. 出站 = 逐渠道落 outbox（src/im/outbox.py，grape_t_channel_outbound_card 补列），
     本进程可投立即投；engine_down/offline 保持 pending 由租约持有实例接力；
     no_binding 落 failed 可见可重发。

不负责：LLM / ChatAgent 触发。通知纯静态内容（与 _run_notify 同规约 §十）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterable, Optional

# Issue #3055：issue 编号判定/强转内核唯一入口
from src.kernel.issue_ident import native_issue_number, is_actionable_issue_number

log = logging.getLogger(__name__)

RETRY = 2                 # 每渠发送尝试次数（1 次首发 + 1 次重试）
_RETRY_BACKOFF = 0.4      # 秒（第 n 次重试等待 n*_RETRY_BACKOFF）
_DEDUPE_TTL = 90.0        # 同一事件去重窗口（秒）
_PENDING_TTL = 1800.0     # 回数字选择的有效期（30 分钟）

# outbox payload 的统一键集（入队与消费重建卡片共用一份口径——QA #1314 S5 data clump）
_PAYLOAD_KEYS = ("kind", "text", "repo_key", "issue_number", "conv_id", "user_id", "note")

_DEDUPE: dict[str, float] = {}
_STATS: dict[str, int] = {
    "attempted": 0,        # 通知批次
    "channels": 0,         # 逐渠发送尝试
    "sent_ok": 0,
    "sent_failed": 0,
    "skipped_offline": 0,     # 渠道没连上（Supervisor 无活渠道）
    "skipped_no_binding": 0,  # 该渠道没私聊过机器人，无 p2p 会话
    "degraded_text": 0,       # 卡片降级为文本（无 RICH_CARD）
    "deduped": 0,
    "queued": 0,              # #2261：入队待租约持有实例接力（engine_down/offline 场景）
}


def stats() -> dict:
    """通知计数快照（/api/im/status 的 notify 段读取）。"""
    return dict(_STATS)


def _bump(key: str, n: int = 1) -> None:
    _STATS[key] = _STATS.get(key, 0) + n


def _reset_for_tests() -> None:
    _DEDUPE.clear()
    for k in _STATS:
        _STATS[k] = 0


# ---------------------------------------------------------------- 内容（Card IR）

_KIND_META = {
    "task": ("📋 信息知会", None),
    "awaiting": ("❓ 需求待澄清", None),
    "pending_review": ("🔎 需求待审核", None),
    "done": ("✅ 任务已完成", None),
    "error": ("⚠️ 任务失败", None),
    "pipeline_failed": ("⚠️ 流水线节点失败", None),
}


def issue_url(repo_key: str, issue_number: int) -> str:
    """repo_key → Issue 公开页（host/路径按平台卡方言派生；坏值 → 空）。"""
    try:
        # P1-12：host/路径段按平台卡方言派生（kernel.repo_key.issue_web_url 单源），
        # 不再二分 github/gitlab/其余 gitcode —— gitee 仓此前会指向 gitcode.com。
        from src.kernel.repo_key import issue_web_url
        # Issue #3055：ident 保形（原 int() 对 IK* 抛错 → 链接恒空）
        n = native_issue_number(issue_number or 0)
        if not repo_key or not is_actionable_issue_number(issue_number):
            return ""
        return issue_web_url(repo_key, n)
    except Exception:
        return ""


def build_card(kind: str, *, text: str, repo_key: str = "", issue_number: int = 0,
               conv_id: str = "", note: str = "", numbered: bool = False) -> Any:
    """通知内容 → Card IR（同一份内容各渠道自行渲染，业务层不出现平台格式）。"""
    from src.domains.runtime.im.channel.card import (
        Action, ButtonStyle, Card, Tone, fields_block, row_block, select_block,
        text_block,
    )

    title = _KIND_META.get(kind, ("📢 通知", None))[0]
    if issue_number:
        title = f"{title} · #{issue_number}"

    blocks: list = []
    if text:
        blocks.append(text_block(text))

    pairs: list[tuple[str, str]] = []
    if repo_key:
        try:
            from src.kernel.repo_key import from_repo_key
            pairs.append(("📦 仓库", from_repo_key(repo_key) or "—"))
        except Exception:
            pairs.append(("📦 仓库", repo_key))
    if issue_number:
        pairs.append(("🎯 Issue", f"#{issue_number}"))
    if pairs:
        blocks.append(fields_block(*pairs))

    open_action = None
    if conv_id:
        open_action = Action(
            id="task-open", label="💬 进入任务会话", style=ButtonStyle.PRIMARY,
            params={"repo_key": repo_key,
                    "issue_number": f"#{issue_number}" if issue_number else "",
                    "conv_id": conv_id},
        )
        blocks.append(row_block(open_action))
        if numbered:
            # 仅文本降级版带编号选项：无按钮渠道会渲染成「1. 进入任务会话 · 回复数字选择」
            blocks.append(select_block("回复数字处理", [open_action]))

    url = issue_url(repo_key, issue_number)
    if url:
        blocks.append(row_block(Action(id="open-issue", label="🔗 查看 Issue", url=url)))

    tone = {"awaiting": Tone.WARNING, "pending_review": Tone.WARNING,
            "done": Tone.SUCCESS, "error": Tone.DANGER,
            "pipeline_failed": Tone.DANGER}.get(kind, Tone.INFO)
    return Card(title=title, tone=tone, blocks=tuple(blocks), hint=note or "")


def build_cards_from_payload(payload: dict) -> tuple:
    """outbox payload → (card, text_card)（消费者重投与本进程内联投递共用，QA #1314 S5）。

    payload 键集见 _PAYLOAD_KEYS；text_card 为编号选项降级版（无 RICH_CARD 渠道用）。
    """
    payload = payload or {}
    text = str(payload.get("text", ""))
    repo_key = str(payload.get("repo_key", ""))
    issue_number = native_issue_number(payload.get("issue_number") or 0)
    conv_id = str(payload.get("conv_id", ""))
    note = str(payload.get("note", ""))
    kind = str(payload.get("kind", "task"))
    card = build_card(kind, text=text, repo_key=repo_key, issue_number=issue_number,
                      conv_id=conv_id, note=note)
    text_card = build_card(kind, text=text, repo_key=repo_key, issue_number=issue_number,
                           conv_id=conv_id, note=note, numbered=True)
    return card, text_card


# ---------------------------------------------------------------- 目标解析
#
# 两张表（渠道安装 / 会话绑定）归 mgmt：SQL 只在 mgmt public 留一份，读口走任意拓扑原语
# （自有 DSN → mgmt 连接；单库 / 测试拓扑 → 调用方游标）。此前直接在这条 IM 句柄上发 SQL
# ——它是**钉在 runtime 库**的连接，三库拓扑下 1146 被本模块的 except 吞成「该用户没有
# 可通知渠道」，扇出静默停摆（不是能看见的报错）。
#
# 为什么仍要拿一条 IM 连接：单库 / 测试拓扑下「进程库」就是 mgmt 库，而 DEVOPS_DB_PATH
# 是**调用期**才定的（测试按用例换库、进程启动后才改 env）；`*_any` 回落的是 import 期
# 绑定的进程主库，只能用调用方游标把「当前这条库」交给 owner 域。自有 DSN 拓扑下
# `_owner_cursor` 会忽略这个游标、改走 mgmt 连接 —— 三库修复正是那一跳。


@contextmanager
def _db_cursor():
    """当前进程库的游标（调用期解析 DEVOPS_DB_PATH；自有 DSN 拓扑由 owner 域忽略）。"""
    from src.domains.runtime.im.engine_wiring import _connect, default_db_path
    conn = _connect(default_db_path())
    try:
        yield conn.cursor()
    finally:
        try:
            conn.close()
        except Exception as _e:      # 共享连接 close() 是空操作；自建连接关失败不影响读口
            log.debug(f"src/domains/runtime/im/notify.py cursor close skipped: {_e}")


def active_targets(user_id: int) -> list[dict]:
    """该用户可通知的安装：每渠道取最新一条 active（多渠并存全都要）。"""
    from src.domains.mgmt import public as mgmt_public
    out: list[dict] = []
    seen: set[str] = set()
    try:
        with _db_cursor() as _cur:
            rows = mgmt_public.installation_user_active_rows_any(user_id, cursor=_cur)
    except Exception as e:
        log.warning("[im-notify] list installations failed user=%s: %s", user_id, e)
        return out
    for inst_id, ctype, _raw in rows or []:
        ct = str(ctype or "").strip().lower()
        if not ct or ct in seen:
            continue
        seen.add(ct)
        out.append({"installation_id": str(inst_id), "channel_type": ct})
    return out


def p2p_chat_id(installation_id: str) -> str:
    """该安装下与用户的 p2p 会话 id（优先非话题会话；无则空）。"""
    from src.domains.mgmt import public as mgmt_public
    try:
        with _db_cursor() as _cur:
            rows = mgmt_public.installation_p2p_chat_ids_any(installation_id, cursor=_cur)
    except Exception as e:
        log.warning("[im-notify] read binding failed inst=%s: %s", installation_id, e)
        return ""
    plain = ""
    for (cid,) in rows or []:
        cid = str(cid or "")
        if not cid:
            continue
        if "#" not in cid:          # Slack 话题会话 "Dxxx#ts" 不适合主动推送
            return cid
        plain = plain or cid
    return plain


# 运行中 ASGI app 引用（lifespan 里登记，进程关闭时置空；resolve_engine 兜底用）。
# 异步函数（无 app 时返回 None），由入口模块 set_running_app 注册。
_running_asgi_app = None


def set_running_app(app) -> None:
    """入口 lifespan 登记运行中 app（resolve_engine 的 app.state 兜底来源）。"""
    global _running_asgi_app
    _running_asgi_app = app


def resolve_engine():
    """多源解析当前进程可用的 IM 引擎句柄（Issue #2261 A）。

    顺序（任一非 None 即返回）：
      ① src.domains.runtime.im.engine_wiring.get_current_engine() —— 全局注册点（A 进程主来源）
      ② src.api.grape_server.im_engine                              —— A 进程模块属性（老代码路径兜底）
      ③ 运行中 ASGI app.state.im_engine                             —— 兜底（模块属性被置空但 app 还活着）

    4 进程口径：只有 A（mgmt）进程起引擎，B/C/D 一律返回 None——它们不投递，
    只把通知写进 outbox（`engine_down` 落 pending），由 A 的消费者按租约归属补投。
    不要再 import src.api.main：单进程入口在四进程部署下不跑，且导入整包 app 有副作用。
    """
    from src.domains.runtime.im import engine_wiring
    eng = engine_wiring.get_current_engine()
    if eng is not None:
        return eng
    try:
        from src.api import grape_server as _gs
        eng = getattr(_gs, "im_engine", None)
        if eng is not None:
            return eng
    except Exception as _e:
        log.debug(f"src/domains/runtime/im/notify.py best-effort skipped: {_e}")
    try:
        app = _running_asgi_app
        if app is not None:
            eng = getattr(getattr(app, "state", None), "im_engine", None)
            if eng is not None:
                return eng
    except Exception as _e:
        log.debug(f"src/domains/runtime/im/notify.py best-effort skipped: {_e}")
    global _ENGINE_WARNED
    if not _ENGINE_WARNED:
        _ENGINE_WARNED = True
        log.warning("[im-notify] no IM engine available in this process "
                    "(engine_wiring/grape_server/app.state all empty)")
    return None


_ENGINE_WARNED = False


def _engine():
    """兼容别名（既有调用与测试）：resolve_engine 的唯一入口。"""
    return resolve_engine()


def _issue_owner(repo_key: str, issue_number: "int | str") -> tuple[int, str, str]:
    """(user_id, conv_id, title) —— 通知目标缺失时为空值。"""
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT user_id, COALESCE(conversation_id,''), COALESCE(title,'') "
                "FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                (repo_key, native_issue_number(issue_number))).fetchone()
        if not row:
            return 0, "", ""
        return int(row[0] or 0), str(row[1] or ""), str(row[2] or "")
    except Exception as e:
        log.warning("[im-notify] resolve issue owner failed %s#%s: %s", repo_key, issue_number, e)
        return 0, "", ""


# ---------------------------------------------------------------- 发送

def _register_numbered_reply(chat_id: str, conv_id: str, repo_key: str, issue_number: int) -> None:
    """文本降级渠道：登记 pending，用户回数字 = 把该渠道接入这条任务会话。

    复用 handler_async._handle_selection 的 "chat" 动作（join_feishu_chat 是通用指针，
    与渠道无关），不新增命令语义。
    """
    if not conv_id:
        return
    try:
        from src.domains.runtime.im import handler_async as h
        payload = {
            "action": "chat",
            "items": [{"id": conv_id, "title": f"Issue #{issue_number}" if issue_number else "任务会话",
                       "repo_key": repo_key, "conv_type": "task"}],
            "page": 1,
        }
        h._set_pending_selection(chat_id, payload)
        payload["expires"] = time.time() + _PENDING_TTL   # 通知有效期放宽到 30 分钟
    except Exception as e:
        log.info("[im-notify] pending selection register skipped: %s", e)


def _render(channel, channel_type: str, card, text_card=None) -> tuple[Any, bool]:
    """(OutboundMessage, degraded)——有 RICH_CARD 走 renderer，否则文本降级。"""
    from src.domains.runtime.im.channel.capability import Capability
    from src.domains.runtime.im.channel.message import OutboundMessage
    from src.domains.runtime.im.channel.card import render_text

    caps = Capability.TEXT
    try:
        caps = channel.capabilities()
    except Exception as _e:
        log.debug(f"src/domains/runtime/im/notify.py best-effort skipped: {_e}")
    if (caps & Capability.RICH_CARD):
        try:
            from src.domains.runtime.im.engine_wiring import _card_renderer_for
            renderer = _card_renderer_for(channel_type)
            if renderer is not None:
                payload = renderer.render(card).payload
                if isinstance(payload, dict):
                    return OutboundMessage(chat_id="", text="",
                                           metadata={"raw_card": payload, "kind": "raw_card"}), False
        except Exception as e:
            log.warning("[im-notify] render failed type=%s: %s", channel_type, e)
    return OutboundMessage(chat_id="", text=render_text(text_card or card)), True


async def _deliver(engine, target: dict, chat_id: str, card,
                   text_card=None) -> tuple[bool, bool, str]:
    """(ok, degraded, reason)。单个渠道内重试 RETRY 次。"""
    sup = getattr(engine, "supervisor", None)
    entry = getattr(sup, "_channels", {}).get(target["installation_id"]) if sup else None
    if not entry:
        return False, False, "offline"
    channel = entry[0]
    out, degraded = _render(channel, target["channel_type"], card, text_card)
    out.chat_id = chat_id
    reason = ""
    for attempt in range(1, RETRY + 1):
        try:
            res = await channel.send(out)
            if res is not None and getattr(res, "ok", False):
                return True, degraded, ""
            reason = str(getattr(res, "error", "") or "send_failed")
        except Exception as e:
            reason = repr(e)[:200]
        if attempt < RETRY:
            await asyncio.sleep(_RETRY_BACKOFF * attempt)
    return False, degraded, reason


def _make_payload(kind: str, text: str, repo_key: str, issue_number: int,
                  conv_id: str, uid: int, note: str) -> dict:
    """入队 payload（键集 = _PAYLOAD_KEYS，消费侧 build_cards_from_payload 同口径）。"""
    return {"kind": kind, "text": text, "repo_key": repo_key or "",
            "issue_number": native_issue_number(issue_number or 0), "conv_id": conv_id or "",
            "user_id": uid, "note": note}


def _event_bucket() -> str:
    """事件桶（90s，与内存去重窗口同量级）：区分「同一 (user,kind,repo,issue) 的新一轮触达」。

    不引入秒级精度是为了让并发多进程在同一事件上仍落到同一桶 → 仍是 1 行 1 次发送。
    """
    return str(int(time.time() // 90))


async def _backfill_existing_row(track: str, ctype: str,
                                 sent: list, failed: list, skipped: dict) -> bool:
    """同单号已存在：读行状态回填本轮结果（不真实重发）。返回是否已回填。"""
    from src.domains.runtime.im import outbox as OB
    try:
        row = await asyncio.to_thread(OB.get_row, track)
    except Exception:
        row = None
    if row is None:
        return False
    status = str(row.get("status") or "")
    if status == "sent":
        # 终态行的回填只用于「同一事件已在途/已发」的并发去重：不冒充本轮 sent
        # （架构守护 Critical-2：此前报 sent 会让监控显示正常、实际没发）
        skipped[ctype] = "deduped_existing"
        _bump("deduped")
    elif status in ("pending", "sending"):
        skipped[ctype] = "queued"
    else:
        failed.append(ctype)
        skipped[ctype] = str(row.get("last_error") or status)
    return True


async def _handle_target(target: dict, *, uid: int, kind: str, text: str,
                         repo_key: str, issue_number: int, conv_id: str,
                         note: str, eng, dedupe: bool, OB) -> None:
    """单渠道：入队 → 认领 → 投递 → 落终态（notify_user 的循环体，QA #1314 S2 抽取）。"""
    sent: list = target.setdefault("_sent", [])
    failed: list = target.setdefault("_failed", [])
    skipped: dict = target.setdefault("_skipped", {})
    inst = str(target.get("installation_id") or "")
    ctype = str(target.get("channel_type") or "")
    cid = str(target.get("chat_id") or "") or await asyncio.to_thread(p2p_chat_id, inst)

    payload = _make_payload(kind, text, repo_key, issue_number, conv_id, uid, note)
    track = OB.out_track_id(uid, kind, repo_key, issue_number, ctype, inst) if OB else ""
    inserted = False
    if OB is not None and track:
        if not cid:
            # no_binding 一步落终态（QA #1314 m2：消除「pending → failed」两步写崩溃窗口；
            # 已存在（replace=False）时 mark_failed_no_attempt 兜底覆盖 pending/sending 行）
            _bump("skipped_no_binding")
            skipped[ctype] = "no_binding"
            try:
                inserted = await asyncio.to_thread(
                    OB.enqueue, out_track_id=track, installation_id=inst,
                    channel_type=ctype, channel_chat_id=cid, payload=payload,
                    expires_at=OB.new_expiry(), status="failed",
                    last_error="no_binding", replace=not dedupe)
                if not inserted:
                    await asyncio.to_thread(OB.mark_failed_no_attempt, track, "no_binding")
            except Exception as e:
                log.warning("[im-notify] outbox no_binding enqueue failed inst=%s: %s", inst, e)
            return
        try:
            inserted = await asyncio.to_thread(
                OB.enqueue, out_track_id=track, installation_id=inst,
                channel_type=ctype, channel_chat_id=cid, payload=payload,
                expires_at=OB.new_expiry(), replace=not dedupe)
        except Exception as e:
            log.warning("[im-notify] outbox enqueue failed inst=%s: %s", inst, e)

        if not inserted:
            # Critical-2：单号已存在且旧行**已落终态** → 这是新一轮触达（事件实例不同），
            # 必须发出去；用 90s 事件桶派生新单号（同事件并发仍同桶 → 仍只 1 行）。
            old = await asyncio.to_thread(OB.get_row, track)
            if old and str(old.get("status") or "") in ("sent", "failed", "expired"):
                track = OB.out_track_id(uid, kind, repo_key, issue_number, ctype, inst,
                                        event_key=_event_bucket())
                try:
                    inserted = await asyncio.to_thread(
                        OB.enqueue, out_track_id=track, installation_id=inst,
                        channel_type=ctype, channel_chat_id=cid, payload=payload,
                        expires_at=OB.new_expiry(), replace=not dedupe)
                except Exception as e:
                    log.warning("[im-notify] outbox re-enqueue failed inst=%s: %s", inst, e)

    if not cid:
        # OB 不可用（导入失败）的退路：维持旧语义只计数
        _bump("skipped_no_binding")
        skipped[ctype] = "no_binding"
        return

    if OB is not None and track and not inserted:
        if await _backfill_existing_row(track, ctype, sent, failed, skipped):
            return

    if eng is None:
        _bump("sent_failed")
        _bump("queued")
        failed.append(ctype)
        skipped[ctype] = "engine_down"
        # 行保持 pending（不置失败）：等租约持有者消费 —— 引擎不在本进程不是行的错
        return

    # 内联投递前原子认领（QA #1314 M1）：pending → sending，与 1s 消费者互斥
    if OB is not None and track:
        try:
            claimed = await asyncio.to_thread(OB.claim_for_sending, track)
        except Exception:
            claimed = True                    # 认领不可用 → 退化为旧行为（直投）
        if not claimed:
            if await _backfill_existing_row(track, ctype, sent, failed, skipped):
                return

    _bump("channels")
    card, text_card = build_cards_from_payload(payload)
    ok, degraded, reason = await _deliver(eng, target, cid, card, text_card)
    if not ok and reason == "offline":
        # 渠道没连上：算"跳过"而不是"失败"（失败留给真连上但发不出去的）。
        # 行保持 pending：本进程 Supervisor 没连上 ≠ 别的主控没连上（租约路由语义）。
        _bump("skipped_offline")
        _bump("queued")
        skipped[ctype] = "offline"
        # 认领了的 sending 行回 pending（留给租约持有实例接力）
        if OB is not None and track:
            try:
                await asyncio.to_thread(OB.release_sending, track)
            except Exception as _e:
                log.debug(f"src/domains/runtime/im/notify.py best-effort skipped: {_e}")
        return
    if degraded:
        _bump("degraded_text")
        if ok:
            _register_numbered_reply(cid, conv_id, repo_key, issue_number)
    if ok:
        _bump("sent_ok")
        sent.append(ctype)
    else:
        _bump("sent_failed")
        failed.append(ctype)
        skipped[ctype] = reason or "send_failed"

    if OB is not None and track:
        try:
            if ok:
                await asyncio.to_thread(OB.mark_sent, track)
            else:
                await asyncio.to_thread(OB.mark_failed, track, reason or "send_failed")
        except Exception as e:
            log.warning("[im-notify] outbox mark failed inst=%s: %s", inst, e)


async def notify_user(user_id: int, kind: str, text: str, *,
                      repo_key: str = "", issue_number: int = 0, conv_id: str = "",
                      targets: Optional[Iterable[dict]] = None,
                      engine: Any = None, dedupe: bool = True) -> dict:
    """主动通知该用户的全部已连接渠道（每渠独立成败）。

    targets：测试/特殊场景显式指定（[{installation_id, channel_type, chat_id?}]）；
    缺省自动解析「active 安装 ∩ 活渠道 ∩ 有 p2p 绑定」。

    Issue #2261 B1：改「入队 + 本进程可投则立即投递」——
      1. 逐渠道写 outbox（out_track_id 主键跨进程幂等：A/B/C 并发只 1 行）；
      2. 无 p2p 绑定 → 一步落 failed/no_binding（不再静默跳过）；
      3. 本进程引擎可用 → 原子认领（pending→sending）后立即投递（成功置 sent）；
         认领失败（消费者已持有）→ 不投，回填行状态；引擎不可用 → 行保持 pending，
         由持有该安装租约的 A 实例消费者接力（≤1s 轮询）；
      4. 同单号已存在（sent/sending/pending/failed...）→ 本轮不再投递（防重复发送）。
    """
    uid = int(user_id or 0)
    if not uid:
        return {"skipped": "no_user", "sent": [], "failed": []}
    key = f"{uid}:{kind}:{repo_key}:{issue_number}"
    if dedupe:
        now = time.time()
        if _DEDUPE.get(key, 0) > now:
            _bump("deduped")
            return {"skipped": "dedupe", "sent": [], "failed": []}
        _DEDUPE[key] = now + _DEDUPE_TTL

    eng = engine if engine is not None else _engine()
    note = f"发送时间：{time.strftime('%Y-%m-%d %H:%M')}"

    pick = list(targets) if targets is not None else await asyncio.to_thread(active_targets, uid)

    try:
        from src.domains.runtime.im import outbox as OB
    except Exception:
        OB = None                                   # outbox 不可用 → 退化旧行为（直投）

    _bump("attempted")
    # 多渠并发（2026-11-18）：顺序 await 会让微信先到、飞书晚 1~2s（用户体感"不是同时"）。
    # 每个 target 内部自成闭环（独立 outbox 行/独立成败），并发互不影响。
    await asyncio.gather(*[
        _handle_target(target, uid=uid, kind=kind, text=text,
                       repo_key=repo_key, issue_number=issue_number,
                       conv_id=conv_id, note=note, eng=eng,
                       dedupe=dedupe, OB=OB)
        for target in pick
    ])

    sent = [c for t in pick for c in t.pop("_sent", [])]
    failed = [c for t in pick for c in t.pop("_failed", [])]
    skipped: dict = {}
    for t in pick:
        skipped.update(t.pop("_skipped", {}))

    log.info("[im-notify] kind=%s user=%s issue=%s#%s sent=%s failed=%s skipped=%s",
             kind, uid, repo_key, issue_number, sent, failed, skipped)
    return {"sent": sent, "failed": failed, "skipped": skipped}


def schedule_notify(user_id: int, kind: str, text: str, **kw) -> None:
    """fire-and-forget（调用方不等待通知发送，不阻塞主流程）。"""
    uid = int(user_id or 0)
    if not uid:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.info("[im-notify] no running loop, skip kind=%s user=%s", kind, uid)
        return
    task = loop.create_task(notify_user(uid, kind, text, **kw))

    def _done(t: asyncio.Task) -> None:
        try:
            t.result()
        except Exception as e:            # 通知失败不得冒泡打断调用方
            log.warning("[im-notify] task failed kind=%s user=%s: %s", kind, uid, e)

    task.add_done_callback(_done)


def notify_issue_event(repo_key: str, issue_number: "int | str", kind: str, text: str,
                       conv_id: str = "") -> None:
    """按 repo/issue 解析归属用户后扇出（auto-finalize / review-pass 等路径用）。"""
    uid, conv, _title = _issue_owner(repo_key, issue_number)
    if not uid:
        log.info("[im-notify] no owner for %s#%s, skip kind=%s", repo_key, issue_number, kind)
        return
    schedule_notify(uid, kind, text, repo_key=repo_key, issue_number=issue_number,
                    conv_id=conv_id or conv)


def notify_repo_group(repo_key: str, kind: str, text: str, *, issue_number: int = 0,
                      discriminator: str = "") -> dict:
    """向仓库绑定的 IM 群推送通知（Issue #2432：流水线失败通知走飞书群）。

    仅入队 outbox（同步、轻量、异常不上抛），实际发送由 IM outbox 消费者异步完成
    ——通知失败不影响调用方（流水线终态写入）主流程。
    Issue #2432 范围仅飞书：只处理 ``im_provider='feishu'`` 且 ``notify_enabled=1``
    （通知开关，/notify off 后不再推送）的群绑定；并按绑定 ``im_app_id`` 选择安装。
    ``discriminator`` 区分同一需求同一节点的不同失败周期，避免跨周期被 outbox 幂等吞掉。

    返回 {"queued": int, "reason": str}；无群绑定 / 无活跃安装时 queued=0。
    """
    res = {"queued": 0, "reason": ""}
    if not repo_key:
        res["reason"] = "no_repo"
        return res
    try:
        from src.kernel.session_manager import db, db_lock
        rows = []
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "SELECT im_chat_id, im_provider, im_app_id, notify_enabled FROM repo_im_group_binding "
                    "WHERE repo_key=? AND im_chat_id<>'' "
                    "AND LOWER(COALESCE(NULLIF(im_provider,''),'feishu'))='feishu' "
                    "AND COALESCE(notify_enabled,0)=1", (repo_key,))
                rows = c.fetchall() or []
            except Exception as e:
                log.warning("[im-notify] read group binding failed repo=%s: %s", repo_key, e)
                rows = []
        if not rows:
            res["reason"] = "no_binding"
            return res

        from src.domains.runtime.im import outbox as OB
        from src.domains.runtime.im.engine_wiring import get_active_installation
        note = f"发送时间：{time.strftime('%Y-%m-%d %H:%M')}"
        payload = {"kind": kind, "text": text, "repo_key": repo_key,
                   "issue_number": native_issue_number(issue_number or 0), "conv_id": "",
                   "user_id": 0, "note": note}
        for chat_id, provider, app_id, _notify_enabled in rows:
            provider = str(provider or "feishu").strip() or "feishu"
            # 群绑定的 im_app_id 是发送该群的 bot；按它过滤安装，避免同仓库多 bot 时用错 app
            inst = get_active_installation("repo", repo_key=repo_key,
                                           channel_type=provider,
                                           app_id=str(app_id or "").strip())
            if not inst:
                log.info("[im-notify] group repo=%s provider=%s app=%s no active installation",
                         repo_key, provider, "set" if app_id else "")
                continue
            iid = str(inst.get("installation_id") or "")
            track = OB.out_track_id(0, kind, repo_key, native_issue_number(issue_number or 0),
                                    provider, iid, event_key=discriminator)
            ok = OB.enqueue(out_track_id=track, installation_id=iid,
                            channel_type=provider, channel_chat_id=str(chat_id or ""),
                            payload=payload, expires_at=OB.new_expiry(),
                            chat_type="group")
            res["queued"] += 1 if ok else 0
        if not res["queued"]:
            res["reason"] = "no_active_installation"
        return res
    except Exception as e:
        log.warning("[im-notify] notify_repo_group failed repo=%s kind=%s: %s",
                    repo_key, kind, e)
        res["reason"] = "error"
        return res


__all__ = ["notify_user", "notify_issue_event", "notify_repo_group", "schedule_notify",
           "build_card", "build_cards_from_payload", "active_targets", "p2p_chat_id",
           "stats", "issue_url", "resolve_engine", "set_running_app"]
