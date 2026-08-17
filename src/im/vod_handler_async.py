"""异步 VOD 消息处理 — 支持 BotContext 隔离的 VOD Bot 消息分发

VOD Bot 职责：收集反馈、创建 Issue、查询进展，不修改代码/创建PR。
"""
import json, os, re, asyncio, time
from datetime import datetime
from typing import TYPE_CHECKING

from src.config import log, WORKSPACE_DIR, SKILLS_DIR
from src.session_manager import db, db_lock
from src.runtime.executor import consume_stream_async
from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT

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
            backend = ""
            if row[2]:
                c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[2],))
                brow = c.fetchone()
                if brow and brow[0]:
                    backend = brow[0]
            return {"id": row[0], "repo_key": row[1], "runtime_id": row[2] or 0, "backend": backend,
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


def _current_vod_repo(chat_id: str) -> str:
    """当前群绑定的 VOD 仓库（vod_conversations → devops_agent_session）"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT agent_session_id FROM vod_conversations WHERE im_chat_id=?", (chat_id,))
            row = c.fetchone()
            if row:
                r2 = c.execute("SELECT repo_key FROM devops_agent_session WHERE id=?", (row[0],)).fetchone()
                if r2:
                    return r2[0]
    except Exception:
        pass
    return ""


async def _send_vod_repo_card(ctx: "BotContext", api: "FeishuAsync", chat_id: str, msg_id: str, page: int = 1) -> bool:
    """生成并发送 `/repo` 卡片（候选=当前 bot 名下已配置 VOD 的仓库），设置 pending。无候选返回 False。"""
    from src.im.handler_async import _set_pending_selection, _candidate_card, _PENDING_SELECT_PAGE_SIZE
    from src.repo_key import from_repo_key
    repos = _list_vod_repos(ctx.app_id)
    if not repos:
        return False
    bound = _current_vod_repo(chat_id)
    total_pages = max(1, -(-len(repos) // _PENDING_SELECT_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    items = []
    for r in repos[(page - 1) * _PENDING_SELECT_PAGE_SIZE: page * _PENDING_SELECT_PAGE_SIZE]:
        full = from_repo_key(r["repo_key"])
        marker = "【绑定中】" if r["repo_key"] == bound else ""
        items.append(f"{full}{marker}")
    _set_pending_selection(chat_id, {"action": "vod_repo", "items": repos, "page": page})
    await api.reply_card(ctx, msg_id, _candidate_card("📁", f"VOD 仓库列表（共 {len(repos)} 个）", items, page, total_pages, "/repo"))
    return True


async def _handle_vod_selection(ctx: "BotContext", api: "FeishuAsync", chat_id: str, msg_id: str,
                                num: str, pending: dict):
    """卡片命令编号选择执行（vod-navigation-commands-design.md §3.2，action=vod_repo）。"""
    try:
        from src.im.handler_async import _clear_pending_selection, _PENDING_SELECT_PAGE_SIZE
        from src.repo_key import from_repo_key
        if num == "0":
            _clear_pending_selection(chat_id)
            await api.reply_card(ctx, msg_id, "**✅ 已取消选择**")
            return
        items = pending.get("items") or []
        page = int(pending.get("page") or 1)
        page_size = int(pending.get("page_size") or _PENDING_SELECT_PAGE_SIZE)
        idx = (page - 1) * page_size + (int(num) - 1)
        if idx < 0 or idx >= len(items):
            await api.reply_card(ctx, msg_id, f"**⚠️ 无效编号 {num}**\n回复 `0` 取消。")
            return
        repo = items[idx]
        agent_session = _get_vod_session(repo["repo_key"])
        if not agent_session or agent_session["im_app_id"] != ctx.app_id:
            await api.reply_card(ctx, msg_id, "**❌ 该仓库未配置当前 bot 的 VOD Agent**")
            return
        # 绑定/换绑 vod_conversations（vod-navigation-commands-design.md §3.4）：
        # 已存在 → UPDATE agent_session_id + 清空 runtime_session_id（防旧仓库 opencode 上下文污染）；不存在 → INSERT
        from src.repo_key import from_repo_key
        with db_lock:
            c = db.cursor()
            existing = c.execute("SELECT id FROM vod_conversations WHERE im_chat_id=?", (chat_id,)).fetchone()
            if existing:
                c.execute(
                    "UPDATE vod_conversations SET agent_session_id=?, runtime_session_id='' WHERE im_chat_id=?",
                    (agent_session["id"], chat_id))
            else:
                c.execute(
                    "INSERT INTO vod_conversations (agent_session_id, im_chat_id, im_channel_name) VALUES (?,?,?)",
                    (agent_session["id"], chat_id, ""))
            db.commit()
        _clear_pending_selection(chat_id)
        await api.reply_card(ctx, msg_id, f"**✅ 已绑定 `{from_repo_key(repo['repo_key'])}`**\n后续消息将进入该仓库的 VOD 会话。")
    except Exception as e:
        log.warning(f"[VOD:{ctx.name}] selection error: {e}")
        try:
            await api.reply_text(ctx, msg_id, f"❌ 选择处理失败: {e}")
        except Exception:
            pass


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
    backend = agent_session.get("backend", "")
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
    # engine-selection-design.md v2：按引擎集合/会话主引擎选引擎；无引擎 → 明确不可用，不隐式 opencode
    from src.runtime.engine_select import pick_engine, engine_name
    engine_id = pick_engine(repo_key, "vod", fallback_runtime_id=agent_session.get("runtime_id") or 0) or 0
    if not engine_id:
        log.warning(f"[VOD agent|repo:{repo_key}] no available engine, skip")
        return "⚠️ 无可用引擎（该仓库 VOD Agent 未配置本机直连或远端引擎），请先在 Agent 配置中设置引擎。"
    _be = engine_name(engine_id) or backend
    kind = runtime_execution_kind(engine_id)
    if kind != EXEC_MODE_DIRECT:
        # 入队 + 轮询：任务由远端 RuntimeAgent claim 执行（见 multi-runtime-instance-design）
        from src.runtime.service import enqueue_task, wait_task_terminal
        runtime_id = engine_id
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
            log.info(f"[VOD agent|repo:{repo_key}] enqueued task={tid} mode={kind}")
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
                                                            backend_name=_be, account=repo_key,
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

        # 即时反馈：进处理前先加 OK 反应（对齐 chat bot，避免 agent 执行期间无任何回应）
        try:
            await api.add_reaction(ctx, msg_id, "OK")
        except Exception:
            pass

        cmd_parts = text.split()
        cmd = cmd_parts[0].lower() if cmd_parts else ""

        # 卡片命令编号选择（vod-navigation-commands-design.md §3.2）：纯数字 + 有 vod_repo pending → 执行选择
        if text.isdigit():
            from src.im.handler_async import _get_pending_selection
            pending = _get_pending_selection(chat_id)
            if pending and pending.get("action") == "vod_repo":
                await _handle_vod_selection(ctx, api, chat_id, msg_id, text, pending)
                return

        if cmd == "/help":
            await api.reply_card(ctx, msg_id,
                "**🤖 VOD Bot 命令**\n"
                "/repo — 列出并绑定仓库（回复编号）\n"
                "/where — 当前位置\n"
                "/help — 本帮助\n\n"
                "回复数字选择 · `0` 取消")
            return

        if cmd in ("/where", "/status"):
            # 与 chat 一致用 /where（/status 兼容别名，vod-navigation-commands-design.md）
            repo = _current_vod_repo(chat_id)
            if repo:
                await api.reply_card(ctx, msg_id, f"**📍 当前位置**\n仓库：`{repo}`")
            else:
                await api.reply_card(ctx, msg_id, "**📍 当前位置**\n仓库：未绑定（回复 `/repo` 选择绑定）")
            return

        if cmd == "/repo":
            if await _send_vod_repo_card(ctx, api, chat_id, msg_id, 1):
                return
            await api.reply_card(ctx, msg_id, "**📁 当前 bot 名下暂无已配置 VOD 的仓库**\n请联系管理员配置。")
            return

        if cmd == "/bind" and len(cmd_parts) >= 2:
            # 弃用兼容（vod-navigation-commands-design.md）：建议用 /repo 编号选择
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
                await api.reply_card(ctx, msg_id, "**⚠️ 未绑定仓库**\n当前 bot 名下暂无已配置 VOD 的仓库，请联系管理员配置。")
                return
            elif len(repos) == 1:
                agent_session = _get_vod_session(repos[0]["repo_key"])
                if agent_session:
                    conv = _get_or_create_conversation(chat_id, agent_session["id"])
                    await api.reply_card(ctx, msg_id, f"**✅ 已自动绑定**\n`{repos[0]['repo_key']}`")
                    row = (agent_session["id"], conv["runtime_session_id"])
                else:
                    await api.reply_card(ctx, msg_id, "**❌ 自动绑定失败**")
                    return
            else:
                # ≥2 仓库：自动弹 /repo 卡片（编号选择，替代纯文本列表，vod-navigation-commands-design.md §3.3）
                if await _send_vod_repo_card(ctx, api, chat_id, msg_id, 1):
                    return
                await api.reply_card(ctx, msg_id, "**⚠️ 未绑定仓库**\n请联系管理员配置。")
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
            agent_session = {"backend": agent_session.get("backend", ""),
                             "skill": agent_session.get("skill", "vod"),
                             "skill_id": agent_session.get("skill_id", 0) or 0,
                             "repo_gitcode_token": agent_session.get("repo_gitcode_token", "")}
        else:
            repo_key = session_row[0]
            backend = ""
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

        if reply == "SKIP":
            # agent 主动跳过（对齐 chat），不回
            pass
        elif reply:
            await api.reply_card(ctx, msg_id, reply[:2000], title="VOD Agent", template="green")
        else:
            # 空回复（无引擎/出错/未生成）→ 明确提示，避免静默无回应（对齐 chat）
            await api.reply_card(ctx, msg_id, "⚠️ 处理完成但未生成回复，请稍后重试或换个说法。", title="VOD Agent", template="green")

    except Exception as e:
        log.error(f"[VOD:{ctx.name}] process error: {e}")
        try:
            if msg_id:
                await api.reply_text(ctx, msg_id, "❌ 处理消息时出错")
        except:
            pass