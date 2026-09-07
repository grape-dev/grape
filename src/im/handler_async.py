"""异步飞书消息处理 — 支持 BotContext 隔离的多Bot消息分发

核心改造:
  - process_chat_async: 接收 (BotContext, parsed) 替代全局函数调用
  - 所有飞书API调用通过 ctx + api 实例方法，按Bot身份隔离
  - 纯逻辑（bind/clean 意图、repo 解析、用户归属）内联本模块，无同步 HTTP；
    IO 型命令（/bind、/refresh 的群成员↔协作者映射同步）走 await api 异步方法。
"""
import re, os, asyncio, time, json
from datetime import datetime
from typing import TYPE_CHECKING

from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import get_or_create_session, update_session, update_conversation, db
from src.prompt_builder import build_chat_prompt
from src.runtime.executor import consume_stream_async
from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
from src.api.event_store import acquire_repo_lock as _acquire_repo_lock

if TYPE_CHECKING:
    from src.im.feishu_async import BotContext, FeishuAsync

# p2p 首次消息待确认绑定：{app_id: {username: {"open_id", "gitcode_nickname", "expires"}}}
# 用户确认后才写入 unified_user_mapping，防止错误绑定。
_pending_binds: dict[str, dict] = {}

# 卡片命令编号选择 pending 态（feishu-repo-navigation-commands-design.md §3.2）：
#   chat_id -> {action: repo|chat|task, repo_key, page, page_size, items, expires}
_PENDING_SELECT_TTL = 120.0
_PENDING_SELECT_PAGE_SIZE = 20
_pending_selections: dict[str, dict] = {}


def _set_pending_selection(chat_id: str, payload: dict):
    payload["expires"] = time.time() + _PENDING_SELECT_TTL
    payload["page_size"] = _PENDING_SELECT_PAGE_SIZE
    _pending_selections[chat_id] = payload


def _get_pending_selection(chat_id: str) -> dict | None:
    p = _pending_selections.get(chat_id)
    if not p:
        return None
    if p.get("expires", 0) < time.time():
        _pending_selections.pop(chat_id, None)
        return None
    return p


def _clear_pending_selection(chat_id: str):
    _pending_selections.pop(chat_id, None)


# 最近图片描述缓存：chat_id -> {"desc": str, "expires": float}
# 用户发图片后，后续追问在 TTL 窗口内自动注入图片描述，避免上下文丢失
# 导致 opencode 以"非 DevOps 话题"拒绝回答图片相关问题。
_PENDING_IMAGE_TTL = 1800.0  # 30 分钟（持久化到 DB 后内存缓存仅做近期加速）
_pending_image_desc: dict[str, dict] = {}


def _set_pending_image(chat_id: str, desc: str):
    _pending_image_desc[chat_id] = {"desc": desc, "expires": time.time() + _PENDING_IMAGE_TTL}


def _get_pending_image(chat_id: str) -> str:
    p = _pending_image_desc.get(chat_id)
    if not p:
        return ""
    if p.get("expires", 0) < time.time():
        _pending_image_desc.pop(chat_id, None)
        return ""
    return p.get("desc", "")


def _clear_pending_image(chat_id: str):
    _pending_image_desc.pop(chat_id, None)


# repo 绑定缓存：{(chat_id, app_id): {"repo_key": str, "expires": float}}
# 防止 DB 抖动（db_lock 竞争/瞬时错误）导致已绑定仓库查不到 → 误弹"选择仓库"卡片。
# TTL 60s：绑定变更通过 /bind /_bind_repo 显式失效缓存，60s 仅兜底 DB 异常窗口。
_repo_bind_cache: dict[tuple, dict] = {}
_REPO_BIND_TTL = 60.0


def _invalidate_repo_bind_cache(chat_id: str, app_id: str = ""):
    """绑定变更时显式失效缓存（/bind /_bind_repo /_handle_selection 调用）。"""
    _repo_bind_cache.pop((chat_id, app_id), None)
    # app_id 为空时清除该 chat_id 下所有 app（兜底）
    if not app_id:
        keys = [k for k in _repo_bind_cache if k[0] == chat_id]
        for k in keys:
            _repo_bind_cache.pop(k, None)


def _current_bound_repo(chat_id: str, account: str, db) -> str:
    """当前飞书聊天绑定的仓库 repo_key（repo_im_group_binding）

    带内存缓存——DB 查询失败时回退到上次已知值，避免瞬时 DB 锁竞争导致误判"未绑定"。
    """
    cache_key = (chat_id, account)
    now = time.time()
    cached = _repo_bind_cache.get(cache_key)
    if cached and cached.get("expires", 0) > now:
        return cached.get("repo_key", "")
    try:
        c = db.cursor()
        c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
        row = c.fetchone()
        repo_key = row[0] if row else ""
        _repo_bind_cache[cache_key] = {"repo_key": repo_key, "expires": now + _REPO_BIND_TTL}
        return repo_key
    except Exception:
        # DB 异常时回退到上次已知值（即使过期也用，总比弹"选择仓库"卡片好）
        if cached:
            return cached.get("repo_key", "")
        return ""


def _candidate_card(emoji: str, title: str, items: list[str], page: int, total_pages: int, nav_cmd: str) -> str:
    """卡片命令候选列表（lark_md 子集）：标题 + 编号行 + 翻页/取消提示"""
    lines = [f"**{emoji} {title}**"]
    for i, label in enumerate(items, 1):
        lines.append(f"{i}. {label}")
    if total_pages > 1:
        lines.append(f"\n第 {page}/{total_pages} 页 · `{nav_cmd} N` 翻页")
    lines.append("回复数字选择 · `0` 取消")
    return "\n".join(lines)


async def _send_card_reply(ctx, api, msg_id, card):
    """发送自定义卡片回复，返回是否成功

    自动在卡片底部追加[帮助]按钮，与 reply_card 行为一致。
    """
    # 追加 [帮助] 按钮（如果卡片还没有）
    elements = card.get("elements", [])
    has_help = any(
        any(btn.get("value", {}).get("cmd") == "/help" for btn in el.get("actions", []))
        for el in elements if el.get("tag") == "action"
    )
    if not has_help:
        elements.append({
            "tag": "action",
            "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"}, "value": {"cmd": "/help"}}]
        })
        card["elements"] = elements
        log.info(f"[{ctx.name}] _send_card_reply: appended [帮助] button, elements={len(elements)}")
    token = await api._ensure_token(ctx)
    if not token:
        return False
    session = await api._get_session()
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply"
    payload = {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json()
            if data.get("code", -1) == 0:
                ctx.metrics.messages_sent += 1
                log.info(f"[{ctx.name}] _send_card_reply ok")
                return True
            log.warning(f"[{ctx.name}] _send_card_reply failed: code={data.get('code')} msg={data.get('msg','')[:50]}")
            return False
    except Exception as e:
        log.warning(f"[{ctx.name}] _send_card_reply error: {e}")
        return False

async def _send_repo_card(ctx: "BotContext", api: "FeishuAsync", chat_id: str, msg_id: str,
                          uid: int, account: str, page: int = 1) -> bool:
    """生成并发送 `/repo` 卡片（候选仓库显示为可点击按钮），设置 pending。

    供 `/repo` 命令与"未绑定仓库"兜底共用（自动触发卡片，feishu-repo-navigation-commands-design.md）。
    无候选返回 False（调用方给引导文案）。
    """
    from src.user.db import get_managed_repos
    from src.repo_key import from_repo_key
    repos = get_managed_repos(uid)
    if not repos:
        return False
    bound = _current_bound_repo(chat_id, account, db)
    total_pages = max(1, -(-len(repos) // _PENDING_SELECT_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    items = []
    for r in repos[(page - 1) * _PENDING_SELECT_PAGE_SIZE: page * _PENDING_SELECT_PAGE_SIZE]:
        full = from_repo_key(r["repo_key"])
        role = "owner" if r.get("is_admin") else ("member" if r.get("is_join") else "")
        marker = "【绑定中】" if (r["repo_key"] == bound or full == bound) else ""
        items.append(f"{full}（{role}）{marker}")
    _set_pending_selection(chat_id, {"action": "repo", "repo_key": bound, "items": repos, "page": page})

    # 文字列表 + 纯数字按钮
    text = _candidate_card("📁", f"已托管仓库列表（共 {len(repos)} 个）", items, page, total_pages, "/repo")
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]
    for i in range(0, len(items), 3):
        row_btns = [{"tag": "button", "text": {"tag": "plain_text", "content": str(j+1)},
                     "value": {"cmd": f"/bind {j+1}"}} for j in range(i, min(i+3, len(items)))]
        elements.append({"tag": "action", "actions": row_btns})
    elements.append({"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"}, "value": {"cmd": "/help"}}]})

    ok = await _send_card_reply(ctx, api, msg_id, {
        "header": {"title": {"tag": "plain_text", "content": "📁 选择仓库"}, "template": "blue"},
        "elements": elements,
    })
    return ok


# 确认/否认意图
_CONFIRM_RE = re.compile(r'^(?:是|对|确认|确定|是的|对的|没错|对的是|对，是|嗯|可以|yes|y)\s*$', re.IGNORECASE)
_DENY_RE = re.compile(r'^(?:不是|不对|否|错|不要|取消|no|n)\s*$', re.IGNORECASE)

_PENDING_TTL = 120  # 秒，待确认有效期

# ===== 纯逻辑函数（无网络 IO，仅 SQLite 快查询） =====

_BIND_PATTERNS = [
    re.compile(r'(?:绑定|连接|关联|设置|使用|切换到|帮我绑定)\s*(?:到)?\s*([\w.-]+/[\w.-]+)'),
    re.compile(r'(?:把|将|让)\s*([\w.-]+/[\w.-]+)\s*(?:绑定|连上|设置)'),
]

_CLEAN_PATTERNS = [
    r'(?:清除|清理|清空|重置)(?:会话|缓存|上下文|Agent|agent|记忆)',
    r'(?:重来|从头开始)(?:吧|一下)?',
]


def detect_bind_intent(text: str) -> str:
    """自然语言绑定检测：用户说'绑定到 owner/repo' 等，返回 repo_key 或空"""
    for pat in _BIND_PATTERNS:
        m = pat.search(text)
        if m:
            repo = m.group(1)
            log.info(f"Feishu: detected bind intent -> {repo}")
            return repo
    return ""


def detect_clean_intent(text: str) -> bool:
    """自然语言清理检测：用户说'清除会话缓存'等"""
    for pat in _CLEAN_PATTERNS:
        if re.search(pat, text):
            log.info("Feishu: detected clean intent")
            return True
    return False


def resolve_repo(text: str, chat_id: str, sender_id: str, db, account: str = "") -> str:
    # 1. 自然语言绑定检测
    bind_repo = detect_bind_intent(text)
    if bind_repo:
        return bind_repo
    # 2. 消息中显式匹配 owner/repo
    m = re.search(r'[\w.-]+/[\w.-]+', text)
    if m:
        return m.group(0)
    # 3. 查询群绑定表（repo_key 带前缀）——走 _current_bound_repo 缓存路径
    bound = _current_bound_repo(chat_id, account, db)
    if bound:
        return bound
    return ""


def get_user_id_by_app_id(app_id: str) -> int | None:
    """通过 im_app_id 查找关联的 user_id"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT user_id FROM chat_agent_session WHERE im_app_id=? AND user_id>0 LIMIT 1", (app_id,))
            row = c.fetchone()
            if row:
                return row[0]
        except Exception:
            pass
    return None


async def process_command_async(ctx: "BotContext", api: "FeishuAsync",
                                text: str, chat_id: str, sender_id: str, db,
                                account: str = "", msg_id: str = "",
                                sender_open_id: str = "") -> str:
    """异步命令处理（卡片化导航 feishu-repo-navigation-commands-design.md）。

    新命令 /repo /chat /task /new /where /cancel /help 用 reply_card 回复并返回空串（调用方跳过文本回复）；
    /clean 沿用同步逻辑后以卡片确认。旧 /bind /unbind /status /refresh /autodev /notify 及
    /对话 /加入 已按设计删除（/repo 取代 /bind、/chat 取代 /对话、/where 承担 /status 定位）。
    """
    text = text.strip()
    parts = text.split()
    if not parts:
        return ""
    cmd = parts[0].lower()
    uid = get_user_id_by_app_id(account) or 0

    if cmd == "/repo":
        if not uid:
            return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
        page = max(1, int(parts[1])) if len(parts) >= 2 and parts[1].isdigit() else 1
        if await _send_repo_card(ctx, api, chat_id, msg_id, uid, account, page):
            return ""
        return "📋 你名下还没有已托管的仓库。"

    if cmd == "/chat":
        if not uid:
            return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
        bound = _current_bound_repo(chat_id, account, db)
        if not bound:
            await _send_card_reply(ctx, api, msg_id, _NO_REPO_CARD)
            return ""
        from src.session_manager import get_user_conversations
        convs = get_user_conversations(uid, repo_key=bound, limit=20)
        if not convs:
            await api.reply_card(ctx, msg_id, f"**📄 {bound} 下暂无会话**\n使用 `/new` 新建并加入。")
            return ""
        page = max(1, int(parts[1])) if len(parts) >= 2 and parts[1].isdigit() else 1
        total_pages = max(1, -(-len(convs) // _PENDING_SELECT_PAGE_SIZE))
        page = min(page, total_pages)
        items = []
        for cv in convs[(page - 1) * _PENDING_SELECT_PAGE_SIZE: page * _PENDING_SELECT_PAGE_SIZE]:
            tag = "任务" if cv.get("conv_type") == "task" else "一般"
            cur = " 👈当前" if cv.get("im_channel_id") == chat_id else ""
            items.append(f"[{tag}] {cv.get('title') or '新对话'}{cur}")
        _set_pending_selection(chat_id, {"action": "chat", "repo_key": bound, "items": convs, "page": page})
        text = _candidate_card("📄", f"{bound} 会话列表", items, page, total_pages, "/chat")
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]
        for i in range(0, len(items), 3):
            row_btns = [{"tag": "button", "text": {"tag": "plain_text", "content": str(j+1)},
                         "value": {"cmd": f"/join {j+1}"}} for j in range(i, min(i+3, len(items)))]
            elements.append({"tag": "action", "actions": row_btns})
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "🔄 新建会话"}, "value": {"cmd": "/new"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"}, "value": {"cmd": "/help"}},
        ]})
        await _send_card_reply(ctx, api, msg_id, {
            "header": {"title": {"tag": "plain_text", "content": "📄 选择会话"}, "template": "blue"},
            "elements": elements,
        })
        return ""

    if cmd == "/task":
        if not uid:
            return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
        bound = _current_bound_repo(chat_id, account, db)
        if not bound:
            await _send_card_reply(ctx, api, msg_id, _NO_REPO_CARD)
            return ""
        c = db.cursor()
        c.execute("SELECT id, issue_number, title, status FROM tasks WHERE user_id=? AND repo_key=? ORDER BY id DESC LIMIT 20",
                  (uid, bound))
        tasks = c.fetchall()
        if not tasks:
            await api.reply_card(ctx, msg_id, f"**📋 {bound} 下暂无任务**")
            return ""
        page = max(1, int(parts[1])) if len(parts) >= 2 and parts[1].isdigit() else 1
        total_pages = max(1, -(-len(tasks) // _PENDING_SELECT_PAGE_SIZE))
        page = min(page, total_pages)
        items = [f"Issue #{t[1]} · {t[3]} · {t[2] or ''}" for t in tasks[(page - 1) * _PENDING_SELECT_PAGE_SIZE: page * _PENDING_SELECT_PAGE_SIZE]]
        _set_pending_selection(chat_id, {"action": "task", "repo_key": bound, "items": tasks, "page": page})
        text = _candidate_card("📋", f"{bound} 任务列表", items, page, total_pages, "/task")
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]
        for i in range(0, len(items), 3):
            row_btns = [{"tag": "button", "text": {"tag": "plain_text", "content": str(j+1)},
                         "value": {"cmd": f"/join {j+1}"}} for j in range(i, min(i+3, len(items)))]
            elements.append({"tag": "action", "actions": row_btns})
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"}, "value": {"cmd": "/help"}},
        ]})
        await _send_card_reply(ctx, api, msg_id, {
            "header": {"title": {"tag": "plain_text", "content": "📋 任务列表"}, "template": "blue"},
            "elements": elements,
        })
        return ""

    if cmd == "/new":
        if not uid:
            return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
        bound = _current_bound_repo(chat_id, account, db)
        if not bound:
            await _send_card_reply(ctx, api, msg_id, _NO_REPO_CARD)
            return ""
        from src.session_manager import create_conversation, join_feishu_chat
        cid = create_conversation(uid, bound)
        if cid and join_feishu_chat(chat_id, cid, uid):
            await api.reply_card(ctx, msg_id, f"**✅ 已新建并加入对话**\n`{bound}` · 新对话\n后续飞书消息将进入该对话。")
        else:
            await api.reply_card(ctx, msg_id, "**❌ 新建对话失败**")
        return ""

    if cmd == "/where":
        from src.session_manager import get_conversation_by_im
        bound = _current_bound_repo(chat_id, account, db)
        conv = get_conversation_by_im(chat_id, uid) if uid else None
        lines = ["**📍 当前位置**", f"仓库：{bound or '未绑定'}"]
        if conv:
            tag = "任务" if conv.get("conv_type") == "task" else "一般"
            title = conv.get("title") or "新对话"
            if conv.get("issue_number"):
                title += f"（Issue #{conv['issue_number']}）"
            lines.append(f"会话：[{tag}] {title}")
            # 对账：群绑定仓库 ≠ 当前会话所属仓库 → 提示不一致（2026-08-16 发散 bug 兜底提示）
            try:
                from src.repo_key import from_repo_key
                if bound and conv.get("repo_key") and from_repo_key(bound) != conv["repo_key"]:
                    lines.append(f"⚠️ 群绑定 `{from_repo_key(bound)}` 与当前会话仓库 `{conv['repo_key']}` 不一致，建议 `/repo` 重绑或 `/new` 新建。")
            except Exception:
                pass
        else:
            lines.append("会话：未加入（新消息将新建会话）")
        await api.reply_card(ctx, msg_id, "\n".join(lines))
        return ""

    if cmd == "/cancel":
        _clear_pending_selection(chat_id)
        await api.reply_card(ctx, msg_id, "**✅ 已取消当前选择**")
        return ""

    if cmd in ("/clean", "/cleansession", "/reset", "/cleancache"):
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。先使用 `/repo` 绑定后再清理。"
            repo_key = row[0]
            from src.repo_key import to_repo_key, from_repo_key
            full_key = to_repo_key(repo_key)
            legacy_key = from_repo_key(repo_key)
            c.execute("UPDATE chat_agent_session SET runtime_session_id='', runtime='' WHERE repo_key=? OR repo_key=?", (full_key, legacy_key))
            db.commit()
            await api.reply_card(ctx, msg_id, f"**✅ 已清除会话缓存**\n`{full_key}` 下次事件将使用全新 Agent 上下文。")
        except Exception as e:
            await api.reply_card(ctx, msg_id, f"**❌ 清理失败**\n{e}")
        return ""

    if cmd in ("/help", "/start"):
        await api.send_quick_cmd_card(ctx, sender_open_id)
        log.info(f"Command: /help -> send_quick_cmd_card to open_id={sender_open_id[:20]}")
        return ""

    # /bind N — 数字按钮直接绑定仓库
    if cmd == "/bind" and len(parts) >= 2 and parts[1].isdigit():
        num = int(parts[1])
        pending = _get_pending_selection(chat_id)
        log.info(f"[/bind] num={num} pending={'found' if pending else 'NONE'} chat={chat_id[:20]}")
        if pending and pending.get("action") == "repo":
            all_items = pending.get("items", [])
            page = int(pending.get("page") or 1)
            page_size = int(pending.get("page_size") or 20)
            idx = (page - 1) * page_size + (num - 1)  # 页码 → 全局索引
            log.info(f"[/bind] items_count={len(all_items)} page={page} idx={idx}")
            if 0 <= idx < len(all_items):
                r = all_items[idx]
                repo_key = r["repo_key"]
                log.info(f"[/bind] binding repo_key={repo_key}")
                await _bind_repo(ctx, api, chat_id, msg_id, repo_key, account)
            else:
                await api.reply_card(ctx, msg_id, f"**⚠️ 无效编号 {num}**\n回复 `0` 取消。")
        return ""

    # /join N — 数字按钮选择会话/任务（chat/task 卡片按钮）
    if cmd == "/join" and len(parts) >= 2 and parts[1].isdigit():
        num = parts[1]
        pending = _get_pending_selection(chat_id)
        log.info(f"[/join] num={num} pending={'found' if pending else 'NONE'} chat={chat_id[:20]}")
        if pending and pending.get("action") in ("chat", "task"):
            await _handle_selection(ctx, api, chat_id, msg_id, num, pending)
        elif pending and pending.get("action") == "repo":
            # 兼容：/join 也能选仓库
            await _handle_selection(ctx, api, chat_id, msg_id, num, pending)
        else:
            await api.reply_card(ctx, msg_id, "**⚠️ 请先使用 `/repo` 或 `/chat` 或 `/task` 选择列表。**")
        return ""

    return ""


_BIND_SUCCESS_CARD_TPL = {
    "header": {"title": {"tag": "plain_text", "content": "✅ 绑定成功"}, "template": "green"},
    "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**✅ 已绑定 `{repo_key}`**\n使用 /chat 选择会话，或直接开始提问。"}},
        {"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "📋 选择会话"}, "value": {"cmd": "/chat"}},
        ]},
    ],
}


async def _send_bind_success_card(ctx, api, msg_id, repo_key):
    """发送绑定成功卡片（含[选择会话]按钮）"""
    import copy
    card = copy.deepcopy(_BIND_SUCCESS_CARD_TPL)
    card["elements"][0]["text"]["content"] = card["elements"][0]["text"]["content"].format(repo_key=repo_key)
    await _send_card_reply(ctx, api, msg_id, card)


async def _bind_repo(ctx, api, chat_id, msg_id, repo_key, uid_str):
    """绑定仓库到当前聊天"""
    from src.session_manager import db_lock
    uid = get_user_id_by_app_id(ctx.app_id) or 0
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES (?,?,?,?,?)",
                  (chat_id, repo_key, "gitcode", str(uid), ctx.app_id))
        c.execute("UPDATE chat_conversations SET im_channel_id='' WHERE im_channel_id=?", (chat_id,))
        db.commit()
    _invalidate_repo_bind_cache(chat_id, ctx.app_id)
    await _send_bind_success_card(ctx, api, msg_id, repo_key)


def _build_feishu_chat_prompt(text: str, repo_key: str, history, skills, exec_account: str,
                              chat_runtime_id: int, conv_id: str, user_info: str = "",
                              has_valid_session: bool = False) -> str:
    """按当前加入会话的 conv_type 分支构建 Feishu chat prompt（feishu-web-chat-unified-design.md §11）。

    task 会话 → build_task_chat_prompt（会话消息 get_messages 作历史，与 web chat 一致）；
    否则 general → build_chat_prompt（事件 history）。
    has_valid_session：飞书侧按 repo 级 oc_sid 是否有效；无效时注入完整对话历史
    （chat-prompt-history-injection-design.md §3）。
    """
    conv_obj = None
    if conv_id:
        try:
            from src.session_manager import get_conversation
            conv_obj = get_conversation(conv_id)
        except Exception as e:
            log.warning(f"Feishu: get conversation failed: {e}")
    if conv_obj and conv_obj.get("conv_type") == "task":
        from src.prompt_builder import build_task_chat_prompt
        from src.repo_key import from_repo_key
        try:
            from src.session_manager import get_messages
            conv_history = get_messages(conv_id, limit=20)
        except Exception:
            conv_history = []
        body = build_task_chat_prompt(text, from_repo_key(repo_key), conv_history, conv_obj, account=exec_account, runtime_id=chat_runtime_id, user_info=user_info, has_valid_session=has_valid_session)
    else:
        # general chat：用 chat_messages 表的历史（格式 {role, content}），与 task 会话一致；
        # 不用 event_history（格式 {type, stdout}，与 _append_chat_history 的 role/content 不匹配）
        conv_history = []
        if conv_id:
            try:
                from src.session_manager import get_messages
                conv_history = get_messages(conv_id, limit=20)
            except Exception:
                pass
        body = build_chat_prompt(text, repo_key, conv_history, skills=skills,
                                 runtime_id=chat_runtime_id, user_info=user_info,
                                 has_valid_session=has_valid_session)
    return body


async def _handle_selection(ctx: "BotContext", api: "FeishuAsync", chat_id: str, msg_id: str,
                            num: str, pending: dict):
    """卡片命令编号选择执行（feishu-repo-navigation-commands-design.md §3.2）。"""
    try:
        if num == "0":
            _clear_pending_selection(chat_id)
            await _send_card_reply(ctx, api, msg_id, {
                "header": {"title": {"tag": "plain_text", "content": "✅ 已取消"}, "template": "grey"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "**✅ 已取消选择**"}}],
            })
            return
        action = pending.get("action")
        uid = get_user_id_by_app_id(ctx.app_id) or 0
        items = pending.get("items") or []
        page = int(pending.get("page") or 1)
        page_size = int(pending.get("page_size") or _PENDING_SELECT_PAGE_SIZE)
        idx = (page - 1) * page_size + (int(num) - 1)
        if idx < 0 or idx >= len(items):
            await _send_card_reply(ctx, api, msg_id, {
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 无效编号"}, "template": "red"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"**⚠️ 无效编号 {num}**\n回复 `0` 取消。"}}],
            })
            return
        if action == "repo":
            repo = items[idx]
            repo_key = repo["repo_key"] if isinstance(repo, dict) else repo
            from src.session_manager import db_lock
            with db_lock:
                c = db.cursor()
                c.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES (?,?,?,?,?)",
                          (chat_id, repo_key, "gitcode", str(uid), ctx.app_id))
                # 绑定即重置会话位置：清掉旧会话指针，下次消息在新仓库下新建会话
                #（修复 2026-08-16 "群绑定仓库 ≠ 会话所属仓库" 发散 bug）
                c.execute("UPDATE chat_conversations SET im_channel_id='' WHERE im_channel_id=?", (chat_id,))
                db.commit()
            _invalidate_repo_bind_cache(chat_id, ctx.app_id)
            _clear_pending_selection(chat_id)
            await _send_bind_success_card(ctx, api, msg_id, repo_key)
        elif action == "chat":
            cv = items[idx]
            from src.session_manager import join_feishu_chat
            if join_feishu_chat(chat_id, cv["id"], uid):
                tag = "任务" if cv.get("conv_type") == "task" else "一般"
                _clear_pending_selection(chat_id)
                await _send_card_reply(ctx, api, msg_id, {
                    "header": {"title": {"tag": "plain_text", "content": "✅ 已加入对话"}, "template": "green"},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"**✅ 已加入对话**\n[{tag}] {cv.get('title') or '新对话'}（{cv.get('repo_key') or ''}）"}}],
                })
            else:
                await _send_card_reply(ctx, api, msg_id, {
                    "header": {"title": {"tag": "plain_text", "content": "❌ 加入失败"}, "template": "red"},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "**❌ 加入失败**"}}],
                })
        elif action == "task":
            tid, issue_number, title, status = items[idx]
            cid = ""
            c = db.cursor()
            row = c.execute("SELECT conversation_id FROM tasks WHERE id=?", (tid,)).fetchone()
            if row and row[0]:
                cid = row[0]
            if not cid:
                from src.session_manager import db_lock
                with db_lock:
                    cc = db.cursor()
                    r2 = cc.execute("SELECT id FROM chat_conversations WHERE user_id=? AND repo_key=? AND conv_type='task' AND issue_number=? LIMIT 1",
                                    (uid, pending.get("repo_key", ""), issue_number)).fetchone()
                    if r2:
                        cid = r2[0]
            if not cid:
                from src.session_manager import create_conversation
                cid = create_conversation(uid, pending.get("repo_key", ""), title=f"Issue #{issue_number}",
                                          conv_type="task", issue_number=issue_number)
            from src.session_manager import join_feishu_chat
            if cid and join_feishu_chat(chat_id, cid, uid):
                _clear_pending_selection(chat_id)
                await _send_card_reply(ctx, api, msg_id, {
                    "header": {"title": {"tag": "plain_text", "content": "✅ 已进入任务会话"}, "template": "green"},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"**✅ 已进入任务会话**\nIssue #{issue_number} · {title or ''}"}}],
                })
            else:
                await _send_card_reply(ctx, api, msg_id, {
                    "header": {"title": {"tag": "plain_text", "content": "❌ 进入失败"}, "template": "red"},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "**❌ 进入任务会话失败**"}}],
                })
    except Exception as e:
        log.warning(f"[Chat:{ctx.name}] selection error: {e}")
        try:
            await api.reply_text(ctx, msg_id, f"❌ 选择处理失败: {e}")
        except Exception:
            pass


_HELP_CARD = (
    "**🤖 DevOps Bot 命令**\n"
    "/repo — 列出并绑定仓库（回复编号）\n"
    "/chat — 列出当前仓库会话并加入（回复编号）\n"
    "/task — 列出当前仓库任务并进入（回复编号）\n"
    "/new — 新建会话并加入\n"
    "/where — 当前位置\n"
    "/cancel — 取消选择\n"
    "/clean — 清除会话缓存\n"
    "/help — 本帮助\n\n"
    "回复数字选择 · `0` 取消"
)

# 未绑定仓库提示卡片（含 [选择仓库] + [帮助] 按钮）
_NO_REPO_CARD = {
    "header": {"title": {"tag": "plain_text", "content": "⚠️ 未绑定仓库"}, "template": "red"},
    "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**⚠️ 未绑定仓库**\n先使用 `/repo` 选择并绑定仓库。"}},
        {"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "📁 选择仓库"}, "value": {"cmd": "/repo"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"}, "value": {"cmd": "/help"}},
        ]},
    ],
}


async def process_chat_async(ctx: "BotContext", api: "FeishuAsync", parsed: dict):
    """异步消息处理入口 — 通过 ctx + api 实现Bot隔离"""
    try:
        if parsed.get("type") != "message":
            return

        # 根因4修复：bot 被 removed 后不再处理新消息
        # BotManager.remove_bot() 设 ctx._running=False + cancel WS task，
        # 但 wait_task_terminal 阻塞的 handler 协程不受 cancel 影响。
        # 这里在入口处检查 _running，removed 的 bot 直接跳过不占 repo lock。
        # 注意：BotContext._running 默认 False（WS 连接前），只有 WS 连接后才设 True。
        # 测试环境不跑 WS 循环，用 getattr 兜底 + 检查 state 字段区分"未启动"和"已停止"。
        if not getattr(ctx, "_running", True):
            _state = getattr(ctx, "state", None)
            _state_val = getattr(_state, "value", str(_state)) if _state else ""
            # state=STOPPED 表示被 remove_bot 显式停止；None/空表示测试环境未初始化，放行
            if _state_val and "stop" in str(_state_val).lower():
                log.info(f"[Chat:{ctx.name}] bot stopped, skip message")
                return
        text = parsed["text"]
        chat_id = parsed["chat_id"]
        msg_id = parsed["message_id"]
        image_key = parsed.get("image_key", "")
        file_key = parsed.get("file_key", "")

        # 视频/文件消息：下载并理解
        media_description = ""
        if file_key:
            msg_type = parsed.get("message_type", "")
            is_video = msg_type == "media"
            if is_video and msg_id and msg_id.startswith("om_"):
                asyncio.create_task(
                    api.reply_text(ctx, msg_id, "⏳ 正在分析视频，可能需要 30-60 秒...\n支持 MP4/MOV/WebM 等常见格式，文件不超过 50MB；视频时长超过 2 分钟时，将截取前 2 分钟的关键帧进行分析"),
                    name=f"video-ack-{ctx.name}",
                )
            try:
                # 飞书消息资源 API 的 type 参数只支持 image 和 file，
                # 视频消息的 file_key 用 type=file 下载（飞书不支持 type=video）
                file_bytes = await api.download_file(ctx, file_key, message_id=msg_id, file_type="file")
                if file_bytes:
                    if is_video:
                        # 视频理解：ffmpeg 抽帧 + VLM 多帧分析
                        import importlib.util as _ilu
                        _spec = _ilu.spec_from_file_location("image_understanding",
                            os.path.join(os.path.dirname(__file__), "..", "agent", "image_understanding.py"))
                        _mod = _ilu.module_from_spec(_spec)
                        _spec.loader.exec_module(_mod)
                        media_description = _mod.understand_video_from_bytes(file_bytes)
                        log.info(f"[Chat:{ctx.name}] video understood: {len(media_description)} chars desc")
                    else:
                        media_description = f"用户发送了文件（file_key={file_key}），暂不支持文件内容理解。"
                    if not media_description:
                        media_description = "用户发送了一个视频/文件，但未能提取有效信息。"
                    # 缓存描述，后续追问在 TTL 窗口内自动注入
                    _set_pending_image(chat_id, media_description)
                    if not text or text in ("[视频]", "[文件]"):
                        text = "请帮我分析这个视频" if is_video else "请帮我处理这个文件"
                else:
                    log.warning(f"[Chat:{ctx.name}] file download failed: {file_key}")
                    media_description = f"用户发送了一个{'视频' if is_video else '文件'}，但下载失败。"
                    if not text or text in ("[视频]", "[文件]"):
                        text = f"请帮我分析这个{'视频' if is_video else '文件'}"
            except Exception as e:
                log.warning(f"[Chat:{ctx.name}] video/file understanding failed: {e}")
                media_description = f"用户发送了一个{'视频' if is_video else '文件'}，但分析出错: {e}"
                if not text or text in ("[视频]", "[文件]"):
                    text = f"请帮我分析这个{'视频' if is_video else '文件'}"

        # 图片消息：下载图片并理解
        image_description = ""
        if image_key:
            try:
                image_bytes = await api.download_image(ctx, image_key, message_id=msg_id)
                if image_bytes:
                    import importlib.util as _ilu
                    _spec = _ilu.spec_from_file_location("image_understanding",
                        os.path.join(os.path.dirname(__file__), "..", "agent", "image_understanding.py"))
                    _mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)
                    image_description = _mod.understand_image_from_bytes(image_bytes)
                    log.info(f"[Chat:{ctx.name}] image understood: {len(image_description)} chars desc")
                    if not image_description:
                        image_description = "用户发送了一张图片，但未能提取有效信息（可能无文字或图片分析失败）。"
                    # 缓存图片描述，后续追问在 TTL 窗口内自动注入
                    _set_pending_image(chat_id, image_description)
                    # 图片消息无文本，用描述作为"文本"
                    if not text or text == "[图片]":
                        text = "请帮我分析这张图片"
                else:
                    log.warning(f"[Chat:{ctx.name}] image download failed: {image_key}")
                    image_description = "用户发送了一张图片，但下载失败，无法分析。"
                    if not text or text == "[图片]":
                        text = "请帮我分析这张图片"
            except Exception as e:
                log.warning(f"[Chat:{ctx.name}] image understanding failed: {e}")
                image_description = f"用户发送了一张图片，但分析出错: {e}"
                if not text or text == "[图片]":
                    text = "请帮我分析这张图片"
        else:
            # 非图片/视频消息：检查是否有近期媒体上下文，自动注入供追问使用
            # （图片/视频描述都缓存在 _pending_image_desc 中，追问时自动注入）
            if not media_description:
                cached = _get_pending_image(chat_id)
                if cached:
                    image_description = cached
                    log.info(f"[Chat:{ctx.name}] reused cached media desc ({len(image_description)} chars) for follow-up")
        # 合并视频/文件描述到 image_description（统一后续 prompt 注入链路）
        if media_description and not image_description:
            image_description = media_description
        # 快捷菜单/card action 没有真实 message_id，优先用 _reply_id（卡片 open_message_id）
        if not msg_id or not msg_id.startswith("om_"):
            _reply_id = parsed.get("_reply_id", "")
            if _reply_id and _reply_id.startswith("om_"):
                msg_id = _reply_id
                parsed["message_id"] = msg_id
            else:
                sender_open_id = parsed.get("sender_open_id", "")
                if not sender_open_id:
                    log.info(f"[Chat:{ctx.name}] card action without open_id, skip")
                    return
                _first_msg_id = await api.send_text_to_user(ctx, sender_open_id, "⏳")
                if not _first_msg_id:
                    log.warning(f"[Chat:{ctx.name}] failed to get message_id for card action")
                    return
                msg_id = _first_msg_id
                parsed["message_id"] = msg_id
        # account 用于 repo_im_group_binding 隔离（区分不同 Feishu bot app）
        account = ctx.app_id

        log.info(f"[Chat:{ctx.name}] message: {text[:100]} (chat={chat_id[:20]})")

        # p2p 私聊身份绑定：仅私聊场景处理。首次消息发确认询问，用户确认后才写入映射。
        # 群聊不处理（可能是别人与 bot 对话）；已有映射不覆盖（以用户确认为准）。
        sender_open_id = parsed.get("sender_open_id", "")
        chat_type = parsed.get("chat_type", "")
        if sender_open_id and chat_type == "p2p":
            try:
                from src.user import db as user_db
                from src.session_manager import update_unified_user_info, get_feishu_open_id
                uid = get_user_id_by_app_id(ctx.app_id)
                if uid:
                    user_row = user_db.get_user(uid)
                    username = (user_row or {}).get("username", "") if user_row else ""
                    if username and user_row:
                        acc = user_db.get_user_account(username) or {}
                        nickname = acc.get("nickname", "") or ""
                        existing = get_feishu_open_id(username, app_id=ctx.app_id)
                        if existing:
                            # 已绑定，无需处理
                            pass
                        else:
                            import time as _time
                            now = _time.time()
                            key = f"{ctx.app_id}:{username}"
                            # 清理过期 pending
                            if key in _pending_binds and _pending_binds[key]["expires"] < now:
                                _pending_binds.pop(key, None)
                            pending = _pending_binds.get(key)
                            if pending:
                                # 有待确认项：判断用户回复是确认还是否认
                                if _CONFIRM_RE.match(text.strip()):
                                    bind_open_id = pending.get("open_id") or sender_open_id
                                    # 优先以 open_id 取发送者飞书昵称（open_id 与 feishu_name 天然匹配，
                                    # feishu-app-scoped-openid-fix.md §2.4）；拿不到则留空由 sync 兜底
                                    feishu_name = ""
                                    try:
                                        _n = await api.get_user_name(
                                            ctx, bind_open_id, user_id_type="open_id")
                                        if _n and isinstance(_n, str):
                                            feishu_name = _n
                                    except Exception:
                                        pass
                                    update_unified_user_info(username, bind_open_id, feishu_name=feishu_name,
                                                             gitcode_nickname=nickname, app_id=ctx.app_id)
                                    _pending_binds.pop(key, None)
                                    await api.reply_text(ctx, msg_id, f"✅ 已确认，你的 GitCode 账号「{username}」已绑定当前飞书账号。")
                                    log.info(f"Chat: confirmed feishu mapping for {username} (app={ctx.app_id[:20]})")
                                    return
                                elif _DENY_RE.match(text.strip()):
                                    _pending_binds.pop(key, None)
                                    await api.reply_text(ctx, msg_id, "好的，本次不绑定。如需绑定请再次给我发任意消息。")
                                    return
                                else:
                                    # 既不是确认也不是否认，重新提示
                                    await api.reply_text(ctx, msg_id, f"请问你的 GitCode 昵称是否是「{nickname or username}」？请回复「是」或「不是」。")
                                    return
                            else:
                                # 首次：发确认询问，不直接绑定
                                _pending_binds[key] = {
                                    "open_id": sender_open_id,
                                    "gitcode_nickname": nickname,
                                    "expires": now + _PENDING_TTL,
                                }
                                await api.reply_text(ctx, msg_id, f"🤝 检测到你正在与我私聊。为绑定你的 GitCode 账号，请确认：\n你的 GitCode 昵称是否是「{nickname or username}」？\n回复「是」确认绑定，回复「不是」则取消。")
                                return
            except Exception as e:
                log.warning(f"Chat: auto-bind feishu mapping failed: {e}")

        if detect_clean_intent(text):
            result = await process_command_async(ctx, api, "/clean", chat_id, parsed.get("sender_id", ""), db, account, msg_id=msg_id, sender_open_id=parsed.get("sender_open_id", ""))
            if result:
                await api.reply_text(ctx, msg_id, result)
            return

        if text.startswith("/"):
            result = await process_command_async(ctx, api, text, chat_id, parsed.get("sender_id", ""), db, account, msg_id=msg_id, sender_open_id=parsed.get("sender_open_id", ""))
            if result:
                await api.reply_text(ctx, msg_id, result)
            return

        # 卡片命令编号选择：纯数字 + 有未过期 pending → 执行选择（feishu-repo-navigation-commands-design.md §3.2）
        if text.strip().isdigit():
            pending = _get_pending_selection(chat_id)
            if pending:
                await _handle_selection(ctx, api, chat_id, msg_id, text.strip(), pending)
                return

        repo_key = resolve_repo(text, chat_id, parsed.get("sender_id", ""), db, account)
        if not repo_key:
            # 未绑定仓库：自动触发 /repo 卡片（候选=该用户名下已托管仓库，当前 bot 属主）。
            # 修复 2026-08-17 误用全局 meta.ws_feishu_app_id 把 Jason7602 的仓库发给其它用户
            #（feishu-web-chat-unified-design.md §10）。
            uid = get_user_id_by_app_id(account) or 0
            try:
                if uid:
                    if await _send_repo_card(ctx, api, chat_id, msg_id, uid, account, 1):
                        return
                    reply_parts = [
                        "当前未绑定任何仓库至本聊天。",
                        "你名下还没有已托管的仓库。",
                        "请先在网页端托管仓库，然后发送 `/repo` 选择绑定。",
                    ]
                    await api.reply_text(ctx, msg_id, "\n".join(reply_parts))
                    return
                c = db.cursor()
                c.execute("SELECT DISTINCT repo_key, repository, source FROM chat_agent_session ORDER BY last_active_at DESC LIMIT 10")
                rows = c.fetchall()
                if rows:
                    reply_parts = ["当前未绑定任何仓库至本聊天。", "已有处理的仓库："]
                    for r in rows:
                        reply_parts.append(f"  📦 {r[1]}")
                    reply_parts.append("")
                    reply_parts.append("绑定方式（任选一种）：")
                    reply_parts.append(f'  "绑定到 {rows[0][1]}"')
                    reply_parts.append(f'  "使用 {rows[0][1]}"')
                    reply_parts.append("  /repo")
                else:
                    reply_parts = ["当前未绑定任何仓库至本聊天。", "尚无任何仓库记录。请发送 /repo 绑定仓库。"]
                await api.reply_text(ctx, msg_id, "\n".join(reply_parts))
            except Exception as e:
                log.warning(f"Feishu: resolve user repos error: {e}")
                await api.reply_text(ctx, msg_id, "请发送 `/repo` 选择并绑定仓库。")
            return

        bind_repo = detect_bind_intent(text)
        if bind_repo:
            from src.repo_key import to_repo_key
            repo_key = to_repo_key(bind_repo)
            try:
                c = db.cursor()
                c.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES (?,?,?,?,?)",
                          (chat_id, repo_key, "gitcode", parsed.get("sender_id", ""), account))
                # 绑定即重置会话位置（修复 2026-08-16 绑定/会话发散 bug）
                c.execute("UPDATE chat_conversations SET im_channel_id='' WHERE im_channel_id=?", (chat_id,))
                db.commit()
            except Exception as e:
                log.warning(f"Feishu: auto-bind failed: {e}")
            _invalidate_repo_bind_cache(chat_id, account)
            await _send_bind_success_card(ctx, api, msg_id, repo_key)
            return

        from src.repo_key import to_repo_key, from_repo_key, parse_repo_key
        repo_key = to_repo_key(repo_key)
        _provider, _owner, _repo = parse_repo_key(repo_key)
        source = _provider
        repository = _repo or from_repo_key(repo_key)

        # 先发 ⏳ 确认消息收到——用户不可接受"没反应"（即使 AI 耗时 20 秒）
        if msg_id and msg_id.startswith("om_"):
            asyncio.create_task(
                api.reply_text(ctx, msg_id, "⏳"),
                name=f"ack-{ctx.name}",
            )

        sess, history, oc_sid, cc_sid, is_new = get_or_create_session(repo_key, source, repository)

        # 同仓库串行锁——仅 direct 模式（本机 opencode 子进程）需要，避免 cwd/session 冲突。
        # 远端入队模式任务在 worker 上执行，不共享本机资源，无需阻塞同仓库后续消息。
        # （2026-08-16 死锁修复的 asyncio.Lock 保留，但范围缩小到 direct 分支）
        lock = _acquire_repo_lock(repo_key)
        await _process_chat_inner_async(ctx, api, parsed, text, repo_key, repository,
                                        msg_id, chat_id, sess, history, oc_sid, cc_sid, is_new,
                                        bind_account=account, exec_account=repo_key,
                                        image_description=image_description,
                                        repo_lock=lock)

    except Exception as e:
        import traceback
        log.error(f"[Chat:{ctx.name}] process error: {e}")
        traceback.print_exc()
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错，请稍后重试")
        except:
            pass


def _resolve_oc_sid(conv: dict | None, repo_oc_sid: str) -> str:
    """解析 opencode 会话 id：优先 conversation.runtime_session_id（与 web 同源），空则返回空让 opencode 新建。

    feishu-chat-reuse-web-session.md §3.1：飞书与 web 对同一 conversation 复用相同 session，
    避免 cwd（conv 级）与 session.directory 不匹配导致历史加载失败。
    首次（conv 无 runtime_session_id）不兜底 repo 级——repo 级会话是旧目录，与 conv 级 cwd 不一致，
    应传空让 opencode 在当前 cwd 下新建会话，随后写回 conv。
    """
    if conv:
        conv_sid = (conv.get("runtime_session_id") or "").strip()
        if conv_sid:
            return conv_sid
    return ""


async def _process_chat_inner_async(ctx: "BotContext", api: "FeishuAsync",
                                     parsed, text, repo_key, repository,
                                     msg_id, chat_id, sess, history, oc_sid, cc_sid, is_new,
                                     bind_account="", exec_account="", conv_id="", conv=None,
                                     image_description="", repo_lock=None):
    """异步版内部处理逻辑

    bind_account: 用于 repo_im_group_binding 隔离（Chat Bot 场景 = bot app_id）
    exec_account: 用于 workspace history / agent 执行（= 仓库全名 owner/repo）
    conv_id/conv: 当前 conversation（与 web 同源，用其 runtime_session_id 复用 opencode 会话）
    """
    try:
        # 获取当前 conversation（飞书 与 web 共源；用于复制 runtime_session_id 复用 opencode 会话）
        if not conv:
            try:
                from src.session_manager import get_conversation_by_im
                _uid = 0
                try:
                    _uid = get_user_id_by_app_id(ctx.app_id) or 0
                except Exception:
                    pass
                conv = get_conversation_by_im(chat_id, _uid) if _uid else None
            except Exception:
                conv = None
        if not conv_id and conv:
            conv_id = conv.get("id", "")

        sender_id = parsed.get('sender_id', '')
        open_id = parsed.get('sender_open_id', '')
        user_info = "## 用户基本信息\n"
        if open_id:
            try:
                gitcode_login = None
                nick_name = ""
                gitcode_nickname = ""
                c = db.cursor()
                c.execute("SELECT gitcode_user, feishu_name, gitcode_nickname FROM unified_user_mapping WHERE feishu_open_id=?",
                          (open_id,))
                row = c.fetchone()
                if row:
                    gitcode_login = row[0]
                    nick_name = row[1] or ""
                    gitcode_nickname = row[2] or ""
                # 飞书昵称 DB 为空 → 用 open_id 调飞书 API 兜底
                if not nick_name and open_id:
                    try:
                        nick_name = await api.get_user_name(ctx, open_id, user_id_type="open_id") or ""
                    except Exception:
                        pass
                if gitcode_login:
                    user_info += f"  GitCode login: {gitcode_login}\n"
                    if gitcode_nickname and gitcode_nickname != gitcode_login:
                        user_info += f"  GitCode 昵称: {gitcode_nickname}\n"
                    if nick_name and nick_name != gitcode_login:
                        user_info += f"  飞书 昵称：{nick_name}\n"
            except Exception as e:
                log.warning(f"Failed to resolve GitCode login: {e}")

        skills = ["chat"]

        # 获取 IM 用户 token（chat_conversations 持久化，幂等创建）
        # im_channel_id 为"当前飞书接入指针"：get_conversation_by_im 返回当前绑定的对话
        #（可被 `/加入` 切换），飞书用户消息写入该对话的 chat_messages，Web 侧可见
        #（feishu-web-chat-unified-design.md §3.2）。
        user_token = ""
        conv_id = ""
        try:
            from src.user.db import get_user_gitcode_token
            from src.session_manager import create_conversation, get_conversation_by_im, update_conversation, add_message
            user_id = get_user_id_by_app_id(ctx.app_id) or 0
            if user_id > 0:
                user_token = get_user_gitcode_token(user_id) or ""
                # 用户 token 为空 → 仓库 api token 兜底（get_gitcode_token：repo_gitcode_token
                # → managed_repos.bot_gitcode_token → 全局 bot token），避免 chat agent 调 GitCode API 401
                if not user_token:
                    from src.user.db import get_gitcode_token
                    user_token = get_gitcode_token(repo_key=repo_key) or ""
                conv = get_conversation_by_im(chat_id, user_id)
                if not conv:
                    cid = create_conversation(user_id, repo_key, im_channel_id=chat_id, user_gitcode_token=user_token)
                    conv_id = cid
                    log.info(f"[Chat:{ctx.name}] created chat_conversation {cid} for user={user_id} chat={chat_id[:20]}")
                else:
                    conv_id = conv["id"]
                    if user_token and not conv.get("user_gitcode_token"):
                        update_conversation(conv["id"], user_gitcode_token=user_token)
                if conv_id:
                    # 图片消息：把图片描述追加到用户消息里存入 DB，
                    # 这样追问时 get_messages 历史里自然包含图片描述，不依赖内存缓存。
                    if image_description:
                        add_message(conv_id, "user", text + "\n\n" + image_description)
                    else:
                        add_message(conv_id, "user", text)
                    # 任务会话收到真实用户消息 → active→processing（task-status-processing-trigger-fix.md §3.1）
                    from src.session_manager import mark_task_processing_by_conversation
                    mark_task_processing_by_conversation(conv_id)
        except Exception as e:
            log.warning(f"[Chat:{ctx.name}] resolve user gitcode token failed: {e}")

        # 用户 chat 配置的引擎：IM Chat 默认本机 direct；配置个人 worker 引擎则入队给该 worker
        chat_runtime_id = 0
        try:
            from src.session_manager import get_chat_config as _chat_cfg
            _uid = get_user_id_by_app_id(ctx.app_id) or 0
            if _uid > 0:
                chat_runtime_id = (_chat_cfg(_uid) or {}).get("runtime_id") or 0
        except Exception:
            chat_runtime_id = 0

        # 引擎类型感知的 has_valid_session：提前解析引擎类型，
        # codearts 忽略 session_id；只有 opencode/hermes 才真正使用 session 续接。
        from src.runtime.engine_select import resolve_local_engine, engine_name
        _rid = chat_runtime_id or (resolve_local_engine() or 0)
        _engine_name = engine_name(_rid) if _rid else ""
        _conv_sid = (conv or {}).get("runtime_session_id", "").strip()
        has_valid_session = bool(_conv_sid) and _engine_name in ("opencode", "hermes")

        prompt = _build_feishu_chat_prompt(text, repo_key, history, skills, exec_account,
                                           chat_runtime_id, conv_id, user_info=user_info,
                                           has_valid_session=has_valid_session)
        # 图片理解描述注入 prompt
        if image_description:
            prompt = prompt + "\n\n" + image_description
            log.info(f"Chat prompt with image desc injected ({len(prompt)} chars total)")
        log.info(f"Chat prompt ({len(prompt)} chars, repo={repository}, engine={_engine_name}, has_session={has_valid_session})")

        # 与 Web Chat 严格一致：同一 conv_id 同一目录 WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}
        # （web-chat-no-response-cwd-fix.md §4；去掉原 feishu/ 段与 sess 尾巴）
        from src.repo_key import parse_repo_key
        _, _owner, _repo = parse_repo_key(repo_key or "")
        wsd = os.path.join(WORKSPACE_DIR, _owner or "__none__", _repo or "__none__",
                           "chat", str(user_id or 0), conv_id or "")
        os.makedirs(wsd, exist_ok=True)

        await api.add_reaction(ctx, msg_id, "OK")

        final_text = ""
        exit_code = 0
        backend = ""
        # 引擎类型已在上方提前解析（_rid, _engine_name）；此处复用
        if not _rid:
            log.warning(f"[Chat:{ctx.name}] no available engine for user, task skipped")
            final_text = "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎"
            exit_code = -1
        else:
            backend = _engine_name
            oc_sid = {"opencode": oc_sid, "cc_sid": cc_sid}.get(backend, "")
            # 优先 conversation 会话（与 web 同源）；空则回退 repo 级
            oc_sid = _resolve_oc_sid(conv, oc_sid)
            new_oc_session_id = oc_sid
            kind = runtime_execution_kind(_rid)
            # queue_remote 强制模式下兜底：配置引擎离线/本机直连无 claim 方 → 解析在线远端引擎
            #（与 web chat.py §353-364 同源；handler_async 缺此兜底致飞书消息卡 queued 黑洞）
            if kind != EXEC_MODE_DIRECT:
                from src.runtime.service import runtime_credential_id, _remote_runtime_ready
                _need_fallback = False
                _cid = runtime_credential_id(_rid)
                if (_cid or 0) == 0:
                    # 本机直连行（credential_id=0）在 queue_remote 下无 claim 方 = 黑洞
                    _need_fallback = True
                elif not _remote_runtime_ready(_rid):
                    # 远端引擎离线/心跳超时/凭据吊销 → 无人 claim = 黑洞
                    _need_fallback = True
                if _need_fallback:
                    _orig_rid = _rid
                    from src.session_manager import _resolve_chat_runtime_id
                    _rid2 = _resolve_chat_runtime_id(backend) or 0
                    # queue_remote 模式下必须找远端引擎（credential_id>0），本机直连行无 claim 方
                    if (_rid2 and (runtime_credential_id(_rid2) or 0) > 0
                            and _remote_runtime_ready(_rid2)):
                        _rid = _rid2
                        kind = runtime_execution_kind(_rid)
                        log.info(f"[Chat:{ctx.name}] engine fallback: {_orig_rid}->{_rid} (original offline/local-direct)")
                    else:
                        # 按名找的引擎仍为本机直连 → 扫描在线远端 worker 引擎兜底
                        from src.session_manager import db as _sdb, db_lock as _slock
                        _found = 0
                        with _slock:
                            _c = _sdb.cursor()
                            _c.execute(
                                "SELECT id FROM agent_runtimes "
                                "WHERE credential_id > 0 AND status = 'online' AND name = ? "
                                "ORDER BY id LIMIT 1", (backend,))
                            _row = _c.fetchone()
                            if _row:
                                _found = _row[0]
                        if _found and _remote_runtime_ready(_found):
                            _rid = _found
                            kind = runtime_execution_kind(_rid)
                            log.info(f"[Chat:{ctx.name}] engine fallback (scan): {_orig_rid}->{_rid} (remote {backend})")
                        else:
                            log.warning(f"[Chat:{ctx.name}] engine fallback failed: no online remote engine for backend={backend}")
            if kind != EXEC_MODE_DIRECT:
                # 入队 + 轮询：任务由远端 RuntimeAgent claim 执行（见 multi-runtime-instance-design）
                import json as _json
                from src.runtime.service import enqueue_task, wait_task_terminal
                runtime_id = _rid
                if runtime_id:
                    uid = 0
                    try:
                        uid = get_user_id_by_app_id(ctx.app_id) or 0
                    except Exception:
                        pass
                    tid = enqueue_task(
                        agent_session_id=sess,
                        runtime_id=runtime_id,
                        event_seq=None, issue_id="",
                        repo_key=repo_key, agent_type="chat", skill="chat",
                        prompt=prompt, cwd=wsd,
                        runtime_session_id=oc_sid,
                        event_type="chat",
                        account=exec_account, user_id=uid,
                        repo_gitcode_token=user_token or "",
                        env_extra={"IM_CHAT_ID": chat_id},
                        pool_agent_type="chat",
                    )
                    log.info(f"[Chat:{ctx.name}] enqueued task={tid} mode={kind}")
                    task = await wait_task_terminal(tid)
                    final_text = task.get("result_text") or ""
                    exit_code = int(task.get("exit_code") or 0)
                    try:
                        usage = _json.loads(task.get("usage_json") or "{}") or {}
                        new_oc_session_id = usage.get("session_id") or oc_sid
                    except Exception:
                        new_oc_session_id = oc_sid
                    log.info(f"[Chat:{ctx.name}] task done: exit={exit_code}, text_len={len(final_text)}")
                else:
                    log.warning(f"[Chat:{ctx.name}] no runtime for backend={backend}, task skipped")
                    final_text = ""
                    exit_code = -1
            else:
                # direct 模式（本机 opencode 子进程）——持有 repo_lock 防止 cwd/session 冲突
                async def _direct_exec():
                    nonlocal final_text, exit_code, new_oc_session_id
                    async for event_type, data in consume_stream_async(prompt, wsd, oc_sid, "chat",
                                                                       backend_name=backend, account=exec_account,
                                                                       user_token=user_token, agent_name="Chat Agent",
                                                                       agent_label=f"[Agent:chat|repo:{repository}]"):
                        if event_type == "done":
                            d = data
                            final_text = d["text"]
                            exit_code = d.get("exit_code", 0)
                            if exit_code == 0 and d.get("oc_session_id"):
                                new_oc_session_id = d["oc_session_id"]
                            log.info(f"  ✅ Done: exit={exit_code}, text_len={len(final_text)}")
                if repo_lock:
                    async with repo_lock:
                        await _direct_exec()
                else:
                    await _direct_exec()

        record = {
            "type": "chat", "time": datetime.utcnow().isoformat(),
            "success": exit_code == 0, "exit_code": exit_code,
            "actor": parsed.get("sender_id", ""),
            "account": exec_account,
            "backend": backend,
            "skill": "chat",
            "stdout": final_text[:2000],
            "oc_session_id": new_oc_session_id,
        }
        history.append(record)
        # 已废弃：session_history 文件不再注入 Prompt，无需写入磁盘
        # from src.session_manager import dump_history_to_file
        # dump_history_to_file(repo_key, history, exec_account)
        update_session(sess, history, "idle", new_oc_session_id, backend)
        # 写回 conversation.runtime_session_id（与 web 同源，保证飞书/web 复用同一 opencode 会话）
        if conv_id and new_oc_session_id:
            try:
                update_conversation(conv_id, runtime_session_id=new_oc_session_id)
            except Exception as _we:
                log.warning(f"[Chat:{ctx.name}] write conv runtime_session_id failed: {_we}")

        output = final_text.strip()
        if output == "SKIP":
            log.info("Agent decided to SKIP (no reply needed)")
        elif output:
            await api.send_card_to_chat(ctx, chat_id, output[:2000], fallback_open_id=open_id)
            # 共享消息存储：agent 答复回写当前 conversation，Web Chat Agent 页可见
            #（feishu-web-chat-unified-design.md §3.2）
            if conv_id:
                try:
                    from src.session_manager import add_message
                    add_message(conv_id, "agent", output)
                except Exception as _we:
                    log.warning(f"[Chat:{ctx.name}] write agent message failed: {_we}")

    except Exception as e:
        import traceback
        log.error(f"[Chat:{ctx.name}] process inner error: {e}")
        traceback.print_exc()
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错，请稍后重试")
        except:
            pass