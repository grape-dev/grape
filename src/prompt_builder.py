"""Prompt 构建：角色定义、技能能力列表、事件 prompt、飞书 prompt"""
import json, os
from src.config import SKILLS_DIR

EMOJI_MAP = {
    "code-review": "📦 代码审查", "build-debug": "🔧 构建诊断",
    "deploy-guard": "🛡️ 部署守卫", "incident-triage": "🚨 事件分诊",
    "security-scan": "🔒 安全扫描", "changelog-gen": "📝 变更日志",
    "devops-event-ops": "🔄 事件编排", "comment": "💬 仓库评论回复",
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


def build_role_definition() -> str:
    return (
        "You are a DevOps operations assistant. Your domain:\n"
        "- Code review, build diagnosis, deployment guard, incident triage\n"
        "- Issue/PR analysis, changelog generation, repository management\n"
        "- GitCode API operations\n"
        "\n"
        "You have no abilities outside DevOps (no weather, email, documents, "
        "PDF, Excel, writing, meetings, voice, etc.).\n"
        "Reply rules:\n"
        "1. User @mentioned you: always reply politely, state you only do DevOps\n"
        "2. User NOT @mentioned + DevOps topic: analyze and reply\n"
        "3. Never write code, modify files, or execute git commands\n"
        "4. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply."
    )


def build_chat_role_definition() -> str:
    caps = get_skill_capabilities()
    return (
        "You are a DevOps operations assistant. Your domain:\n"
        "- Code review, build diagnosis, deployment guard, incident triage\n"
        "- Issue/PR analysis, changelog generation, repository management\n"
        "- GitCode API operations\n"
        "\n"
        "You have no abilities outside DevOps (no weather, email, documents, "
        "PDF, Excel, writing, meetings, voice, etc.).\n"
        f"Your capabilities:\n{caps}\n\n"
        "Reply rules:\n"
        "1. DevOps topic: analyze and reply\n"
        "2. NOT DevOps topic: politely explain you only handle DevOps, list your capabilities above\n"
        "3. Never write code, modify files, or execute git commands\n"
        "4. Always reply in Chinese. Never include internal reasoning or process steps."
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


def build_prompt(evt, history, oc_session_id, skills):
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
            parts.append(f"  [{s}] {h.get('type','')}")

    if skills:
        skill_path = os.path.join(SKILLS_DIR, skills[0], "SKILL.md")
        parts.append(f"Read the file at {skill_path} using the read tool. Execute its instructions step by step. Do not analyze or propose — execute.")

    parts.append(build_role_definition())
    return "\n".join(parts)


def build_chat_prompt(user_text: str, repo_key: str, history: list, skills: list = None) -> str:
    parts = [f"User asks about {repo_key}: {user_text}"]
    if history:
        parts.append(f"Session history ({len(history)} events):")
        for h in history[-8:]:
            status = "ok" if h.get("success") else "fail"
            parts.append(f"  [{status}] {h.get('type','')}")
    else:
        parts.append("No events yet for this repository.")
    if skills:
        parts.append(f"Skills: {', '.join(skills)}")
        skill_path = os.path.join(SKILLS_DIR, skills[0], "SKILL.md")
        parts.append(f"Read the file at {skill_path} using the read tool. Follow its instructions.")
    parts.append(build_chat_role_definition())
    return "\n".join(parts)
