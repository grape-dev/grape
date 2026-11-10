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

不负责：LLM / ChatAgent 触发。通知纯静态内容（与 _run_notify 同规约 §十）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

RETRY = 2                 # 每渠发送尝试次数（1 次首发 + 1 次重试）
_RETRY_BACKOFF = 0.4      # 秒（第 n 次重试等待 n*_RETRY_BACKOFF）
_DEDUPE_TTL = 90.0        # 同一事件去重窗口（秒）
_PENDING_TTL = 1800.0     # 回数字选择的有效期（30 分钟）

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
}


def issue_url(repo_key: str, issue_number: int) -> str:
    """repo_key → Issue 公开页（gitcode/github/gitlab 分流；坏值 → 空）。"""
    try:
        from src.repo_key import split_owner_repo
        owner, repo = split_owner_repo(repo_key or "")
        n = int(issue_number or 0)
        if not owner or not repo or not n:
            return ""
        provider = str(repo_key).split(":", 1)[0]
        if provider == "github":
            return f"https://github.com/{owner}/{repo}/issues/{n}"
        if provider == "gitlab":
            return f"https://gitlab.com/{owner}/{repo}/-/issues/{n}"
        return f"https://gitcode.com/{owner}/{repo}/issues/{n}"
    except Exception:
        return ""


def build_card(kind: str, *, text: str, repo_key: str = "", issue_number: int = 0,
               conv_id: str = "", note: str = "", numbered: bool = False) -> Any:
    """通知内容 → Card IR（同一份内容各渠道自行渲染，业务层不出现平台格式）。"""
    from src.im.channel.card import (
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
            from src.repo_key import from_repo_key
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
            "done": Tone.SUCCESS, "error": Tone.DANGER}.get(kind, Tone.INFO)
    return Card(title=title, tone=tone, blocks=tuple(blocks), hint=note or "")


# ---------------------------------------------------------------- 目标解析

def _conn():
    from src.im.engine_wiring import _connect, default_db_path
    return _connect(default_db_path())


def active_targets(user_id: int) -> list[dict]:
    """该用户可通知的安装：每渠道取最新一条 active（多渠并存全都要）。"""
    out: list[dict] = []
    seen: set[str] = set()
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT id, channel_type, config FROM grape_t_channel_installation "
                "WHERE scope='user' AND owner_user_id=? AND status='active' "
                "ORDER BY updated_at DESC", (str(user_id),)).fetchall()
        finally:
            conn.close()
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
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT channel_chat_id FROM grape_t_channel_chat_session_binding "
                "WHERE installation_id=? AND chat_type='p2p' AND channel_chat_id<>'' "
                "ORDER BY updated_at DESC", (str(installation_id),)).fetchall()
        finally:
            conn.close()
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


def _engine():
    try:
        from src.api.main import im_engine
        return im_engine
    except Exception:
        return None


def _issue_owner(repo_key: str, issue_number: int) -> tuple[int, str, str]:
    """(user_id, conv_id, title) —— 通知目标缺失时为空值。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT user_id, COALESCE(conversation_id,''), COALESCE(title,'') "
                "FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                (repo_key, int(issue_number))).fetchone()
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
        from src.im import handler_async as h
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
    from src.im.channel.capability import Capability
    from src.im.channel.message import OutboundMessage
    from src.im.channel.card import render_text

    caps = Capability.TEXT
    try:
        caps = channel.capabilities()
    except Exception:
        pass
    if (caps & Capability.RICH_CARD):
        try:
            from src.im.engine_wiring import _card_renderer_for
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


async def notify_user(user_id: int, kind: str, text: str, *,
                      repo_key: str = "", issue_number: int = 0, conv_id: str = "",
                      targets: Optional[Iterable[dict]] = None,
                      engine: Any = None, dedupe: bool = True) -> dict:
    """主动通知该用户的全部已连接渠道（每渠独立成败）。

    targets：测试/特殊场景显式指定（[{installation_id, channel_type, chat_id?}]）；
    缺省自动解析「active 安装 ∩ 活渠道 ∩ 有 p2p 绑定」。
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
    _note = f"发送时间：{time.strftime('%Y-%m-%d %H:%M')}"
    card = build_card(kind, text=text, repo_key=repo_key, issue_number=issue_number,
                      conv_id=conv_id, note=_note)
    text_card = build_card(kind, text=text, repo_key=repo_key, issue_number=issue_number,
                           conv_id=conv_id, note=_note, numbered=True)
    _bump("attempted")

    pick = list(targets) if targets is not None else await asyncio.to_thread(active_targets, uid)
    sent: list[str] = []
    failed: list[str] = []
    skipped: dict[str, str] = {}
    for target in pick:
        cid = str(target.get("chat_id") or "") or await asyncio.to_thread(
            p2p_chat_id, target["installation_id"])
        if not cid:
            _bump("skipped_no_binding")
            skipped[target["channel_type"]] = "no_binding"
            continue
        if eng is None:
            _bump("sent_failed")
            failed.append(target["channel_type"])
            skipped[target["channel_type"]] = "engine_down"
            continue
        _bump("channels")
        ok, degraded, reason = await _deliver(eng, target, cid, card, text_card)
        if not ok and reason == "offline":
            # 渠道没连上：算"跳过"而不是"失败"（失败留给真连上但发不出去的）
            _bump("skipped_offline")
            skipped[target["channel_type"]] = "offline"
            continue
        if degraded:
            _bump("degraded_text")
            if ok:
                _register_numbered_reply(cid, conv_id, repo_key, issue_number)
        if ok:
            _bump("sent_ok")
            sent.append(target["channel_type"])
        else:
            _bump("sent_failed")
            failed.append(target["channel_type"])
            skipped[target["channel_type"]] = reason or "send_failed"

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


def notify_issue_event(repo_key: str, issue_number: int, kind: str, text: str,
                       conv_id: str = "") -> None:
    """按 repo/issue 解析归属用户后扇出（auto-finalize / review-pass 等路径用）。"""
    uid, conv, _title = _issue_owner(repo_key, issue_number)
    if not uid:
        log.info("[im-notify] no owner for %s#%s, skip kind=%s", repo_key, issue_number, kind)
        return
    schedule_notify(uid, kind, text, repo_key=repo_key, issue_number=issue_number,
                    conv_id=conv_id or conv)


__all__ = ["notify_user", "notify_issue_event", "schedule_notify", "build_card",
           "active_targets", "p2p_chat_id", "stats", "issue_url"]