"""异步飞书消息处理 — 支持 BotContext 隔离的多Bot消息分发

核心改造:
  - process_chat_async: 接收 (BotContext, parsed) 替代全局函数调用
  - 所有飞书API调用通过 ctx + api 实例方法，按Bot身份隔离
  - 纯逻辑（bind/clean 意图、repo 解析、用户归属）内联本模块，无同步 HTTP；
    IO 型命令（/bind、/refresh 的群成员↔协作者映射同步）走 await api 异步方法。
"""
import re, os, asyncio
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
                                account: str = "") -> str:
    """异步命令处理。纯 DB 命令保持同步（SQLite 毫秒级）；含网络 IO 的
    /bind、/refresh 走 await api.sync_repo_feishu_mapping，不阻塞事件循环。
    """
    text = text.strip()
    parts = text.split()
    if not parts:
        return ""
    cmd = parts[0].lower()
    if cmd == "/bind" and len(parts) >= 2:
        from src.repo_key import to_repo_key
        repo_key = to_repo_key(parts[1])
        try:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES (?,?,?,?,?)",
                (chat_id, repo_key, "gitcode", sender_id, account)
            )
            db.commit()
            matched = await api.sync_repo_feishu_mapping(ctx, repo_key, chat_id)
            if matched > 0:
                return f"✅ 已绑定 `{repo_key}`，同步了 {matched} 个用户映射"
            return f"✅ 已绑定当前群到 `{repo_key}`"
        except Exception as e:
            return f"❌ 绑定失败: {e}"
    if cmd == "/unbind":
        try:
            c = db.cursor()
            c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
            db.commit()
            return "✅ 已解绑当前群"
        except Exception as e:
            return f"❌ 解绑失败: {e}"
    if cmd == "/status":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key, created_at FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
            row = c.fetchone()
            if row:
                return f"📋 当前绑定: `{row[0]}` (since {row[1]})"
            return "📋 未绑定任何仓库。使用 `/bind owner/repo` 绑定"
        except Exception as e:
            return f"❌ 查询失败: {e}"
    if cmd in ("/对话", "/convs", "/list"):
        # 列出该用户参与仓库下的对话，支持 `/对话 <仓库>` 过滤（feishu-web-chat-unified-design.md §3.3）
        try:
            from src.session_manager import get_user_conversations, db_lock
            uid = get_user_id_by_app_id(account)
            if not uid:
                return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
            from src.user.db import get_user_repos
            repos = get_user_repos(uid)
            if not repos:
                return "📋 你没有已同步的仓库。"
            repo_filter = parts[1] if len(parts) >= 2 else ""
            lines = ["📋 你的对话（输入 `/加入 <conversation_id>` 切换当前飞书对话）："]
            for r in repos:
                if repo_filter and repo_filter not in r["full_name"]:
                    continue
                convs = get_user_conversations(uid, repo_key=r["full_name"], limit=10)
                if not convs:
                    continue
                lines.append(f"\n**{r['full_name']}**")
                for cv in convs:
                    tag = "任务" if cv.get("conv_type") == "task" else "一般"
                    cur = " 👈当前" if cv.get("im_channel_id") == chat_id else ""
                    lines.append(f"  `{cv['id'][:8]}` [{tag}] {cv.get('title') or '新对话'}{cur}")
            if len(lines) == 1:
                return "📋 没有可切换的对话。"
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 查询失败: {e}"
    if cmd == "/加入" and len(parts) >= 2:
        # 切换当前飞书对话：清旧 im_channel_id，新 conversation 写入当前 chat_id
        #（feishu-web-chat-unified-design.md §3.1/§3.3）
        try:
            from src.session_manager import join_feishu_chat, get_conversation, db_lock
            uid = get_user_id_by_app_id(account)
            if not uid:
                return "❌ 未找到关联平台用户（请在 Chat Agent 页配置飞书）。"
            cid = parts[1]
            full = cid
            if len(cid) < 36:
                with db_lock:
                    c = db.cursor()
                    row = c.execute("SELECT id FROM chat_conversations WHERE id LIKE ? AND user_id=? LIMIT 1", (cid + "%", uid)).fetchone()
                    if row:
                        full = row[0]
                    else:
                        return f"❌ 未找到以 `{cid}` 开头的对话（用 `/对话` 查看列表）。"
            conv = get_conversation(full)
            if not conv or conv.get("user_id") != uid:
                return "❌ 对话不存在或无权限。"
            if join_feishu_chat(chat_id, full, uid):
                tag = "任务" if conv.get("conv_type") == "task" else "一般"
                return f"✅ 已加入对话「{conv.get('title') or '新对话'}」([{tag}] {conv.get('repo_key') or ''})。后续飞书消息将进入该对话。"
            return "❌ 加入失败。"
        except Exception as e:
            return f"❌ 加入失败: {e}"
    if cmd == "/refresh":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后刷新。"
            matched = await api.sync_repo_feishu_mapping(ctx, row[0], chat_id)
            return f"✅ 已刷新，同步了 {matched} 个用户映射"
        except Exception as e:
            return f"❌ 刷新失败: {e}"
    if cmd == "/autodev":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后设置。"
            repo_key = row[0]
            from src.session_manager import set_auto_impl_enabled, get_auto_impl_enabled
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                enabled = parts[1].lower() == "on"
                set_auto_impl_enabled(repo_key, enabled)
                return f"✅ 仓库 `{repo_key}` 自动实现已{'开启' if enabled else '关闭'}"
            current = get_auto_impl_enabled(repo_key)
            return f"📋 仓库 `{repo_key}` 自动实现：{'✅ 开启' if current else '❌ 关闭'}"
        except Exception as e:
            return f"❌ 设置失败: {e}"
    if cmd == "/notify":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后设置。"
            repo_key = row[0]
            from src.session_manager import set_notify_enabled, get_notify_enabled
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                enabled = parts[1].lower() == "on"
                set_notify_enabled(repo_key, enabled)
                return f"✅ 仓库 `{repo_key}` 飞书通知已{'开启' if enabled else '关闭'}"
            current = get_notify_enabled(repo_key)
            return f"📋 仓库 `{repo_key}` 飞书通知：{'✅ 开启' if current else '❌ 关闭'}"
        except Exception as e:
            return f"❌ 设置失败: {e}"
    if cmd in ("/clean", "/cleansession", "/reset", "/cleancache"):
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM repo_im_group_binding WHERE im_chat_id=? AND im_app_id=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后再清理。"
            repo_key = row[0]
            from src.repo_key import to_repo_key, from_repo_key
            full_key = to_repo_key(repo_key)
            legacy_key = from_repo_key(repo_key)
            c.execute("UPDATE chat_agent_session SET runtime_session_id='', runtime='' WHERE repo_key=? OR repo_key=?", (full_key, legacy_key))
            db.commit()
            return f"✅ 已清除仓库 `{full_key}` 的 Agent 会话缓存。下次事件将使用全新的 Agent 上下文。"
        except Exception as e:
            return f"❌ 清理失败: {e}"
    if cmd in ("/help", "/start"):
        return (
            f"🤖 DevOps Bot\n\n"
            f"**命令**:\n  `/bind owner/repo`  — 绑定当前群到仓库\n"
            f"  `/unbind`           — 解绑并清除用户映射\n  `/status`           — 查看绑定\n  `/refresh`          — 刷新用户映射\n"
            f"  `/autodev on/off`   — 自动实现开关\n  `/notify on/off`    — 飞书通知开关\n  `/clean`            — 清除 Agent 会话缓存\n"
            f"  `/help`             — 帮助\n\n"
            f"**直接提问**:\n  输入问题即可查询仓库状态"
        )
    return ""


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
                from src.session_manager import set_feishu_open_id, get_feishu_open_id
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
                                    set_feishu_open_id("", username, bind_open_id, gitcode_nickname=nickname)
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
            result = await process_command_async(ctx, api, "/clean", chat_id, parsed.get("sender_id", ""), db, account)
            if result:
                await api.reply_text(ctx, msg_id, result)
            return

        if text.startswith("/"):
            result = await process_command_async(ctx, api, text, chat_id, parsed.get("sender_id", ""), db, account)
            if result:
                await api.reply_text(ctx, msg_id, result)
            return

        repo_key = resolve_repo(text, chat_id, parsed.get("sender_id", ""), db, account)
        if not repo_key:
            reply_parts = ["当前未绑定任何仓库至本群。"]
            try:
                user_id = None
                app_id = ""
                c = db.cursor()
                c.execute("SELECT value FROM meta WHERE key='ws_feishu_app_id'")
                row = c.fetchone()
                if row:
                    app_id = row[0]
                    user_id = get_user_id_by_app_id(app_id)
                if user_id:
                    from src.user import db as user_db
                    repos = user_db.get_managed_repos(user_id)
                    if repos:
                        reply_parts.append("你已加入以下仓库，请选择绑定的仓库：")
                        for i, r in enumerate(repos, 1):
                            reply_parts.append(f"  {i}. {r['full_name']}")
                        reply_parts.append("")
                        reply_parts.append("发送 /bind 序号 或 /bind owner/repo 完成绑定")
                    else:
                        reply_parts.append("你可能还没有加入任何仓库。")
                        reply_parts.append("请先在网页端加入仓库，或发送 /bind owner/repo 直接绑定。")
                else:
                    c.execute("SELECT DISTINCT repo_key, repository, source FROM chat_agent_session ORDER BY last_active_at DESC LIMIT 10")
                    rows = c.fetchall()
                    if rows:
                        reply_parts.append("已有处理的仓库：")
                        for r in rows:
                            reply_parts.append(f"  📦 {r[1]}")
                        reply_parts.append("")
                        reply_parts.append("绑定方式（任选一种）：")
                        reply_parts.append(f'  "绑定到 {rows[0][1]}"')
                        reply_parts.append(f'  "使用 {rows[0][1]}"')
                        reply_parts.append(f"  /bind {rows[0][1]}")
                    else:
                        reply_parts.append("尚无任何仓库记录。请发送 /bind owner/repo 绑定仓库。")
            except Exception as e:
                log.warning(f"Feishu: resolve user repos error: {e}")
                reply_parts.append("请发送：绑定到 owner/repo_name")
            await api.reply_text(ctx, msg_id, "\n".join(reply_parts))
            return

        bind_repo = detect_bind_intent(text)
        if bind_repo:
            result = await process_command_async(ctx, api, f"/bind {bind_repo}", chat_id, parsed.get("sender_id", ""), db, account)
            await api.reply_text(ctx, msg_id, result)
            return

        from src.repo_key import to_repo_key, from_repo_key, parse_repo_key
        repo_key = to_repo_key(repo_key)
        _provider, _owner, _repo = parse_repo_key(repo_key)
        source = _provider
        repository = _repo or from_repo_key(repo_key)

        sess, history, oc_sid, cc_sid, is_new = get_or_create_session(repo_key, source, repository)

        lock = _acquire_repo_lock(repo_key)
        lock.acquire()
        try:
            await _process_chat_inner_async(ctx, api, parsed, text, repo_key, repository,
                                            msg_id, chat_id, sess, history, oc_sid, cc_sid, is_new,
                                            bind_account=account, exec_account=repo_key)
        finally:
            lock.release()

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

        prompt = user_info + "\n\n" + build_chat_prompt(text, repo_key, history, skills, exec_account,
                                                        runtime_id=chat_runtime_id)
        log.info(f"Chat prompt ({len(prompt)} chars): {prompt[:2000]}")

        wsd = os.path.join(WORKSPACE_DIR, "feishu", repository.replace("/", "_"), sess.split("-")[-1])
        os.makedirs(wsd, exist_ok=True)

        await api.add_reaction(ctx, msg_id, "OK")

        final_text = ""
        exit_code = 0
        backend = ""
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

        output = final_text.strip()
        if output == "SKIP":
            log.info("Agent decided to SKIP (no reply needed)")
        elif output:
            await api.reply_card(ctx, msg_id, output[:2000])
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