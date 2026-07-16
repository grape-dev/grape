"""Prompt 构建：角色定义、技能能力列表、事件 prompt、飞书 prompt"""
import json, os
from src.config import SKILLS_DIR

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
    ]
    if skills and "implementation" in skills:
        rules.append("3. You MAY write code, modify files, create branches, and execute git commands — within the scope of the current Issue")
    else:
        rules.append("3. Never write code, modify files, or execute git commands")
    rules.append("4. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply.")
    rules.append("5. When you finish, output a single line starting with 'OUTCOME:' summarizing the key result. Do NOT include any other text after the OUTCOME line.")
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
        lines.append("\n### Known VCS -> Feishu Mappings (cached in feishu_users table)")
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

### Save User Mapping (INSERT into feishu_users)
Use Python to save discovered mappings in /root/devops-bot/data/sessions.db:
  import sqlite3; db = sqlite3.connect('/root/devops-bot/data/sessions.db')
  db.execute("INSERT OR REPLACE INTO feishu_users (repo_key,gitcode_user,feishu_open_id,feishu_name,updated_at) VALUES (?,?,?,?,datetime('now'))", ('{repo_key}', GITCODE_USER, OPEN_ID, NAME))
  db.commit(); db.close()

### Name Resolution Workflow
1. Determine target user (vcs_login, e.g. "Jason7602")
2. If in Known Mappings -> use open_id directly, send notification
3. Else: find user's nick_name from Collaborators list above
4. Match nick_name against Group Members name -> get open_id
5. Match found -> save to feishu_users -> send notification
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
        "5. When you finish, output a single line starting with 'OUTCOME:' summarizing the key result. Do NOT include any other text after the OUTCOME line."
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


def build_prompt(evt, history, oc_session_id, skills, feishu_ctx=None):
    number = extract_event_number(evt['payload'])
    repo = evt['repository']
    payload = evt['payload']
    issue_data = {}
    if isinstance(payload.get("issue"), dict):
        issue_data = payload["issue"]
    elif isinstance(payload.get("object_attributes"), dict):
        issue_data = payload["object_attributes"]
    title = json.dumps(issue_data.get("title", ""), ensure_ascii=True)
    body = json.dumps(issue_data.get("body", issue_data.get("description", "")), ensure_ascii=True)[:500]
    parts = []
    evt_type = evt.get("type", "event")

    if evt_type == "issue_comment":
        note = ""
        if isinstance(payload.get("object_attributes"), dict):
            note = payload["object_attributes"].get("note", "")
        prefix = f"User commented on issue #{number} of {repo}" if number is not None else f"User commented on {repo}"
        parts.append(prefix)
        if note:
            parts.append(f'Comment: {json.dumps(note, ensure_ascii=True)[:1000]}')
            if any(f"@{u}" in note for u in __import__("src.config", fromlist=["BOT_USERNAMES"]).BOT_USERNAMES):
                parts.append("(User @mentioned the bot)")
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
        parts.append(f"Previous events in this session: {len(history)}")
        for h in history[-3:]:
            s = "ok" if h.get("success") else "fail"
            summary = h.get("summary", "")
            line = f"  [{s}] {h.get('type','')}"
            if summary:
                line += f" \u2192 {summary[:100]}"
            parts.append(line)

    if skills:
        skill_path = os.path.join(SKILLS_DIR, skills[0], "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, encoding="utf-8") as f:
                skill_content = f.read()
            parts.append(f"## Skill: {skills[0]}\n{skill_content}\n\nExecute the instructions above step by step. Do not analyze or propose — execute.")
        else:
            parts.append(f"Read the file at {skill_path} using the read tool. Execute its instructions step by step. Do not analyze or propose — execute.")

    if feishu_ctx:
        repo_key = f"{evt['source']}:{evt['repository']}"
        parts.append(_build_feishu_context_section(repo_key, feishu_ctx))

    parts.append(build_role_definition(skills))
    return "\n".join(parts)


def build_chat_prompt(user_text: str, repo_key: str, history: list, skills: list = None) -> str:
    parts = [f"User asks about {repo_key}: {user_text}"]
    if history:
        parts.append(f"Session history ({len(history)} events):")
        for h in history[-8:]:
            status = "ok" if h.get("success") else "fail"
            summary = h.get("summary", "")
            line = f"  [{status}] {h.get('type','')}"
            if summary:
                line += f" \u2192 {summary[:100]}"
            parts.append(line)
    else:
        parts.append("No events yet for this repository.")
    if skills:
        parts.append(f"Skills: {', '.join(skills)}")
        skill_path = os.path.join(SKILLS_DIR, skills[0], "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, encoding="utf-8") as f:
                skill_content = f.read()
            parts.append(f"## Skill: {skills[0]}\n{skill_content}\n\nFollow the instructions above.")
        else:
            parts.append(f"Read the file at {skill_path} using the read tool. Follow its instructions.")
    parts.append(build_chat_role_definition())
    return "\n".join(parts)
