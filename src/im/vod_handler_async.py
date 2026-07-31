"""异步 VOD 消息处理 — 支持 BotContext 隔离的 VOD Bot 消息分发

VOD Bot 职责：收集反馈、创建 Issue、查询进展，不修改代码/创建PR。
"""
import json, os, re, asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import db, db_lock
from src.agent.executor import stream_agent

if TYPE_CHECKING:
    from src.im.feishu_async import BotContext, FeishuAsync

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


def _get_vod_session(repo_key: str) -> dict | None:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, repo_key, backend, skill, vod_bot_app_id, repo_gitcode_token "
                      "FROM agent_sessions WHERE agent_type='vod' AND repo_key=?", (repo_key,))
            row = c.fetchone()
        if row:
            return {"id": row[0], "repo_key": row[1], "backend": row[2] or "opencode",
                    "skill": row[3] or "vod", "vod_bot_app_id": row[4] or "",
                    "repo_gitcode_token": row[5] or ""}
    except Exception:
        pass
    return None


def _get_or_create_conversation(chat_id: str, agent_session_id: str) -> dict:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, agent_session_id, oc_session_id FROM vod_conversations WHERE feishu_chat_id=?",
                      (chat_id,))
            row = c.fetchone()
            if row:
                return {"id": row[0], "agent_session_id": row[1], "oc_session_id": row[2] or ""}
            import uuid
            conv_id = str(uuid.uuid4())[:8]
            c.execute("INSERT INTO vod_conversations (id, feishu_chat_id, agent_session_id, feishu_chat_name) "
                      "VALUES (?,?,?,?)", (conv_id, chat_id, agent_session_id, ""))
            db.commit()
            return {"id": conv_id, "agent_session_id": agent_session_id, "oc_session_id": ""}
    except Exception as e:
        log.warning(f"VOD conversation error: {e}")
    return {"id": "", "agent_session_id": agent_session_id, "oc_session_id": ""}


def _list_vod_repos(bot_app_id: str) -> list:
    results = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key FROM agent_sessions WHERE agent_type='vod' AND vod_bot_app_id=?",
                      (bot_app_id,))
            for r in c.fetchall():
                results.append({"repo_key": r[0]})
    except Exception:
        pass
    return results


def _execute_vod_agent(repo_key: str, agent_session: dict, conv: dict, evt: dict) -> str:
    from src.prompt_builder import build_chat_prompt
    oc_session_id = conv.get("oc_session_id", "")
    backend = agent_session.get("backend", "opencode")
    skill = agent_session.get("skill", "vod")
    repo_token = agent_session.get("repo_gitcode_token", "")
    user_msg = evt.get("payload", {}).get("user_message", "")

    prompt = _VOD_ROLE_DEFINITION + "\n\n用户消息: " + user_msg
    wsd = os.path.join(WORKSPACE_DIR, "vod", repo_key.replace(":", "_").replace("/", "_"))
    os.makedirs(wsd, exist_ok=True)

    final_text = ""
    new_oc_sid = oc_session_id
    for event_type, data in stream_agent(prompt, wsd, oc_session_id, skill,
                                          backend_name=backend, account=repo_key,
                                          repo_gitcode_token=repo_token):
        if event_type == "done":
            final_text = data.get("text", "")
            if data.get("exit_code", 1) == 0 and data.get("oc_session_id"):
                new_oc_sid = data["oc_session_id"]

    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE vod_conversations SET oc_session_id=? WHERE feishu_chat_id=?",
                      (new_oc_sid, evt.get("payload", {}).get("feishu_chat_id", "")))
            db.commit()
    except Exception:
        pass

    return final_text.strip() if final_text else "处理完成，但未生成回复"


async def process_vod_async(ctx: "BotContext", api: "FeishuAsync", parsed: dict):
    """异步 VOD 消息处理入口"""
    try:
        if parsed.get("type") != "message":
            return
        text = parsed.get("text", "")
        chat_id = parsed.get("chat_id", "")
        msg_id = parsed.get("message_id", "")
        sender_open_id = parsed.get("sender_open_id", "")

        if not text or not chat_id:
            return

        text = text.strip()
        log.info(f"[VOD:{ctx.name}] msg from {sender_open_id[:20]}: {text[:100]}")

        cmd_parts = text.split()
        cmd = cmd_parts[0].lower() if cmd_parts else ""

        if cmd == "/help":
            await api.reply_text(ctx, msg_id,
                "可用命令：\n/bind owner/repo - 绑定仓库\n/unbind - 解绑\n/status - 查看绑定\n/clean - 清除会话\n/help - 帮助")
            return

        if cmd == "/status":
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT agent_session_id FROM vod_conversations WHERE feishu_chat_id=?", (chat_id,))
                    row = c.fetchone()
                if row:
                    with db_lock:
                        c = db.cursor()
                        c.execute("SELECT repo_key FROM agent_sessions WHERE id=?", (row[0],))
                        repo_row = c.fetchone()
                    repo = repo_row[0] if repo_row else "?"
                    await api.reply_text(ctx, msg_id, f"📋 当前绑定: `{repo}`")
                else:
                    await api.reply_text(ctx, msg_id, "📋 未绑定。使用 `/bind owner/repo` 绑定")
            except Exception as e:
                await api.reply_text(ctx, msg_id, f"❌ 查询失败: {e}")
            return

        if cmd == "/bind" and len(cmd_parts) >= 2:
            repo_key = cmd_parts[1]
            agent_session = _get_vod_session(repo_key)
            if not agent_session:
                await api.reply_text(ctx, msg_id, f"❌ 仓库 `{repo_key}` 未配置 VOD Agent")
                return
            if agent_session["vod_bot_app_id"] != ctx.app_id:
                await api.reply_text(ctx, msg_id, f"❌ 仓库使用了其他机器人凭证")
                return
            _get_or_create_conversation(chat_id, agent_session["id"])
            await api.reply_text(ctx, msg_id, f"✅ 已绑定 `{repo_key}`")
            return

        if cmd == "/unbind":
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("DELETE FROM vod_conversations WHERE feishu_chat_id=?", (chat_id,))
                    db.commit()
                await api.reply_text(ctx, msg_id, "✅ 已解绑")
            except Exception as e:
                await api.reply_text(ctx, msg_id, f"❌ 解绑失败: {e}")
            return

        if cmd in ("/clean", "/reset"):
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE vod_conversations SET oc_session_id='' WHERE feishu_chat_id=?", (chat_id,))
                    db.commit()
                await api.reply_text(ctx, msg_id, "✅ 已清除会话")
            except Exception as e:
                await api.reply_text(ctx, msg_id, f"❌ 清除失败: {e}")
            return

        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT agent_session_id, oc_session_id FROM vod_conversations WHERE feishu_chat_id=?",
                          (chat_id,))
                row = c.fetchone()
        except Exception:
            row = None

        if not row:
            repos = _list_vod_repos(ctx.app_id)
            if len(repos) == 0:
                await api.reply_text(ctx, msg_id, "❌ 未绑定仓库，请联系管理员配置")
                return
            elif len(repos) == 1:
                agent_session = _get_vod_session(repos[0]["repo_key"])
                if agent_session:
                    conv = _get_or_create_conversation(chat_id, agent_session["id"])
                    await api.reply_text(ctx, msg_id, f"✅ 已自动绑定 `{repos[0]['repo_key']}`")
                    row = (agent_session["id"], conv["oc_session_id"])
                else:
                    await api.reply_text(ctx, msg_id, "❌ 自动绑定失败")
                    return
            else:
                repo_list = "\n".join([f"  - `{r['repo_key']}`" for r in repos])
                await api.reply_text(ctx, msg_id,
                    f"已绑定以下仓库：\n{repo_list}\n请发送 `/bind owner/repo` 选择")
                return

        agent_session_id = row[0]
        oc_session_id = row[1] or ""

        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT repo_key, backend, skill, repo_gitcode_token FROM agent_sessions WHERE id=?",
                          (agent_session_id,))
                session_row = c.fetchone()
        except Exception:
            session_row = None

        if not session_row:
            await api.reply_text(ctx, msg_id, "❌ 找不到 Agent 配置，请重新绑定")
            return

        repo_key = session_row[0]
        agent_session = {"backend": session_row[1] or "opencode", "skill": session_row[2] or "vod",
                         "repo_gitcode_token": session_row[3] or ""}

        evt = {
            "type": "vod_signal", "repository": repo_key, "source": "vod_im",
            "actor": sender_open_id[:20] if sender_open_id else "unknown",
            "branch": "",
            "payload": {"user_message": text, "feishu_chat_id": chat_id, "feishu_sender_id": sender_open_id},
        }

        reply = _execute_vod_agent(repo_key, agent_session, {"id": 0, "oc_session_id": oc_session_id}, evt)

        if reply and reply != "SKIP":
            await api.reply_card(ctx, msg_id, reply[:2000], title="VOD Agent", template="green")

    except Exception as e:
        log.error(f"[VOD:{ctx.name}] process error: {e}")
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错")
        except:
            pass