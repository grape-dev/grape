"""共享知识：正文落工作区文件，DB 仅存索引/状态/审计。

见 event-conversation-design.md §3.1.1：知识正文以文件为唯一正体（参考 project-info / event.json 存放），
按 scope 映射到不同目录；本模块负责计算路径、读、写，并维护 agent_shared_knowledge 索引行。

scope → 文件路径（WORKSPACE_DIR/{owner}/{repo} 下）：
- project    : project-info/knowledge.md
- issue      : issues/{n}/knowledge.md
- issue_agent: {agent}/issues/{n}/knowledge.md
"""
import os
from src.config import WORKSPACE_DIR
from src.session_manager import db, db_lock


def _issue_num_from(issue_id) -> int:
    """'#5' / '5' / 5 → 5；无法解析返回 0。"""
    import re
    m = re.search(r"(\d+)", str(issue_id or ""))
    return int(m.group(1)) if m else 0


def _owner_repo(repo_key: str) -> tuple[str, str]:
    from src.repo_key import split_owner_repo
    try:
        owner, rname = split_owner_repo(repo_key)
        return owner, rname
    except Exception:
        return "", ""


def knowledge_path(repo_key: str, scope: str, agent_type: str = "", issue_num: int = 0) -> str:
    """按 scope 返回知识文件绝对路径（scope 见 3.1.1）。"""
    owner, rname = _owner_repo(repo_key)
    base = os.path.join(WORKSPACE_DIR, owner, rname)
    if scope == "project":
        return os.path.join(base, "project-info", "knowledge.md")
    if scope == "issue":
        return os.path.join(base, "issues", str(issue_num), "knowledge.md")
    if scope == "issue_agent":
        return os.path.join(base, "issues", str(issue_num), agent_type or "agent", "knowledge.md")
    raise ValueError(f"unknown knowledge scope: {scope}")


def read_knowledge(repo_key: str, agent_type: str = "", issue_num: int = 0) -> dict:
    """读当前作用域下的 accepted 知识，返回 {scope: content}（仅文件存在时）。

    按范围从大到小合并：project + issue + issue_agent；文件不存在则跳过。
    """
    out = {}
    for scope in ("project", "issue", "issue_agent"):
        if scope == "project":
            p = knowledge_path(repo_key, scope)
        elif scope == "issue":
            p = knowledge_path(repo_key, scope, issue_num=issue_num)
        else:
            p = knowledge_path(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
        try:
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    out[scope] = content
        except Exception:
            continue
    return out


def write_knowledge(*, repo_key: str, scope: str, content: str, status: str = "accepted",
                    agent_type: str = "", issue_num: int = 0,
                    source_seq: int = 0, created_by: str = "") -> str:
    """写入知识正文文件 + 登记索引行。返回文件绝对路径。

    accepted → 追加进对应文件（作为后续任务可注入的知识）；rejected → 仅登记索引、不落文件正文。
    """
    content = (content or "").strip()
    if not content:
        return ""
    if scope == "project":
        issue_num = 0
        agent_type = ""
    elif scope == "issue":
        agent_type = ""
    elif scope != "issue_agent":
        raise ValueError(f"unknown knowledge scope: {scope}")

    file_path = knowledge_path(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
    rel_path = os.path.relpath(file_path, WORKSPACE_DIR)

    if status == "accepted":
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            marker = f"- [seq {source_seq or 0} / {created_by or ''}] {content}"
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(marker + "\n")
        except Exception as e:
            raise RuntimeError(f"write knowledge file failed: {e}") from e

    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_shared_knowledge "
            "(scope, repo_key, agent_type, issue_num, file_path, status, source_seq, created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (scope, repo_key, agent_type, issue_num, rel_path, status, source_seq, created_by)
        )
        db.commit()
    return file_path


def knowledge_files(repo_key: str, agent_type: str = "", issue_num: int = 0) -> dict:
    """任务级交付快照：{rel_path: content}（参照 project_info_files，供远端 worker materialize）。"""
    files = {}
    for scope, content in read_knowledge(repo_key, agent_type=agent_type, issue_num=issue_num).items():
        p = knowledge_path(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
        try:
            with open(p, encoding="utf-8") as f:
                files[os.path.relpath(p, WORKSPACE_DIR)] = f.read()
        except Exception:
            continue
    # 成员上传文档（project-info/member/**）一并交付远端 worker（见 knowledge-board-design.md §2.5）
    for rel, content in member_files(repo_key).items():
        files.setdefault(rel, content)
    return files


def member_files(repo_key: str) -> dict:
    """成员上传文档快照：{rel_path: content}（project-info/member/ 下 md/txt/json）。

    rel 相对 WORKSPACE_DIR（与 knowledge_files 交付语义一致，供 worker materialize 到 .knowledge/）。
    """
    owner, rname = _owner_repo(repo_key)
    base = os.path.join(WORKSPACE_DIR, owner, rname, "project-info", "member")
    files = {}
    if not os.path.isdir(base):
        return files
    for root, _dirs, names in os.walk(base):
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            p = os.path.join(root, n)
            try:
                rel = os.path.relpath(p, WORKSPACE_DIR).replace("\\", "/")
                with open(p, encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                continue
    return files


def member_files_pi(repo_key: str) -> dict:
    """项目级成员上传文档快照（key 相对 project-info 目录）。

    `member/{username}/{name}`，与 load_project_info / agent_files 的 key 语义一致，
    供 enqueue_task 并入 project_info_files → worker 物化到 `.project-info/member/`。
    见 knowledge-board-design.md §6.7（方案 A：项目级全 Agent 共享）。
    """
    owner, rname = _owner_repo(repo_key)
    base = os.path.join(WORKSPACE_DIR, owner, rname, "project-info", "member")
    files = {}
    if not os.path.isdir(base):
        return files
    for root, _dirs, names in os.walk(base):
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            p = os.path.join(root, n)
            try:
                rel = os.path.relpath(p, os.path.join(WORKSPACE_DIR, owner, rname, "project-info")).replace("\\", "/")
                with open(p, encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                continue
    return files


def agent_files(repo_key: str, agent_type: str = "") -> dict:
    """Agent 专属成员上传文档快照：{rel_path: content}（project-info/agent/{agent_type}/ 下 md/txt/json）。

    key 相对 project-info 目录（`agent/{agent_type}/{name}`，与 load_project_info 的 files key 语义一致），
    供 enqueue_task 定向打包到 project_info_files → worker 物化到 `.project-info/agent/{agent_type}/`。
    见 knowledge-board-design.md §六。
    """
    owner, rname = _owner_repo(repo_key)
    base = os.path.join(WORKSPACE_DIR, owner, rname, "project-info", "agent", agent_type or "")
    files = {}
    if not os.path.isdir(base):
        return files
    for root, _dirs, names in os.walk(base):
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            p = os.path.join(root, n)
            try:
                rel = os.path.relpath(p, os.path.join(WORKSPACE_DIR, owner, rname, "project-info")).replace("\\", "/")
                with open(p, encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                continue
    return files
