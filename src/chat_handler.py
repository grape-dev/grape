"""飞书消息处理"""
import re, os
from datetime import datetime
from src.config import log, WORKSPACE_DIR, SKILLS_DIR, get_domain_skill
from src.session_manager import get_or_create_session, update_session
from src.prompt_builder import build_chat_prompt, get_skill_capabilities, build_role_definition
from src.agent_executor import stream_opencode
from src.feishu_handler import (
    FEISHU_BINDINGS_TABLE_SQL, FEISHU_APP_ID, FEISHU_APP_SECRET,
    FEISHU_BOT_NAME, reply_text, add_reaction,
    resolve_repo, process_command, detect_bind_intent, detect_clean_intent,
    start_ws, stop_ws
)
from src.session_manager import db
from src.event_processor import _acquire_repo_lock


def process_chat(parsed: dict):
    try:
        if parsed.get("type") != "message":
            return
        text = parsed["text"]
        chat_id = parsed["chat_id"]
        msg_id = parsed["message_id"]
        log.info(f"Feishu message: {text[:100]} (chat={chat_id})")

        # 能力询问
        capability_patterns = [
            r'(?:你|您)(?:有|会|能|可以)?\s*什么\s*(?:能力|功能|本领|技能|用处|作用)',
            r'(?:你|您)(?:会|能|可以)\s*(?:做|干|处理|干嘛)\s*(?:什么|哪些)',
            r'what\s*(?:can|are)\s*(?:you|your)\s*(?:do|capabilities|skills|features)',
            r'(?:介绍|说说|告诉)(?:一下|下)?\s*(?:你|你自己)',
        ]
        if any(re.search(p, text, re.IGNORECASE) for p in capability_patterns):
            reply_text(msg_id, get_skill_capabilities())
            return

        # 清除会话缓存
        if detect_clean_intent(text):
            result = process_command("/clean", chat_id, parsed.get("sender_id", ""), db)
            if result:
                reply_text(msg_id, result)
            return

        # 命令处理
        if text.startswith("/"):
            result = process_command(text, chat_id, parsed.get("sender_id", ""), db)
            if result:
                reply_text(msg_id, result)
            return

        # 绑定检测
        repo_key = resolve_repo(text, chat_id, parsed.get("sender_id", ""), db)
        if not repo_key:
            reply_parts = ["当前未绑定任何仓库至本群。"]
            try:
                c = db.cursor()
                c.execute("SELECT DISTINCT repo_key, repository, source FROM sessions ORDER BY last_active_at DESC LIMIT 10")
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
                    reply_parts.append("尚无任何仓库的 Session 记录。")
                    reply_parts.append("请先在 GitCode 上操作仓库触发事件，")
                    reply_parts.append('或发送：绑定到 owner/repo_name')
            except Exception as e:
                log.warning(f"Feishu: query sessions error: {e}")
                reply_parts.append("请发送：绑定到 owner/repo_name")
            reply_text(msg_id, "\n".join(reply_parts))
            return

        bind_repo = detect_bind_intent(text)
        if bind_repo:
            result = process_command(f"/bind {bind_repo}", chat_id, parsed.get("sender_id", ""), db)
            reply_text(msg_id, result)
            return

        source, repository = (repo_key.split(":", 1) + ["", ""])[:2]
        if not source or not repository:
            if "/" in repo_key:
                source = "gitcode"
                repository = repo_key
                repo_key = f"gitcode:{repo_key}"

        sess, history, oc_sid, is_new = get_or_create_session(repo_key, source, repository)

        lock = _acquire_repo_lock(repo_key)
        lock.acquire()
        try:
            _process_chat_inner(parsed, text, repo_key, repository, msg_id, sess, history, oc_sid, is_new)
        finally:
            lock.release()

    except Exception as e:
        import traceback
        log.error(f"Feishu process error: {e}")
        traceback.print_exc()
        try:
            local_vars = locals()
            if 'msg_id' in local_vars and local_vars['msg_id']:
                reply_text(local_vars['msg_id'], "❌ 处理消息时出错，请稍后重试")
        except:
            pass


def _process_chat_inner(parsed, text, repo_key, repository, msg_id, sess, history, oc_sid, is_new):
    try:
        prompt = build_chat_prompt(text, repo_key, history, ["chat"])
        log.info(f"Chat prompt ({len(prompt)} chars): {prompt[:200]}")

        wsd = os.path.join(WORKSPACE_DIR, repository)
        os.makedirs(wsd, exist_ok=True)

        add_reaction(msg_id, "OK")

        final_text = ""
        new_oc_session_id = oc_sid
        for event_type, data in stream_opencode(prompt, wsd, oc_sid, "chat"):
            if event_type == "step_start":
                log.info(f"  ▶ {data}")
            elif event_type == "text":
                log.info(f"  💬 {data[:120]}")
            elif event_type == "reasoning":
                log.info(f"  🤔 {data[:120]}")
            elif event_type == "tool_use":
                log.info(f"  🔧 {data[:150]}")
            elif event_type == "step_finish":
                log.info(f"  ✅ {data}")
            elif event_type == "done":
                d = data
                final_text = d["text"]
                new_oc_session_id = d["oc_session_id"] or new_oc_session_id

        record = {
            "type": "chat", "time": datetime.now().isoformat(),
            "success": True, "actor": parsed.get("sender_id", ""),
            "stdout": final_text[:2000],
            "oc_session_id": new_oc_session_id
        }
        history.append(record)
        update_session(sess, history, "idle", new_oc_session_id)

        output = final_text.strip()
        if output == "SKIP":
            log.info("Agent decided to SKIP (no reply needed)")
        elif output:
            reply_text(msg_id, output[:2000])

    except Exception as e:
        import traceback
        log.error(f"Feishu process error: {e}")
        traceback.print_exc()
        try:
            local_vars = locals()
            if 'msg_id' in local_vars and local_vars['msg_id']:
                reply_text(local_vars['msg_id'], "❌ 处理消息时出错，请稍后重试")
        except:
            pass
