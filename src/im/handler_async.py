"""异步飞书消息处理 — 支持 BotContext 隔离的多Bot消息分发

核心改造:
  - process_chat_async: 接收 (BotContext, parsed) 替代全局函数调用
  - 所有飞书API调用通过 ctx + api 实例方法，按Bot身份隔离
  - 纯逻辑（bind/clean 意图、repo 解析、用户归属）内联本模块，无同步 HTTP；
    IO 型命令（/bind、/refresh 的群成员↔协作者映射同步）走 await api 异步方法。
"""
import re, os, asyncio, time
from datetime import datetime
from typing import TYPE_CHECKING

from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import get_or_create_session, update_session, db
from src.prompt_builder import build_chat_prompt, get_skill_capabilities
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


def _current_bound_repo(chat_id: str, account: str, db) -> str:
    """当前飞书聊天绑定的仓库 repo_key（repo_im_group_binding）"""
    try:
        c = db.cursor()
        c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
        row = c.fetchone()
        return row[0] if row else ""
    except Exception:
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


async def _send_repo_card(ctx: "BotContext", api: "FeishuAsync", chat_id: str, msg_id: str,
                          uid: int, account: str, page: int = 1) -> bool:
    """生成并发送 `/repo` 卡片（候选=该用户名下**已托管**仓库，未托管不显示），设置 pending。

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
    await api.reply_card(ctx, msg_id, _candidate_card("📁", f"已托管仓库列表（共 {len(repos)} 个）", items, page, total_pages, "/repo"))
    return True


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
    # 3. 查询群绑定表（repo_key 带前缀）
    try:
        c = db.cursor()
        c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
        row = c.fetchone()
        if row:
            return row[0]
    except Exception as e:
        log.warning(f"Feishu: resolve_repo db error: {e}")
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
                                account: str = "", msg_id: str = "") -> str:
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
            await api.reply_card(ctx, msg_id, "**⚠️ 未绑定仓库**\n先使用 `/repo` 选择并绑定仓库。")
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
        await api.reply_card(ctx, msg_id, _candidate_card("📄", f"{bound} 会话列表", items, page, total_pages, "/chat"))
        return ""

    if cmd == "/task":
        if not uid:
            return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
        bound = _current_bound_repo(chat_id, account, db)
        if not bound:
            await api.reply_card(ctx, msg_id, "**⚠️ 未绑定仓库**\n先使用 `/repo` 选择并绑定仓库。")
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
        await api.reply_card(ctx, msg_id, _candidate_card("📋", f"{bound} 任务列表", items, page, total_pages, "/task"))
        return ""

    if cmd == "/new":
        if not uid:
            return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
        bound = _current_bound_repo(chat_id, account, db)
        if not bound:
            await api.reply_card(ctx, msg_id, "**⚠️ 未绑定仓库**\n先使用 `/repo` 选择并绑定仓库。")
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
        await api.reply_card(ctx, msg_id, _HELP_CARD)
        return ""

    return ""


def _build_feishu_chat_prompt(text: str, repo_key: str, history, skills, exec_account: str,
                              chat_runtime_id: int, conv_id: str, user_info: str = "") -> str:
    """按当前加入会话的 conv_type 分支构建 Feishu chat prompt（feishu-web-chat-unified-design.md §11）。

    task 会话 → build_task_chat_prompt（会话消息 get_messages 作历史，与 web chat 一致）；
    否则 general → build_chat_prompt（事件 history）。
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
        body = build_task_chat_prompt(text, from_repo_key(repo_key), conv_history, conv_obj, account=exec_account)
    else:
        body = build_chat_prompt(text, repo_key, history, skills, exec_account, runtime_id=chat_runtime_id)
    return (user_info + "\n\n" + body) if user_info else body


async def _handle_selection(ctx: "BotContext", api: "FeishuAsync", chat_id: str, msg_id: str,
                            num: str, pending: dict):
    """卡片命令编号选择执行（feishu-repo-navigation-commands-design.md §3.2）。"""
    try:
        if num == "0":
            _clear_pending_selection(chat_id)
            await api.reply_card(ctx, msg_id, "**✅ 已取消选择**")
            return
        action = pending.get("action")
        uid = get_user_id_by_app_id(ctx.app_id) or 0
        items = pending.get("items") or []
        page = int(pending.get("page") or 1)
        page_size = int(pending.get("page_size") or _PENDING_SELECT_PAGE_SIZE)
        idx = (page - 1) * page_size + (int(num) - 1)
        if idx < 0 or idx >= len(items):
            await api.reply_card(ctx, msg_id, f"**⚠️ 无效编号 {num}**\n回复 `0` 取消。")
            return
        if action == "repo":
            repo = items[idx]
            repo_key = repo["repo_key"] if isinstance(repo, dict) else repo
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES (?,?,?,?,?)",
                      (chat_id, repo_key, "gitcode", str(uid), ctx.app_id))
            # 绑定即重置会话位置：清掉旧会话指针，下次消息在新仓库下新建会话
            #（修复 2026-08-16 "群绑定仓库 ≠ 会话所属仓库" 发散 bug）
            c.execute("UPDATE chat_conversations SET im_channel_id='' WHERE im_channel_id=?", (chat_id,))
            db.commit()
            _clear_pending_selection(chat_id)
            await api.reply_card(ctx, msg_id, f"**✅ 已绑定 `{repo_key}`**\n使用 `/chat` 选择会话，或直接开始提问。")
        elif action == "chat":
            cv = items[idx]
            from src.session_manager import join_feishu_chat
            if join_feishu_chat(chat_id, cv["id"], uid):
                tag = "任务" if cv.get("conv_type") == "task" else "一般"
                _clear_pending_selection(chat_id)
                await api.reply_card(ctx, msg_id, f"**✅ 已加入对话**\n[{tag}] {cv.get('title') or '新对话'}（{cv.get('repo_key') or ''}）")
            else:
                await api.reply_card(ctx, msg_id, "**❌ 加入失败**")
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
                await api.reply_card(ctx, msg_id, f"**✅ 已进入任务会话**\nIssue #{issue_number} · {title or ''}")
            else:
                await api.reply_card(ctx, msg_id, "**❌ 进入任务会话失败**")
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


async def process_chat_async(ctx: "BotContext", api: "FeishuAsync", parsed: dict):
    """异步消息处理入口 — 通过 ctx + api 实现Bot隔离"""
    try:
        if parsed.get("type") != "message":
            return
        text = parsed["text"]
        chat_id = parsed["chat_id"]
        msg_id = parsed["message_id"]
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
                        existing = get_feishu_open_id(username)
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

        capability_patterns = [
            r'(?:你|您)(?:有|会|能|可以)?\s*什么\s*(?:能力|功能|本领|技能|用处|作用)',
            r'(?:你|您)(?:会|能|可以)\s*(?:做|干|处理|干嘛)\s*(?:什么|哪些)',
            r'what\s*(?:can|are)\s*(?:you|your)\s*(?:do|capabilities|skills|features)',
            r'(?:介绍|说说|告诉)(?:一下|下)?\s*(?:你|你自己)',
        ]
        if any(re.search(p, text, re.IGNORECASE) for p in capability_patterns):
            await api.reply_text(ctx, msg_id, get_skill_capabilities())
            return

        if detect_clean_intent(text):
            result = await process_command_async(ctx, api, "/clean", chat_id, parsed.get("sender_id", ""), db, account, msg_id=msg_id)
            if result:
                await api.reply_text(ctx, msg_id, result)
            return

        if text.startswith("/"):
            result = await process_command_async(ctx, api, text, chat_id, parsed.get("sender_id", ""), db, account, msg_id=msg_id)
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
            await api.reply_card(ctx, msg_id, f"**✅ 已绑定 `{repo_key}`**\n使用 `/chat` 选择会话，或直接开始提问。")
            return

        from src.repo_key import to_repo_key, from_repo_key, parse_repo_key
        repo_key = to_repo_key(repo_key)
        _provider, _owner, _repo = parse_repo_key(repo_key)
        source = _provider
        repository = _repo or from_repo_key(repo_key)

        sess, history, oc_sid, cc_sid, is_new = get_or_create_session(repo_key, source, repository)

        # 同仓库串行（asyncio.Lock，async with 不阻塞事件循环）。
        # 旧实现同步 threading.Lock.acquire() 在事件循环里阻塞，同仓库并发消息会死锁整机
        #（2026-08-16 事故，见 docs/new-version2/feishu-chat-event-loop-deadlock-fix.md）。
        lock = _acquire_repo_lock(repo_key)
        async with lock:
            await _process_chat_inner_async(ctx, api, parsed, text, repo_key, repository,
                                            msg_id, chat_id, sess, history, oc_sid, cc_sid, is_new,
                                            bind_account=account, exec_account=repo_key)

    except Exception as e:
        import traceback
        log.error(f"[Chat:{ctx.name}] process error: {e}")
        traceback.print_exc()
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错，请稍后重试")
        except:
            pass


async def _process_chat_inner_async(ctx: "BotContext", api: "FeishuAsync",
                                     parsed, text, repo_key, repository,
                                     msg_id, chat_id, sess, history, oc_sid, cc_sid, is_new,
                                     bind_account="", exec_account=""):
    """异步版内部处理逻辑

    bind_account: 用于 repo_im_group_binding 隔离（Chat Bot 场景 = bot app_id）
    exec_account: 用于 workspace history / agent 执行（= 仓库全名 owner/repo）
    """
    try:
        sender_id = parsed.get('sender_id', '')
        sender_name = await api.get_user_name(ctx, sender_id) if sender_id else ""
        open_id = parsed.get('sender_open_id', '')
        user_info = f"User: {sender_name or sender_id} (ID: {sender_id})"
        if open_id:
            user_info += f"\nFeishu open_id: {open_id}\nTo @mention user, use: <at id={open_id}></at>"
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
                if gitcode_login:
                    user_info += f"\nGitCode login: {gitcode_login}"
                    if gitcode_nickname and gitcode_nickname != gitcode_login:
                        user_info += f"\nGitCode 昵称: {gitcode_nickname}"
                    if nick_name and nick_name != gitcode_login:
                        user_info += f"\nGitCode nick (use this in cards): {nick_name}"
                    from src.repo_key import from_repo_key
                    repository_part = from_repo_key(repo_key).split("/", 1)
                    if len(repository_part) == 2:
                        user_info += f"\nTo check pending features: GET /api/v5/repos/{repository_part[0]}/{repository_part[1]}/issues?state=open&assignee={gitcode_login}&labels=feature"
            except Exception as e:
                log.warning(f"Failed to resolve GitCode login for {sender_name}: {e}")

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

        prompt = _build_feishu_chat_prompt(text, repo_key, history, skills, exec_account,
                                           chat_runtime_id, conv_id, user_info=user_info)
        log.info(f"Chat prompt ({len(prompt)} chars): {prompt[:2000]}")

        wsd = os.path.join(WORKSPACE_DIR, "feishu", repository.replace("/", "_"), sess.split("-")[-1])
        os.makedirs(wsd, exist_ok=True)

        await api.add_reaction(ctx, msg_id, "OK")

        # 占位消息（飞书 Chat 进度反馈，见 docs/new-version2/feishu-chat-progress-feedback-design.md）
        import time as _time
        _started = _time.time()
        reply_id = ""
        try:
            reply_id = await api.reply_text(ctx, msg_id, "⏳ 正在处理...")
        except Exception:
            pass

        final_text = ""
        exit_code = 0
        backend = ""
        new_oc_session_id = ""
        # engine-selection-design.md v2：无引擎（本机直连/远端均未配置）→ 明确不可用，不隐式 opencode
        from src.runtime.engine_select import resolve_local_engine, engine_name
        _rid = chat_runtime_id or (resolve_local_engine() or 0)
        if not _rid:
            log.warning(f"[Chat:{ctx.name}] no available engine for user, task skipped")
            final_text = "⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎"
            exit_code = -1
        else:
            backend = engine_name(_rid) or ""
            oc_sid = {"opencode": oc_sid, "cc_sid": cc_sid}.get(backend, "")
            new_oc_session_id = oc_sid
            kind = runtime_execution_kind(_rid)
            if kind != EXEC_MODE_DIRECT:
                # 入队 + 心跳轮询（统一心跳，direct/queue 同逻辑）
                import json as _json
                from src.runtime.service import enqueue_task, get_task
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
                    )
                    log.info(f"[Chat:{ctx.name}] enqueued task={tid} mode={kind}")
                    # 自定义轮询（代替 wait_task_terminal，含心跳 patch）
                    _deadline = _time.time() + 1800.0
                    while True:
                        await asyncio.sleep(3)
                        task = get_task(tid)
                        if task and task.get("status") in ("completed", "failed", "cancelled"):
                            final_text = task.get("result_text") or ""
                            exit_code = int(task.get("exit_code") or 0)
                            try:
                                usage = _json.loads(task.get("usage_json") or "{}") or {}
                                new_oc_session_id = usage.get("session_id") or oc_sid
                            except Exception:
                                new_oc_session_id = oc_sid
                            log.info(f"[Chat:{ctx.name}] task done: exit={exit_code}, text_len={len(final_text)}")
                            break
                        if _time.time() > _deadline:
                            final_text = "❌ 执行超时，请稍后重试"
                            exit_code = -1
                            log.warning(f"[Chat:{ctx.name}] task timeout: {tid}")
                            break
                        # 心跳更新占位卡片
                        _elapsed = int(_time.time() - _started)
                        _st = (task or {}).get("status", "")
                        _status_text = {"queued": "任务已排队", "dispatched": "已分配"}.get(_st, "执行中")
                        _heartbeat = f"⏳ {_status_text}... ({_elapsed}s)"
                        if reply_id:
                            try:
                                await api.update_message(ctx, reply_id, _heartbeat)
                            except Exception:
                                pass
                else:
                    log.warning(f"[Chat:{ctx.name}] no runtime for backend={backend}, task skipped")
                    final_text = ""
                    exit_code = -1
            else:
                # direct 模式 + 并行心跳
                _direct_done = False
                async def _heartbeat_direct():
                    while not _direct_done:
                        await asyncio.sleep(3)
                        _elapsed = int(_time.time() - _started)
                        if reply_id:
                            try:
                                await api.update_message(ctx, reply_id, f"⏳ 执行中... ({_elapsed}s)")
                            except Exception:
                                pass
                _hb = asyncio.create_task(_heartbeat_direct())
                try:
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
                    _direct_done = True
                    _hb.cancel()
                except Exception:
                    _direct_done = True
                    _hb.cancel()
                    raise

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
        from src.session_manager import dump_history_to_file
        dump_history_to_file(repo_key, history, exec_account)
        update_session(sess, history, "idle", new_oc_session_id, backend)

        # 占位卡片替换为最终结果（或错误信息）；失败兜底 reply_card
        output = final_text.strip()
        _elapsed = int(_time.time() - _started)
        _placeholder_replaced = False
        if output == "SKIP":
            log.info("Agent decided to SKIP (no reply needed)")
        elif output:
            if reply_id and _elapsed >= 2:
                try:
                    await api.update_message(ctx, reply_id, output[:2000])
                    _placeholder_replaced = True
                except Exception:
                    pass
            if not _placeholder_replaced:
                await api.reply_card(ctx, msg_id, output[:2000])
            # 共享消息存储：agent 答复回写当前 conversation，Web Chat Agent 页可见
            #（feishu-web-chat-unified-design.md §3.2）
            if conv_id:
                try:
                    from src.session_manager import add_message
                    add_message(conv_id, "agent", output)
                except Exception as _we:
                    log.warning(f"[Chat:{ctx.name}] write agent message failed: {_we}")
        elif exit_code != 0 and output:
            _err = f"❌ 任务执行失败 (exit={exit_code})\n{output[:200]}"
            if reply_id:
                try:
                    await api.update_message(ctx, reply_id, _err[:2000])
                except Exception:
                    pass
        # 无引擎 / 静默失败：占位消息已发，保留占位或由外层 except 兜底

    except Exception as e:
        import traceback
        log.error(f"[Chat:{ctx.name}] process inner error: {e}")
        traceback.print_exc()
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错，请稍后重试")
        except:
            pass