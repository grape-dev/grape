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
        _knowledge_cache[storage_key] = (content, actual_hash)
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
                    source_seq: int = 0, created_by: str = "") -> str:
    """写入知识正文文件 + 登记索引行。返回文件绝对路径。

    accepted → 追加进对应文件（作为后续任务可注入的知识）；rejected → 仅登记索引、不落文件正文。

    OBS 改造：
    1. 先写本地文件（保持现有行为）
    2. 若 OBS 启用：写 OBS storage.save_bytes + 计算 content_hash
    3. DB INSERT 带 obs_key + content_hash（OBS 写失败时 log warning 但不影响本地写入）
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

    # 1. 先写本地文件（保持现有行为）
    if status == "accepted":
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
    if status == "accepted":
        try:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                obs_key = knowledge_storage_key(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
                # 读取写入后的完整文件内容（含追加的 marker）
                with open(file_path, "rb") as f:
                    file_bytes = f.read()
                storage.save_bytes(obs_key, file_bytes)
                content_hash = hashlib.sha256(file_bytes).hexdigest()
        except Exception as e:
            log.warning(f"[knowledge] OBS write failed for {repo_key}/{scope}: {e} — local file unaffected")
            obs_key = ""
            content_hash = ""

    # 3. DB INSERT 带 obs_key + content_hash
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_shared_knowledge "
            "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (scope, repo_key, agent_type, issue_num, rel_path, obs_key, content_hash, status, source_seq, created_by)
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
            p = os.path.join(root, n)
            try:
                rel = os.path.relpath(p, os.path.join(WORKSPACE_DIR, owner, rname, "project-info")).replace("\\", "/")
                with open(p, encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                continue
    return files


async def knowledge_sync_loop():
    """后台定时同步老数据到 OBS——每小时一次，分批 500 条。"""
    import asyncio, hashlib
    from src.session_manager import db, db_lock
    from src.storage import get_storage

    storage = get_storage()
    if not storage:
        return

    while True:
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
                        # upload_knowledge 用 repo_base 相对（project-info/...）
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
                                # member 文档：用 file_path 生成（和 upload_knowledge 一致）
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
        except Exception as e:
            log.error(f"[knowledge] sync error: {e}")
        await asyncio.sleep(3600)
