"""共享知识：正文落工作区文件，DB 仅存索引/状态/审计。

见 event-conversation-design.md §3.1.1：知识正文以文件为唯一正体（参考 project-info / event.json 存放），
按 scope 映射到不同目录；本模块负责计算路径、读、写，并维护 agent_shared_knowledge 索引行。

scope → 文件路径（WORKSPACE_DIR/{owner}/{repo} 下）：
- project    : project-info/knowledge.md
- issue      : issues/{n}/knowledge.md
- issue_agent: {agent}/issues/{n}/knowledge.md

OBS 改造（feat/knowledge-obs）：
- write_knowledge 写本地文件后，若 OBS 启用则同时写 OBS + 计算 content_hash 存 DB（obs_key + content_hash）
- read_knowledge 查 DB obs_key：非空走 OBS 存储 + hash 缓存；空走本地文件（保持原行为）
- storage_key 生成不依赖 rel_path，只依赖 repo_key + scope + filename
"""
import hashlib
import os
import threading
from src.config import log, WORKSPACE_DIR
from src.session_manager import db, db_lock


# ===== 内存缓存（hash 比对——不用 TTL，全实例立即一致） =====
_knowledge_cache: dict[str, tuple[str, str]] = {}  # storage_key -> (content, content_hash)
_knowledge_cache_lock = threading.Lock()
KNOWLEDGE_CACHE_MAX = int(os.environ.get("KNOWLEDGE_CACHE_MAX", "1000"))  # 最大缓存条数，可配置
_sync_not_found: set[int] = set()  # sync_loop 文件不存在的记录 ID（跳过重复处理）


def read_with_cache(storage_key: str, db_hash: str) -> str | None:
    """从内存缓存或 OBS 存储读取知识正文（hash 比对保证一致性）。

    - db_hash 为空 → 无 DB 记录，返回 None（调用方走本地文件兜底）
    - 缓存 hash == db_hash → 用缓存内容（0ms）
    - 缓存 hash != db_hash → 从 OBS 重新拉 → 更新缓存 → 返回内容
    """
    if not db_hash:
        return None
    with _knowledge_cache_lock:
        cached = _knowledge_cache.pop(storage_key, None)
        if cached and cached[1] == db_hash:
            _knowledge_cache[storage_key] = cached  # 重新插入到末尾（LRU）
            return cached[0]
    # 缓存 miss 或 hash 不一致 → 从存储拉
    from src.storage import get_storage
    storage = get_storage()
    if storage is None:
        return None
    data = storage.read_bytes(storage_key)
    if data is None:
        return None
    content = data.decode("utf-8", errors="replace")
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != db_hash:
        log.warning(f"[knowledge] hash mismatch: storage_key={storage_key} db_hash={db_hash[:16]}... actual_hash={actual_hash[:16]}... (OBS content may be stale)")
    with _knowledge_cache_lock:
        # 存 db_hash 而不是 actual_hash——下次 DB hash 没变就命中缓存
        _knowledge_cache[storage_key] = (content, db_hash)
        # LRU 淘汰：超过最大条数时淘汰最旧的
        if len(_knowledge_cache) > KNOWLEDGE_CACHE_MAX:
            oldest_key = next(iter(_knowledge_cache))
            _knowledge_cache.pop(oldest_key, None)
    return content


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


def knowledge_storage_key(repo_key: str, scope: str, agent_type: str = "", issue_num: int = 0) -> str:
    """生成 OBS 存储 key（不依赖 rel_path，只依赖 repo_key + scope + filename）。

    格式：knowledge/{owner}/{repo}/{scope_path}/{filename}
    - scope=project     → knowledge/{owner}/{repo}/project/knowledge.md
    - scope=issue       → knowledge/{owner}/{repo}/issues/{n}/knowledge.md
    - scope=issue_agent → knowledge/{owner}/{repo}/issues/{n}/{agent}/knowledge.md
    """
    owner, rname = _owner_repo(repo_key)
    parts = ["knowledge", owner, rname]
    if scope == "project":
        parts.append("project")
    elif scope == "issue":
        parts.extend(["issues", str(issue_num)])
    elif scope == "issue_agent":
        parts.extend(["issues", str(issue_num), agent_type or "agent"])
    else:
        raise ValueError(f"unknown knowledge scope: {scope}")
    parts.append("knowledge.md")
    return "/".join(parts)


def _query_knowledge_obs(repo_key: str, scope: str, agent_type: str, issue_num: int) -> tuple[str, str]:
    """查 DB 拿 obs_key + content_hash（最新一条 accepted 记录）。

    返回 (obs_key, content_hash)；无记录或异常返回 ("", "")。
    """
    sql = ("SELECT obs_key, content_hash FROM agent_shared_knowledge "
           "WHERE repo_key=? AND scope=? AND status='accepted'")
    params: list = [repo_key, scope]
    if scope == "issue_agent":
        sql += " AND agent_type=?"
        params.append(agent_type)
        if issue_num:
            sql += " AND issue_num=?"
            params.append(issue_num)
    elif scope == "issue":
        if issue_num:
            sql += " AND issue_num=?"
            params.append(issue_num)
    sql += " ORDER BY id DESC LIMIT 1"
    try:
        with db_lock:
            row = db.cursor().execute(sql, params).fetchone()
        if row:
            return (row[0] or "", row[1] or "")
    except Exception:
        pass
    return "", ""


def read_knowledge(repo_key: str, agent_type: str = "", issue_num: int = 0) -> dict:
    """读当前作用域下的 accepted 知识，返回 {scope: content}（仅文件存在时）。

    按范围从大到小合并：project + issue + issue_agent；文件不存在则跳过。

    OBS 改造：先查 DB obs_key，非空走 OBS 存储 + hash 缓存；空走本地文件（保持原行为）。
    """
    out = {}
    for scope in ("project", "issue", "issue_agent"):
        if scope == "project":
            obs_key, db_hash = _query_knowledge_obs(repo_key, scope, "", 0)
        elif scope == "issue":
            obs_key, db_hash = _query_knowledge_obs(repo_key, scope, "", issue_num)
        else:
            obs_key, db_hash = _query_knowledge_obs(repo_key, scope, agent_type, issue_num)

        # obs_key 非空 → 走 OBS 存储 + hash 缓存
        if obs_key:
            content = read_with_cache(obs_key, db_hash)
            if content:
                out[scope] = content.strip()
            continue

        # obs_key 空 → 走本地文件（保持原行为）
        p = knowledge_path(repo_key, scope,
                           agent_type=agent_type if scope == "issue_agent" else "",
                           issue_num=issue_num if scope != "project" else 0)
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
                    source_seq: int = 0, created_by: str = "",
                    source: str = "", description: str = "", name: str = "") -> str:
    """写入知识正文文件 + 登记索引行。返回文件绝对路径。

    Issue #67 改动：
    - 先查 DB 有无同 scope+repo_key+agent_type+issue_num 的记录——有就 UPDATE，没有才 INSERT
    - rejected 不覆盖已有记录
    - 写入后自动生成 scene_knowledge_ref 关联记录（scope=member 除外——用户手动关联）
    - 新增 source 参数（system/agent/manual）
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

    # 默认 source：scope=baseline → system，其他 → agent
    if not source:
        source = "system" if scope == "baseline" else "agent"

    file_path = knowledge_path(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
    rel_path = os.path.relpath(file_path, WORKSPACE_DIR)

    # rejected 只登记索引，不落文件——有已存在记录则跳过
    if status == "rejected":
        with db_lock:
            existing = db.cursor().execute(
                "SELECT id FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?",
                (repo_key, scope, agent_type, issue_num)
            ).fetchone()
            if existing:
                return ""  # 已有记录，跳过
            db.cursor().execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scope, repo_key, agent_type, issue_num, rel_path, "", "", status, source_seq, created_by, source)
            )
            db.commit()
        return ""

    # 1. 先写本地文件（追加）
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        marker = f"- [seq {source_seq or 0} / {created_by or ''}] {content}"
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(marker + "\n")
    except Exception as e:
        raise RuntimeError(f"write knowledge file failed: {e}") from e

    # 2. 写 OBS（启用时） + 计算 content_hash
    obs_key = ""
    content_hash = ""
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            obs_key = knowledge_storage_key(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            storage.save_bytes(obs_key, file_bytes)
            content_hash = hashlib.sha256(file_bytes).hexdigest()
    except Exception as e:
        log.warning(f"[knowledge] OBS write failed for {repo_key}/{scope}: {e} — local file unaffected")
        obs_key = ""
        content_hash = ""

    # 3. DB 写入——先查有无已存在记录（Issue #67：一个 scope 一条记录）
    # 自动生成描述（如果调用方没传）
    if not description:
        _agent_desc = {
            "vod": "VOD Agent：需求分析、拆解 Issue、规划开发路径",
            "coding": "Coding Agent：代码实现、修复缺陷、创建 PR",
            "qa": "QA Agent：代码审查、测试验证、质量把关",
            "sre": "SRE Agent：部署发布、监控告警、运维排障",
            "chat": "Chat Agent：对话交互、知识沉淀、流程控制",
        }
        if scope == "baseline":
            description = "VOD 基线分析自动生成"
        elif scope == "project":
            _role = _agent_desc.get(agent_type, "Agent")
            description = f"{_role} 沉淀的项目级经验"
        elif scope == "issue":
            description = f"Issue #{issue_num} 对话沉淀的经验"
        elif scope == "issue_agent":
            _role = _agent_desc.get(agent_type, agent_type)
            description = f"Issue #{issue_num} {_role} 沉淀的经验"
        elif scope == "member":
            description = ""  # 用户手动填写

    # 自动生成 name（system/agent；manual 由调用方传入）
    if not name and source != "manual":
        _agent_name = {
            "vod": "VOD", "issue": "Issue", "coding": "Coding",
            "qa": "QA", "sre": "SRE", "chat": "Chat",
        }
        if scope == "baseline":
            name = "项目基线分析"
        elif scope == "project":
            _an = _agent_name.get(agent_type)
            name = f"项目知识 - {_an}" if _an else "项目知识"
        elif scope == "issue":
            name = f"Issue #{issue_num} 知识"
        elif scope == "issue_agent":
            _an = _agent_name.get(agent_type, agent_type or "Agent")
            name = f"Issue #{issue_num} - {_an} 知识"

    knowledge_id = None
    with db_lock:
        c = db.cursor()
        existing = c.execute(
            "SELECT id FROM agent_shared_knowledge "
            "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?",
            (repo_key, scope, agent_type, issue_num)
        ).fetchone()

        if existing:
            # 有记录 → UPDATE（不插入新记录）
            c.execute(
                "UPDATE agent_shared_knowledge "
                "SET content_hash=?, obs_key=?, source_seq=?, description=?, name=?, updated_at=datetime('now') "
                "WHERE id=?",
                (content_hash, obs_key, source_seq, description, name, existing[0])
            )
            knowledge_id = existing[0]
        else:
            # 无记录 → INSERT
            c.execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, description, name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope, repo_key, agent_type, issue_num, rel_path, obs_key, content_hash, status, source_seq, created_by, source, description, name)
            )
            knowledge_id = c.lastrowid
        db.commit()

    # 4. 自动生成 scene_knowledge_ref 关联记录（scope=member 除外——用户手动关联）
    if knowledge_id and scope != "member":
        _auto_generate_knowledge_ref(knowledge_id, scope, agent_type, issue_num)

    return file_path


def _auto_generate_knowledge_ref(knowledge_id: int, scope: str, agent_type: str = "", issue_num: int = 0):
    """系统写入知识时按 scope 规则自动生成 scene_knowledge_ref 关联记录。"""
    ref_map = {
        "baseline":    {"scene_id": "", "agent_type": "",           "issue_num": 0},           # 全局（基线分析，所有 Agent 共享）
        "project":     {"scene_id": "", "agent_type": "",           "issue_num": 0},           # 项目级——全局共享（所有 Agent 共写一个文件）
        "issue":       {"scene_id": "", "agent_type": "",           "issue_num": issue_num},   # Issue 级——只匹配该 Issue
        "issue_agent": {"scene_id": "", "agent_type": agent_type,   "issue_num": issue_num},   # Issue+Agent 级——只匹配该 Issue + 该 Agent
        # member 不自动关联——用户手动选择
    }
    ref = ref_map.get(scope)
    if not ref:
        return
    try:
        with db_lock:
            db.cursor().execute(
                "INSERT OR IGNORE INTO scene_knowledge_ref "
                "(knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (knowledge_id, ref["scene_id"], ref["agent_type"], ref["issue_num"], "system")
            )
            db.commit()
    except Exception as e:
        log.warning(f"[knowledge] auto generate ref failed: {e}")


def _archive_issue_knowledge(repo_key: str, issue_num: int):
    """Issue 关闭时归档该 Issue 的知识（Issue #67）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE agent_shared_knowledge SET archived = 1 "
            "WHERE repo_key = ? AND scope IN ('issue', 'issue_agent') "
            "AND issue_num = ?",
            (repo_key, issue_num)
        )
        c.execute(
            "UPDATE scene_knowledge_ref SET enabled = 0, updated_by = 'system' "
            "WHERE knowledge_id IN ("
            "  SELECT id FROM agent_shared_knowledge "
            "  WHERE repo_key = ? AND scope IN ('issue', 'issue_agent') AND issue_num = ?"
            ")",
            (repo_key, issue_num)
        )
        db.commit()
    log.info(f"[knowledge] archived issue #{issue_num} knowledge for {repo_key}")


def cleanup_archived_knowledge():
    """清理超过 90 天的归档知识（Issue #67 定时任务调用）。"""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()

    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, obs_key, file_path, repo_key FROM agent_shared_knowledge "
            "WHERE archived = 1 AND updated_at < ?",
            (cutoff,)
        ).fetchall()

        for kid, obs_key, file_path, repo_key in rows:
            if obs_key:
                try:
                    from src.storage import get_storage
                    storage = get_storage()
                    if storage is not None:
                        storage.delete_object(obs_key)
                except Exception:
                    pass
            if file_path:
                try:
                    from src.config import WORKSPACE_DIR
                    full = os.path.normpath(os.path.join(WORKSPACE_DIR, file_path))
                    if os.path.isfile(full):
                        os.remove(full)
                except Exception:
                    pass
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id = ?", (kid,))
            c.execute("DELETE FROM agent_shared_knowledge WHERE id = ?", (kid,))

        db.commit()
        if rows:
            log.info(f"[knowledge] cleaned up {len(rows)} archived knowledge entries")


def knowledge_files(repo_key: str, agent_type: str = "", issue_num: int = 0, scene_id: str = "") -> dict:
    """任务级交付快照：{rel_path: content}（供远端 worker materialize）。

    Issue #67：改为走 scene_knowledge_ref 表——只交付关联的知识文件。
    未关联的知识不打包给 worker（和 prompt 注入逻辑一致）。
    """
    from src.session_manager import db, db_lock
    from src.api.routes.knowledge import _read_entry_content
    from src.storage import get_storage
    _storage = get_storage()

    files = {}
    try:
        with db_lock:
            rows = db.cursor().execute(
                "SELECT DISTINCT k.id, k.obs_key, k.file_path, k.content_hash "
                "FROM scene_knowledge_ref r "
                "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
                "WHERE r.enabled = 1 "
                "AND (r.scene_id = ? OR r.scene_id = '') "
                "AND (r.agent_type = ? OR r.agent_type = '') "
                "AND (r.issue_num = ? OR r.issue_num = 0) "
                "AND k.repo_key = ? "
                "AND k.status = 'accepted' "
                "AND k.archived = 0",
                (scene_id, agent_type, issue_num, repo_key)
            ).fetchall()

        for kid, obs_key, file_path, content_hash in rows:
            content = _read_entry_content(repo_key, obs_key or "", file_path or "", content_hash or "")
            if content:
                files[file_path or ""] = content
    except Exception:
        # 向后兼容：ref 表不存在时回退到 read_knowledge 三级 scope
        for scope, content in read_knowledge(repo_key, agent_type=agent_type, issue_num=issue_num).items():
            p = knowledge_path(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
            rel = os.path.relpath(p, WORKSPACE_DIR)
            if content:
                files[rel] = content
    return files


_CREDENTIAL_SUBSTRINGS = (
    "credential", "token", "secret", "password", "passwd",
    "apikey", "api_key", "api-key",
    "private_key", "private-key",
    "access_key", "access-key",
)
_CREDENTIAL_EXTENSIONS = (".pem", ".key", ".env", ".p12", ".pfx")


def _is_credential_file(rel_path: str) -> bool:
    """按文件名判断是否凭证类文件（token/密钥/password 等），用于知识库物化前过滤。

    仅匹配文件名（basename），布尔子串 + 敏感扩展名；不拦普通文档与 pipelines-user.json。
    """
    if not rel_path:
        return False
    name = os.path.basename(rel_path).lower()
    if name.endswith(_CREDENTIAL_EXTENSIONS):
        return True
    return any(sub in name for sub in _CREDENTIAL_SUBSTRINGS)


def member_files(repo_key: str) -> dict:
    """成员上传文档快照：{rel_path: content}（project-info/member/ 下 md/txt/json）。

    rel 相对 WORKSPACE_DIR（与 knowledge_files 交付语义一致，供 worker materialize 到 .knowledge/）。
    OBS 模式：查 DB scope='member' + OBS 读取（不依赖本地磁盘）。
    """
    owner, rname = _owner_repo(repo_key)
    files = {}
    # OBS 分支：查 DB + OBS 读取
    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, file_path, obs_key FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope='member' AND (agent_type='' OR agent_type IS NULL) "
                "AND obs_key!='' AND obs_key IS NOT NULL "
                "ORDER BY id DESC",
                (repo_key,)
            ).fetchall()
        seen = set()
        for rid, fp, ok in rows:
            if fp and ok and fp not in seen:
                ext = os.path.splitext(fp)[1].lower()
                if ext not in (".md", ".txt", ".json"):
                    continue
                if _is_credential_file(fp):
                    continue
                seen.add(fp)
                try:
                    data = storage.read_bytes(ok)
                    if data:
                        rel = os.path.join(owner, rname, fp).replace("\\", "/")
                        files[rel] = data.decode("utf-8", errors="replace")
                except Exception:
                    continue
        return files
    # 本地模式（和 dev 一致）
    base = os.path.join(WORKSPACE_DIR, owner, rname, "project-info", "member")
    if not os.path.isdir(base):
        return files
    for root, _dirs, names in os.walk(base):
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            if _is_credential_file(n):
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
    OBS 模式：查 DB scope='member' + OBS 读取（不依赖本地磁盘）。
    """
    owner, rname = _owner_repo(repo_key)
    files = {}
    # OBS 分支：查 DB + OBS 读取
    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, file_path, obs_key FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope='member' AND (agent_type='' OR agent_type IS NULL) "
                "AND obs_key!='' AND obs_key IS NOT NULL "
                "ORDER BY id DESC",
                (repo_key,)
            ).fetchall()
        seen = set()
        for rid, fp, ok in rows:
            if fp and ok and fp not in seen:
                ext = os.path.splitext(fp)[1].lower()
                if ext not in (".md", ".txt", ".json"):
                    continue
                if _is_credential_file(fp):
                    continue
                seen.add(fp)
                try:
                    data = storage.read_bytes(ok)
                    if data:
                        # rel_path 相对 project-info 目录（member/user1/design.md）
                        files[fp.replace("project-info/", "", 1).replace("\\", "/")] = data.decode("utf-8", errors="replace")
                except Exception:
                    continue
        return files
    # 本地模式（和 dev 一致）
    base = os.path.join(WORKSPACE_DIR, owner, rname, "project-info", "member")
    files = {}
    if not os.path.isdir(base):
        return files
    for root, _dirs, names in os.walk(base):
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            if _is_credential_file(n):
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
    OBS 模式：查 DB scope='member' AND agent_type + OBS 读取（不依赖本地磁盘）。
    """
    owner, rname = _owner_repo(repo_key)
    files = {}
    # OBS 分支：查 DB + OBS 读取
    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, file_path, obs_key FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope='member' AND agent_type=? AND obs_key!='' AND obs_key IS NOT NULL "
                "ORDER BY id DESC",
                (repo_key, agent_type or "")
            ).fetchall()
        seen = set()
        for rid, fp, ok in rows:
            if fp and ok and fp not in seen:
                ext = os.path.splitext(fp)[1].lower()
                if ext not in (".md", ".txt", ".json"):
                    continue
                if _is_credential_file(fp):
                    continue
                seen.add(fp)
                try:
                    data = storage.read_bytes(ok)
                    if data:
                        files[fp.replace("project-info/", "", 1).replace("\\", "/")] = data.decode("utf-8", errors="replace")
                except Exception:
                    continue
        return files
    # 本地模式（和 dev 一致）
    base = os.path.join(WORKSPACE_DIR, owner, rname, "project-info", "agent", agent_type or "")
    files = {}
    if not os.path.isdir(base):
        return files
    for root, _dirs, names in os.walk(base):
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            if _is_credential_file(n):
                continue
            p = os.path.join(root, n)
            try:
                rel = os.path.relpath(p, os.path.join(WORKSPACE_DIR, owner, rname, "project-info")).replace("\\", "/")
                with open(p, encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                continue
    return files


async def knowledge_sync_loop():
    """后台定时同步老数据到 OBS——每小时一次，分批 500 条。

    并发保护：通过 meta 表 knowledge_sync_running 标记 + 10 分钟超时，
    防止多实例同时执行 sync_loop 导致重复写 OBS + DB。
    """
    import asyncio, hashlib, time
    from src.session_manager import db, db_lock
    from src.storage import get_storage
    from src.user.db import get_meta_value, set_meta_value

    storage = get_storage()
    if not storage:
        return

    SYNC_LOCK_KEY = "knowledge_sync_running"
    SYNC_LOCK_TIMEOUT = 600  # 10 分钟超时（实例挂了自动恢复）

    while True:
        # 并发保护：检查是否有其他实例正在执行
        running = get_meta_value(SYNC_LOCK_KEY)
        if running:
            try:
                running_ts = float(running)
                if time.time() - running_ts < SYNC_LOCK_TIMEOUT:
                    log.info("[knowledge] sync skipped: another instance is running")
                    await asyncio.sleep(3600)
                    continue
            except ValueError:
                pass  # 值不是时间戳 → 视为过期 → 继续执行

        # 抢锁
        set_meta_value(SYNC_LOCK_KEY, str(time.time()))

        try:
            cursor = 0
            while True:
                with db_lock:
                    c = db.cursor()
                    rows = c.execute(
                        "SELECT id, file_path, scope, repo_key, agent_type, issue_num "
                        "FROM agent_shared_knowledge WHERE (obs_key='' OR obs_key IS NULL) AND id>? ORDER BY id LIMIT 500",
                        (cursor,)
                    ).fetchall()
                if not rows:
                    break
                for row in rows:
                    rid, file_path, scope, repo_key, agent_type, issue_num = row
                    if rid in _sync_not_found:
                        continue
                    try:
                        # 兼容 file_path 格式：write_knowledge/builder 用 WORKSPACE_DIR 相对（owner/repo/...），
                        # member 文档：用 file_path 生成
                        owner, rname = _owner_repo(repo_key)
                        if file_path.startswith(f"{owner}/{rname}/"):
                            full_path = os.path.join(WORKSPACE_DIR, file_path)
                        else:
                            full_path = os.path.join(WORKSPACE_DIR, owner, rname, file_path)
                        if not os.path.isfile(full_path):
                            _sync_not_found.add(rid)
                            log.error(f"[knowledge] sync skip id={rid}: file not found {full_path} (repo may be deleted)")
                            continue
                        content = ""
                        with open(full_path, encoding="utf-8") as f:
                            content = f.read()
                        if content:
                            # 按 scope 生成 storage_key——member/baseline 不走 knowledge_storage_key
                            if scope == "member":
                                # member 文档：用 file_path 生成 storage_key
                                storage_key = f"knowledge/{owner}/{rname}/{file_path.replace('project-info/', '', 1)}"
                            elif scope == "baseline":
                                # 基线文件：用 file_path 生成（和 builder.py 一致）
                                _fname = os.path.basename(file_path)
                                storage_key = f"project-info/{repo_key}/{_fname}"
                            else:
                                # 共享知识：project/issue/issue_agent
                                storage_key = knowledge_storage_key(repo_key, scope, agent_type or "", issue_num or 0)
                            storage.save_bytes(storage_key, content.encode("utf-8"))
                            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:64]
                            with db_lock:
                                c2 = db.cursor()
                                c2.execute(
                                    "UPDATE agent_shared_knowledge SET obs_key=?, content_hash=? WHERE id=?",
                                    (storage_key, content_hash, rid)
                                )
                                db.commit()
                    except Exception as e:
                        
                        log.warning(f"[knowledge] sync failed id={rid}: {e}")
                    cursor = rid
                if len(rows) < 500:
                    break
            with db_lock:
                c3 = db.cursor()
                remaining = c3.execute(
                    "SELECT COUNT(*) FROM agent_shared_knowledge WHERE obs_key='' OR obs_key IS NULL"
                ).fetchone()[0]
            if remaining == 0:
                log.info("[knowledge] sync complete, all data migrated to OBS")
                # 不 return——继续每小时检查新增的 obs_key 为空的记录
            # 补扫本地 project-info 基线文件（OBS 代码之前 onboard 的 repo 没有 DB baseline 记录）
            try:
                _sync_baseline_files()
            except Exception as e:
                log.error(f"[knowledge] sync baseline files error: {e}")
        except Exception as e:
            log.error(f"[knowledge] sync error: {e}")
        finally:
            # 释放锁
            set_meta_value(SYNC_LOCK_KEY, "")
        await asyncio.sleep(3600)


def _sync_baseline_files():
    """扫描本地 project-info 目录，把缺失的 baseline 文件写入 OBS + DB。

    从 managed_repos 表获取项目列表，检查每个项目的 project-info 目录下
    3 个基线文件（project-graph.md / project-capabilities.md / .metadata.json）
    是否已在 DB 有 baseline 记录。没有就写 OBS + INSERT DB。
    """
    import hashlib as _hl
    from src.repo_key import split_owner_repo
    from src.storage import get_storage

    storage = get_storage()
    if not storage:
        return

    BASELINE_FILES = ("project-graph.md", "project-capabilities.md", ".metadata.json")

    # 从 managed_repos 表获取所有项目
    with db_lock:
        rows = db.cursor().execute("SELECT repo_key FROM managed_repos").fetchall()

    for (repo_key,) in rows:
        owner, repo = split_owner_repo(repo_key)
        if not owner or not repo:
            continue
        pi_dir = os.path.join(WORKSPACE_DIR, owner, repo, "project-info")
        if not os.path.isdir(pi_dir):
            continue

        for fname in BASELINE_FILES:
            fpath = os.path.join(pi_dir, fname)
            if not os.path.isfile(fpath):
                continue

            file_path = os.path.relpath(fpath, WORKSPACE_DIR)

            # 查 DB 是否已有 baseline 记录
            with db_lock:
                row = db.cursor().execute(
                    "SELECT id, obs_key FROM agent_shared_knowledge "
                    "WHERE repo_key=? AND scope='baseline' AND file_path=? "
                    "ORDER BY id DESC LIMIT 1",
                    (repo_key, file_path)
                ).fetchone()

            if row and row[1]:  # DB 有记录且 obs_key 非空 → 跳过
                continue

            # 写 OBS + DB
            try:
                with open(fpath, "rb") as f:
                    content_bytes = f.read()
                storage_key = f"project-info/{repo_key}/{fname}"
                storage.save_bytes(storage_key, content_bytes)
                content_hash = _hl.sha256(content_bytes).hexdigest()[:64]

                if row:  # DB 有记录但 obs_key 为空 → UPDATE
                    with db_lock:
                        c = db.cursor()
                        c.execute(
                            "UPDATE agent_shared_knowledge SET obs_key=?, content_hash=? WHERE id=?",
                            (storage_key, content_hash, row[0])
                        )
                        db.commit()
                else:  # DB 无记录 → INSERT
                    _baseline_desc = {
                        "project-graph.md": "VOD 基线分析：项目代码结构图谱（模块/依赖/技术栈）",
                        "project-capabilities.md": "VOD 基线分析：项目能力清单（功能特性/支持能力）",
                        ".metadata.json": "VOD 基线分析：项目元数据（分析时间/分支/提交哈希）",
                    }.get(fname, "VOD 基线分析自动生成")
                    _baseline_name = {
                        "project-graph.md": "项目结构图谱",
                        "project-capabilities.md": "项目能力清单",
                        ".metadata.json": "项目元数据",
                    }.get(fname, "项目基线分析")
                    with db_lock:
                        c = db.cursor()
                        c.execute(
                            "INSERT INTO agent_shared_knowledge "
                            "(scope, repo_key, agent_type, issue_num, file_path, status, obs_key, content_hash, created_by, source, description, name) "
                            "VALUES ('baseline', ?, '', 0, ?, 'accepted', ?, ?, 'sync', 'system', ?, ?)",
                            (repo_key, file_path, storage_key, content_hash, _baseline_desc, _baseline_name)
                        )
                        db.commit()
                log.info(f"[knowledge] sync baseline {repo_key}/{fname} → OBS")
            except Exception as e:
                log.warning(f"[knowledge] sync baseline failed {repo_key}/{fname}: {e}")
