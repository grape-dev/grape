"""Prompt 构建：角色定义、事件 prompt、飞书 prompt"""
import json, os, re
from src.config import SKILLS_DIR, WORKSPACE_DIR, DB_PATH, resolve_official_skill_path

# 部署根目录（历史遗留路径 devops-bot 已迁至 grape；统一由 DB_PATH 推导）
_DEPLOY_ROOT = os.path.dirname(os.path.dirname(DB_PATH))


def _local_port() -> str:
    """本机服务端口，优先读环境变量，默认 3000（兼容老 devops-bot）。

    grape-main 部署时在 systemd Environment 中设 GRAPE_MAIN_PORT=8000 覆盖。
    """
    return os.environ.get("GRAPE_MAIN_PORT", "3000")


def _resolve_server_url(runtime_id: int = 0) -> str:
    """返回服务端可达地址（A2A/tasks 等对外调用用）。

    本地引擎走 localhost 直连，避免 NAT 回环不通
    （服务端无法通过自己的公网 IP 访问自己）；
    远程 worker 走 meta.server_base_url（公网地址）。

    判定本地：credential_id == 0（本机直连引擎）或 credential.last_ip == 127.0.0.1（本机 worker）。
    """
    try:
        from src.user.db import get_server_base_url
        public_url = get_server_base_url()
        if runtime_id and runtime_id > 0:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
                row = c.fetchone()
                if row:
                    cid = row[0] or 0
                    if cid == 0:
                        return f"http://127.0.0.1:{_local_port()}"
                    # 检查是否本机 worker（last_ip == 127.0.0.1）
                    c.execute("SELECT last_ip FROM runtime_credentials WHERE id=?", (cid,))
                    cred_row = c.fetchone()
                    if cred_row and (cred_row[0] or "").startswith("127."):
                        return f"http://127.0.0.1:{_local_port()}"
        return public_url
    except Exception:
        pass
    return "http://localhost:8080"


_PROJECT_INFO_MAX_CHARS = 2500

_CHAT_PROMPT_SUFFIX = "\nAssistant:"  # 触发 LLM 生成回复的关键 token，不可省略


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


def _build_vod_scenes_section(agent_type: str, repo_key: str = "") -> str:
    """VOD 专属：注入该仓库可用开发场景列表（issue-39 方案 B：场景只与仓库关联）。

    独立于项目知识基线——即使项目知识加载异常，VOD 场景列表也不受影响。

    - 场景候选 = get_scenes(repo_key)（scope 仅仓库级）
    - 无「default」场景；仓库无任何场景时 VOD 需输出「未配置场景」终止信号。
    """
    if agent_type != "vod":
        return ""
    try:
        from src.api.pipeline_config import get_scenes, get_pipeline_config
        scenes = get_scenes(repo_key=repo_key)
        lines = []
        if not scenes:
            lines.append("")
            lines.append("> ⚠️ 该仓库**未配置任何开发场景**：请立即终止处理。")
            lines.append('> 在最终 JSON 产出块中输出：`{"terminate": true, "reason": "未配置场景"}`。')
            lines.append("> 不要继续分析需求、不要给出 recommended_scene（没有 default 场景）。")
            return "\n".join(lines)
        lines.append("\n## 可用开发场景（该仓库已配置，请根据需求特征从中选择）")
        lines.append("| scene_id | 名称 | 技能链 | 来源 |")
        lines.append("|----------|------|--------|------|")
        for s in scenes:
            cfg = get_pipeline_config(s["id"], repo_key=repo_key)
            if cfg:
                chain = " → ".join(n.get("skill", "") for n in cfg.get("nodes", []))
                _disp = s.get("display_name") or s.get("name") or s["id"]
                lines.append(f"| {s['id']} | {_disp} | {chain} | 内置 |")

        # 加载成员上传的自定义场景（project-info/member/ 下的 pipelines-user.json）
        if repo_key:
            try:
                from src.api.knowledge import member_files
                _mf = member_files(repo_key)
                for rel, content in _mf.items():
                    if not rel.endswith("pipelines-user.json"):
                        continue
                    import json as _json
                    try:
                        _user_pipes = _json.loads(content)
                    except Exception:
                        continue
                    _user_scenes = _user_pipes.get("scenes", {})
                    for sid, scfg in _user_scenes.items():
                        _name = scfg.get("name", sid)
                        _nodes = scfg.get("nodes", [])
                        _chain = " → ".join(n.get("skill", "") for n in _nodes) if _nodes else "—"
                        lines.append(f"| {sid} | {_name} | {_chain} | 自定义 |")
            except Exception:
                pass

        lines.append("")
        lines.append("> 请根据项目知识基线 + 成员上传文档 + 需求特征，判定最匹配的 scene_id。")
        lines.append("> 成员上传文档（project-info/member/）可能包含自定义场景定义（pipelines-user.json），")
        lines.append("> 如有自定义场景且与需求匹配，优先推荐自定义场景。")
        lines.append("⚠️ **必须**在最终 JSON 产出块中额外包含 `recommended_scene` 和 `scene_reason` 两个字段。")
        lines.append("- `recommended_scene`：**只能填写上表中该仓库的场景 scene_id**，不允许使用其它值。")
        lines.append("- `scene_reason`：一句话说明选择理由。")
        lines.append("- **没有 `default` 场景**：禁止输出 `\"default\"`。")
        lines.append("- 无法确定时 `recommended_scene` 填该仓库当前默认场景，不得省略。")
        lines.append("- 注意：系统会根据用户设置决定是否采纳你的推荐，未采纳时保持仓库默认场景不变。")
        return "\n".join(lines)
    except Exception as _e:
        from src.config import log as _log
        _log.warning(f"prompt_builder: inject scenes for VOD failed: {_e}")
        return ""


_advanced_options_cache: dict = {}  # repo_key -> (options_str, timestamp)


def _build_scene_advanced_options(repo_key: str) -> str:
    """注入场景高级选项：工作分支 + 自定义配置（仅执行类 agent coding/qa/sre）。

    读取 managed_repos.dev_scene → devops_pipeline_scene.advanced_options JSON，
    拼装为 prompt 段落。无配置时返回空串。60s TTL 内存缓存减少 DB 查询。
    """
    import time as _time
    now = _time.time()
    cached = _advanced_options_cache.get(repo_key)
    if cached and now - cached[1] < 60:
        return cached[0]
    try:
        from src.session_manager import db, db_lock
        # 1. 读 managed_repos.dev_scene 拿当前场景 scene_id
        with db_lock:
            c = db.cursor()
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        scene_id = row[0]
        # 2. 读 devops_pipeline_scene.advanced_options
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        import json as _json
        opts = _json.loads(row[0]) if row[0] else {}
        if not opts:
            return ""
        lines = ["\n## 场景高级选项"]
        wb = opts.get("work_branch", "")
        if wb:
            lines.append(f"- 工作基线分支：{wb}（Issue 分支从该分支切出，PR/MR 目标分支必须为 {wb}）")
        fields = opts.get("custom_fields", [])
        if fields:
            lines.append("- 项目自定义配置：")
            for f in fields:
                _k = f.get("key", "")
                _v = f.get("value", "")
                if _k:
                    lines.append(f"  - {_k}: {_v}")
        result = "\n".join(lines) if len(lines) > 1 else ""
        _advanced_options_cache[repo_key] = (result, now)
        return result
    except Exception as _e:
        from src.config import log as _log
        _log.warning(f"prompt_builder: inject advanced options failed: {_e}")
        return ""


def build_role_definition(skills=None, agent_type="") -> str:
    rules = [
        "You are a DevOps operations assistant. Your domain:",
        "- Code review, build diagnosis, deployment guard, incident triage",
        "- Issue/PR analysis, changelog generation, repository management",
        "- Repository (GitCode/GitHub) API operations",
        "",
        "SECURITY (MUST FOLLOW):",
        "1. Use ONLY the GitCode token provided in this prompt (BOT_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.",
        "2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data."
        "3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs admin/maintainer permission (configure it manually in GitCode). NEVER try other tokens or workarounds.",
        "4. NEVER write any token into files, logs, or comments.",
    ]
    if agent_type == "coding":
        rules.append("3. You MAY write code, modify files, create branches, and execute git commands — within the scope of the current Issue")
    else:
        rules.append("3. Never write code, modify files, or execute git commands")
    rules.append("4. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply.")
    rules.append("5. MEDIA EXCEPTION (overrides rule 2): When prompt contains \"## 用户上传的图片分析\" or \"## 用户上传的视频分析\" section, the image/video has already been analyzed by the system. You MUST answer the user's question based on that analysis. This includes ANY question about the image/video — UI/UX, design, layout, content, code shown in screenshot, architecture, video content, etc. These are NOT \"non-DevOps topics\". Do NOT refuse. Do NOT say you cannot help with design or UI. Just answer based on the analysis provided.")
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
    return """You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations

You have no abilities outside DevOps (no weather, email, documents, PDF, Excel, writing, meetings, voice, etc.).

Reply rules:
1. DevOps topic: analyze and reply
2. NOT DevOps topic: politely explain you only handle DevOps
3. Never write code, modify files, or execute git commands
4. Always reply in Chinese. Never include internal reasoning or process steps.
5. MEDIA EXCEPTION (overrides rule 2): When prompt contains "## 用户上传的图片分析" or "## 用户上传的视频分析" section, the image/video has already been analyzed by the system. You MUST answer the user's question based on that analysis. This includes ANY question about the image/video — UI/UX, design, layout, content, code shown in screenshot, architecture, video content, etc. These are NOT "non-DevOps topics". Do NOT refuse. Do NOT say you cannot help with design or UI. Just answer based on the analysis provided.

SECURITY (MUST FOLLOW):
1. Use ONLY the GitCode token provided in this prompt (USER_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.
2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.
3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs the required permission (configure it manually in GitCode). NEVER try other tokens or workarounds.
4. NEVER write any token into files, logs, or comments."""


def build_task_role_definition() -> str:
    """Task 会话专属 Role（task-vs-general-chat-flow-analysis.md §2.1）。

    与 general 的 build_chat_role_definition 不同：task 会话是**已被分配 Issue** 的对话，
    职责是澄清该 Issue 需求并回帖，不是"收到任务再创建新 Issue"。因此去掉
    general 的「创建 Issue 转交流水线」流程，突出澄清+回帖边界（对齐 chat-prompt-overview §2.2）。
    """
    return """You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations

You have no abilities outside DevOps (no weather, email, documents, PDF, Excel, writing, meetings, voice, etc.).

## 当前职责（任务对话）
你正参与一个**已被分配 Issue** 的需求澄清对话。你的职责：
1. 围绕该 Issue 的需求与用户多轮沟通，澄清关键参数与实现方案；
2. 将澄清结论回帖到对应 Issue（GitCode 评论）；
3. 不实施 / 不部署 / 不写代码、不执行 git 命令——实现由 pipeline（coding/sre）在回帖后执行。

SECURITY (MUST FOLLOW):
1. 只使用本 prompt 提供的 GitCode token（USER_GITCODE_TOKEN）。
2. 绝不访问 bot 内部 data/、configs/、sessions.db、user_accounts。
3. GitCode API 403：停止并用政策性话术告知用户需要权限。
4. 绝不把 token 写入文件、日志、评论。

图片分析规则：
当 prompt 包含 "## 用户上传的图片分析" 段落时，说明系统已完成图片视觉理解。你应基于该分析结果回答用户问题，这属于你的正常职责。不得以"无法分析图片"为由拒绝回答。对图片内容的任何提问（包括 UI/UX、设计、布局、内容等）均属于图片上下文问题，必须回答，不得以"非 DevOps 话题"拒绝。"""


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


_agent_registry_cache: dict = {}
_agent_registry_mtime: float = 0.0


def _load_agent_registry() -> dict:
    """加载 A2A 协议配置（mtime 缓存，文件未变时不重复读盘）。"""
    global _agent_registry_cache, _agent_registry_mtime
    agents_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "agent-description.json")
    if not os.path.isfile(agents_file):
        return {}
    try:
        mtime = os.path.getmtime(agents_file)
        if _agent_registry_cache and mtime == _agent_registry_mtime:
            return _agent_registry_cache
        with open(agents_file, encoding="utf-8") as f:
            _agent_registry_cache = json.load(f)
            _agent_registry_mtime = mtime
            return _agent_registry_cache
    except Exception:
        return _agent_registry_cache or {}


def _build_output_contract() -> str:
    """处理记录输出要求 + 行为约束（所有 DevOps agent 共用，见 agent-runtime-event-full-design.md §8）。

    包含：JSON 契约、任务结束规则、内部思考 vs 对外结论分离、评论内容边界、发布前自检。
    """
    return """
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
- JSON 块必须保持纯 JSON 结构，不得与叙述混排，且只能是最终答复末尾唯一的追加内容。
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造。
- `issue_summary` 应反映截至当前链尾的最新状态，供下游 Agent 快速浏览。

## 任务结束

完成所有业务动作（发评论 / 打标签 / 交办 A2A / 提交 PR 等）后，输出上述最终 JSON 块并**结束任务**。
不要继续思考、不要等待用户输入、不要追加额外内容；任务以你的最终输出为终点。

⚠️ 思考过程禁止出现在对外输出：任何思考过程叙述（如"让我…""现在我…""Let me…""Now I…"）
只能出现在工具调用前的自然语言里，绝不能出现在最终答复、报告或 GitCode 评论 body 中。
最终答复只输出：动作结果结论 + 处理记录 JSON 块。

## 内部思考 vs 对外结论（必须分清）

内部思考（只存在于思考/工具调用过程，绝不进最终答复与评论）：
  ❌ "让我分析一下这个问题……"   ❌ "现在我理解了代码库……"
  ❌ "我需要先获取环境变量……"   ❌ "Let me analyze the Issue……"
  ❌ "Now I have a thorough understanding……"

对外结论（最终答复与评论唯一允许的正文）：
  ✅ "已定位 #38 泄漏根因：result_text 一鱼两吃导致 JSON 块入评。"
  ✅ "完成：分类=bug，标签=bug/P1，已 A2A 交办 coding。"

规则：最终答复/评论中的每个句子，都必须能在"给用户看的提交记录"里站得住——只陈述完成的事与结果。

⚠️ 评论内容边界（必须遵守）：发评论时，body 只保留 [角色名 Agent] 前缀 +
对外结论（做了什么 / 结果 / 下一步 / 必要的原文引用）。
禁止把思考过程（"让我…""现在…""我需要先…"这类叙述）、
处理记录 JSON 块（actions/conclusion/...）以及任何 HTML 注释标记
（如 <!-- xxx -->）写进评论正文。处理记录 JSON 只放在最终答复末尾供系统
存档，绝不通过评论 API 发送。

⚠️ 发布前自检：把评论 body 当作**对外发布文案**朗读一遍，
出现任何"我"的思考叙述即为不合格，改写后再发布。
"""


def build_devops_agent_prompt(evt, history, oc_session_id, skills, feishu_ctx=None, skill_path="", runtime_id=0,
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
        parts.append("\n⚠️ **Issue 评论格式规范（必须遵守）**：评论 body 开头必须用 `[角色名 Agent]` 做前缀（如 `[Coding Agent]`、`[QA Agent]`、`[SRE Agent]`、`[Issue Agent]`、`[VOD Agent]`），角色名首字母大写。**禁止**使用 skill 名、仓库名或其他自定义前缀。")
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

    # 优先用 evt 里已有的 repo_key（A2A 事件 source='a2a' 会导致拼接错误）
    repo_key = evt.get("repo_key") or f"{evt['source']}:{evt['repository']}"
    account = evt.get("account") or evt.get("repository", "")
    parts.append(_build_project_info_section(repo_key, agent_type or (skills[0] if skills else "")))

    # VOD 场景列表（独立于项目知识基线，避免项目知识异常时场景列表消失）
    _vod_scenes = _build_vod_scenes_section(agent_type, repo_key)
    if _vod_scenes:
        parts.append(_vod_scenes)

    # 场景高级选项注入（仅执行类 agent：coding/qa/sre）
    if agent_type in ("coding", "qa", "sre"):
        _adv = _build_scene_advanced_options(repo_key)
        if _adv:
            parts.append(_adv)

    # 子任务/多任务模式上下文注入
    _issue_id = evt.get("issue_id", "")
    if _issue_id and repo_key:
        try:
            from src.api.session_store import get_parent_issue, is_subtask_mode_enabled
            _parent = get_parent_issue(repo_key, _issue_id)
            if _parent:
                parts.append(
                    f"\n## 子任务上下文\n"
                    f"本 Issue 是父任务 {_parent} 的子任务。\n"
                    f"父任务已完成需求澄清和方案设计，你不需要重复澄清。\n"
                    f"{'VOD Agent：请直接 A2A 交办 Issue Agent（payload: is_subtask=true, parent_issue=' + _parent + '）。' if agent_type == 'vod' else ''}\n"
                    f"{'Issue Agent：请跳过方案设计，直接从父 Issue body 中提取本子任务的目标和验收标准，生成精简 Spec 后 A2A 交办 coding。' if agent_type == 'issue' else ''}"
                )
            elif is_subtask_mode_enabled(repo_key, _issue_id):
                parts.append(
                    f"\n## 多任务模式\n"
                    f"本任务启用了多任务模式（[multi-task-mode]）。\n"
                    f"{'VOD Agent：正常澄清需求后 A2A 交办 Issue Agent，payload 中增加 is_subtask_mode=true。' if agent_type == 'vod' else ''}\n"
                    f"{'Issue Agent：方案设计后评估可拆分性，创建子 Issue（每个 body 含父任务链接），更新父 Issue body 子任务列表。不 A2A coding——子 Issue 创建后 webhook 自动触发流水线。' if agent_type == 'issue' else ''}"
                )
        except Exception:
            pass

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

    parts.append(build_role_definition(skills, agent_type=agent_type))

    # A2A protocol instructions + available agents（mtime 缓存，避免每次构建 prompt 读文件）
    agent_registry = _load_agent_registry()
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

    # 角色特定的 A2A 交办规则（按 agent_type 注入，指引何时 A2A 给哪个 Agent）
    _a2a_role_rules = {
        "qa": (
            "\n### QA Agent A2A 交办规则\n"
            "PR 审查完成后，根据审查结果决定 A2A 目标：\n"
            "- 审查通过（无 Critical/Required 问题）且 PR 已合并 → A2A 给 `sre`，context 写明「部署」+ PR 编号 + 合并摘要\n"
            "- 审查通过但 PR 未合并 → 不 A2A，在评论中建议维护者合并\n"
            "- 审查不通过（有 Critical/Required 问题）→ A2A 给 `coding`，context 写明问题清单和修复建议\n"
            "⚠️ 审查通过+PR合并后必须 A2A 给 `sre` 触发部署，不要输出「无需 A2A 交办」。\n"
            "⚠️ A2A 给 `sre` 时**不传 `pr_number`**（避免触发订阅去重导致 SRE 任务被跳过），在 context 中写明 PR 编号和合并摘要即可。\n"
        ),
        "coding": (
            "\n### Coding Agent A2A 交办规则\n"
            "任务完成后，根据是否创建了 PR 决定 A2A 目标：\n"
            "- 创建了 PR（代码改动类任务）→ A2A 给 `qa`，context 写明 PR 编号、改动摘要和验收标准，触发代码审查\n"
            "- 未创建 PR（直接执行类任务，如博客生成/发布、脚本执行、配置变更等）→ A2A 给 `sre`，context 写明「发布验证」+ 执行摘要 + 产物链接（如文章 URL），触发发布后验证\n"
            "⚠️ 任务完成后必须 A2A 交办下一个 Agent，不要输出「任务已完成，如需 XX 可调用 A2A」这类建议性语句。直接调用。\n"
            "⚠️ 创建了 PR 时 A2A 给 `qa` 传 `pr_number`；未创建 PR 时 A2A 给 `sre` 不传 `pr_number`（避免触发订阅去重导致 SRE 任务被跳过），在 context 中写明执行摘要和产物链接即可。\n"
        ),
        "issue": (
            "\n### Issue Agent A2A 交办规则\n"
            "需求澄清完成后，根据 Issue 标签决定 A2A 目标：\n"
            "- 标签含 `sre-deploy` → A2A 给 `sre`，context 写明「部署」+ 资源规格\n"
            "- 标签含 `sre-release` → A2A 给 `sre`，context 写明「资源释放」+ 要释放的资源清单\n"
            "- 标签含 `feature`/`bug` → A2A 给 `coding`\n"
            "A2A 时 `pr_number` 仅在当前任务有直接关联的 PR（如 PR 审查后交办）时传入。纯部署/资源释放类请求不传 `pr_number`，否则会触发订阅去重导致 SRE 任务被跳过。\n"
        ),
    }
    _role_rule = _a2a_role_rules.get(agent_type, "")
    if _role_rule:
        parts.append(_role_rule)

    parts.append(_build_output_contract())
    # 替换 {{SERVER_URL}} 为服务端可达地址（A2A/tasks 等对外调用；worker 端会再替换一次，幂等）
    return "\n".join(parts).replace("{{SERVER_URL}}", _resolve_server_url(runtime_id))


_AGENT_TAG_RE = None


def _agent_tag_pattern():
    """匹配 [XXX Agent] 标记（如 [QA Agent]、[Coding Agent][Issue Agent]）。"""
    global _AGENT_TAG_RE
    if _AGENT_TAG_RE is None:
        import re as _re
        _AGENT_TAG_RE = _re.compile(r'\[[A-Za-z_]+?\s+Agent\]', _re.IGNORECASE)
    return _AGENT_TAG_RE


def _append_chat_history(parts: list, history: list = None, has_valid_session: bool = False):
    """注入对话历史（chat-prompt-history-injection-design.md §3）。

    - 无有效 session → 注入完整「对话历史」（最近 10 条，每条截 500 字符）；
    - 有 session → 最近 3 条对话历史作为兜底上下文（防止远端 worker session 丢失）；
    - 无论 session，**含 [XXX Agent] 的 Issue 进展始终注入**为「Issue Agent 进展」段。
    """
    history = history or []
    if not history:
        return
    agent_items = [m for m in history if _agent_tag_pattern().search((m.get("content") or ""))]
    if agent_items:
        parts.append("\n## Issue Agent 进展")
        for m in agent_items[-10:]:
            parts.append(f"[{m.get('role')}] {m.get('content', '')[:500]}")
    # 始终注入最近对话历史：has_valid_session 时只注入最近 3 条兜底，
    # 无 session 时注入最近 10 条（与原逻辑一致）。
    norm = [m for m in history if m.get("role") in ("user", "agent")]
    if norm:
        recent = norm[-3:] if has_valid_session else norm[-10:]
        if recent:
            parts.append("\n## 对话历史")
            for m in recent:
                parts.append(f"[{m.get('role')}] {m.get('content', '')[:500]}")


def build_general_chat_prompt(message: str, repo_key: str = "", history: list = None,
                              skill_name: str = "", skill_id: int = 0,
                              runtime_id: int = 0, backend_name: str = "",
                              user_info: str = "", skills: list = None,
                              account: str = "", has_valid_session: bool = False) -> str:
    """统一通用 Chat prompt（Web / 飞书共用，方案 B：以简洁英文结构为准）。

    原 _build_chat_prompt（web）与旧 build_general_chat_prompt（飞书）二合一：
    - Role 与 §2.1 文档一致（build_chat_role_definition）；
    - 不含全量技能枚举/中文标题（不在 Role 里展开 get_skill_capabilities）；
    - 注入项目知识基线（§2.1 要求）。
    """
    parts = [build_chat_role_definition()]
    if backend_name:
        parts.append(f"Runtime engine: {backend_name}")
    elif runtime_id:
        try:
            from src.runtime.engine_select import engine_name
            _be = engine_name(runtime_id) or ""
            if _be:
                parts.append(f"Runtime engine: {_be}")
        except Exception:
            pass
    if user_info:
        parts.append(user_info)
    if repo_key:
        parts.append(f"\nIMPORTANT: You are working in repository: {repo_key}")
    # 添加事件上下文
    if repo_key:
        try:
            from src.api.event_store import get_issue_summary
            summary = get_issue_summary(repo_key)
            if summary:
                parts.append(f"\nRepository event context:\n{summary}")
        except Exception:
            pass
    # 项目知识基线（chat-prompt-overview.md §2.1）
    parts.append(_build_project_info_section(repo_key, agent_type="chat"))
    # 对话历史 / Issue Agent 进展（chat-prompt-history-injection-design.md §3）
    _append_chat_history(parts, history, has_valid_session)
    # 使用技能：优先 skill_name/skill_id，其次 skills[0]（飞书/tasks 传入）。
    # 兼容旧位置签名：第4参曾为 skills(list)，若收到 list 取其首元素。
    if isinstance(skill_name, list):
        skill_name = skill_name[0] if skill_name else ""
    _sn = skill_name or (skills[0] if skills else "")
    if _sn or skill_id:
        # 官方技能 → {{SKILL_DIR}}/<skill>/SKILL.md 占位指引（worker/direct 由 executor 解析，§2.1）；
        # skill_id 明确指定（custom/repo scope 用户技能）→ 解析真实绝对路径。
        if skill_id and not resolve_official_skill_path(_sn):
            from src.user.db import get_skill_path
            _abs = get_skill_path(skill_id)
            skill_path = _abs or os.path.join(SKILLS_DIR, _sn, "SKILL.md")
            parts.append(f"\n## Current Skill: {_sn or skill_id}\nSkill file: {skill_path}\nRead this file and follow its instructions.")
        else:
            parts.append(
                f"## 使用技能\n"
                f"Skills: {_sn}\n"
                f"Skill 文件：{{{{SKILL_DIR}}}}/{_sn}/SKILL.md\n"
                f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令去完成任务（不得跳过读取或凭空猜测）。"
            )
    # GitCode API 访问说明
    parts.append("""
## GitCode API Access
Your token is available as the USER_GITCODE_TOKEN environment variable.
  curl -s -H "Authorization: Bearer $USER_GITCODE_TOKEN" \\
    https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues
Never print or reveal the token value.
""")
    repo_note = f" (in {repo_key})" if repo_key else ""
    parts.append(f"\nUser{repo_note}: {message}{_CHAT_PROMPT_SUFFIX}")
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
                           account: str = "", runtime_id: int = 0, user_info: str = "",
                           has_valid_session: bool = False) -> str:
    """任务对话独立 prompt：注入 Issue 上下文 + `场景: task`，Agent 直接走任务处理流程。

    见 template-onboarding-task-full-chain-design.md §5.7.1。用于任务中心「去对话」会话
    （chat_conversations.conv_type='task'），替代通用 build_chat_prompt。
    """
    conv = conv or {}
    issue_number = conv.get("issue_number") or 0
    parts = []

    # Role Definition（task 专属，见 task-vs-general-chat-flow-analysis.md §2.1；
    # 与 general 不同：去掉"创建 Issue 转交流水线"，突出澄清+回帖）
    parts.append(build_task_role_definition())

    # 用户基本信息（飞书侧前置）
    if user_info:
        parts.append(user_info)

    parts.extend([f"## 场景: task", f"## 任务背景", f"仓库: {repo_key}", f"Issue: #{issue_number}"])
    if conv.get("title"):
        parts.append(f"标题: {conv['title']}")
    # 注入当前执行的 runtime 引擎信息
    if runtime_id:
        try:
            from src.runtime.engine_select import engine_name
            _be = engine_name(runtime_id) or ""
            if _be:
                parts.append(f"Runtime engine: {_be} (runtime_id={runtime_id})")
        except Exception:
            pass
    # 任务描述（tasks 表，职责边界已并入 Role「当前职责」）
    task_desc = _get_task_description(conv)
    if task_desc:
        parts.append(f"\n### 任务描述\n{task_desc}")

    try:
        from src.api.event_store import get_issue_summary
        issue_summary = get_issue_summary(repo_key)
        if issue_summary:
            parts.append(f"\n{issue_summary}")
    except Exception:
        pass

    # 项目知识基线
    parts.append(_build_project_info_section(repo_key, agent_type="chat"))

    # 对话历史 / Issue Agent 进展（chat-prompt-history-injection-design.md §3）
    _append_chat_history(parts, history, has_valid_session)

    parts.append(f"\n## 本次用户消息\n{user_text}")

    _sp = "{{SKILL_DIR}}/chat/SKILL.md"
    parts.append(
        f"## 使用技能\n"
        f"Skills: chat\n"
        f"Skill 文件：{_sp}\n"
        f"**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令去完成任务（不得跳过读取或凭空猜测）。"
    )
    return "\n".join(parts)


# ====== Event 对话（event-conversation-design.md）======

def _build_knowledge_section(repo_key: str, agent_type: str = "", issue_num: int = 0,
                             scene_id: str = "") -> tuple:
    """共享知识段落：查 scene_knowledge_ref 表注入关联知识 + 基线文件。

    Issue #67 改动：
    - 新增 scene_id 参数
    - 注入逻辑改为查 scene_knowledge_ref 表（DISTINCT knowledge_id 去重）
    - 向后兼容：表不存在或无关联记录时回退到 scope 三级注入
    - 返回 (知识段落文本, 注入的知识 ID 列表)
    """
    lines = ["### 用户反馈沉淀的共享知识（仅 enabled）"]
    found = False
    injected_ids = []

    # 1. 查 scene_knowledge_ref 表注入关联知识
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            rows = db.cursor().execute(
                "SELECT DISTINCT k.id, k.scope, k.source, k.obs_key, k.file_path "
                "FROM scene_knowledge_ref r "
                "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
                "WHERE r.enabled = 1 "
                "AND (r.scene_id = ? OR r.scene_id = '') "
                "AND (r.agent_type = ? OR r.agent_type = '') "
                "AND k.repo_key = ? "
                "AND k.status = 'accepted' "
                "AND k.archived = 0",
                (scene_id, agent_type, repo_key)
            ).fetchall()

        from src.api.routes.knowledge import _read_entry_content
        for kid, scope, source, obs_key, file_path in rows:
            content = _read_entry_content(repo_key, obs_key or "", file_path or "")
            if content:
                found = True
                injected_ids.append(kid)
                lines.append(f"[{source} 级] {content[:500]}")
    except Exception:
        # 向后兼容：scene_knowledge_ref 表不存在时回退到 scope 三级注入
        try:
            from src.api.knowledge import read_knowledge
            k = read_knowledge(repo_key, agent_type=agent_type, issue_num=issue_num)
            for scope in ("project", "issue", "issue_agent"):
                content = k.get(scope)
                if content:
                    found = True
                    lines.append(f"[{scope} 级] {content}")
        except Exception:
            pass

    # 2. 成员上传文档摘要（保持现有逻辑）
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
        return "", []
    lines.append("后续任务应遵循以上知识，除非用户当前明确推翻。")
    return "\n".join(lines), injected_ids


def build_event_chat_prompt(evt: dict, user_message: str, thread: list,
                               agent_type: str = "", issue_num: int = 0) -> tuple:
    """Event 对话专用 prompt：对话 skill 指令 + 事件上下文 + 线程历史 + 共享知识。

    返回 (prompt, injected_ids)——injected_ids 是注入的知识 ID 列表，供调用方记录到事件线程。
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

    knowledge, _injected_ids = _build_knowledge_section(evt.get("repo_key", ""), agent_type=agent_type, issue_num=issue_num)
    if knowledge:
        parts.append(f"## 共享知识\n{knowledge}")

    parts.append(f"## 用户消息\n{user_message}")
    parts.append("请用中文简洁回复；如需沉淀共享知识或流程干预，调用对应端点（POST /api/events/{seq}/knowledge、/flow/reset，带 BOT_GITCODE_TOKEN）。")
    return "\n\n".join(parts), _injected_ids


# Backward-compat aliases (see docs/new-version2/chat-prompt-overview.md for new names)
build_chat_prompt = build_general_chat_prompt  # deprecated: use build_general_chat_prompt
build_conversation_prompt = build_event_chat_prompt  # deprecated: use build_event_chat_prompt
