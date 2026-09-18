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
import json
import os
import threading
from datetime import datetime, timedelta
from src.config import log, WORKSPACE_DIR
from src.lock import acquire_lock, release_lock, renew_lock
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


def read_entry_content(repo_key: str, obs_key: str, file_path: str, content_hash: str = "") -> str:
    """读取知识条目正文（业务层）。

    obs_key 非空走 OBS（优先 hash 缓存），否则走本地文件。失败返回空串。
    routes 层和 prompt_builder 都应调用此函数，不应跨层依赖 routes。
    """
    if obs_key:
        try:
            if content_hash:
                content = read_with_cache(obs_key, content_hash)
                if content is not None:
                    return content
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                data = storage.read_bytes(obs_key)
                if data is not None:
                    return data.decode("utf-8", errors="replace")
        except Exception as e:
            log.warning(f"OBS read failed for entry {repo_key}/{obs_key}: {e} — fallback to local")
    # 本地模式：直接读文件（路径校验由 API 层入口负责，内部调用不重复）
    if file_path:
        try:
            full = os.path.normpath(os.path.join(WORKSPACE_DIR, file_path))
            # 安全：确保路径不逃逸 WORKSPACE_DIR
            if full != WORKSPACE_DIR and not full.startswith(WORKSPACE_DIR + os.sep):
                return ""
            if os.path.isfile(full):
                with open(full, "r", encoding="utf-8") as f:
                    return f.read()
            # Issue #190：兼容旧格式 file_path（不含 provider 前缀）
            from src.repo_key import workspace_repo_dir
            alt = os.path.join(workspace_repo_dir(repo_key), file_path)
            if os.path.isfile(alt):
                with open(alt, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as e:
            log.warning(f"Local read failed for {repo_key}/{file_path}: {e}")
    return ""


def _issue_num_from(issue_id) -> int:
    """'#5' / '5' / 5 → 5；无法解析返回 0。"""
    import re
    m = re.search(r"(\d+)", str(issue_id or ""))
    return int(m.group(1)) if m else 0


def knowledge_path(repo_key: str, scope: str, agent_type: str = "", issue_num: int = 0) -> str:
    """按 scope 返回知识文件绝对路径（scope 见 3.1.1）。"""
    from src.repo_key import workspace_repo_dir
    base = workspace_repo_dir(repo_key)
    if scope == "project":
        return os.path.join(base, "project-info", "knowledge.md")
    if scope == "issue":
        return os.path.join(base, "issues", str(issue_num), "knowledge.md")
    if scope == "issue_agent":
        return os.path.join(base, "issues", str(issue_num), agent_type or "agent", "knowledge.md")
    raise ValueError(f"unknown knowledge scope: {scope}")


def knowledge_storage_key(repo_key: str, scope: str, agent_type: str = "", issue_num: int = 0) -> str:
    """生成 OBS 存储 key（不依赖 rel_path，只依赖 repo_key + scope + filename）。

    Issue #190：含 provider 前缀，避免跨 provider 同名仓库冲突。
    格式：knowledge/{provider}/{owner}/{repo}/{scope_path}/{filename}
    - scope=project     → knowledge/{provider}/{owner}/{repo}/project/knowledge.md
    - scope=issue       → knowledge/{provider}/{owner}/{repo}/issues/{n}/knowledge.md
    - scope=issue_agent → knowledge/{provider}/{owner}/{repo}/issues/{n}/{agent}/knowledge.md
    """
    from src.repo_key import knowledge_obs_prefix
    prefix = knowledge_obs_prefix(repo_key)
    if scope == "project":
        return f"{prefix}/project/knowledge.md"
    if scope == "issue":
        return f"{prefix}/issues/{issue_num}/knowledge.md"
    if scope == "issue_agent":
        return f"{prefix}/issues/{issue_num}/{agent_type or 'agent'}/knowledge.md"
    raise ValueError(f"unknown knowledge scope: {scope}")


def _query_knowledge_obs(repo_key: str, scope: str, agent_type: str, issue_num: int) -> tuple[str, str]:
    """查 DB 拿 obs_key + content_hash（最新一条 accepted 记录）。

    返回 (obs_key, content_hash)；无记录或异常返回 ("", "")。
    """
    sql = ("SELECT obs_key, content_hash FROM agent_shared_knowledge "
           "WHERE repo_key=? AND scope=? AND status='accepted' AND archived=0")
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
                    source: str = "", description: str = "", name: str = "") -> int:
    """写入知识正文文件 + 登记索引行。返回 task_id（Issue #103：生产者-消费者模式）。

    Issue #103 改动：
    - 入队 knowledge_write_queue 表（生产者）
    - 立即尝试消费（第一层），抢到锁同步处理，抢不到锁返回 task_id 留 pending
    - 后台 worker 兜底消费（第二层）
    - 同维度（repo_key+scope+agent_type+issue_num）分布式锁串行
    - 内容去重：split("] ", 1)[1] == content 精确匹配，防重复追加

    Issue #67 / #95 保留：
    - 先查 DB 有无同 scope+repo_key+agent_type+issue_num 的记录——有就 UPDATE，没有才 INSERT
    - rejected 不覆盖已有记录
    - 写入后自动生成 scene_knowledge_ref 关联记录（scope=member 除外）
    - source 参数（system/agent/manual）
    """
    content = (content or "").strip()
    if not content:
        return 0
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

    # 自动生成 description（如果调用方没传）
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

    # 1. 入队（生产者）
    task_id = _enqueue_knowledge_write(
        repo_key=repo_key, scope=scope, agent_type=agent_type, issue_num=issue_num,
        content=content, status=status, source_seq=source_seq, created_by=created_by,
        source=source, description=description, name=name)

    # 2. 第一层消费：立即尝试加锁处理
    lock_key = _knowledge_lock_key(repo_key, scope, agent_type, issue_num)
    if acquire_lock(lock_key, ttl=60):
        try:
            _do_write_knowledge(
                repo_key=repo_key, scope=scope, agent_type=agent_type, issue_num=issue_num,
                content=content, status=status, source_seq=source_seq, created_by=created_by,
                source=source, description=description, name=name)
            _delete_queue_task(task_id)
        except Exception as e:
            log.warning(f"[knowledge] first-layer consume failed for task {task_id}: {e}")
            _fail_queue_task(task_id, str(e), retry_count=0)
        finally:
            release_lock(lock_key)

    # 拿不到锁 → 返回 task_id，任务留 pending，worker 兜底
    return task_id


def _knowledge_lock_key(repo_key: str, scope: str, agent_type: str, issue_num: int) -> str:
    """分布式锁 key——MD5 压缩防 VARCHAR(128) 溢出。"""
    raw = f"{repo_key}:{scope}:{agent_type}:{issue_num}"
    return f"knowledge_write:{hashlib.md5(raw.encode()).hexdigest()[:16]}"


def _enqueue_knowledge_write(*, repo_key, scope, agent_type, issue_num, content, status,
                             source_seq, created_by, source, description, name) -> int:
    """INSERT 到 knowledge_write_queue 表，返回自增 id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO knowledge_write_queue "
            "(repo_key, scope, agent_type, issue_num, content, status, "
            " source_seq, created_by, source, description, name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (repo_key, scope, agent_type, issue_num, content, status,
             source_seq, created_by, source, description, name))
        db.commit()
        return c.lastrowid


def _do_write_knowledge(*, repo_key, scope, agent_type, issue_num, content, status,
                        source_seq, created_by, source, description, name):
    """实际写入逻辑（从原 write_knowledge 提取）。在分布式锁内调用。

    name/description 从队列任务取（入队时已生成），不重新计算。
    """
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
                return
            db.cursor().execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scope, repo_key, agent_type, issue_num, rel_path, "", "", status, source_seq, created_by, source)
            )
            db.commit()
        return

    # 内容去重 + 崩溃恢复：检查文件内容 + DB 记录 + content_hash 一致性
    file_has_content = False
    db_has_record = False
    db_content_hash = ""
    db_knowledge_id = None
    if os.path.isfile(file_path):
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if stripped.startswith("- [seq ") and "] " in stripped:
                    existing_content = stripped.split("] ", 1)[1]
                    if existing_content == content:
                        file_has_content = True
                        break
    with db_lock:
        existing = db.cursor().execute(
            "SELECT id, content_hash FROM agent_shared_knowledge "
            "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?",
            (repo_key, scope, agent_type, issue_num)
        ).fetchone()
        if existing:
            db_has_record = True
            db_content_hash = existing[1] or ""
            db_knowledge_id = existing[0]

    if file_has_content and db_has_record:
        # 文件有 + DB 有 → 校验 content_hash 是否匹配当前文件
        # 不匹配说明上次 DB UPDATE 失败（文件已追加但 hash 没更新）→ 不跳过，补写 OBS + DB
        current_hash = ""
        try:
            with open(file_path, "rb") as f:
                current_hash = hashlib.sha256(f.read()).hexdigest()
        except Exception:
            pass
        if current_hash and db_content_hash == current_hash:
            # hash 一致 → 完全跳过，但补 ref + 更新审计字段（上次可能 ref 失败或元数据过期）
            if db_knowledge_id and scope != "member":
                _auto_generate_knowledge_ref(db_knowledge_id, scope, agent_type, issue_num)
            # 更新审计字段（source_seq/created_by/status），不改 content_hash/obs_key
            with db_lock:
                db.cursor().execute(
                    "UPDATE agent_shared_knowledge SET source_seq=?, status=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (source_seq, status, db_knowledge_id))
                db.commit()
            return
        # hash 不一致 → 落入下面的补写流程（不追加文件因为 file_has_content=True）
    # file_has_content and not db_has_record → 不追加文件，补写 OBS + DB
    # not file_has_content → 正常全流程（追加 + OBS + DB）

    # 1. 文件追加（仅当文件没有此 content）
    if not file_has_content:
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

    # 3. DB 写入——先查有无已存在记录
    knowledge_id = None
    with db_lock:
        c = db.cursor()
        existing = c.execute(
            "SELECT id FROM agent_shared_knowledge "
            "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?",
            (repo_key, scope, agent_type, issue_num)
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE agent_shared_knowledge "
                "SET content_hash=?, obs_key=?, source_seq=?, description=?, name=?, "
                "source=COALESCE(NULLIF(source, ''), ?), status=?, "
                "updated_at=datetime('now') "
                "WHERE id=?",
                (content_hash, obs_key, source_seq, description, name,
                 source, status, existing[0])
            )
            knowledge_id = existing[0]
        else:
            c.execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, description, name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope, repo_key, agent_type, issue_num, rel_path, obs_key, content_hash, status, source_seq, created_by, source, description, name)
            )
            knowledge_id = c.lastrowid
        db.commit()

    # 4. 自动生成 scene_knowledge_ref（scope=member 除外）
    if knowledge_id and scope != "member":
        _auto_generate_knowledge_ref(knowledge_id, scope, agent_type, issue_num)


def _delete_queue_task(task_id: int):
    """任务处理成功后直接 DELETE。"""
    with db_lock:
        db.cursor().execute(
            "DELETE FROM knowledge_write_queue WHERE id=?", (task_id,))
        db.commit()


def _fail_queue_task(task_id: int, error: str, retry_count: int):
    """任务处理失败——更新 last_error + next_retry_at（指数退避）。

    retry_count >= 100 → 标记 failed，不再重试（防止永久性错误无限重试）。
    """
    new_count = retry_count + 1
    delay = min(new_count * 3, 60)
    next_retry = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")

    # 监控日志（在 failed 分支之前，确保 count=100 也能触发）
    if new_count in (10, 50, 100):
        log.warning(f"[knowledge] queue task {task_id} retried {new_count} times, last_error: {error[:200]}")

    if new_count >= 100:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE knowledge_write_queue SET queue_status='failed', "
                "retry_count=?, last_error=?, updated_at=? "
                "WHERE id=?",
                (new_count, error[:500], next_retry, task_id))
            db.commit()
        log.warning(f"[knowledge] queue task {task_id} marked failed after {new_count} retries, last_error: {error[:200]}")
        return
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE knowledge_write_queue SET queue_status='pending', "
            "retry_count=?, last_error=?, next_retry_at=?, updated_at=? "
            "WHERE id=?",
            (new_count, error[:500], next_retry, next_retry, task_id))
        db.commit()


# ===== 第二层消费者：后台 worker =====

_worker_pool = None


def _get_worker_pool():
    """模块级全局 ThreadPoolExecutor 复用。"""
    global _worker_pool
    if _worker_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _worker_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kwq-worker")
    return _worker_pool


async def knowledge_write_worker():
    """后台 worker：每 3 秒轮询 pending 任务，线程池并行消费。

    同维度串行靠分布式锁保证。拿不到锁的跳过留 pending。
    """
    import asyncio
    while True:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 1. 捞 pending 任务
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT id, repo_key, scope, agent_type, issue_num, content, status, "
                    "source_seq, created_by, source, description, name, retry_count "
                    "FROM knowledge_write_queue "
                    "WHERE queue_status='pending' AND next_retry_at <= ? "
                    "ORDER BY created_at LIMIT 50", (now,)
                ).fetchall()
            if not rows:
                await asyncio.sleep(3)
                continue

            # 2. 按维度分组，每组取最老一条
            seen_dims = set()
            tasks_to_process = []
            for row in rows:
                dim = (row[1], row[2], row[3], row[4])  # repo_key, scope, agent_type, issue_num
                if dim not in seen_dims:
                    seen_dims.add(dim)
                    tasks_to_process.append(row)

            # 3. 线程池并行处理
            loop = asyncio.get_running_loop()
            futures = []
            for row in tasks_to_process:
                futures.append(loop.run_in_executor(_get_worker_pool(), _worker_process_task, row))
            if futures:
                await asyncio.gather(*futures, return_exceptions=True)

        except asyncio.CancelledError:
            log.info("Knowledge write worker cancelled")
            raise
        except Exception:
            log.exception("Knowledge write worker error")
        await asyncio.sleep(3)


def _worker_process_task(row):
    """worker 线程内处理单条任务。拿到锁后连续处理同维度所有 pending。"""
    task_id = row[0]
    repo_key, scope, agent_type, issue_num = row[1], row[2], row[3], row[4]
    content, status, source_seq, created_by = row[5], row[6], row[7], row[8]
    source, description, name, retry_count = row[9], row[10], row[11], row[12]

    lock_key = _knowledge_lock_key(repo_key, scope, agent_type, issue_num)
    if not acquire_lock(lock_key, ttl=60):
        return  # 拿不到锁，留 pending 下一轮

    try:
        # 处理当前任务（可能已被第一层 DELETE）
        _process_one_task(task_id, repo_key, scope, agent_type, issue_num,
                          content, status, source_seq, created_by,
                          source, description, name, retry_count)

        # 连续处理同维度剩余 pending 任务（不再重新抢锁，每条后续 renew）
        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with db_lock:
                next_row = db.cursor().execute(
                    "SELECT id, content, status, source_seq, created_by, "
                    "source, description, name, retry_count "
                    "FROM knowledge_write_queue "
                    "WHERE queue_status='pending' AND next_retry_at <= ? "
                    "AND repo_key=? AND scope=? AND agent_type=? AND issue_num=? "
                    "ORDER BY created_at LIMIT 1",
                    (now, repo_key, scope, agent_type, issue_num)
                ).fetchone()
            if not next_row:
                break
            # 续期锁，防止批量处理时锁过期（返回 False = 锁被别人抢走 → 中止）
            if not renew_lock(lock_key, ttl=60):
                log.warning(f"[knowledge] worker lock lost during batch for {repo_key}/{scope}, stopping")
                break
            _process_one_task(next_row[0], repo_key, scope, agent_type, issue_num,
                              next_row[1], next_row[2], next_row[3], next_row[4],
                              next_row[5], next_row[6], next_row[7], next_row[8])
    finally:
        release_lock(lock_key)


def _process_one_task(task_id, repo_key, scope, agent_type, issue_num,
                      content, status, source_seq, created_by,
                      source, description, name, retry_count):
    """处理单条队列任务。在锁内调用，不抢锁。"""
    # 再查任务是否存在（可能已被第一层 DELETE）
    with db_lock:
        exists = db.cursor().execute(
            "SELECT 1 FROM knowledge_write_queue WHERE id=?", (task_id,)).fetchone()
    if not exists:
        return  # 已被第一层消费

    try:
        _do_write_knowledge(
            repo_key=repo_key, scope=scope, agent_type=agent_type, issue_num=issue_num,
            content=content, status=status, source_seq=source_seq, created_by=created_by,
            source=source, description=description, name=name)
        _delete_queue_task(task_id)
    except Exception as e:
        _fail_queue_task(task_id, str(e), retry_count)


def _auto_generate_knowledge_ref(knowledge_id: int, scope: str, agent_type: str = "", issue_num: int = 0):
    """系统写入知识时按 scope 规则自动生成 scene_knowledge_ref 关联记录。

    Issue #103：失败时抛异常（不再 catch 吞掉），让上层任务重试。
    重试时内容去重会跳过文件/DB 写入，但走 ref 补生成路径（见 _do_write_knowledge 跳过分支）。
    """
    ref_map = {
        "baseline":    {"scene_id": "", "agent_type": "",           "issue_num": 0},
        "project":     {"scene_id": "", "agent_type": "",           "issue_num": 0},
        "issue":       {"scene_id": "", "agent_type": "",           "issue_num": issue_num},
        "issue_agent": {"scene_id": "", "agent_type": agent_type,   "issue_num": issue_num},
    }
    ref = ref_map.get(scope)
    if not ref:
        return
    with db_lock:
        db.cursor().execute(
            "INSERT OR IGNORE INTO scene_knowledge_ref "
            "(knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (knowledge_id, ref["scene_id"], ref["agent_type"], ref["issue_num"], "system")
        )
        db.commit()


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


def agent_knowledge_files(repo_key: str, agent_type: str = "", issue_num: int = 0, scene_id: str = "") -> dict:
    """查 ref 表返回关联的 agent 沉淀知识（source='agent'），供 worker 物化到 .knowledge/。

    Issue #95：source='agent' 过滤——只查 agent 沉淀知识，和 member_files_pi(source='manual') /
    load_project_info(scope='baseline') 不交叉。加 credential 过滤。
    返回 {rel: {content, obs_key}}，obs_key 供 generate_file_urls 生成 URL。
    """
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
                "AND k.source = 'agent' "
                "AND k.status = 'accepted' "
                "AND k.archived = 0",
                (scene_id, agent_type, issue_num, repo_key)
            ).fetchall()

        for kid, obs_key, file_path, content_hash in rows:
            if _is_credential_file(file_path):
                continue
            content = read_entry_content(repo_key, obs_key or "", file_path or "", content_hash or "")
            if content:
                files[file_path or ""] = {"content": content, "obs_key": obs_key or ""}
    except Exception:
        # 向后兼容：ref 表不存在时回退到 read_knowledge 三级 scope（只查 agent 知识）
        for scope, content in read_knowledge(repo_key, agent_type=agent_type, issue_num=issue_num).items():
            p = knowledge_path(repo_key, scope, agent_type=agent_type, issue_num=issue_num)
            rel = os.path.relpath(p, WORKSPACE_DIR)
            if content:
                files[rel] = {"content": content, "obs_key": ""}
    return files


# 默认值——配置文件 configs/credential-filter.json 不存在时使用
_DEFAULT_CREDENTIAL_SUBSTRINGS = (
    "credential", "token", "secret", "password", "passwd",
    "apikey", "api_key", "api-key",
    "private_key", "private-key",
    "access_key", "access-key",
    "repo-cr",
)
# Issue #95：精确匹配的凭证文件名（basename 完全匹配）
# "cr" 单独作为子串会误伤 cr-notes.md / scrum-cr.md
# 但 cr.json / cr.yaml 是凭证文件——用精确匹配避免误伤
_DEFAULT_CREDENTIAL_EXACT_NAMES = frozenset({
    "cr.json", "cr.yaml", "cr.yml", "cr.txt",
    "cr.env",
})
_DEFAULT_CREDENTIAL_EXTENSIONS = (".pem", ".key", ".env", ".p12", ".pfx")

def _load_credential_filter():
    """从 configs/credential-filter.json 加载凭证过滤配置，失败用默认值。"""
    try:
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "configs", "credential-filter.json")
        with open(config_path, encoding="utf-8") as f:
            cfg = json.loads(f.read())
        substrings = tuple(cfg.get("substrings") or _DEFAULT_CREDENTIAL_SUBSTRINGS)
        extensions = tuple(cfg.get("extensions") or _DEFAULT_CREDENTIAL_EXTENSIONS)
        return substrings, extensions
    except Exception:
        return _DEFAULT_CREDENTIAL_SUBSTRINGS, _DEFAULT_CREDENTIAL_EXTENSIONS

_CREDENTIAL_SUBSTRINGS, _CREDENTIAL_EXTENSIONS = _load_credential_filter()


def _is_credential_file(rel_path: str) -> bool:
    """按文件名判断是否凭证类文件（token/密钥/password 等），用于知识库物化前过滤。

    仅匹配文件名（basename），布尔子串 + 敏感扩展名 + 精确匹配；不拦普通文档与 pipelines-user.json。
    Issue #95：cr.json/cr.yaml 用精确匹配，避免 "cr" 子串误伤 cr-notes.md。
    """
    if not rel_path:
        return False
    name = os.path.basename(rel_path).lower()
    if name in _DEFAULT_CREDENTIAL_EXACT_NAMES:
        return True
    if name.endswith(_CREDENTIAL_EXTENSIONS):
        return True
    return any(sub in name for sub in _CREDENTIAL_SUBSTRINGS)


def member_files(repo_key: str) -> dict:
    """[Issue #95 废弃] 用户上传知识统一由 member_files_pi 走 ref 表注入，此函数保留向后兼容但不再被调用。"""
    return {}


def member_files_pi(repo_key: str, agent_type: str = "", issue_num: int = 0, scene_id: str = "") -> dict:
    """查 ref 表返回关联的用户知识（source='manual'），key 相对 project-info 目录。

    Issue #95：走 ref 表 source='manual' + 三维匹配，不再全量查 scope='member'。
    member 知识不自动关联——用户手动绑定到场景或 agent 才注入。
    ref 表里绑定到场景（scene_id 非空）或绑定到 agent（agent_type 非空）都算匹配。
    返回 {rel: {content, obs_key}}，obs_key 供 generate_file_urls 生成 URL。
    返回 key 统一 member/ 前缀（老数据 agent/{type}/ 归一化为 member/{type}/）。
    """
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
                "AND k.source = 'manual' "
                "AND k.status = 'accepted' "
                "AND k.archived = 0",
                (scene_id, agent_type, issue_num, repo_key)
            ).fetchall()
        for kid, obs_key, file_path, content_hash in rows:
            if _is_credential_file(file_path):
                continue
            ext = os.path.splitext(file_path)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            content = read_entry_content(repo_key, obs_key or "", file_path or "", content_hash or "")
            if content:
                # key 归一化：取 project-info/ 后面的部分，agent/ 替换为 member/
                if "project-info/" in file_path:
                    rel = file_path.split("project-info/", 1)[-1]
                else:
                    rel = file_path
                rel = rel.replace("\\", "/")
                if rel.startswith("agent/"):
                    rel = "member/" + rel[len("agent/"):]
                files[rel] = {"content": content, "obs_key": obs_key or ""}
    except Exception:
        pass  # ref 表异常时 member 不注入（符合"没关联不注入"设计）
    return files


def agent_files(repo_key: str, agent_type: str = "") -> dict:
    """[Issue #95 废弃] 用户上传知识统一由 member_files_pi 走 ref 表注入，此函数保留向后兼容但不再被调用。"""
    return {}


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
                        # 兼容 file_path 格式：write_knowledge/builder 用 WORKSPACE_DIR 相对（含 provider 前缀），
                        # member 文档：用 file_path 生成
                        from src.repo_key import workspace_repo_dir
                        _ws_repo = workspace_repo_dir(repo_key)
                        _ws_repo_rel = os.path.relpath(_ws_repo, WORKSPACE_DIR)
                        if file_path.startswith(_ws_repo_rel + "/"):
                            full_path = os.path.join(WORKSPACE_DIR, file_path)
                        else:
                            full_path = os.path.join(_ws_repo, file_path)
                        if not os.path.isfile(full_path):
                            _sync_not_found.add(rid)
                            log.error(f"[knowledge] sync skip id={rid}: file not found {full_path} (repo may be deleted)")
                            continue
                        content = ""
                        with open(full_path, encoding="utf-8") as f:
                            content = f.read()
                        if content:
                            # Issue #190：按 scope 生成 storage_key（含 provider 前缀）
                            from src.repo_key import knowledge_obs_prefix
                            _obs_prefix = knowledge_obs_prefix(repo_key)
                            if scope == "member":
                                # Issue #190：从 file_path 提取 member/ 或 agent/ 后的相对路径
                                # 不用 replace（file_path 含 owner/repo 时会导致路径重复）
                                for marker in ("member/", "agent/"):
                                    idx = file_path.find(marker)
                                    if idx >= 0:
                                        _rel = file_path[idx:]
                                        break
                                else:
                                    _rel = os.path.basename(file_path)
                                storage_key = f"{_obs_prefix}/{_rel}"
                            elif scope == "baseline":
                                _fname = os.path.basename(file_path)
                                storage_key = f"{_obs_prefix}/project-info/{_fname}"
                            else:
                                # 共享知识：project/issue/issue_agent
                                storage_key = knowledge_storage_key(repo_key, scope, agent_type or "", issue_num or 0)
                            storage.save_bytes(storage_key, content.encode("utf-8"))
                            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:64]
                            with db_lock:
                                c2 = db.cursor()
                                # Issue #95：UPDATE 补 source（COALESCE 空值不覆盖）
                                c2.execute(
                                    "UPDATE agent_shared_knowledge SET obs_key=?, content_hash=?, "
                                    "source=COALESCE(NULLIF(source, ''), CASE WHEN scope='baseline' THEN 'system' "
                                    "WHEN scope='member' THEN 'manual' ELSE 'agent' END) "
                                    "WHERE id=?",
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
            # Issue #103：清理 7 天前的 failed 队列记录
            try:
                _cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                with db_lock:
                    db.cursor().execute(
                        "DELETE FROM knowledge_write_queue "
                        "WHERE queue_status='failed' AND created_at < ?",
                        (_cutoff,))
                    db.commit()
            except Exception:
                pass
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

    Issue #103：加分布式锁保护，防止多实例同时写 baseline。
    """
    import hashlib as _hl
    from src.repo_key import split_owner_repo, knowledge_obs_prefix
    from src.storage import get_storage

    storage = get_storage()
    if not storage:
        return

    # Issue #103：per-repo 分布式锁，不同 repo 可并行同步
    BASELINE_FILES = ("project-graph.md", "project-capabilities.md", ".metadata.json")

    # 从 managed_repos 表获取所有项目
    with db_lock:
        rows = db.cursor().execute("SELECT repo_key FROM managed_repos").fetchall()

    for (repo_key,) in rows:
        # per-repo 锁：同 repo 串行，不同 repo 并行
        _repo_lock = f"project_info_build:{_hl.md5(repo_key.encode()).hexdigest()[:16]}"
        if not acquire_lock(_repo_lock, ttl=300):
            log.info(f"[knowledge] baseline sync: lock contention for {repo_key}, skipped")
            continue
        try:
            owner, repo = split_owner_repo(repo_key)
            if not owner or not repo:
                continue
            from src.repo_key import workspace_repo_dir
            pi_dir = os.path.join(workspace_repo_dir(repo_key), "project-info")
            if not os.path.isdir(pi_dir):
                continue

            for fname in BASELINE_FILES:
                fpath = os.path.join(pi_dir, fname)
                if not os.path.isfile(fpath):
                    continue

                file_path = os.path.relpath(fpath, WORKSPACE_DIR)

                # Issue #190：查重改用 name（不受 file_path 格式变化影响）
                _baseline_name = BASELINE_NAME_MAP.get(fname, fname)
                with db_lock:
                    row = db.cursor().execute(
                        "SELECT id, obs_key FROM agent_shared_knowledge "
                        "WHERE repo_key=? AND scope='baseline' AND name=? AND archived=0 "
                        "ORDER BY id DESC LIMIT 1",
                        (repo_key, _baseline_name)
                    ).fetchone()

                if row and row[1]:  # DB 有记录且 obs_key 非空
                    # Issue #190：检测旧格式 obs_key → 强制更新
                    _old_obs = row[1] or ""
                    _new_prefix = knowledge_obs_prefix(repo_key)
                    if _old_obs.startswith(_new_prefix):
                        continue  # 已是新格式 → 跳过
                    # OLD_A（project-info/ 前缀）或 OLD_B（knowledge/ 缺 provider）→ 走写入逻辑
                    # 不 continue，走下面的写入逻辑

                # 写 OBS + DB
                try:
                    with open(fpath, "rb") as f:
                        content_bytes = f.read()
                    # Issue #190：OBS key 含 provider 前缀
                    storage_key = f"{knowledge_obs_prefix(repo_key)}/project-info/{fname}"
                    storage.save_bytes(storage_key, content_bytes)
                    content_hash = _hl.sha256(content_bytes).hexdigest()[:64]

                    if row:  # DB 有记录但 obs_key 为空 → UPDATE
                        with db_lock:
                            c = db.cursor()
                            # Issue #95：UPDATE 补 source/name/description（COALESCE 空值不覆盖）
                            _bn = {"project-graph.md": "项目结构图谱", "project-capabilities.md": "项目能力清单", ".metadata.json": "项目元数据"}.get(fname, fname)
                            c.execute(
                                "UPDATE agent_shared_knowledge SET obs_key=?, content_hash=?, "
                                "source=COALESCE(NULLIF(source, ''), 'system'), "
                                "name=COALESCE(NULLIF(name, ''), ?) "
                                "WHERE id=?",
                                (storage_key, content_hash, _bn, row[0])
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
        finally:
            release_lock(_repo_lock)
