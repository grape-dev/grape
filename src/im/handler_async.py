"""异步飞书消息处理 — 支持 BotContext 隔离的多Bot消息分发

核心改造:
  - process_chat_async: 接收 (BotContext, parsed) 替代全局函数调用
  - 所有飞书API调用通过 ctx + api 实例方法，按Bot身份隔离
  - 保持与 handler.py 相同的业务逻辑（命令/绑定/Agent执行）
"""
import re, os, asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import get_or_create_session, update_session, db
from src.prompt_builder import build_chat_prompt, get_skill_capabilities
from src.agent.executor import stream_agent
from src.im.feishu import (
    resolve_repo, process_command, detect_bind_intent, detect_clean_intent,
    get_chat_members, sync_repo_feishu_mapping,
)
from src.api.event_store import acquire_repo_lock as _acquire_repo_lock

if TYPE_CHECKING:
    from src.im.feishu_async import BotContext, FeishuAsync


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
            result = process_command("/clean", chat_id, parsed.get("sender_id", ""), db, account)
            if result:
                await api.reply_text(ctx, msg_id, result)
            return

        if text.startswith("/"):
            result = process_command(text, chat_id, parsed.get("sender_id", ""), db, account)
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
                    from src.im.feishu import get_user_id_by_app_id
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
            result = process_command(f"/bind {bind_repo}", chat_id, parsed.get("sender_id", ""), db, account)
            await api.reply_text(ctx, msg_id, result)
            return

        source, repository = (repo_key.split(":", 1) + ["", ""])[:2]
        if not source or not repository:
            if "/" in repo_key:
                source = "gitcode"
                repository = repo_key
                repo_key = f"gitcode:{repo_key}"

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
                c = db.cursor()
                c.execute("SELECT gitcode_user, feishu_name FROM unified_user_mapping WHERE feishu_open_id=?",
                          (open_id,))
                row = c.fetchone()
                if row:
                    gitcode_login = row[0]
                    nick_name = row[1] or ""
                if gitcode_login:
                    user_info += f"\nGitCode login: {gitcode_login}"
                    if nick_name and nick_name != gitcode_login:
                        user_info += f"\nGitCode nick (use this in cards): {nick_name}"
                    repository_part = repo_key.replace("gitcode:", "").split("/", 1)
                    if len(repository_part) == 2:
                        user_info += f"\nTo check pending features: GET /api/v5/repos/{repository_part[0]}/{repository_part[1]}/issues?state=open&assignee={gitcode_login}&labels=feature"
            except Exception as e:
                log.warning(f"Failed to resolve GitCode login for {sender_name}: {e}")

        skills = ["chat"]

        # 获取 IM 用户 token（chat_conversations 持久化，幂等创建）
        user_token = ""
        try:
            from src.im.feishu import get_user_id_by_app_id
            from src.user.db import get_user_gitcode_token
            from src.session_manager import create_conversation, get_conversation_by_im, update_conversation
            user_id = get_user_id_by_app_id(ctx.app_id) or 0
            if user_id > 0:
                user_token = get_user_gitcode_token(user_id) or ""
                conv = get_conversation_by_im(chat_id, user_id)
                if not conv:
                    cid = create_conversation(user_id, repo_key, im_channel_id=chat_id, user_gitcode_token=user_token)
                    log.info(f"[Chat:{ctx.name}] created chat_conversation {cid} for user={user_id} chat={chat_id[:20]}")
                elif user_token and not conv.get("user_gitcode_token"):
                    update_conversation(conv["id"], user_gitcode_token=user_token)
        except Exception as e:
            log.warning(f"[Chat:{ctx.name}] resolve user gitcode token failed: {e}")

        prompt = user_info + "\n\n" + build_chat_prompt(text, repo_key, history, skills, exec_account)
        log.info(f"Chat prompt ({len(prompt)} chars): {prompt[:2000]}")

        wsd = os.path.join(WORKSPACE_DIR, "feishu", repository.replace("/", "_"), sess.split("-")[-1])
        os.makedirs(wsd, exist_ok=True)

        await api.add_reaction(ctx, msg_id, "OK")

        final_text = ""
        exit_code = 0
        backend = "opencode"
        oc_sid = {"opencode": oc_sid, "codearts": cc_sid}.get(backend, "")
        new_oc_session_id = oc_sid

        for event_type, data in stream_agent(prompt, wsd, oc_sid, "chat",
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

    except Exception as e:
        import traceback
        log.error(f"[Chat:{ctx.name}] process inner error: {e}")
        traceback.print_exc()
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错，请稍后重试")
        except:
            pass