"""异步 VOD 消息处理 — 支持 BotContext 隔离的 VOD Bot 消息分发

VOD Bot 职责：收集反馈、创建 Issue、查询进展，不修改代码/创建PR。
"""
import json, os, re, asyncio, time
from datetime import datetime
from typing import TYPE_CHECKING

from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import db, db_lock
from src.runtime.executor import consume_stream_async
from src.runtime.service import get_execution_mode

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
            c.execute("SELECT id, repo_key, runtime_id, skill, skill_id, im_app_id, repo_gitcode_token "
                      "FROM devops_agent_session WHERE agent_type='vod' AND repo_key=?", (repo_key,))
            row = c.fetchone()
        if row:
            backend = "opencode"
            if row[2]:
                c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[2],))
                brow = c.fetchone()
                if brow and brow[0]:
                    backend = brow[0]
            return {"id": row[0], "repo_key": row[1], "backend": backend,
                    "skill": row[3] or "vod", "skill_id": row[4] or 0, "im_app_id": row[5] or "",
                    "repo_gitcode_token": row[6] or ""}
    except Exception:
        pass
    return None


def _get_or_create_conversation(chat_id: str, agent_session_id: str) -> dict:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, agent_session_id, runtime_session_id FROM vod_conversations WHERE im_chat_id=?",
                      (chat_id,))
            row = c.fetchone()
            if row:
                return {"id": row[0], "agent_session_id": row[1], "runtime_session_id": row[2] or ""}
            import time
            c.execute("INSERT INTO vod_conversations (agent_session_id, im_chat_id, im_channel_name) "
                      "VALUES (?,?,?)", (agent_session_id, chat_id, ""))
            db.commit()
            conv_id = c.lastrowid
            log.info(f"VOD conv created: id={conv_id}, chat_id={chat_id[:20]}, agent_session={agent_session_id[:20]}")
            return {"id": conv_id, "agent_session_id": agent_session_id, "runtime_session_id": "", "is_new": True}
    except Exception as e:
        log.warning(f"VOD conversation error: {e}")
    return {"id": "", "agent_session_id": agent_session_id, "runtime_session_id": ""}


def _list_vod_repos(bot_app_id: str) -> list:
    results = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key FROM devops_agent_session WHERE agent_type='vod' AND im_app_id=?",
                      (bot_app_id,))
            for r in c.fetchall():
                results.append({"repo_key": r[0]})
    except Exception:
        pass
    return results


def _build_vod_prompt(evt: dict, skill_name: str, skill_path: str = "") -> str:
    from src.api.event_store import get_issue_summary
    user_msg = evt.get("payload", {}).get("user_message", "")
    user_name = evt.get("actor", "未知用户")
    repo = evt.get("repository", "")

    parts = []
    parts.append(f"## 用户消息\n来自 {user_name} 在仓库 {repo}")
    parts.append(f"消息内容: {user_msg[:2000]}")
    parts.append("")

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    summary = get_issue_summary(repo_key)
    if summary:
        parts.append(summary)
        parts.append("")

    # VOD v2.0：项目知识基线 + 生命周期阶段 + Issue 富上下文（见 vod-skill-upgrade-plan.md §3.6）
    try:
        from src.repo_key import split_owner_repo
        _owner, _rname = split_owner_repo(repo_key)
        _pi = os.path.join(WORKSPACE_DIR, _owner, _rname, "project-info")
        _g = os.path.join(_pi, "project-graph.md")
        _c = os.path.join(_pi, "project-capabilities.md")
        if os.path.exists(_g) or os.path.exists(_c):
            parts.append("\n## 项目知识基线")
            for _f, _label in ((_g, "project-graph"), (_c, "project-capabilities")):
                if os.path.exists(_f):
                    _content = open(_f, encoding="utf-8").read()[:2500]
                    parts.append(f"\n### {_label}\n{_content}")
            parts.append("\n> 若知识基线缺失或过期，请按 issue-advance/references/codegraph-project-analysis.md 生成/更新。")
        # 当前 Issue 生命周期阶段（若有）
        _iid = evt.get("issue_id", "") or ""
        if _iid:
            try:
                from src.api.session_store import get_issue_stage
                _st = get_issue_stage(repo_key, _iid)
                if _st:
                    parts.append(f"\n## Issue 生命周期\n当前 Issue {_iid} 处于 **{_st['stage']}** 阶段，归属 **{_st['owner_agent']}**。")
            except Exception:
                pass
        # event.json 富上下文（背景档案）
        try:
            from src.api.event_archive import build_issue_context
            _ctx = build_issue_context(repo_key, _iid.lstrip("#")) if _iid else ""
            if _ctx:
                parts.append(f"\n## Issue 档案摘要\n{_ctx}")
        except Exception:
            pass
    except Exception:
        pass

    _sp = skill_path or os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    parts.append(
        f"## Skill: {skill_name}\n"
        f"Skill 文件：{_sp}\n"
        f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令（不得跳过读取或凭名称猜测）。\n"
        f"SKILL.md 中如指引读取 references/ 目录下的文件，请按需读取后再执行。"
    )

    parts.append("## 角色定义")
    parts.append(_VOD_ROLE_DEFINITION)

    try:
        agents_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "agent-description.json")
        if os.path.isfile(agents_file):
            with open(agents_file, encoding="utf-8") as f:
                registry = json.load(f)
            parts.append("\n## Available Agents")
            for ag, info in registry.items():
                desc = info.get("description", "")
                parts.append(f"- {ag}：{desc}")
    except Exception:
        pass

    # 替换 {{SERVER_URL}} 为服务端可达地址（A2A/tasks 等对外调用；worker 端会再替换一次，幂等）
    try:
        from src.prompt_builder import _resolve_server_url
        return "\n".join(parts).replace("{{SERVER_URL}}", _resolve_server_url())
    except Exception:
        return "\n".join(parts)


async def _execute_vod_agent(repo_key: str, agent_session: dict, conv: dict, evt: dict) -> str:
    runtime_session_id = conv.get("runtime_session_id", "")
    backend = agent_session.get("backend", "opencode")
    skill = agent_session.get("skill", "vod")
    skill_path = ""
    if agent_session.get("skill_id"):
        try:
            from src.user.db import get_skill_path
            skill_path = get_skill_path(agent_session.get("skill_id"))
        except Exception:
            pass
    repo_token = agent_session.get("repo_gitcode_token", "") or ""
    if not repo_token:
        try:
            from src.user.db import get_gitcode_token
            repo_token = get_gitcode_token(repo_key=repo_key) or ""
        except Exception:
            pass

    prompt = _build_vod_prompt(evt, skill, skill_path)
    from src.repo_key import from_repo_key
    wsd = os.path.join(WORKSPACE_DIR, "vod", from_repo_key(repo_key).replace("/", "_"))
    os.makedirs(wsd, exist_ok=True)

    final_text = ""
    new_oc_sid = runtime_session_id
    exit_code = -1
    start_time = time.time()
    mode = get_execution_mode()
    if mode != "direct":
        # 入队 + 轮询：任务由 RuntimeAgent（本地/远端边端）claim 执行（见 multi-runtime-instance-design）
        from src.runtime.service import enqueue_task, wait_task_terminal
        from src.user.db import get_runtime_id_by_name, ensure_default_runtime
        runtime_id = get_runtime_id_by_name(backend) or ensure_default_runtime(backend) or 0
        if runtime_id:
            tid = enqueue_task(
                agent_session_id=f"vod:{repo_key}",
                runtime_id=runtime_id,
                event_seq=None, issue_id=evt.get("issue_id", ""),
                repo_key=repo_key, agent_type="vod", skill=skill,
                prompt=prompt, cwd=wsd,
                runtime_session_id=runtime_session_id,
                event_type=evt.get("type", "vod_signal"),
                account=repo_key, user_id=0,
                repo_gitcode_token=repo_token or "",
                skill_path=skill_path or "",
            )
            log.info(f"[VOD agent|repo:{repo_key}] enqueued task={tid} mode={mode}")
            task = await wait_task_terminal(tid)
            final_text = task.get("result_text") or ""
            exit_code = int(task.get("exit_code") or -1)
            try:
                usage = json.loads(task.get("usage_json") or "{}") or {}
                new_oc_sid = usage.get("session_id") or runtime_session_id
            except Exception:
                new_oc_sid = runtime_session_id
            log.info(f"[VOD agent|repo:{repo_key}] task done: exit={exit_code}, text_len={len(final_text)}")
        else:
            log.warning(f"[VOD agent|repo:{repo_key}] no runtime for backend={backend}, task skipped")
            final_text = ""
            exit_code = -1
    else:
        async for event_type, data in consume_stream_async(prompt, wsd, runtime_session_id, skill,
                                                           backend_name=backend, account=repo_key,
                                                           bot_token=repo_token, agent_name="VOD Agent",
                                                           agent_label=f"[Agent:vod|repo:{repo_key}]"):
            if event_type == "done":
                final_text = data.get("text", "")
                exit_code = data.get("exit_code", -1)
                new_sid = data.get("oc_session_id") or data.get("runtime_session_id")
                if exit_code == 0 and new_sid:
                    new_oc_sid = new_sid
                log.info(f"[VOD agent|repo:{repo_key}] done: exit={exit_code}, text_len={len(final_text)}")
            elif event_type == "error":
                log.warning(f"[VOD agent|repo:{repo_key}] error: {data}")
            elif event_type == "step_start":
                log.info(f"[VOD agent|repo:{repo_key}] {data}")
            elif event_type == "text":
                log.info(f"[VOD agent|repo:{repo_key}] TEXT: {str(data)[:200]}")
            elif event_type == "reasoning":
                log.info(f"[VOD agent|repo:{repo_key}] REASONING: {str(data)[:200]}")
            elif event_type == "tool_use":
                log.info(f"[VOD agent|repo:{repo_key}] TOOL: {str(data)[:200]}")
            elif event_type == "step_finish":
                log.info(f"[VOD agent|repo:{repo_key}] {data}")
            else:
                log.info(f"[VOD agent|repo:{repo_key}] {event_type}: {str(data)[:200]}")

    duration = time.time() - start_time

    # 仅当创建了 Issue 时才写入 events 审计记录
    if final_text and re.search(r'/issues/\d+', final_text):
        from src.api.event_store import append_event, update_event_complete
        user_msg = evt.get("payload", {}).get("user_message", "")
        actor = evt.get("actor", "unknown")
        m = re.search(r'/issues/(\d+)', final_text)
        result_text = f"采纳建议创建了 Issue #{m.group(1) if m else '?'}"
        vod_issue_id = f"#{m.group(1)}" if m else "#0"
        event_seq = append_event(
            repo_key=repo_key, event_type="vod_signal", source="vod_im",
            agent_type="vod", actor=actor, title=user_msg[:100],
            issue_id=vod_issue_id, summary=result_text, payload=None,
            status="completed", result=result_text
        )
        if event_seq:
            update_event_complete(event_seq, "completed", exit_code=exit_code,
                                  duration=duration, completed_at=datetime.utcnow().isoformat(),
                                  result=result_text)

    chat_id = evt.get("payload", {}).get("im_chat_id", "")
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE vod_conversations SET runtime_session_id=?, last_active_at=datetime('now') WHERE im_chat_id=?",
                      (new_oc_sid, chat_id))
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
                    c.execute("SELECT agent_session_id FROM vod_conversations WHERE im_chat_id=?", (chat_id,))
                    row = c.fetchone()
                if row:
                    with db_lock:
                        c = db.cursor()
                        c.execute("SELECT repo_key FROM devops_agent_session WHERE id=?", (row[0],))
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
            if agent_session["im_app_id"] != ctx.app_id:
                await api.reply_text(ctx, msg_id, f"❌ 仓库使用了其他机器人凭证")
                return
            _get_or_create_conversation(chat_id, agent_session["id"])
            await api.reply_text(ctx, msg_id, f"✅ 已绑定 `{repo_key}`")
            return

        if cmd == "/unbind":
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("DELETE FROM vod_conversations WHERE im_chat_id=?", (chat_id,))
                    db.commit()
                await api.reply_text(ctx, msg_id, "✅ 已解绑")
            except Exception as e:
                await api.reply_text(ctx, msg_id, f"❌ 解绑失败: {e}")
            return

        if cmd in ("/clean", "/reset"):
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE vod_conversations SET runtime_session_id='' WHERE im_chat_id=?", (chat_id,))
                    db.commit()
                await api.reply_text(ctx, msg_id, "✅ 已清除会话")
            except Exception as e:
                await api.reply_text(ctx, msg_id, f"❌ 清除失败: {e}")
            return

        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT agent_session_id, runtime_session_id FROM vod_conversations WHERE im_chat_id=?",
                          (chat_id,))
                row = c.fetchone()
        except Exception as e:
            log.warning(f"[VOD:{ctx.name}] conv query error: {e}")
            row = None

        log.info(f"[VOD:{ctx.name}] chat_id={chat_id[:30]}, conv_row={row}")

        if not row:
            repos = _list_vod_repos(ctx.app_id)
            log.info(f"[VOD:{ctx.name}] no conv, vod_repos={[r['repo_key'] for r in repos]}")
            if len(repos) == 0:
                await api.reply_text(ctx, msg_id, "❌ 未绑定仓库，请联系管理员配置")
                return
            elif len(repos) == 1:
                agent_session = _get_vod_session(repos[0]["repo_key"])
                if agent_session:
                    conv = _get_or_create_conversation(chat_id, agent_session["id"])
                    if conv.get("is_new"):
                        await api.reply_text(ctx, msg_id, f"✅ 已自动绑定 `{repos[0]['repo_key']}`")
                    row = (agent_session["id"], conv["runtime_session_id"])
                else:
                    await api.reply_text(ctx, msg_id, "❌ 自动绑定失败")
                    return
            else:
                repo_list = "\n".join([f"  - `{r['repo_key']}`" for r in repos])
                await api.reply_text(ctx, msg_id,
                    f"已绑定以下仓库：\n{repo_list}\n请发送 `/bind owner/repo` 选择")
                return

        agent_session_id = row[0]
        runtime_session_id = row[1] or ""

        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT repo_key, runtime_id, skill, skill_id, repo_gitcode_token FROM devops_agent_session WHERE id=?",
                          (agent_session_id,))
                session_row = c.fetchone()
        except Exception:
            session_row = None

        if not session_row:
            log.warning(f"[VOD:{ctx.name}] agent_session_id={agent_session_id} not found, auto-rebind")
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM vod_conversations WHERE im_chat_id=?", (chat_id,))
                db.commit()
            repos = _list_vod_repos(ctx.app_id)
            if len(repos) == 0:
                await api.reply_text(ctx, msg_id, "❌ 未绑定仓库，请联系管理员配置")
                return
            agent_session = _get_vod_session(repos[0]["repo_key"])
            if not agent_session:
                await api.reply_text(ctx, msg_id, "❌ 自动绑定失败")
                return
            conv = _get_or_create_conversation(chat_id, agent_session["id"])
            agent_session_id = agent_session["id"]
            runtime_session_id = ""
            repo_key = repos[0]["repo_key"]
            agent_session = {"backend": agent_session.get("backend", "opencode"),
                             "skill": agent_session.get("skill", "vod"),
                             "skill_id": agent_session.get("skill_id", 0) or 0,
                             "repo_gitcode_token": agent_session.get("repo_gitcode_token", "")}
        else:
            repo_key = session_row[0]
            backend = "opencode"
            if session_row[1]:
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (session_row[1],))
                    brow = c.fetchone()
                    if brow and brow[0]:
                        backend = brow[0]
            agent_session = {"backend": backend, "skill": session_row[2] or "vod",
                             "skill_id": session_row[3] or 0,
                             "repo_gitcode_token": session_row[4] or ""}

        evt = {
            "type": "vod_signal", "repository": repo_key, "source": "vod_im",
            "actor": sender_open_id[:20] if sender_open_id else "unknown",
            "branch": "",
            "payload": {"user_message": text, "im_chat_id": chat_id, "im_sender_id": sender_open_id},
        }

        reply = await _execute_vod_agent(repo_key, agent_session, {"id": 0, "runtime_session_id": runtime_session_id}, evt)

        if reply and reply != "SKIP":
            await api.reply_card(ctx, msg_id, reply[:2000], title="VOD Agent", template="green")

    except Exception as e:
        log.error(f"[VOD:{ctx.name}] process error: {e}")
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错")
        except:
            pass