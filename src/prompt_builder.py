"""Prompt 构建：角色定义、技能能力列表、事件 prompt、飞书 prompt"""
import json, os
from src.config import SKILLS_DIR, WORKSPACE_DIR, DB_PATH

# 部署根目录（历史遗留路径 devops-bot 已迁至 grape；统一由 DB_PATH 推导）
_DEPLOY_ROOT = os.path.dirname(os.path.dirname(DB_PATH))

EMOJI_MAP = {
    "code-review": "📦 代码审查", "build-debug": "🔧 构建诊断",
    "deploy-guard": "🛡️ 部署守卫", "incident-triage": "🚨 事件分诊",
    "security-scan": "🔒 安全扫描", "changelog-gen": "📝 变更日志",
    "comment": "💬 仓库评论回复",
    "chat": "🗨️ 飞书对话", "issue": "📋 Issue 运营", "pr": "🔄 PR 运营",
}


def get_skill_capabilities() -> str:
    lines = ["我是 DevOps Bot，专注于以下领域：\n"]
    try:
        for entry in sorted(os.listdir(SKILLS_DIR)):
            skill_dir = os.path.join(SKILLS_DIR, entry)
            if not os.path.isdir(skill_dir) or entry.startswith("."):
                continue
            md_path = os.path.join(skill_dir, "SKILL.md")
            if not os.path.exists(md_path):
                continue
            display = EMOJI_MAP.get(entry, entry)
            desc = ""
            with open(md_path, encoding="utf-8") as f:
                content = f.read()
            if "description: |" in content:
                desc_block = content.split("description: |", 1)[1].split("---", 1)[0].strip()
                desc = desc_block.split("\n")[0].strip().rstrip(".。").strip('"').strip("'")
            lines.append(f"  {display}")
            if desc:
                lines[-1] += f" — {desc}"
        lines.append("\n我的能力范围仅限于 DevOps 相关领域。")
        return "\n".join(lines)
    except Exception as e:
        return "我是 DevOps Bot，专注于 DevOps 相关领域。"


def _resolve_server_url() -> str:
    """返回服务端可达地址（A2A/tasks 等对外调用用）。取 meta.server_base_url（仅基址），
    无则从 webhook_url 剥后缀兜底；再兜底 localhost:8080。"""
    try:
        from src.user.db import get_server_base_url
        return get_server_base_url()
    except Exception:
        pass
    return "http://localhost:8080"


def _build_history_file_guide(repo_key: str, account: str = "", runtime_id: int = 0) -> str:
    """Build prompt section listing available history files."""
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_REMOTE
    if runtime_execution_kind(runtime_id) == EXEC_MODE_REMOTE:
        # 远端模式：本地历史文件不可达，历史上下文改由 event.json 档案承载
        #（见 multi-issue-isolation-design.md §4.2 / skill-delivery-remote.md §3.4）
        return ""
    repo_name = repo_key.replace("gitcode:", "").replace("/", "_")
    if not account:
        account = repo_key.replace("gitcode:", "")
    base = f"{WORKSPACE_DIR}/history/{account}/{repo_name}/"
    from src.session_manager import get_history_file_list
    files = get_history_file_list(repo_key, account)
    if files:
        lines = ["Session history files (newest first):"]
        for f in files:
            lines.append(f"  {f}")
        lines.append("Use the read tool to access any of these files when you need context about past events.")
        return "\n".join(lines)
    return f"Session history file: {base}session_history.json (none yet)"


_PROJECT_INFO_MAX_CHARS = 2500


def _build_project_info_section(repo_key: str, agent_type: str = "") -> str:
    """注入 repo 全局项目知识基线（服务端权威、多 VM 共享，见 multi-issue-isolation-design.md §2.4/§4.2）。

    - 内联摘要（截断）+ {{PROJECT_INFO_DIR}} 占位符（worker/本地落地后可用 read tool 读全文）
    - agent_type 非空 → 追加「本 Agent 专属知识」路径指引（knowledge-board-design.md §六）
    - 无基线 → 明确允许降级分析
    """
    try:
        from src.runtime.service import load_project_info
        files, meta = load_project_info(repo_key)
    except Exception:
        files, meta = {}, {}
    lines = ["\n## 项目知识基线（repo 全局）"]
    if not files:
        lines.append("[无项目知识基线] 允许降级分析：可基于 README 与 GitCode API 获取基础信息，相关结论标注 [degraded]。")
        if agent_type:
            lines.append(f"> 项目级成员知识：{{{{PROJECT_INFO_DIR}}}}/member/，存在则用 read tool 读取并遵循。")
            lines.append(f"> 本 Agent 专属知识：{{{{PROJECT_INFO_DIR}}}}/agent/{agent_type}/，存在则用 read tool 读取并遵循。")
        return "\n".join(lines)
    _meta = meta or {}
    if _meta.get("degraded"):
        lines.append("> ⚠️ 该基线为降级生成（codegraph 不可用），仅供基础参考。")
    _sha = _meta.get("last_commit_sha") or ""
    _upd = _meta.get("last_updated") or ""
    if _sha or _upd:
        lines.append(f"> 基线 commit={_sha or '-'} updated={_upd or '-'}")
    for rel, label in (("project-graph.md", "Project Graph"), ("project-capabilities.md", "Project Capabilities")):
        content = files.get(rel, "")
        if not content:
            continue
        truncated = len(content) > _PROJECT_INFO_MAX_CHARS
        body = content[:_PROJECT_INFO_MAX_CHARS] + ("\n...(已截断)" if truncated else "")
        lines.append(f"### {label}\n{body}")
    lines.append("> 全文副本已落地：{{PROJECT_INFO_DIR}}/project-graph.md 与 {{PROJECT_INFO_DIR}}/project-capabilities.md，可用 read tool 读取全文。")
    if agent_type:
        lines.append(f"> 项目级成员知识：{{{{PROJECT_INFO_DIR}}}}/member/，存在则用 read tool 读取并遵循。")
        lines.append(f"> 本 Agent 专属知识：{{{{PROJECT_INFO_DIR}}}}/agent/{agent_type}/，存在则用 read tool 读取并遵循。")
    return "\n".join(lines)


def build_role_definition(skills=None) -> str:
    rules = [
        "You are a DevOps operations assistant. Your domain:",
        "- Code review, build diagnosis, deployment guard, incident triage",
        "- Issue/PR analysis, changelog generation, repository management",
        "- Repository (GitCode/GitHub) API operations",
        "",
        "You have no abilities outside DevOps (no weather, email, documents, "
        "PDF, Excel, writing, meetings, voice, etc.).",
        "Reply rules:",
        "1. User @mentioned you: always reply politely, state you only do DevOps",
        "2. User NOT @mentioned + DevOps topic: analyze and reply",
        "",
        "SECURITY (MUST FOLLOW):",
        "1. Use ONLY the GitCode token provided in this prompt (BOT_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.",
        "2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data."
        "3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs admin/maintainer permission (configure it manually in GitCode). NEVER try other tokens or workarounds.",
        "4. NEVER write any token into files, logs, or comments.",
    ]
    if skills and "implementation" in skills:
        rules.append("3. You MAY write code, modify files, create branches, and execute git commands — within the scope of the current Issue")
    else:
        rules.append("3. Never write code, modify files, or execute git commands")
    rules.append("4. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply.")
    return "\n".join(rules)


def _build_feishu_context_section(repo_key: str, ctx: dict) -> str:
    """Build the Feishu notification context for prompt injection."""
    lines = ["\n## Feishu Notification Capability\n"]

    if ctx.get("token"):
        lines.append(f"Feishu tenant token: {ctx['token']}")

    # Collaborators list
    collabs = ctx.get("collaborators", [])
    if collabs:
        lines.append("\n### Repository Collaborators")
        for m in collabs:
            lines.append(f"  {m.get('username','?')}  nick={m.get('nick_name','?')}  role={m.get('role_name_cn','?')}  id={m.get('id','?')}")

    # Group members
    group = ctx.get("group_members", [])
    if group:
        lines.append("\n### Feishu Group Members (name -> open_id)")
        for m in group:
            lines.append(f"  {m.get('name','?')}  ->  {m.get('open_id','?')}")

    # Known mappings
    mappings = ctx.get("mappings", {})
    if mappings:
        lines.append("\n### Known VCS -> Feishu Mappings (cached in unified_user_mapping table)")
        for gitcode_user, info in mappings.items():
            lines.append(f"  {gitcode_user}  ->  {info['open_id']}")

    # Owner info
    owner_login = ctx.get("owner_login", "")
    owner_nick = ctx.get("owner_nick", "")
    if owner_login:
        owner_open_id = ""
        if mappings and owner_login in mappings:
            owner_open_id = mappings[owner_login]["open_id"]
        elif group:
            for m in group:
                if m.get("name") == owner_nick:
                    owner_open_id = m.get("open_id", "")
                    break
        lines.append(f"\n### Repo Owner\n  {owner_login} (nick={owner_nick})  open_id={owner_open_id}")

    # Auto implementation status
    auto_enabled = ctx.get("auto_impl_enabled", False)
    lines.append(f"\n### Auto Implementation\n  Status: {'ON' if auto_enabled else 'OFF (default)'}")
    if not auto_enabled:
        lines.append("  - Bug: categorize + assign + notify only, do NOT add PATCH label, do NOT auto-fix")
        lines.append("  - Feature: categorize + assign + notify only, do NOT add PATCH label, collect feedback")
        lines.append("  - To enable: send /autodev on in the bound Feishu group")
    else:
        lines.append("  - Bug: categorize + assign + notify + add PATCH label -> auto-fix")
        lines.append("  - Feature: categorize + assign + notify + await approval + add PATCH -> auto-develop")

    # Notification status
    notify_enabled = ctx.get("notify_enabled", False)
    lines.append(f"\n### Feishu Notification\n  Status: {'ON' if notify_enabled else 'OFF (default)'}")
    if not notify_enabled:
        lines.append("  - Notifications are disabled. Do NOT send any Feishu notification cards.\n  - To enable: send /notify on in the bound Feishu group")
    else:
        lines.append("  - Notifications are enabled. Send Feishu notification cards as instructed.")

    lines.append("\n### Card Content Rule\n  When mentioning a repository user in Feishu notification cards, ALWAYS use `nick_name` from Collaborators (Chinese real name), NEVER use `username`/`login` (English ID).\n  Example: \u5ed6\u5829\u54f2 NOT Jason7602\u3001\u5434\u626c NOT younger_eric")

    # API instructions
    lines.append(f"""
### Send Notification Card
curl -sL -X POST \\
  -H "Authorization: Bearer {ctx.get('token','YOUR_TOKEN')}" \\
  -H "Content-Type: application/json" \\
  -d '{{"receive_id":"OPEN_ID","msg_type":"interactive","content":"{{\\"header\\":{{\\"title\\":{{\\"tag\\":\\"plain_text\\",\\"content\\":\\"DevOps Bot\\"}},\\"template\\":\\"blue\\"}},\\"elements\\":[{{\\"tag\\":\\"div\\",\\"text\\":{{\\"tag\\":\\"lark_md\\",\\"content\\":\\"MESSAGE\\"}}}}]}}"}}' \\
  "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"

### Save User Mapping (INSERT into unified_user_mapping)
Use Python to save discovered mappings in the bot's sessions.db:
  import sqlite3; db = sqlite3.connect('{DB_PATH}')
  db.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id,gitcode_user,feishu_open_id,feishu_name,updated_at) VALUES (?,?,?,?,datetime('now'))", (USER_ID, GITCODE_USER, OPEN_ID, NAME))
  db.commit(); db.close()
  # 若不知道 user_id，可用 0 占位，后续由 sync_repo_feishu_mapping 回填

### Name Resolution Workflow
1. Determine target user (vcs_login, e.g. "Jason7602")
2. If in Known Mappings -> use open_id directly, send notification
3. Else: find user's nick_name from Collaborators list above
4. Match nick_name against Group Members name -> get open_id
5. Match found -> save to unified_user_mapping -> send notification
   6. Match not found -> notify Owner: "{{login}}({{nick_name}})" in Feishu group, ask to check nickname consistency
7. Card content rules: max 2000 chars, no ```code blocks, use [text](url) for links
""")
    return "\n".join(lines)


def build_chat_role_definition() -> str:
    caps = get_skill_capabilities()
    return (
        "You are a DevOps operations assistant. Your domain:\n"
        "- Code review, build diagnosis, deployment guard, incident triage\n"
        "- Issue/PR analysis, changelog generation, repository management\n"
        "- Repository (GitCode/GitHub) API operations\n"
        "\n"
        "You have no abilities outside DevOps (no weather, email, documents, "
        "PDF, Excel, writing, meetings, voice, etc.).\n"
        f"Your capabilities:\n{caps}\n\n"
        "Reply rules:\n"
        "1. DevOps topic: analyze and reply\n"
        "2. NOT DevOps topic: politely explain you only handle DevOps, list your capabilities above\n"
        "3. Never write code, modify files, or execute git commands\n"
        "4. Always reply in Chinese. Never include internal reasoning or process steps.\n"
        "\n"
        "SECURITY (MUST FOLLOW):\n"
        "1. Use ONLY the GitCode token provided in this prompt (USER_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.\n"
        "2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.\n"
        "3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs the required permission (configure it manually in GitCode). NEVER try other tokens or workarounds.\n"
        "4. NEVER write any token into files, logs, or comments."
    )


def extract_event_number(payload):
    for key in ["issue", "pull_request", "merge_request"]:
        obj = payload.get(key, {})
        if isinstance(obj, dict):
            if "number" in obj:
                return obj["number"]
            if "iid" in obj:
                return obj["iid"]
    obj_attrs = payload.get("object_attributes", {})
    if isinstance(obj_attrs, dict):
        if "noteable_iid" in obj_attrs:
            return obj_attrs["noteable_iid"]
        if "iid" in obj_attrs:
            return obj_attrs["iid"]
        if "id" in obj_attrs:
            return obj_attrs["id"]
    if "number" in payload:
        return payload["number"]
    return None


def build_prompt(evt, history, oc_session_id, skills, feishu_ctx=None, skill_path="", runtime_id=0,
                 agent_type=""):
    number = extract_event_number(evt['payload'])
    repo = evt['repository']
    payload = evt['payload']
    issue_data = {}
    if isinstance(payload.get("issue"), dict):
        issue_data = payload["issue"]
    elif isinstance(payload.get("object_attributes"), dict):
        issue_data = payload["object_attributes"]
    title = json.dumps(issue_data.get("title", ""), ensure_ascii=False)
    body = json.dumps(issue_data.get("body", issue_data.get("description", "")), ensure_ascii=False)[:500]
    parts = []
    evt_type = evt.get("type", "event")

    # A2A context injection —— Issue 编号放在最主要位置
    a2a_ctx = evt.get("_a2a_context", "")
    if a2a_ctx:
        issue_num = evt.get("issue_id", "") or ""
        if issue_num:
            parts.append(f"## 任务 Issue：{issue_num}\n")
            parts.append(f"了解任务背景和其它Agent处理过程，请到Issue查看。\n")
        parts.append("## 前序 Agent 处理摘要\n")
        parts.append(a2a_ctx)
        pr_num = evt.get("payload", {}).get("pr_number", "")
        if pr_num:
            parts.append(f"A2A 目标 PR：#{pr_num}\n")
        parts.append("请基于以上上下文继续处理。\n")

    if evt_type == "issue_comment":
        note = ""
        if isinstance(payload.get("object_attributes"), dict):
            note = payload["object_attributes"].get("note", "")
        prefix = f"User commented on issue #{number} of {repo}" if number is not None else f"User commented on {repo}"
        parts.append(prefix)
        if note:
            parts.append(f'Comment: {json.dumps(note, ensure_ascii=False)[:1000]}')
            # 检测是否 @ 了 Bot
            if any(f"@{u}" in note for u in __import__("src.user.db", fromlist=["get_all_bot_usernames"]).get_all_bot_usernames()):
                parts.append("(User @mentioned the bot)")
        if number is None:
            parts.append("\n⚠️ 本评论事件未携带 Issue 编号。你必须先通过 GitCode API 定位该评论落在哪个 Issue：")
            parts.append(f"  查询仓库 {repo} 最近的 Issue 评论（GET /repos/{repo.split('/')[0]}/{repo.split('/')[-1]}/issues/{{n}}/comments），")
            parts.append("  用评论内容匹配确认 Issue 编号，然后对用户评论进行回复。")
            parts.append("  注意：即使缺少编号，也必须对用户评论作出回复，不得静默跳过。")
        parts.append("\n必须回复该评论：使用 POST /repos/{owner}/{repo}/issues/{n}/comments，body 按你的技能规定的评论前缀开头，并引用用户评论原文。")
    elif evt_type == "vod_signal":
        user_msg = payload.get("user_message", "")
        user_name = evt.get("actor", "未知用户")
        parts.append(f"## 用户消息\n来自 {user_name} 在仓库 {repo}")
        parts.append(f"消息内容: {user_msg[:2000]}")
    else:
        if number is not None:
            parts.append(f"Event: {evt_type} #{number} on {repo}")
        else:
            parts.append(f"Event: {evt_type} on {repo}")
        if title != '""':
            parts.append(f"Title: {title}")
        if body != '""':
            parts.append(f"Body: {body}")

    if history:
        parts.append(f"This repository has {len(history)} past events recorded.")

    try:
        from src.api.event_store import get_issue_summary
        repo_key = f"{evt['source']}:{evt['repository']}"
        issue_summary = get_issue_summary(repo_key)
        if issue_summary:
            parts.append(issue_summary)
    except Exception:
        pass

    repo_key = f"{evt['source']}:{evt['repository']}"
    account = evt.get("account") or evt.get("repository", "")
    parts.append(_build_history_file_guide(repo_key, account, runtime_id))
    parts.append(_build_project_info_section(repo_key, agent_type or (skills[0] if skills else "")))

    if skills:
        # 技能路径占位符：worker 替换为随任务下发的物化目录；direct 由 executor 替换为服务端 SKILLS_DIR
        _sp = f"{{{{SKILL_DIR}}}}/{skills[0]}/SKILL.md"
        parts.append(
            f"## Skill: {skills[0]}\n"
            f"Skill 文件：{_sp}\n"
            f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令（不得跳过读取或凭名称猜测）。\n"
            f"SKILL.md 中如指引读取 references/ 目录下的文件，请按需读取后再执行。\n"
            f"执行以上指令，不要分析或提议——直接执行。"
        )

    if feishu_ctx and skills and skills[0] == "im-notify":
        repo_key = f"{evt['source']}:{evt['repository']}"
        parts.append(_build_feishu_context_section(repo_key, feishu_ctx))

    parts.append(build_role_definition(skills))

    # A2A protocol instructions + available agents
    try:
        agents_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "agent-description.json")
        if os.path.isfile(agents_file):
            with open(agents_file, encoding="utf-8") as f:
                agent_registry = json.load(f)
    except Exception:
        agent_registry = {}
    if agent_registry:
        parts.append("\n## Agent-to-Agent 协议\n")
        parts.append("当你需要调用其他 Agent 时，从下表中选择最合适的：\n")
        for ag, info in agent_registry.items():
            desc = info.get("description", "")
            parts.append(f"- {ag}：{desc}")
        parts.append("""
### 调用方式

如果你认为当前任务需要其他技能继续处理，请先完成当前任务，
然后调用 A2A API：

  curl -X POST {{SERVER_URL}}/api/a2a \\
    -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \\
    -H "Content-Type: application/json" \\
    -d '{
      "agent": "coding",
      "payload": {
        "repository": "'"$REPO_FULL_NAME"'",
        "account": "'"$ACCOUNT"'",
        "issue_number": "'"$ISSUE_NUMBER"'",
        "pr_number": "'"$PR_NUMBER"'",
        "context": "当前处理摘要"
      }
    }'

⚠️ **字段规则（A2A 服务端强制校验，必须遵守）：**
1. `issue_number`（Issue 编号）**必填**：必须填写当前任务关联的真实 Issue 编号。**如果无法确定对应的 issue_number，就不要调用 A2A API**，直接在回复中说明原因。
2. `pr_number`（PR 编号）**可选但有则必填**：如果当前任务涉及 PR（有 `$PR_NUMBER`），必须一并传入；没有则不传（例如 Issue 刚创建、PR 尚未创建）。
3. **严禁字段混用**：`pr_number` 只能填 PR 编号，`issue_number` 只能填 Issue 编号，两者不可互换。
4. `repository` 与 `account` 必填。

### context 构造规则

context 是传给下一个 Agent 的接力说明，应包含：
1. 原始需求：本 prompt 的核心内容摘要（如果来源于 Issue，包含 Issue 标题和关键信息）
2. 你的分析结论：关键分析结果
3. 建议下一步：需要下一个 Agent 做什么

⚡ context 要简洁完整，让下一个 Agent 无需重复分析。
""")

    # M1 结构化产出契约（所有 skill 生效，一处维护，见 agent-runtime-event-full-design.md §8）
    parts.append("""
## 处理记录输出要求

完成当前任务后，在最终答复末尾输出以下 JSON 块（系统会解析写入该 Issue 的 event.json 档案）：

{
  "actions": ["做了什么，如 labeled:bug / assigned:@bob / 提交了PR / 发布了审查评论"],
  "conclusion": "处理结论/分析结果（简要）",
  "artifacts": ["产生的产物：PR#N / 评论 / 标签 / 部署 / Issue"],
  "next_step": "下一步建议；如需交办其他 Agent 则说明调用 A2A 的目标",
  "issue_summary": "Issue 最新聚合结论（覆盖档案 summary；紧凑文本，用 | 分隔）"
}

注意：
- 只输出一个 JSON 块，放在答复末尾。
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造。
- `issue_summary` 应反映截至当前链尾的最新状态，供下游 Agent 快速浏览。

## 任务结束

完成所有业务动作（发评论 / 打标签 / 交办 A2A / 提交 PR 等）后，输出上述最终 JSON 块并**结束任务**。
不要继续思考、不要等待用户输入、不要追加额外内容；任务以你的最终输出为终点。
""")
    # 替换 {{SERVER_URL}} 为服务端可达地址（A2A/tasks 等对外调用；worker 端会再替换一次，幂等）
    return "\n".join(parts).replace("{{SERVER_URL}}", _resolve_server_url())


def build_chat_prompt(user_text: str, repo_key: str, history: list, skills: list = None, account: str = "",
                      runtime_id: int = 0) -> str:
    parts = [f"User asks about {repo_key}: {user_text}"]
    # Chat messages don't have `evt`, so A2A context is not injected here
    if history:
        parts.append(f"This repository has {len(history)} past events recorded.")

    # 注入当前执行的 runtime 引擎信息（供 agent 回答"你是什么runtime"之类的问题）
    if runtime_id:
        try:
            from src.runtime.engine_select import engine_name
            _be = engine_name(runtime_id) or ""
            if _be:
                parts.append(f"Runtime engine: {_be} (runtime_id={runtime_id})")
        except Exception:
            pass

    try:
        from src.api.event_store import get_issue_summary
        issue_summary = get_issue_summary(repo_key)
        if issue_summary:
            parts.append(issue_summary)
    except Exception:
        pass

    parts.append(_build_history_file_guide(repo_key, account, runtime_id))
    if skills:
        parts.append(f"Skills: {', '.join(skills)}")
        _sp = f"{{{{SKILL_DIR}}}}/{skills[0]}/SKILL.md"
        parts.append(
            f"Skill 文件：{_sp}\n"
            f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令（不得跳过读取或凭名称猜测）。"
        )
    parts.append(build_chat_role_definition())
    return "\n".join(parts)


def _get_task_description(conv: dict | None) -> str:
    """按 conversation_id 或 (repo_key, issue_number) 反查 tasks 表任务描述。

    见 task-conversation-prompt-align-design.md §3.1。conv.id 非空优先按 conversation_id 精确反查；
    否则回退 (repo_key, issue_number)。查不到返回空串（调用方跳过任务描述段）。
    """
    conv = conv or {}
    issue_number = conv.get("issue_number") or 0
    repo_key = conv.get("repo_key") or ""
    conv_id = conv.get("id") or ""
    if not issue_number:
        return ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            if conv_id:
                c.execute(
                    "SELECT task_description FROM tasks WHERE conversation_id=? AND task_description!='' ORDER BY id DESC LIMIT 1",
                    (conv_id,))
            else:
                c.execute(
                    "SELECT task_description FROM tasks WHERE repo_key=? AND issue_number=? AND task_description!='' ORDER BY id DESC LIMIT 1",
                    (repo_key, issue_number))
            row = c.fetchone()
            return row[0] if row else ""
    except Exception:
        return ""


def build_task_chat_prompt(user_text: str, repo_key: str, history: list, conv: dict = None,
                           account: str = "", runtime_id: int = 0) -> str:
    """任务对话独立 prompt：注入 Issue 上下文 + `场景: task`，Agent 直接走任务处理流程。

    见 template-onboarding-task-full-chain-design.md §5.7.1。用于任务中心「去对话」会话
    （chat_conversations.conv_type='task'），替代通用 build_chat_prompt。
    显式注入 `## 任务描述`（tasks 表反查），不依赖历史消息兜底（task-conversation-prompt-align-design.md §3.1）。
    """
    conv = conv or {}
    issue_number = conv.get("issue_number") or 0
    parts = [f"## 场景: task", f"## 任务 Issue", f"仓库: {repo_key}", f"Issue: #{issue_number}"]
    # 注入当前执行的 runtime 引擎信息
    if runtime_id:
        try:
            from src.runtime.engine_select import engine_name
            _be = engine_name(runtime_id) or ""
            if _be:
                parts.append(f"Runtime engine: {_be} (runtime_id={runtime_id})")
        except Exception:
            pass
    # 任务边界（最高优先级，随每轮 prompt 强注入）：澄清+回帖，绝不亲自执行任务。
    # 见 skills/chat/references/task-handling.md §0 与 coding-worktree-skill-collision.md §4.5。
    parts.append(
        "## 任务边界（最高优先级）\n"
        "本对话的职责是【澄清被分配 Issue 的需求并回帖】，不是实施/部署/写代码。\n"
        "用户选择的部署方式/实现方案属于需求澄清内容，回帖后由 pipeline（coding/sre）执行。\n"
        "严禁 clone/部署/起服务/修改文件/执行 git 命令。"
    )
    if conv.get("title"):
        parts.append(f"标题: {conv['title']}")
    # 显式注入任务描述（结构化，随每轮 prompt 携带，不随历史裁剪）
    task_desc = _get_task_description(conv)
    if task_desc:
        parts.append(f"\n## 任务描述\n{task_desc}")
    try:
        from src.api.event_store import get_issue_summary
        issue_summary = get_issue_summary(repo_key)
        if issue_summary:
            parts.append(f"\n{issue_summary}")
    except Exception:
        pass
    if history:
        parts.append("\n## 对话历史")
        for m in history[-10:]:
            parts.append(f"[{m.get('role')}] {m.get('content', '')[:500]}")
    parts.append(f"\n## 用户消息\n{user_text}")
    parts.append("Skills: chat")
    _sp = f"{{{{SKILL_DIR}}}}/chat/SKILL.md"
    parts.append(
        f"Skill 文件：{_sp}\n"
        f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令（不得跳过读取或凭名称猜测）。"
    )
    parts.append(build_chat_role_definition())
    return "\n".join(parts)


# ====== Event 对话（event-conversation-design.md）======

def _build_knowledge_section(repo_key: str, agent_type: str = "", issue_num: int = 0) -> str:
    """共享知识段落：project + issue + issue_agent 三级 accepted 知识 + 成员上传文档合并注入。"""
    lines = ["### 用户反馈沉淀的共享知识（仅 accepted）"]
    found = False
    try:
        from src.api.knowledge import read_knowledge
        k = read_knowledge(repo_key, agent_type=agent_type, issue_num=issue_num)
    except Exception:
        k = {}
    for scope in ("project", "issue", "issue_agent"):
        content = k.get(scope)
        if content:
            found = True
            lines.append(f"[{scope} 级] {content}")
    # 成员上传文档摘要（knowledge-board-design.md §2.5：并入对话类 Agent 上下文）
    try:
        from src.api.knowledge import member_files
        mf = member_files(repo_key)
        if mf:
            found = True
            lines.append("[member 级] 项目成员上传文档：")
            for rel in sorted(mf):
                body = mf[rel].strip()
                lines.append(f"- {rel.split('/')[-1]}: {body[:200]}{'…' if len(body) > 200 else ''}")
    except Exception:
        pass
    if not found:
        return ""
    lines.append("后续任务应遵循以上知识，除非用户当前明确推翻。")
    return "\n".join(lines)


def build_conversation_prompt(evt: dict, user_message: str, thread: list,
                              agent_type: str = "", issue_num: int = 0) -> str:
    """Event 对话专用 prompt：对话 skill 指令 + 事件上下文 + 线程历史 + 共享知识。

    对话 skill 为元技能（skills/event-dialogue/SKILL.md），约束解释/沉淀/流程控制行为。
    """
    parts = [f"# Event 对话（与 {agent_type or 'agent'} 就事件对话）"]

    evt_ctx = (
        f"## 事件上下文\n"
        f"- Seq: #{evt.get('seq')}\n"
        f"- 事件类型: {evt.get('event_type')}\n"
        f"- 标题: {evt.get('title') or '（无）'}\n"
        f"- 状态: {evt.get('status')}\n"
        f"- Agent 结论: {evt.get('result') or '（无）'}"
    )
    parts.append(evt_ctx)

    skill_md = "{{SKILL_DIR}}/event-dialogue/SKILL.md"
    parts.append(
        f"## Skill 指令\n"
        f"Skill 文件：{skill_md}\n"
        f"**必须先执行**：用 read tool 读取该 SKILL.md 并严格执行。"
        f"只做对话 / 沉淀共享知识 / 流程控制；不修改代码、不创建 PR、不直接改动 Issue 状态。"
    )

    if thread:
        hist = "\n".join(f"[{m.get('role')}] {m.get('content', '')}" for m in thread[-20:])
        parts.append(f"## 对话历史\n{hist}")

    knowledge = _build_knowledge_section(evt.get("repo_key", ""), agent_type=agent_type, issue_num=issue_num)
    if knowledge:
        parts.append(knowledge)

    parts.append(f"## 用户消息\n{user_message}")
    parts.append("请用中文简洁回复；如需沉淀共享知识或流程干预，调用对应端点（POST /api/events/{seq}/knowledge、/flow/reset，带 BOT_GITCODE_TOKEN）。")
    return "\n\n".join(parts)
