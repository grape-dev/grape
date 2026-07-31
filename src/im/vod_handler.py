"""VOD Agent IM 消息处理器 — 飞书群反馈收集入口"""
import json, os, threading, uuid
from datetime import datetime
from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import db, db_lock
from src.api.event_store import update_event_status, get_issue_summary

_VOD_ROLE_DEFINITION = """
你是项目的公共反馈入口，部署在飞书群中供所有成员提交建议和反馈。

你的核心能力：
1. 收集反馈 — 收集开发者对项目的问题、建议、痛点，记录到 Issue
2. 创建 Issue — 将确认的反馈自动创建为 GitCode Issue，标注来源 "飞书用户反馈"
3. 查询进展 — 查询 Issue/PR 当前处理状态、CI 状态、部署状态
4. 解答疑问 — 回答关于项目工作流、代码规范、团队流程等问题

你的限制：
- 不修改代码、不创建 PR、不合并代码
- 不处理个人闲聊、天气、新闻等非 DevOps 话题
- 不回复敏感信息（Token、密码、AK/SK）

回复规则：
1. 用户在群内 @你 或有明确 DevOps 问题时，必须回复
2. 始终用中文回复，简洁清晰
3. 创建的 Issue 必须在正文中标注 auto-submitted by VOD Agent (Feishu)
4. 不确定的问题，回复 "已记录你的反馈，我会转给相关团队处理"
5. 不要输出内部推理过程、分析步骤等英文内容
"""


def _build_vod_prompt(evt: dict, skill_name: str) -> str:
    """构建 VOD IM 专用的 prompt"""
    user_msg = evt.get("payload", {}).get("user_message", "")
    user_name = evt.get("actor", "未知用户")
    repo = evt.get("repository", "")

    parts = []
    parts.append(f"## 用户消息\n来自 {user_name} 在仓库 {repo}")
    parts.append(f"消息内容: {user_msg[:2000]}")
    parts.append("")

    repo_key = f"gitcode:{repo}"
    summary = get_issue_summary(repo_key)
    if summary:
        parts.append(summary)
        parts.append("")

    skill_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if os.path.exists(skill_path):
        with open(skill_path, encoding="utf-8") as f:
            parts.append(f"## Skill: {skill_name}\n{f.read()}\n")
    else:
        parts.append(f"Read the file at {skill_path} using the read tool. Execute its instructions step by step.")

    parts.append("## 角色定义")
    parts.append(_VOD_ROLE_DEFINITION)

    try:
        agents_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "agent-description.json")
        if os.path.isfile(agents_file):
            with open(agents_file, encoding="utf-8") as f:
                registry = json.load(f)
            parts.append("\n## Available Agents (for A2A calls)")
            for agent_type, info in registry.items():
                parts.append(f"- {info.get('name', agent_type)} ({agent_type})")
    except Exception:
        pass

    return "\n".join(parts)


def _get_vod_session(repo_key: str) -> dict | None:
    """获取仓库的 VOD Agent session 信息"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, backend, skill, repo_gitcode_token, vod_bot_app_id FROM agent_sessions WHERE repo_key=? AND agent_type='vod'",
                (repo_key,)
            )
            row = c.fetchone()
            if row:
                return {"id": row[0], "backend": row[1] or "opencode",
                        "skill": row[2] or "vod", "repo_gitcode_token": row[3] or "",
                        "vod_bot_app_id": row[4] or ""}
    except Exception:
        pass
    return None


def _get_or_create_conversation(feishu_chat_id: str, agent_session_id: str) -> dict:
    """获取或创建 vod_conversations"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, oc_session_id FROM vod_conversations WHERE feishu_chat_id=?", (feishu_chat_id,))
            row = c.fetchone()
            if row:
                return {"id": row[0], "oc_session_id": row[1] or "", "new": False}
            c.execute(
                "INSERT INTO vod_conversations (agent_session_id, feishu_chat_id, oc_session_id, status, last_active_at) VALUES (?,?,?,?,datetime('now'))",
                (agent_session_id, feishu_chat_id, "", "active")
            )
            db.commit()
            return {"id": c.lastrowid, "oc_session_id": "", "new": True}
    except Exception:
        pass
    return {"id": 0, "oc_session_id": "", "new": False}


def _update_conversation_oc_session(conv_id: int, oc_session_id: str):
    """更新 vod_conversations 的 oc_session_id"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE vod_conversations SET oc_session_id=?, last_active_at=datetime('now') WHERE id=?", (oc_session_id, conv_id))
            db.commit()
    except Exception:
        pass


def _execute_vod_agent(repo_key: str, agent_session: dict, conv: dict, evt: dict) -> str:
    """执行 VOD Agent 并返回回复文本"""
    from src.agent.executor import stream_agent

    skill_name = agent_session.get("skill", "vod")
    prompt = _build_vod_prompt(evt, skill_name)
    ws = os.path.join(WORKSPACE_DIR, repo_key, "vod-agent")
    os.makedirs(ws, exist_ok=True)

    exit_code = -1
    final_text = ""
    new_oc_session_id = ""
    try:
        for evt_type, data in stream_agent(
            prompt, ws, session_id=conv.get("oc_session_id", ""),
            event_type="vod_signal", backend_name=agent_session.get("backend", "opencode"),
            account=repo_key, repo_gitcode_token=agent_session.get("repo_gitcode_token", ""),
        ):
            if evt_type == "done":
                d = data
                exit_code = d.get("exit_code", -1)
                final_text = d.get("text", "")
                new_oc_session_id = d.get("oc_session_id", "")
            elif evt_type == "error":
                log.warning(f"VOD agent error: {data}")
    except Exception as e:
        log.warning(f"VOD agent execute failed: {e}")

    if new_oc_session_id and new_oc_session_id != conv.get("oc_session_id", ""):
        _update_conversation_oc_session(conv["id"], new_oc_session_id)

    if exit_code != 0:
        return f"处理失败，请稍后重试 (exit={exit_code})"
    return final_text


def _get_bot_credential(bot_app_id: str) -> tuple[str, str]:
    """获取 VOD 机器人凭证"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT bot_app_id, bot_app_secret FROM im_bot_config WHERE bot_type='vod_bot' AND bot_app_id=?", (bot_app_id,))
            row = c.fetchone()
            if row:
                return row[0], row[1]
    except Exception:
        pass
    return "", ""


def _send_feishu_reply(bot_app_id: str, bot_app_secret: str, chat_id: str, text: str):
    """通过 VOD 机器人回复飞书卡片消息"""
    from src.im.feishu import get_tenant_token_from
    token = get_tenant_token_from(bot_app_id, bot_app_secret)
    if not token:
        log.warning("VOD IM: no feishu token, skip reply")
        return
    try:
        from urllib.request import Request, urlopen
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        card = {
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": text[:2000]}
            }],
            "header": {"title": {"tag": "plain_text", "content": "DevOps Bot"}, "template": "blue"}
        }
        payload = json.dumps({
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }).encode()
        req = Request(url, data=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        resp = urlopen(req, timeout=10)
        log.info(f"[VOD] card reply sent, chat={chat_id[:20]}, status={resp.status}")
    except Exception as e:
        log.warning(f"VOD IM: send card reply failed: {e}")


def _list_vod_repos(bot_app_id: str) -> list[dict]:
    """列出当前 VOD 机器人已配置的所有仓库"""
    results = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key FROM agent_sessions WHERE agent_type='vod' AND vod_bot_app_id=?", (bot_app_id,))
            for r in c.fetchall():
                results.append({"repo_key": r[0]})
    except Exception:
        pass
    return results


def process_vod_message(message: dict, bot_app_id: str):
    """处理 VOD 飞书消息（WS 回调入口）"""
    try:
        # 解析消息
        event = message.get("event", {})
        chat_id = ""
        sender_id = ""
        text = ""
        msg_type = event.get("message", {}).get("message_type", "")

        if msg_type == "text":
            content_str = event.get("message", {}).get("content", "{}")
            try:
                content = json.loads(content_str)
                text = content.get("text", "")
            except Exception:
                text = content_str

        chat_id = event.get("message", {}).get("chat_id", "")
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {}).get("open_id", "")

        if not text or not chat_id:
            return

        text = text.strip()
        log.info(f"[VOD] msg from {sender_id[:20]}: {text[:100]}")

        # 命令处理
        cmd_parts = text.split()
        cmd = cmd_parts[0].lower() if cmd_parts else ""

        if cmd == "/help":
            help_text = (
                "可用命令：\n"
                "/bind owner/repo - 绑定仓库\n"
                "/unbind - 解绑当前群\n"
                "/status - 查看当前绑定\n"
                "/clean - 清除会话上下文\n"
                "/help - 显示此帮助"
            )
            _send_feishu_reply_text(bot_app_id, chat_id, help_text)
            return

        if cmd == "/status":
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT agent_session_id, oc_session_id FROM vod_conversations WHERE feishu_chat_id=?", (chat_id,))
                    row = c.fetchone()
                if row:
                    # 通过 agent_session_id 查 repo_key
                    with db_lock:
                        c = db.cursor()
                        c.execute("SELECT repo_key FROM agent_sessions WHERE id=?", (row[0],))
                        repo_row = c.fetchone()
                    repo = repo_row[0] if repo_row else "?"
                    _send_feishu_reply_text(bot_app_id, chat_id, f"📋 当前绑定: `{repo}`")
                else:
                    _send_feishu_reply_text(bot_app_id, chat_id, "📋 未绑定任何仓库。使用 `/bind owner/repo` 绑定")
            except Exception as e:
                _send_feishu_reply_text(bot_app_id, chat_id, f"❌ 查询失败: {e}")
            return

        if cmd == "/bind" and len(cmd_parts) >= 2:
            repo_key = cmd_parts[1]
            agent_session = _get_vod_session(repo_key)
            if not agent_session:
                _send_feishu_reply_text(bot_app_id, chat_id, f"❌ 仓库 `{repo_key}` 未托管或未配置 VOD Agent")
                return
            if agent_session["vod_bot_app_id"] != bot_app_id:
                _send_feishu_reply_text(bot_app_id, chat_id, f"❌ 仓库 `{repo_key}` 使用了其他机器人凭证，无法绑定")
                return
            _get_or_create_conversation(chat_id, agent_session["id"])
            _send_feishu_reply_text(bot_app_id, chat_id, f"✅ 已绑定仓库 `{repo_key}`，现在可以向我提交反馈了")
            return

        if cmd == "/unbind":
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("DELETE FROM vod_conversations WHERE feishu_chat_id=?", (chat_id,))
                    db.commit()
                _send_feishu_reply_text(bot_app_id, chat_id, "✅ 已解除绑定，不再接收该仓库的消息")
            except Exception as e:
                _send_feishu_reply_text(bot_app_id, chat_id, f"❌ 解绑失败: {e}")
            return

        if cmd in ("/clean", "/cleansession", "/reset"):
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE vod_conversations SET oc_session_id='' WHERE feishu_chat_id=?", (chat_id,))
                    db.commit()
                _send_feishu_reply_text(bot_app_id, chat_id, "✅ 已清除会话上下文")
            except Exception as e:
                _send_feishu_reply_text(bot_app_id, chat_id, f"❌ 清除失败: {e}")
            return

        # 正常消息处理
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT agent_session_id, oc_session_id FROM vod_conversations WHERE feishu_chat_id=?", (chat_id,))
                row = c.fetchone()
        except Exception:
            row = None

        if not row:
            # 自动绑定
            repos = _list_vod_repos(bot_app_id)
            if len(repos) == 0:
                _send_feishu_reply_text(bot_app_id, chat_id, "❌ 当前机器人未绑定任何仓库，请联系管理员在 Dashboard 中配置")
                return
            elif len(repos) == 1:
                agent_session = _get_vod_session(repos[0]["repo_key"])
                if agent_session:
                    conv = _get_or_create_conversation(chat_id, agent_session["id"])
                    _send_feishu_reply_text(bot_app_id, chat_id, f"✅ 已自动绑定仓库 `{repos[0]['repo_key']}`")
                    # 继续处理当前消息
                    row = (agent_session["id"], conv["oc_session_id"])
                else:
                    _send_feishu_reply_text(bot_app_id, chat_id, "❌ 自动绑定失败，请联系管理员配置")
                    return
            else:
                repo_list = "\n".join([f"  - `{r['repo_key']}`" for r in repos])
                _send_feishu_reply_text(bot_app_id, chat_id,
                    f"当前机器人已绑定以下仓库：\n{repo_list}\n请发送 `/bind owner/repo` 指定要绑定的仓库")
                return

        agent_session_id = row[0]
        oc_session_id = row[1] or ""

        # 查 agent_session 获取 repo 等配置
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT repo_key, backend, skill, repo_gitcode_token FROM agent_sessions WHERE id=?", (agent_session_id,))
                session_row = c.fetchone()
        except Exception:
            session_row = None

        if not session_row:
            _send_feishu_reply_text(bot_app_id, chat_id, "❌ 找不到 Agent 会话配置，请重新绑定")
            return

        repo_key = session_row[0]
        agent_session = {"backend": session_row[1] or "opencode", "skill": session_row[2] or "vod",
                         "repo_gitcode_token": session_row[3] or ""}

        # 构建事件
        evt = {
            "type": "vod_signal",
            "repository": repo_key,
            "source": "vod_im",
            "actor": sender_id[:20] if sender_id else "unknown",
            "branch": "",
            "payload": {"user_message": text, "feishu_chat_id": chat_id, "feishu_sender_id": sender_id},
        }

        # 执行 Agent
        reply_text = _execute_vod_agent(repo_key, agent_session, {"id": 0, "oc_session_id": oc_session_id}, evt)

        # 回复飞书
        bot_app_id_val, bot_app_secret_val = _get_bot_credential(bot_app_id)
        if bot_app_id_val and bot_app_secret_val:
            _send_feishu_reply(bot_app_id_val, bot_app_secret_val, chat_id, reply_text)

    except Exception as e:
        log.error(f"VOD IM process error: {e}")


def _send_feishu_reply_text(bot_app_id: str, chat_id: str, text: str):
    """发送纯文本消息到飞书群"""
    bot_id, secret = _get_bot_credential(bot_app_id)
    if bot_id and secret:
        _send_feishu_reply(bot_id, secret, chat_id, text)
