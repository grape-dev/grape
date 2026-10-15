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


def _is_github_repo(repo_key: str) -> bool:
    """判断 repo_key 是否为 GitHub 仓库。"""
    return bool(repo_key and repo_key.startswith("github:"))


def _vcs_api_base(repo_key: str) -> str:
    """返回 VCS API 基础 URL。"""
    return "https://api.github.com" if _is_github_repo(repo_key) else "https://gitcode.com/api/v5"


def _vcs_token_env(repo_key: str) -> str:
    """返回 VCS token 环境变量名。"""
    return "USER_GITHUB_TOKEN" if _is_github_repo(repo_key) else "USER_GITCODE_TOKEN"


def _vcs_bot_token_env(repo_key: str) -> str:
    """返回 VCS bot token 环境变量名。"""
    return "BOT_GITHUB_TOKEN" if _is_github_repo(repo_key) else "BOT_GITCODE_TOKEN"


def _vcs_name(repo_key: str) -> str:
    """返回 VCS 平台名称。"""
    return "GitHub" if _is_github_repo(repo_key) else "GitCode"


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


_PROJECT_INFO_SUMMARY_CHARS = 400

_CHAT_PROMPT_SUFFIX = "\nAssistant:"  # 触发 LLM 生成回复的关键 token，不可省略


def _build_project_info_section(repo_key: str, agent_type: str = "",
                                issue_num: int = 0, scene_id: str = "") -> str:
    """注入 repo 全局项目知识基线（服务端权威、多 VM 共享，见 multi-issue-isolation-design.md §2.4/§4.2）。

    - 内联摘要（截断）+ {{PROJECT_INFO_DIR}} 占位符（worker/本地落地后可用 read tool 读全文）
    - Issue #95：member 引导改条件输出（查 ref 表有 source='manual' 关联才输出）
    - Issue #95：去掉 agent/{type}/ 引导（agent_files 废弃后不再物化到 agent/ 目录）
    - 无基线 → 明确允许降级分析
    """
    try:
        from src.runtime.service import load_project_info
        files, meta = load_project_info(repo_key)
    except Exception:
        files, meta = {}, {}
    lines = ["\n## 项目知识基线（repo 全局）"]
    if not files:
        lines.append(f"[无项目知识基线] 允许降级分析：可基于 README 与 {_vcs_name(repo_key)} API 获取基础信息，相关结论标注 [degraded]。")
        # Issue #95：条件输出 member 引导（不固定输出）
        if _has_manual_ref(repo_key, agent_type, issue_num, scene_id):
            lines.append(f"> 项目级成员知识：{{{{PROJECT_INFO_DIR}}}}/member/，存在则用 read tool 读取并遵循。")
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
        truncated = len(content) > _PROJECT_INFO_SUMMARY_CHARS
        body = content[:_PROJECT_INFO_SUMMARY_CHARS] + ("\n...(已截断)" if truncated else "")
        lines.append(f"### {label}\n{body}")
    lines.append("> 全文副本已落地：{{PROJECT_INFO_DIR}}/project-graph.md 与 {{PROJECT_INFO_DIR}}/project-capabilities.md，可用 read tool 读取全文。")
    # Issue #95：条件输出 member 引导（不固定输出，去掉 agent/{type}/ 引导）
    if _has_manual_ref(repo_key, agent_type, issue_num, scene_id):
        lines.append(f"> 项目级成员知识：{{{{PROJECT_INFO_DIR}}}}/member/，存在则用 read tool 读取并遵循。")
    return "\n".join(lines)


def _has_manual_ref(repo_key: str, agent_type: str = "", issue_num: int = 0, scene_id: str = "") -> bool:
    """查 ref 表是否有 source='manual' 的关联记录（Issue #95 条件引导用）。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM scene_knowledge_ref r "
                "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
                "WHERE r.enabled = 1 "
                "AND (r.scene_id = ? OR r.scene_id = '') "
                "AND (r.agent_type = ? OR r.agent_type = '') "
                "AND (r.issue_num = ? OR r.issue_num = 0) "
                "AND k.repo_key = ? "
                "AND k.source = 'manual' "
                "AND k.status = 'accepted' "
                "AND k.archived = 0",
                (scene_id, agent_type, issue_num, repo_key)
            ).fetchone()
        return bool(row and row[0] > 0)
    except Exception:
        return False


_advanced_options_cache: dict = {}  # (repo_key, issue_id) -> (options_str, timestamp)


def _build_scene_advanced_options(repo_key: str, issue_id: str = "") -> str:
    """注入场景高级选项指引（仅执行类 agent coding/qa/sre）。

    值不暴露在 prompt 文本里——通过环境变量注入（GRAPE_WORK_BRANCH / 自定义变量名），
    prompt 只输出固定指引文本，agent 自行 printenv 读取。

    优先读 issue_state.scene_id（Issue 级快照），空则回退 managed_repos.dev_scene。
    60s TTL 内存缓存减少 DB 查询，缓存 key 为 (repo_key, issue_id)。
    """
    import time as _time
    now = _time.time()
    _cache_key = (repo_key, issue_id)
    cached = _advanced_options_cache.get(_cache_key)
    if cached and now - cached[1] < 60:
        return cached[0]
    try:
        from src.session_manager import db, db_lock
        # 1. 优先读 issue_state.scene_id（Issue 级快照），空则回退 managed_repos.dev_scene
        scene_id = ""
        if issue_id:
            try:
                from src.api.session_store import get_issue_scene
                scene_id, _ = get_issue_scene(repo_key, issue_id)
            except Exception:
                scene_id = ""
        if not scene_id:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
                row = c.fetchone()
            scene_id = (row[0] if row else "") or ""
        if not scene_id:
            return ""
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
        # 3. 判断有哪些配置项（决定是否输出指引；自定义变量指引按 Issue #236 移除，使用方式见技能 SKILL.md）
        wb = opts.get("work_branch", "")
        fields = [f for f in (opts.get("custom_fields", []) or []) if f.get("name")]
        has_any = bool(wb) or bool(fields)
        if not has_any:
            return ""
        # 4. 输出固定指引文本（不暴露任何值）
        lines = ["\n## Scene Custom Options"]
        lines.append("Custom scene options are injected as environment variables. Before executing, you MUST read the following environment variables:")
        if wb:
            lines.append("- GRAPE_WORK_BRANCH: work baseline branch (printenv GRAPE_WORK_BRANCH)")
        lines.append("Read all configured options first, then start working.")
        result = "\n".join(lines)
        _advanced_options_cache[_cache_key] = (result, now)
        return result
    except Exception as _e:
        from src.config import log as _log
        _log.warning(f"prompt_builder: inject advanced options failed: {_e}")
        return ""


def build_role_definition(skills=None, agent_type="", repo_key="") -> str:
    """角色定义（所有 DevOps agent 共用）：领域 + 合并为连续编号 1-7 的安全/行为规则（issue-219 方向 C+E）。

    说明：
    - SECURITY 与行为规则合并为单一编号（修复原相邻字符串字面量缺逗号导致规则 2/3 粘连的 bug）。
    - `## 角色定义` 章节标题由调用方注入（方向 B）。
    """
    _vcs = _vcs_name(repo_key)
    _code_rule = (
        "You MAY write code, modify files, create branches, and execute git commands - within the scope of the current Issue"
        if agent_type == "coding"
        else "Never write code, modify files, or execute git commands"
    )
    rules = [
        "You are a DevOps operations assistant. Your domain:",
        "- Code review, build diagnosis, deployment guard, incident triage",
        "- Issue/PR analysis, changelog generation, repository management",
        "- Repository (GitCode/GitHub) API operations",
        "",
        "## SECURITY (MUST FOLLOW):",
        f"1. Access ALL {_vcs} operations ONLY through the `grape` CLI (`grape git/git-repo/issues/...`, carrying GRAPE_SESSION_TOKEN). NEVER read, guess, or use any platform token from the database, config files, logs, environment, or history. Do NOT call VCS APIs with raw tokens.",
        "2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.",
        f"3. If the {_vcs} API returns 403 (no permission): STOP that operation and tell the user the action needs admin/maintainer permission (configure it manually in {_vcs}). NEVER try other tokens or workarounds.",
        "4. NEVER write any token into files, logs, or comments.",
        f"5. {_code_rule}",
        "6. Always reply in the same language as the Issue/comment context (if the user writes in Chinese, reply in Chinese; if English, reply in English). Never include internal reasoning, process steps, or English analysis in your reply.",
        "7. MEDIA EXCEPTION (overrides rule 5): When the prompt contains the section \"## 用户上传的图片分析\" or \"## 用户上传的视频分析\", the image/video has already been analyzed by the system. You MUST answer the user's question based on that analysis. This includes ANY question about the image/video - UI/UX, design, layout, content, code shown in the screenshot, architecture, video content, etc. These are NOT \"non-DevOps topics\". Do NOT refuse. Do NOT say you cannot help with design or UI. Just answer based on the analysis provided.",
    ]
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


def _build_base_chat_role(repo_key="") -> str:
    """Chat 级公共角色基底（领域声明 + SECURITY），general/issue 在此之上叠加差异段。

    抽自原 build_chat_role_definition / build_task_role_definition 的共同部分，
    避免措辞漂移（A4）。
    """
    _vcs = _vcs_name(repo_key)
    return f"""You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations

You have no abilities outside DevOps (no weather, email, documents, PDF, Excel, writing, meetings, voice, etc.).

SECURITY (MUST FOLLOW):
1. Access ALL {_vcs} operations ONLY through the `grape` CLI (`grape git/git-repo/issues/...`, carrying GRAPE_SESSION_TOKEN). NEVER read, guess, or use any platform token from the database, config files, logs, environment, or history. Do NOT call VCS APIs with raw tokens.
2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.
3. If the {_vcs} API returns 403 (no permission): STOP that operation and tell the user the action needs the required permission (configure it manually in {_vcs}). NEVER try other tokens or workarounds.
4. NEVER write any token into files, logs, or comments."""


def build_chat_role_definition(repo_key="") -> str:
    _base = _build_base_chat_role(repo_key)
    return f"""{_base}

Reply rules:
1. DevOps topic: analyze and reply
2. NOT DevOps topic: politely explain you only handle DevOps
3. Never write code, modify files, or execute git commands
4. Always reply in the same language the user used (if the user writes in Chinese, reply in Chinese; if English, reply in English). Never include internal reasoning or process steps.
5. MEDIA EXCEPTION (overrides rule 2): When prompt contains "## 用户上传的图片分析" or "## 用户上传的视频分析" section, the image/video has already been analyzed by the system. You MUST answer the user's question based on that analysis. This includes ANY question about the image/video — UI/UX, design, layout, content, code shown in screenshot, architecture, video content, etc. These are NOT "non-DevOps topics". Do NOT refuse. Do NOT say you cannot help with design or UI. Just answer based on the analysis provided."""


def build_issue_role_definition(repo_key="") -> str:
    """Issue 会话专属 Role（原 build_task_role_definition 重命名+语义扩展）。

    v2 决策 R1/R2：从「任务澄清」放宽为「围绕该 Issue 的讨论」——
    可咨询状态、评审方案、解读背景、澄清需求后回帖；但保留红线
    （不实现/不部署/不写代码/不修改仓库状态）。
    """
    _base = _build_base_chat_role(repo_key)
    _vcs = _vcs_name(repo_key)
    return f"""{_base}

## Current Role (issue conversation)
You are in a conversation about a **specific Issue**. Your duties:
1. Discuss the Issue with the user: clarify requirements when needed, answer questions about the Issue's status/background, and review proposed approaches;
2. If requirement clarification completes, post the clarified conclusion back to the corresponding Issue ({_vcs} comment) using the user's identity;
3. Do NOT implement / deploy / write code / run git commands / change Issue or PR state - implementation is executed by the pipeline (coding/sre) after you post the comment.

Image analysis rule:
When the prompt contains the "## 用户上传的图片分析" section, the system has completed image visual understanding. You should answer the user's question based on that analysis; this is part of your normal duties. Do not refuse on the grounds of "cannot analyze images". Any question about the image content (including UI/UX, design, layout, content, etc.) is an image-context question and must be answered; do not refuse on the grounds of "non-DevOps topic"."""


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


def _build_output_contract(repo_key: str = "") -> str:
    """处理记录输出要求 + 行为约束（所有 DevOps agent 共用，见 agent-runtime-event-full-design.md §8）。

    分节：Task Completion（收尾二选一）→ Comment Reply（思考禁止 / 内部思考 vs 对外结论 /
    评论内容边界 / 发布前自检）。标题与正文为英文（issue-219 方向 D+E），对外回复语言仍由
    角色规则指定为中文。
    """
    _vcs = _vcs_name(repo_key)
    return f"""
## Task Completion

After finishing the current task, follow the closing flow of your current skill (SKILL.md). Choose exactly ONE of the following two paths:

**Case 1: The skill provides `grape tasks complete` structured closing (CLI-based skill)**
- Calling `grape tasks complete` (structured actions/conclusion/artifacts/next_step/issue_summary) finishes the task.
- **Do NOT repeat the JSON block below** - the content has already been submitted to the server via complete.
- ⚠️ If the task is already in a terminal state, an `already:true` response is normal - ignore it.

**Case 2: The skill does not provide `grape tasks complete` closing**
- Append the following JSON block at the end of your final answer (the system parses it into the event.json archive):

{{
  "actions": ["what was done, e.g. labeled:bug / assigned:@bob / opened a PR / posted a review comment"],
  "conclusion": "handling conclusion/analysis result (brief)",
  "artifacts": ["produced artifacts: PR#N / comment / label / deployment / Issue"],
  "next_step": "suggested next step; if handing off to another Agent, state the A2A target",
  "issue_summary": "latest aggregated conclusion for the Issue (overrides archive summary; compact, | separated)"
}}

Notes:
- The two paths are mutually exclusive: never output the JSON block AND call `grape tasks complete` at the end (duplicated content).
- In Case 2, output exactly one JSON block at the end of your answer; pure JSON, not mixed with prose, and the only trailing content permitted.
- If you cannot determine an action or conclusion, use empty arrays/empty strings - do not fabricate.
- `issue_summary` must reflect the latest state at the tail of the current chain, for downstream Agents to skim.

## Comment Reply
⚠️ Thinking must never appear in external output: any thinking narration (e.g. "让我…", "现在我…", "Let me…", "Now I…")
may only appear in natural language before a tool call - never in the final answer, a report, or the {_vcs} comment body.
The final answer only outputs: action result conclusions + closing artifacts.

## Internal Thinking vs External Conclusion (MUST DISTINGUISH)

Internal thinking (exists only in thinking/tool-call processes, never enters the final answer or comments):
  ❌ "Let me analyze this problem..."   ❌ "Now I understand the codebase..."
  ❌ "Let me first fetch the environment variables..."   ❌ "Let me analyze the Issue..."
  ❌ "Now I have a thorough understanding..."

External conclusions (the only allowed body of the final answer and comments):
  ✅ "Root cause of #38 leak located: result_text double-used, leaking the JSON block into the comment."
  ✅ "Done: classification=bug, label=bug/P1, A2A handoff to coding agent."

Rule: every sentence in the final answer/comments must hold up as a submission record the user can read - state only what was done and the results.

⚠️ Comment content boundary (MUST FOLLOW): when posting a comment, the body keeps only the `[Role Agent]` prefix +
external conclusions (what was done / result / next step / necessary quoted references).
Never include in the comment body: thinking narration ("让我…", "现在…", "我需要先…", etc.), the processing-record JSON block (actions/conclusion/...), or any HTML comment markers (e.g. <!-- xxx -->). The processing-record JSON goes only at the end of the final answer for archiving - never via the comment API.

⚠️ Pre-publish self-check: read the comment body aloud as a public post; if any "I"-based thinking narration appears, it is unqualified - rewrite and then publish.
"""


# 附件文件扩展名分类
_ATTACH_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

def _inject_attachments_by_role(parts, repo_key, issue_id, agent_type, runtime_id=0):
    """统一附件注入入口。按 agent_type 分级，从 OBS/文件系统扫描附件。

    所有 agent_type 都走此函数，内部按角色决定注入内容：
    - vod:     图片+文件 URL + 完整处理指引（分析图片 / 下载附件）
    - issue:   只注入文件名清单（无 URL、无下载指引）
    - coding:  非图片附件的文件名 + URL + 简短下载指引
    - qa/sre:  不注入
    """
    if agent_type not in ("vod", "issue", "coding"):
        return

    if not repo_key or not issue_id:
        return

    from src.repo_key import split_owner_repo
    try:
        _owner, _rname = split_owner_repo(repo_key)
    except Exception:
        return
    if not _owner or not _rname:
        return

    _issue_num = str(issue_id).lstrip("#")
    if not _issue_num.isdigit():
        return

    # 通过 tasks._attach_list 获取附件列表（自动适配 OBS/本地）
    # 注意：_attach_list 尚未实现（远端 bbca2f9a 引用但未定义），容错跳过
    try:
        from src.api.issues_center import _attach_list
    except ImportError:
        return
    _attachments = _attach_list(_owner, _rname, _issue_num)
    if not _attachments:
        return

    _images = [a for a in _attachments if a.get("is_image")]
    _files = [a for a in _attachments if not a.get("is_image")]

    # --- VOD：强制读取所有附件 ---
    if agent_type == "vod":
        _lines = []
        for a in _images:
            _lines.append(f"- image {a['filename']}: {a['url']}")
        for a in _files:
            _lines.append(f"- file {a['filename']}: {a['url']}")
        parts.append(
            "\n## User Attachments\n"
            "The Issue contains the following user-uploaded attachments; read ALL of them to understand the user's requirement:\n"
            + "\n".join(_lines) + "\n"
            "- Images: analyze the image content to get a description (analysis method is described in your current skill's SKILL.md).\n"
            "- Text/code/documents: download them into the current working directory, then read the content with the read tool.\n"
            "- After image analysis completes, include the image description in the A2A handoff context for downstream Agents to reference.\n"
        )

    # --- Issue：只注入文件名清单 ---
    elif agent_type == "issue":
        _names = [a["filename"] for a in _files] + [a["filename"] for a in _images]
        if _names:
            parts.append(
                "\n## User Attachments\n"
                f"The user uploaded the following attachments: {', '.join(_names)}\n"
                "Image attachments have been analyzed by the VOD Agent; see the descriptions in the prior Agent summary. "
                "Non-image attachments can be referenced by filename when handing off to the coding agent via A2A.\n"
            )

    # --- Coding：非图片附件文件名 + URL + 强制读取指引 ---
    elif agent_type == "coding":
        if _files:
            _lines = [f"- {a['filename']}: {a['url']}" for a in _files]
            parts.append(
                "\n## User Attachments\n"
                "The following user-uploaded attachments must ALL be downloaded into the current working directory and read with the read tool, "
                "as reference material for implementing the requirement:\n"
                + "\n".join(_lines) + "\n"
                "Image attachments have been analyzed by the VOD Agent; see the descriptions in the prior Agent summary.\n"
            )


def build_devops_agent_prompt(evt, history, oc_session_id, skills, feishu_ctx=None, skill_path="", runtime_id=0,
                 agent_type=""):
    number = extract_event_number(evt['payload'])
    repo = evt['repository']
    payload = evt['payload']
    _rk = evt.get("repo_key", "")
    _vcs = _vcs_name(_rk)
    issue_data = {}
    if isinstance(payload.get("issue"), dict):
        issue_data = payload["issue"]
    elif isinstance(payload.get("object_attributes"), dict):
        issue_data = payload["object_attributes"]
    title = json.dumps(issue_data.get("title", ""), ensure_ascii=False)
    body = json.dumps(issue_data.get("body", issue_data.get("description", "")), ensure_ascii=False)[:4000]
    parts = []
    evt_type = evt.get("type", "event")

    # A2A context injection —— Issue 编号放在最主要位置
    a2a_ctx = evt.get("_a2a_context", "")
    if a2a_ctx:
        issue_num = evt.get("issue_id", "") or ""
        if issue_num:
            parts.append(f"## Task Issue: {issue_num}\n")
            parts.append(f"To understand the task background and how other Agents processed it, refer to the Issue.\n")
        parts.append("## Prior Agent Processing Summary\n")
        parts.append(a2a_ctx)
        pr_num = evt.get("payload", {}).get("pr_number", "")
        if pr_num:
            parts.append(f"A2A target PR: #{pr_num}\n")
        parts.append("Continue processing based on the context above.\n")

    # 关联仓库提示（QA/SRE agent 需要知道关联仓库信息以做联合验证/部署）
    _linked_repos_env = evt.get("_env_extra", {}).get("GRAPE_LINKED_REPOS", "") or os.environ.get("GRAPE_LINKED_REPOS", "")
    if _linked_repos_env and agent_type in ("qa", "sre"):
        try:
            _lr_list = json.loads(_linked_repos_env) if isinstance(_linked_repos_env, str) else _linked_repos_env
            if _lr_list:
                _lr_names = ", ".join(r.get("repo_key", "").replace("gitcode:", "") for r in _lr_list if isinstance(r, dict))
                if _lr_names:
                    parts.append(f"\n## 关联仓库\n本 Issue 涉及多仓库开发，关联仓库: {_lr_names}\n")
                    if agent_type == "qa":
                        parts.append("请对每个关联仓库的 PR 也进行代码审查，并做联合验证（接口契约一致性、数据模型匹配等）。\n")
                    parts.append(f"环境变量 $GRAPE_LINKED_REPOS 和 $GRAPE_LINKED_WORKTREES 包含关联仓库的完整列表和 worktree 路径。\n")
        except Exception:
            pass

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
            parts.append(f"\n⚠️ This comment event does not carry an Issue number. You MUST locate which Issue this comment belongs to via the {_vcs} API:")
            parts.append(f"  Query the recent Issue comments of repository {repo} (GET /git-repo/{repo.split('/')[0]}/{repo.split('/')[-1]}/issues/{{n}}/comments),")
            parts.append("  match the comment content to confirm the Issue number, then reply to the user's comment.")
            parts.append("  Note: even without the number, you MUST reply to the comment - do not skip it silently.")
        parts.append("\nYou MUST reply to this comment: use POST /git-repo/{owner}/{repo}/issues/{n}/comments; start the body with the comment prefix required by your skill, and quote the user's original comment.")
        parts.append("\n⚠️ **Issue comment format (MUST FOLLOW)**: the comment body MUST start with `[Role Agent]` as a prefix (e.g. `[Coding Agent]`, `[QA Agent]`, `[SRE Agent]`, `[Issue Agent]`, `[VOD Agent]`), with the role name capitalized. **Do NOT** use the skill name, repository name, or any other custom prefix.")
    elif evt_type == "vod_signal":
        user_msg = payload.get("user_message", "")
        user_name = evt.get("actor", "unknown user")
        parts.append(f"## User Message\nfrom {user_name} in repository {repo}")
        parts.append(f"Message content: {user_msg[:2000]}")
    else:
        if number is not None:
            parts.append(f"Event: {evt_type} #{number} on {repo}")
        else:
            parts.append(f"Event: {evt_type} on {repo}")
        if title != '""':
            parts.append(f"Title: {title}")
        if body != '""':
            parts.append(f"Body: {body}")

    # PR 订阅场景提示
    if evt.get("_pr_sub_scene_id"):
        parts.append(f"\n## PR 审查任务\n本任务来自 PR 订阅场景，PR 无关联 Issue。请审查 PR #{number} 的变更内容，结果回帖到 PR 评论。")

    # 附件注入：按 agent_type 分级（统一走文件系统扫描，不再从 body 正则匹配）
    _inject_attachments_by_role(parts, _rk, evt.get("issue_id", "") or str(number or ""), agent_type, runtime_id)

    if history:
        parts.append(f"This repository has {len(history)} past events recorded.")

    # VOD issue_opened：用户已选场景时跳过查重（复制需求选不同场景流水线不应被判重复）
    _user_selected_scene = False
    if agent_type == "vod" and evt_type == "issue_opened":
        _issue_id_for_scene = evt.get("issue_id", "") or str(number or "")
        if _issue_id_for_scene and _rk:
            try:
                from src.api.session_store import get_issue_scene
                _scene_tuple = get_issue_scene(_rk, _issue_id_for_scene)
                if _scene_tuple and _scene_tuple[0]:
                    _user_selected_scene = True
            except Exception:
                pass

    try:
        from src.api.event_store import get_issue_summary
        repo_key = f"{evt['source']}:{evt['repository']}"
        _current_issue = str(evt.get("issue_id") or number or "").lstrip("#")
        issue_summary = get_issue_summary(repo_key, exclude_issue=_current_issue)
        if issue_summary:
            if _user_selected_scene:
                parts.append(issue_summary)
                parts.append(
                    "\n⚠️ **重要：用户已为此 Issue 指定了开发场景，请直接按场景流水线执行，"
                    "不要因为标题或内容与已有 Issue 相似而判断为重复需求或跳过执行。"
                    "复制需求使用不同场景流水线是正常操作。**"
                )
            else:
                parts.append(issue_summary)
    except Exception:
        pass

    # 优先用 evt 里已有的 repo_key（A2A 事件 source='a2a' 会导致拼接错误）
    repo_key = evt.get("repo_key") or f"{evt['source']}:{evt['repository']}"
    # 场景高级选项注入（仅执行类 agent：coding/qa/sre）
    if agent_type in ("coding", "qa", "sre"):
        _issue_id_for_scene = evt.get("issue_id", "") or ""
        _adv = _build_scene_advanced_options(repo_key, _issue_id_for_scene)
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
                    f"\n## Subtask Context\n"
                    f"This Issue is a subtask of parent task {_parent}.\n"
                    f"The parent task has already completed requirement clarification and solution design; you do NOT need to re-clarify.\n"
                    f"{'VOD Agent: directly hand off to the Issue Agent via A2A (payload: is_subtask=true, parent_issue=' + _parent + ').' if agent_type == 'vod' else ''}\n"
                    f"{'Issue Agent: skip the design phase; extract the goal and acceptance criteria of this subtask directly from the parent Issue body, generate a concise Spec, then hand off to the coding agent via A2A.' if agent_type == 'issue' else ''}"
                )
            elif is_subtask_mode_enabled(repo_key, _issue_id):
                parts.append(
                    f"\n## Multi-Task Mode\n"
                    f"This task has multi-task mode enabled ([multi-task-mode]).\n"
                    f"{'VOD Agent: clarify requirements normally, then hand off to the Issue Agent via A2A, adding is_subtask_mode=true to the payload.' if agent_type == 'vod' else ''}\n"
                    f"{'Issue Agent: after the solution design, evaluate splitability, create subtask Issues (each body containing the parent task link), and update the subtask list in the parent Issue body. Do NOT hand off to coding via A2A - the pipeline is triggered automatically by the webhook once subtask Issues are created.' if agent_type == 'issue' else ''}"
                )
        except Exception:
            pass

    if skills:
        # 技能路径占位符：worker 替换为随任务下发的物化目录；direct 由 executor 替换为服务端 SKILLS_DIR
        _sp = f"{{{{SKILL_DIR}}}}/{skills[0]}/SKILL.md"
        parts.append(
            f"## Skill: {skills[0]}\n"
            f"Skill file: {_sp}\n"
            f"**MANDATORY**: Read this SKILL.md with the read tool and strictly follow its instructions (do not skip reading or guess from the name).\n"
            f"If the SKILL.md instructs reading reference files under references/, read them as needed before you proceed.\n"
            f"Execute the instructions directly - do not analyze or propose; just execute."
        )

    if feishu_ctx and skills and skills[0] == "im-notify":
        repo_key = f"{evt['source']}:{evt['repository']}"
        parts.append(_build_feishu_context_section(repo_key, feishu_ctx))

    parts.append("\n## Role Definition\n" + build_role_definition(skills, agent_type=agent_type, repo_key=_rk))

    # 引擎绑定信息（避免 agent 查 /openapi.json 核验引擎）
    if runtime_id:
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
                _rt_row = c.fetchone()
            _engine_name = (_rt_row[0] if _rt_row else "") or "unknown"
            parts.append(f"\n## Runtime Engine\nbound engine: {_engine_name} (runtime_id={runtime_id})\n"
                         "Engine binding info is provided in the prompt; no need to check /openapi.json. "
                         "A 403 from an interface is a normal security restriction and should not block subsequent scheduling.\n")
        except Exception:
            pass

    parts.append(_build_output_contract(_rk))
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
        parts.append("\n## Issue Agent Progress")
        for m in agent_items[-10:]:
            parts.append(f"[{m.get('role')}] {m.get('content', '')[:500]}")
    # 始终注入最近对话历史：has_valid_session 时只注入最近 3 条兜底，
    # 无 session 时注入最近 10 条（与原逻辑一致）。
    norm = [m for m in history if m.get("role") in ("user", "agent")]
    if norm:
        recent = norm[-3:] if has_valid_session else norm[-10:]
        if recent:
            parts.append("\n## Chat History")
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
    parts = ["## Role Definition\n" + build_chat_role_definition(repo_key)]
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
                f"## Current Skill: {_sn}\n"
                f"Skill file: {{{{SKILL_DIR}}}}/{_sn}/SKILL.md\n"
                f"**MANDATORY**: Read this SKILL.md with the read tool and strictly follow its instructions to complete the task (do not skip reading or guess)."
            )
    repo_note = f" (in {repo_key})" if repo_key else ""
    parts.append(f"\nUser{repo_note}: {message}{_CHAT_PROMPT_SUFFIX}")
    return "\n".join(parts)


def build_issue_chat_prompt(user_text: str, repo_key: str, history: list, conv: dict = None,
                            account: str = "", runtime_id: int = 0, user_info: str = "",
                            has_valid_session: bool = False) -> str:
    """Issue 会话独立 prompt：围绕该 Issue 展开讨论（咨询/评审/澄清/解读）。

    原 build_task_chat_prompt（决策 R1 重命名）；语义从「任务澄清」放宽为
    「围绕该 Issue 的讨论」（决策 R2：注入 Issue 上下文，取代 task 描述）。
    用于 chat_conversations.conv_type='task' 会话（需求中心「去对话」/ 飞书 /task）。
    """
    conv = conv or {}
    issue_number = conv.get("issue_number") or 0
    parts = []

    # Role Definition（issue 专属，见 build_issue_role_definition）
    parts.append("## Role Definition\n" + build_issue_role_definition(repo_key))

    # 用户基本信息（飞书侧前置）
    if user_info:
        parts.append(user_info)

    parts.extend([f"## Scene: issue", f"## Issue Context", f"Repository: {repo_key}", f"Issue: #{issue_number}"])
    if conv.get("title"):
        parts.append(f"Title: {conv['title']}")
    # 注入当前执行的 runtime 引擎信息；403 是正常安全限制（D3 借鉴）
    if runtime_id:
        try:
            from src.runtime.engine_select import engine_name
            _be = engine_name(runtime_id) or ""
            if _be:
                parts.append(f"Runtime engine: {_be} (runtime_id={runtime_id})")
            parts.append(
                "Bound engine info is provided in the prompt; do not check /openapi.json. "
                "A 403 from an API is a normal security restriction - report it to the user, do not retry with other tokens."
            )
        except Exception:
            pass

    # Issue 上下文：事件/评论摘要（取代旧 task_description 段，R2）
    try:
        from src.api.event_store import get_issue_summary
        issue_summary = get_issue_summary(repo_key, exclude_issue=str(issue_number))
        if issue_summary:
            parts.append(f"\n{issue_summary}")
    except Exception:
        pass

    # 对话历史 / Issue Agent 进展（chat-prompt-history-injection-design.md §3）
    _append_chat_history(parts, history, has_valid_session)

    parts.append(f"\n## Current User Message\n{user_text}")

    _sp = "{{SKILL_DIR}}/chat/SKILL.md"
    parts.append(
        f"## Current Skill: chat\n"
        f"Skill file: {_sp}\n"
        f"**MANDATORY**: Read this SKILL.md with the read tool and strictly follow its instructions (do not skip reading or guess).\n"
        f"If the SKILL.md instructs reading reference files under references/ (e.g. task-handling.md), read them as needed before you proceed.\n"
        f"Reply in the same language the user used, concise, aligned with your current Issue discussion."
    )
    return "\n".join(parts) + _CHAT_PROMPT_SUFFIX


# ====== Event 对话（event-conversation-design.md）======

def _build_agent_knowledge_section(repo_key: str, agent_type: str = "", issue_num: int = 0,
                             scene_id: str = "") -> tuple:
    """agent 沉淀知识段落：查 ref 表注入 source='agent' 关联知识。

    Issue #95 改动：
    - 改名 _build_knowledge_section → _build_agent_knowledge_section（语义明确）
    - SQL 加 source='agent' 过滤——只内联 agent 沉淀知识
    - 恢复 #67 之前的原始语义（#67 之前调 read_knowledge 只查 project/issue/issue_agent）
    - 不内联 baseline（load_project_info 已处理）和 member（member_files_pi 已处理）
    - 返回 (知识段落文本, 注入的知识 ID 列表)
    """
    lines = ["### Shared Knowledge from User Feedback (enabled only)"]
    found = False
    injected_ids = []

    # 1. 查 scene_knowledge_ref 表注入关联知识（source='agent'）
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            rows = db.cursor().execute(
                "SELECT DISTINCT k.id, k.scope, k.source, k.obs_key, k.file_path, k.content_hash "
                "FROM scene_knowledge_ref r "
                "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
                "WHERE r.enabled = 1 "
                "AND (r.scene_id = ? OR r.scene_id = '') "
                "AND (r.agent_type = ? OR r.agent_type = '') "
                "AND (r.issue_num = ? OR r.issue_num = 0) "
                "AND k.repo_key = ? "
                "AND k.source = 'agent' "
                "AND k.status = 'accepted' "
                "AND k.archived = 0",
                (scene_id, agent_type, issue_num, repo_key)
            ).fetchall()

        from src.api.knowledge import read_entry_content
        for kid, scope, source, obs_key, file_path, content_hash in rows:
            content = read_entry_content(repo_key, obs_key or "", file_path or "", content_hash or "")
            if content:
                found = True
                injected_ids.append(kid)
                lines.append(f"[{scope} 级] {content}")
    except Exception:
        # 向后兼容：scene_knowledge_ref 表不存在时回退到 scope 三级注入（只查 agent 知识）
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

    if not found:
        return "", []
    lines.append("Follow the above knowledge in subsequent tasks unless the user explicitly overrides it.")
    return "\n".join(lines), injected_ids


def build_event_chat_prompt(evt: dict, user_message: str, thread: list,
                               agent_type: str = "", issue_num: int = 0) -> tuple:
    """Event 对话专用 prompt：对话 skill 指令 + 事件上下文 + 线程历史 + 共享知识。

    返回 (prompt, injected_ids)——injected_ids 是注入的知识 ID 列表，供调用方记录到事件线程。
    """
    parts = [f"# Event Dialogue (with {agent_type or 'agent'})"]

    evt_ctx = (
        f"## Event Context\n"
        f"- Seq: #{evt.get('seq')}\n"
        f"- Event type: {evt.get('event_type')}\n"
        f"- Title: {evt.get('title') or '(none)'}\n"
        f"- Status: {evt.get('status')}\n"
        f"- Agent conclusion: {evt.get('result') or '(none)'}"
    )
    parts.append(evt_ctx)

    skill_md = "{{SKILL_DIR}}/event-dialogue/SKILL.md"
    parts.append(
        f"## Skill Instructions\n"
        f"Skill file: {skill_md}\n"
        f"**MANDATORY**: Read this SKILL.md with the read tool and strictly follow it. "
        f"Only converse / accumulate shared knowledge / control the flow; do not modify code, do not create PRs, do not change the Issue status directly."
    )

    if thread:
        hist = "\n".join(f"[{m.get('role')}] {m.get('content', '')}" for m in thread[-20:])
        parts.append(f"## Chat History\n{hist}")

    knowledge, _injected_ids = _build_agent_knowledge_section(evt.get("repo_key", ""), agent_type=agent_type, issue_num=issue_num)
    if knowledge:
        parts.append(f"## Shared Knowledge\n{knowledge}")

    parts.append(f"## User Message\n{user_message}")
    _rk = evt.get("repo_key", "")
    _seq = evt.get("seq", "")
    parts.append("Reply concisely, in the same language the user used; to accumulate shared knowledge or intervene in the flow, call the corresponding endpoints "
                 f"(POST /api/events/{_seq}/knowledge、/flow/reset, authenticated with your GRAPE_SESSION_TOKEN via `Authorization: Bearer ${{GRAPE_SESSION_TOKEN}}`). "
                 "Never use raw platform tokens.")
    return "\n\n".join(parts), _injected_ids


# Backward-compat aliases (see docs/new-version2/chat-prompt-overview.md for new names)
build_chat_prompt = build_general_chat_prompt  # deprecated: use build_general_chat_prompt
build_conversation_prompt = build_event_chat_prompt  # deprecated: use build_event_chat_prompt


# ===== 智能推荐 Prompt 构建 =====

def _load_scene_recommend_config(repo_key: str) -> dict:
    """从 managed_repos.scene_recommend_config 读取用户经验规则（结构化数据）。

    返回 {"fallback_scene": "", "rules": [{"scene_id": "", "condition": ""}]}。
    读取时兜底过滤：规则中 scene_id 不在当前场景列表中的直接丢弃。
    """
    try:
        from src.user.db import get_scene_recommend_config
        config = get_scene_recommend_config(repo_key)
        fallback = config.get("fallback_scene", "") or ""
        rules = config.get("rules", []) or []
        # 读取时兜底过滤
        from src.api.pipeline_config import get_scenes
        valid_ids = {s["id"] for s in get_scenes(repo_key=repo_key)}
        rules = [r for r in rules if isinstance(r, dict) and r.get("scene_id") in valid_ids]
        if fallback and fallback not in valid_ids:
            fallback = ""
        return {"fallback_scene": fallback, "rules": rules}
    except Exception:
        return {"fallback_scene": "", "rules": []}


def _sanitize_recommend_config(repo_key: str) -> dict | None:
    """自检并修复推荐配置：删除指向不存在场景的规则，重置失效的兜底场景。

    在场景删除时主动调用。返回修复后的 config（已写回 DB），或 None（无需修复）。
    """
    from src.api.pipeline_config import get_scenes
    from src.user.db import get_scene_recommend_config, set_scene_recommend_config

    scenes = get_scenes(repo_key=repo_key)
    valid_ids = {s["id"] for s in scenes}
    if not valid_ids:
        return None

    config = get_scene_recommend_config(repo_key)
    changed = False

    old_rules = config.get("rules") or []
    new_rules = [r for r in old_rules if isinstance(r, dict) and r.get("scene_id") in valid_ids]
    if len(new_rules) != len(old_rules):
        config["rules"] = new_rules
        changed = True

    fallback = config.get("fallback_scene") or ""
    if fallback not in valid_ids:
        config["fallback_scene"] = scenes[0]["id"] if scenes else ""
        changed = True

    if changed:
        set_scene_recommend_config(repo_key, config["fallback_scene"], config["rules"])
        from src.config import log
        log.info(f"RecommendConfig: auto-sanitized for {repo_key}, "
                 f"fallback={config['fallback_scene']}, rules={len(config['rules'])}")
        return config
    return None


def build_scene_recommender_prompt(evt, repo_key: str) -> str:
    """构建智能推荐专用 prompt（三层结构：系统数据 + 用户经验规则 + 固定指令）。

    用户经验规则从 managed_repos.scene_recommend_config (JSON) 读取，结构化存储，
    注入时转为自然语言。
    """
    payload = evt.get("payload", {})
    issue_data = {}
    if isinstance(payload.get("issue"), dict):
        issue_data = payload["issue"]
    elif isinstance(payload.get("object_attributes"), dict):
        issue_data = payload["object_attributes"]
    title = issue_data.get("title", "")
    body = issue_data.get("body", issue_data.get("description", ""))[:4000]

    parts = []

    # === 第一层：系统数据（不可改）===
    parts.append("## 任务信息")
    parts.append(f"- 标题：{title}")
    parts.append(f"- 描述：{body}")

    from src.api.pipeline_config import get_scenes, get_pipeline_config
    scenes = get_scenes(repo_key=repo_key)
    if not scenes:
        parts.append("\n## 可用开发场景\n> 该仓库未配置任何开发场景。")
        parts.append('\n输出：{"scene_id": "", "reason": "未配置场景"}')
        parts.append("\n## 硬约束\n1. 只输出 JSON，不得有任何前后文字。\n2. 任务立即结束。")
        return "\n".join(parts)

    parts.append("\n## 可用开发场景（该仓库已配置）")
    parts.append("| scene_id | 名称 | 描述 | 技能链 |")
    parts.append("|----------|------|------|--------|")
    for s in scenes:
        cfg = get_pipeline_config(s["id"], repo_key=repo_key)
        chain = " → ".join(n.get("skill", "") for n in cfg.get("nodes", [])) if cfg else "—"
        disp = s.get("display_name") or s.get("name") or s["id"]
        desc = s.get("description") or ""
        parts.append(f"| {s['id']} | {disp} | {desc} | {chain} |")

    # === 第二层：用户经验规则（可改，从 DB 读取结构化数据，注入时转为自然语言）===
    config = _load_scene_recommend_config(repo_key)
    parts.append("\n## 用户经验规则")
    if config.get("fallback_scene"):
        parts.append(f"兜底场景：{config['fallback_scene']}")
    else:
        parts.append("兜底场景：未设置（由系统自动选择最通用的场景）")
    rules = config.get("rules") or []
    if rules:
        parts.append("路由规则：")
        for i, rule in enumerate(rules, 1):
            sid = rule.get("scene_id", "")
            cond = rule.get("condition", "")
            parts.append(f"{i}. {cond} → 走 {sid} 场景")
    else:
        parts.append("路由规则：未设置（纯语义匹配模式）")

    # === 第三层：固定指令（不可改，代码内置）===
    parts.append("\n## 角色")
    parts.append("你是智能推荐器。唯一职责：根据任务描述，从上方场景列表中匹配最优场景，返回 scene_id。")
    parts.append("不执行任何其他操作。")

    parts.append("\n## 匹配优先级（从高到低）")
    parts.append("1. 用户路由规则：检查任务标题/描述是否命中上方「用户经验规则」中的条件，命中则直接走对应场景")
    parts.append("2. 语义匹配：任务描述与场景名称/描述的语义相关性")
    parts.append("3. 用户兜底场景：以上都未命中时，走用户设置的兜底场景")
    parts.append("4. 系统兜底：用户未设兜底场景时，选择技能链最通用的场景")

    parts.append("\n## 能力边界")
    parts.append("- 创建/修改/总结 Issue → 禁止")
    parts.append("- 发送 Issue 评论 → 禁止")
    parts.append("- A2A 交办 → 禁止")
    parts.append("- 多轮对话/需求澄清 → 禁止")
    parts.append("- 读取项目知识基线 → 禁止")
    parts.append(f"- 调用 {_vcs_name(repo_key)} API → 禁止")
    parts.append("- 读写文件 → 禁止")
    parts.append("- 输出 JSON 以外的内容 → 禁止")

    parts.append("\n## 输出格式")
    parts.append('只输出以下 JSON，不输出任何其他内容（无分析、无解释、无叙述）：')
    parts.append('')
    parts.append('{"scene_id": "场景列表中存在的 scene_id", "reason": "一句话选择理由（≤50字）"}')
    parts.append('')
    parts.append("边界情况：")
    parts.append('- 仓库无场景 → {"scene_id": "", "reason": "未配置场景"}')
    parts.append('- 无法精确匹配 → 走兜底场景，reason 注明"未精确匹配，使用兜底场景"')

    parts.append("\n## 硬约束")
    parts.append("1. 只输出 JSON：答复中只能有一个 JSON 块，不得有任何前后文字。")
    parts.append(f"2. 不调用任何 API：不访问 {_vcs_name(repo_key)}、不调用 A2A、不发评论、不查 Issue。")
    parts.append("3. 不读取任何文件：所有信息已在 prompt 中注入。")
    parts.append("4. scene_id 必须来自列表：只能填写 prompt 注入的场景列表中存在的 scene_id。")
    parts.append("5. 用户路由规则优先于你的语义判断：规则命中时必须遵从，不要用语义匹配覆盖。")
    parts.append("6. 任务立即结束：输出 JSON 后结束，不继续思考、不等待输入。")

    return "\n".join(parts)
