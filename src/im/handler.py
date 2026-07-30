"""飞书消息处理"""
import re, os
from datetime import datetime
from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import get_or_create_session, update_session
from src.prompt_builder import build_chat_prompt, get_skill_capabilities, build_role_definition
from src.agent.executor import stream_agent
from src.im.feishu import (
    resolve_repo, process_command, detect_bind_intent, detect_clean_intent,
    get_user_name, start_ws, stop_ws, get_chat_members, sync_repo_feishu_mapping,
    add_reaction,
)
from src.session_manager import db
from src.api.event_store import acquire_repo_lock as _acquire_repo_lock


def process_chat(parsed: dict):
    try:
        if parsed.get("type") != "message":
            return
        text = parsed["text"]
        chat_id = parsed["chat_id"]
        msg_id = parsed["message_id"]
        account = parsed.get("account", "default")
        log.info(f"Feishu message: {text[:100]} (chat={chat_id[:20]}, account={account})")

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
            result = process_command("/clean", chat_id, parsed.get("sender_id", ""), db, account)
            if result:
                reply_text(msg_id, result)
            return

        # 命令处理
        if text.startswith("/"):
            result = process_command(text, chat_id, parsed.get("sender_id", ""), db, account)
            if result:
                reply_text(msg_id, result)
            return

        # 绑定检测
        repo_key = resolve_repo(text, chat_id, parsed.get("sender_id", ""), db, account)
        if not repo_key:
            reply_parts = ["当前未绑定任何仓库至本群。"]
            try:
                # 通过 meta 表中记录的 ws_feishu_app_id 查找 user_id
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
                    # 查询该用户已加入的仓库
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
                    # 兜底：显示最近的 session 记录
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
                        reply_parts.append("尚无任何仓库记录。请发送 /bind owner/repo 绑定仓库。")
            except Exception as e:
                log.warning(f"Feishu: resolve user repos error: {e}")
                reply_parts.append("请发送：绑定到 owner/repo_name")
            reply_text(msg_id, "\n".join(reply_parts))
            return

        bind_repo = detect_bind_intent(text)
        if bind_repo:
            result = process_command(f"/bind {bind_repo}", chat_id, parsed.get("sender_id", ""), db, account)
            reply_text(msg_id, result)
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
            _process_chat_inner(parsed, text, repo_key, repository, msg_id, sess, history, oc_sid, cc_sid, is_new, account)
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


def _process_chat_inner(parsed, text, repo_key, repository, msg_id, sess, history, oc_sid, cc_sid, is_new, account="default"):
    try:
        sender_id = parsed.get('sender_id', '')
        sender_name = get_user_name(sender_id) if sender_id else ""
        open_id = parsed.get('sender_open_id', '')
        user_info = f"User: {sender_name or sender_id} (ID: {sender_id})"
        if open_id:
            user_info += f"\nFeishu open_id: {open_id}\nTo @mention user, use: <at id={open_id}></at>"
            # Resolve GitCode login from feishu_users mapping
            try:
                gitcode_login = None
                nick_name = ""
                c = db.cursor()
                c.execute("SELECT gitcode_user, feishu_name FROM feishu_users WHERE repo_key=? AND feishu_open_id=?", (repo_key, open_id))
                row = c.fetchone()
                if row:
                    gitcode_login = row[0]
                    nick_name = row[1] or ""
                if not gitcode_login:
                    c.execute("SELECT gitcode_user, feishu_name FROM feishu_users WHERE feishu_open_id=?", (open_id,))
                    row = c.fetchone()
                    if row:
                        gitcode_login = row[0]
                        nick_name = row[1] or ""
                if gitcode_login:
                    user_info += f"\nGitCode login: {gitcode_login}"
                    if nick_name and nick_name != gitcode_login:
                        user_info += f"\nGitCode nick (use this in cards): {nick_name}"
                    # Check for pending feature issues
                    repository_part = repo_key.replace("gitcode:", "").split("/", 1)
                    if len(repository_part) == 2:
                        user_info += f"\nTo check pending features: GET /api/v5/repos/{repository_part[0]}/{repository_part[1]}/issues?state=open&assignee={gitcode_login}&labels=feature"
            except Exception as e:
                log.warning(f"Failed to resolve GitCode login for {sender_name}: {e}")
        skills = ["chat"]
        prompt = user_info + "\n\n" + build_chat_prompt(text, repo_key, history, skills, account)
        log.info(f"Chat prompt ({len(prompt)} chars): {prompt[:200]}")

        wsd = os.path.join(WORKSPACE_DIR, "feishu", repository.replace("/", "_"), sess.split("-")[-1])
        os.makedirs(wsd, exist_ok=True)

        add_reaction(msg_id, "OK")

        final_text = ""
        exit_code = 0
        backend = "opencode"
        oc_sid = {"opencode": oc_sid, "codearts": cc_sid}.get(backend, "")
        new_oc_session_id = oc_sid
        # 获取 repo 绑定的 DevOps Bot Token
        repo_token = ""
        try:
            parts = repo_key.replace("gitcode:", "").split("/")
            if len(parts) == 2:
                from src.user.db import get_bot_token_by_repo
                repo_token = get_bot_token_by_repo(parts[0], parts[1])
        except Exception:
            pass
        for event_type, data in stream_agent(prompt, wsd, oc_sid, "chat", backend_name=backend, account=account, repo_gitcode_token=repo_token):
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
                exit_code = d.get("exit_code", 0)
                if exit_code == 0 and d.get("oc_session_id"):
                    new_oc_session_id = d["oc_session_id"]
                log.info(f"  ✅ Done: exit={exit_code}, text_len={len(final_text)}, stderr={d.get('stderr','')[:200]}, errors={d.get('errors',[])}")

        record = {
            "type": "chat", "time": datetime.now().isoformat(),
            "success": exit_code == 0, "exit_code": exit_code,
            "actor": parsed.get("sender_id", ""),
            "account": account,
            "backend": backend,
            "skill": "chat",
            "stdout": final_text[:2000],
            "oc_session_id": new_oc_session_id
        }
        history.append(record)
        from src.session_manager import dump_history_to_file
        dump_history_to_file(repo_key, history, account)
        update_session(sess, history, "idle", new_oc_session_id, backend)

        output = final_text.strip()
        if output == "SKIP":
            log.info("Agent decided to SKIP (no reply needed)")
        elif output:
            reply_card(msg_id, output[:2000])

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
