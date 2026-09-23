"""Knowledge 路由：项目知识看板聚合（项目级维度 + DevOps Agent 维度）。

见 docs/new-version2/knowledge-board-design.md：
- GET  /api/knowledge         聚合返回两类维度知识清单（项目级 + Agent 维度）
- POST /api/knowledge/entry   成员手动上传知识文档（md/txt/json，鉴权当前用户）
- GET  /api/knowledge/{id}    读取单个知识全文（按 id）
- PUT  /api/knowledge/{id}    更新知识内容
- DEL  /api/knowledge/{id}    删除知识条目（按 id）
"""
import os, time, uuid, hashlib, json
from src.repo_key import knowledge_obs_prefix
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from src.api.deps import get_current_user_or_api_key, require_no_session_token
from src.api.knowledge import read_entry_content
from src.config import log, WORKSPACE_DIR
from src.localfs import write_text as _atomic_write
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _repo_base(repo_key: str) -> str:
    from src.repo_key import workspace_repo_dir
    return workspace_repo_dir(repo_key)


def _resolve_repo_key(repo: str) -> str:
    """将前端传入的 owner/repo 解析为 DB 中实际使用的 repo_key。
    
    to_repo_key 默认 gitcode: 前缀，但 GitHub OAuth 仓库在 DB 中是 github: 前缀。
    查 managed_repos 表确定实际前缀，找不到则回退 to_repo_key 默认值。
    """
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    if ":" in repo:
        return repo_key  # 已带前缀，直接返回
    # 查 DB 确定实际前缀
    base = repo
    candidates = [f"gitcode:{base}", f"github:{base}"]
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            placeholders = ",".join("?" * len(candidates))
            row = c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({placeholders}) LIMIT 1", candidates).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return repo_key


def _check_repo_member(user: dict, repo_key: str):
    """校验知识读取权限。

    - 会话 token（`tk_`，devops agent 无 user_repos）：按 token 绑定的仓库作用域匹配
      （provider + owner/repo 一致才放行，防绑 A 读 B，见 issue-219 知识按需获取）。
    - 用户（JWT / `grape_` API Key）：仓库成员（托管者或已加入）。
    """
    if user.get("auth_type") == "session_token":
        from src.repo_key import parse_repo_key
        _tok_prov, _tok_owner, _tok_repo = parse_repo_key(user.get("repo_key") or "")
        _req_prov, _req_owner, _req_repo = parse_repo_key(repo_key)
        if _tok_owner and (_tok_prov, _tok_owner, _tok_repo) == (_req_prov, _req_owner, _req_repo):
            return
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问此仓库的知识库（仓库作用域不匹配）"})
    from src.user.db import get_user_repos
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无法识别用户身份"})
    try:
        repos = get_user_repos(user_id) or []
        is_member = any(r.get("repo_key") == repo_key for r in repos)
        if not is_member:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问此仓库的知识库"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "权限校验失败"})


@router.get("", operation_id="knowledge_list", summary="List knowledge (paged)", description="Unified knowledge list: paged rows from DB only (stats live in a separate endpoint).")
def list_knowledge(user: dict = Depends(get_current_user_or_api_key),
                         repo: str = Query("", description="repo_key"),
                         source: str = Query("", description="按来源筛选：system/agent/manual"),
                         page: int = Query(1, description="页码，从1开始"),
                         page_size: int = Query(50, description="每页条数，默认50"),
                         sort: str = Query("", description="排序：latest=按更新时间倒序"),
                         q: str = Query("", description="搜索关键词（匹配 name/description）")):
    """统一知识列表——只查 DB 返回分页列表（不含统计，统计走独立接口）。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    repo_key = _resolve_repo_key(repo)
    _check_repo_member(user, repo_key)

    from src.session_manager import db, db_lock

    # 构建 WHERE 条件
    where = "WHERE repo_key=?"
    params = [repo_key]
    if source:
        where += " AND source=?"
        params.append(source)
    if q:
        where += " AND (name LIKE ? OR description LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])

    with db_lock:
        c = db.cursor()
        total = c.execute(f"SELECT COUNT(*) FROM agent_shared_knowledge {where}", params).fetchone()[0]

        # 分页查询
        offset = (page - 1) * page_size
        rows = c.execute(
            f"SELECT id, scope, repo_key, agent_type, issue_num, file_path, status, "
            f"source_seq, created_by, created_at, obs_key, content_hash, updated_at, source, archived, description, name "
            f"FROM agent_shared_knowledge {where} "
            f"ORDER BY {'updated_at DESC,' if sort == 'latest' else ''} id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

    # 构建统一列表（不读 content，只返回 description——提高性能）
    all_knowledge = []
    for r in rows:
        all_knowledge.append({
            "id": r[0],
            "scope": r[1],
            "repo_key": r[2],
            "agent_type": r[3] or "",
            "issue_num": r[4] or 0,
            "file_path": r[5] or "",
            "status": r[6] or "accepted",
            "source_seq": r[7] or 0,
            "created_by": r[8] or "",
            "created_at": normalize_event_time(r[9] or ""),
            "obs_key": r[10] or "",
            "source": r[13] or "",
            "description": r[15] or "",
            "name": r[16] or "",
            "issue_tag": f"Issue #{r[4]}" if r[4] and r[1] in ("issue", "issue_agent") else "",
        })

    # 查关联记录
    kid_list = [k["id"] for k in all_knowledge if k["id"]]
    if kid_list:
        placeholders = ",".join("?" * len(kid_list))
        try:
            with db_lock:
                ref_rows = db.cursor().execute(
                    f"SELECT r.knowledge_id, r.scene_id, r.agent_type, r.enabled, s.name "
                    f"FROM scene_knowledge_ref r "
                    f"LEFT JOIN devops_pipeline_scene s ON r.scene_id = s.scene_id AND s.repo_key = ? AND s.is_active=1 "
                    f"WHERE r.knowledge_id IN ({placeholders})",
                    [repo_key] + kid_list
                ).fetchall()
            ref_map = {}
            for kid, sid, at, en, sname in ref_rows:
                ref_map.setdefault(kid, []).append(
                    {"scene_id": sid, "agent_type": at, "enabled": en, "scene_name": sname or ""})
            for k in all_knowledge:
                refs = ref_map.get(k["id"], [])
                k["refs"] = refs
                k["ref_summary"] = _build_ref_summary(refs)
        except Exception:
            for k in all_knowledge:
                k["refs"] = []
                k["ref_summary"] = "未关联"
    else:
        for k in all_knowledge:
            k["refs"] = []
            k["ref_summary"] = "未关联"

    return {
        "repo_key": repo_key,
        "knowledge": all_knowledge,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats", operation_id="knowledge_stats", summary="Knowledge stats", description="Knowledge counts grouped by system/agent/manual/unlinked.")
def knowledge_stats(user: dict = Depends(get_current_user_or_api_key),
                     repo: str = Query("")):
    """独立统计接口——返回 system/agent/manual/unlinked 数量。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    repo_key = _resolve_repo_key(repo)
    _check_repo_member(user, repo_key)

    from src.session_manager import db, db_lock
    with db_lock:
        stats_row = db.cursor().execute(
            "SELECT "
            "SUM(CASE WHEN source='system' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN source='agent' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN source='manual' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN id NOT IN (SELECT DISTINCT knowledge_id FROM scene_knowledge_ref WHERE enabled=1) THEN 1 ELSE 0 END) "
            "FROM agent_shared_knowledge WHERE repo_key=?",
            (repo_key,)
        ).fetchone()
    return {
        "system": stats_row[0] or 0,
        "agent": stats_row[1] or 0,
        "manual": stats_row[2] or 0,
        "unlinked": stats_row[3] or 0,
    }


# ===== Issue #67：知识 CRUD + 关联管理 + 归档 API =====

class KnowledgeEntryBody(BaseModel):
    repo: str = ""
    content: str = ""
    agent_type: str = ""
    description: str = ""
    name: str = ""
    scene_ids: list[str] = []
    agent_types: list[str] = []

class KnowledgeUpdateBody(BaseModel):
    content: str = ""
    description: str = ""
    name: str = ""

MAX_NAME_LEN = 50
MAX_DESC_LEN = 200
MAX_CONTENT_BYTES = 10 * 1024
VALID_AGENT_TYPES = {'vod', 'issue', 'coding', 'qa', 'sre', 'chat'}

# name 是展示名，不强制唯一——用 id 做唯一标识
# 如需重名警告，前端可查列表对比提示，后端不拦截

class KnowledgeRefBody(BaseModel):
    scene_id: str = ""
    agent_type: str = ""

class KnowledgeRefBatchBody(BaseModel):
    scene_ids: list[str] = []
    agent_types: list[str] = []

class KnowledgeRefUpdateBody(BaseModel):
    enabled: bool = True



def _build_ref_summary(refs: list) -> str:
    """根据关联记录生成摘要文本。"""
    if not refs:
        return "未关联"
    enabled_refs = [r for r in refs if r.get("enabled")]
    if not enabled_refs:
        return "未关联"
    scene_count = sum(1 for r in enabled_refs if r.get("scene_id"))
    agent_types = [r.get("agent_type") for r in enabled_refs if r.get("agent_type")]
    parts = []
    if scene_count:
        parts.append(f"已关联 {scene_count} 个场景")
    if agent_types:
        parts.append(f"已关联 {', '.join(set(agent_types))}")
    return " · ".join(parts) if parts else "已关联"


def _get_knowledge_by_id(knowledge_id: int):
    """按 id 查询单条知识记录。"""
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute(
            "SELECT id, scope, repo_key, agent_type, issue_num, file_path, "
            "status, source_seq, created_by, created_at, obs_key, content_hash, "
            "updated_at, source, archived, description, name "
            "FROM agent_shared_knowledge WHERE id = ?",
            (knowledge_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "scope": row[1], "repo_key": row[2],
        "agent_type": row[3] or "", "issue_num": row[4] or 0,
        "file_path": row[5], "status": row[6] or "accepted",
        "source_seq": row[7] or 0, "created_by": row[8] or "",
        "created_at": row[9] or "", "obs_key": row[10] or "",
        "content_hash": row[11] or "", "updated_at": row[12] or "",
        "source": row[13] or "", "archived": row[14] or 0,
        "description": row[15] or "",
        "name": row[16] or "",
    }


def _check_ref_permission(knowledge_id: int, user: dict):
    """校验用户是否有权限管理该知识的关联。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})
    if row["source"] == "manual":
        if row["created_by"] != user.get("username"):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可管理关联"})
    else:
        # system/agent 知识——仓库托管者可管理
        from src.user.db import get_user_repos
        user_id = user.get("id")
        repo_key = row.get("repo_key", "")
        try:
            repos = get_user_repos(user_id) or []
            is_admin = any(r.get("repo_key") == repo_key and r.get("is_admin") for r in repos)
            if not is_admin:
                raise HTTPException(status_code=403, detail={"code": 403, "message": "仅仓库托管者可管理系统知识的关联"})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "权限校验失败"})
    return row


def _update_knowledge_content(knowledge_id: int, row: dict, new_content: str):
    """更新知识内容：写文件 + OBS + DB content_hash + updated_at。"""
    repo_key = row["repo_key"]
    file_path = row["file_path"]
    from src.repo_key import workspace_repo_dir
    from src.localfs import safe_workspace_path
    try:
        full = safe_workspace_path(file_path)
    except ValueError:
        # 旧格式 file_path 不含 provider/owner/repo 前缀
        full = os.path.normpath(os.path.join(workspace_repo_dir(repo_key), file_path))
    _atomic_write(full, new_content)

    obs_key = row.get("obs_key", "")
    content_hash = ""
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None and obs_key:
            with open(full, "rb") as f:
                file_bytes = f.read()
            storage.save_bytes(obs_key, file_bytes)
            content_hash = hashlib.sha256(file_bytes).hexdigest()
    except Exception as e:
        log.warning(f"[knowledge] OBS update failed for {obs_key}: {e}")

    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_shared_knowledge "
            "SET content_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (content_hash, knowledge_id)
        )
        db.commit()


def _delete_knowledge_full(knowledge_id: int, row: dict):
    """删除知识：OBS 对象 + 本地文件 + DB 记录 + 关联记录。"""
    from src.session_manager import db, db_lock

    obs_key = row.get("obs_key", "")
    if obs_key:
        try:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                storage.delete_object(obs_key)
        except Exception as e:
            log.warning(f"[knowledge] OBS delete failed for {obs_key}: {e}")

    file_path = row.get("file_path", "")
    if file_path:
        from src.repo_key import workspace_repo_dir
        from src.localfs import safe_workspace_path
        try:
            full = safe_workspace_path(file_path)
        except ValueError:
            # 旧格式 file_path 不含 provider/owner/repo 前缀
            full = os.path.normpath(os.path.join(workspace_repo_dir(row.get("repo_key", "")), file_path))
        if os.path.isfile(full):
            try:
                os.remove(full)
            except Exception:
                pass

    with db_lock:
        db.cursor().execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id = ?", (knowledge_id,))
        db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE id = ?", (knowledge_id,))
        db.commit()


# ===== CRUD API（固定路径在 {id} 之前） =====

@router.post("/entry", operation_id="knowledge_entry_create", summary="Create knowledge entry", description="Creates a knowledge entry from plain text (source=manual).")
def create_knowledge_entry(body: KnowledgeEntryBody, user: dict = Depends(require_no_session_token)):
    """用户直接输入文本创建知识条目（source=manual）。

    Issue #199：支持 scene_ids / agent_types 参数，创建条目 + 绑定关联在同一个事务内完成。
    校验在 INSERT 之前——校验失败不 commit，自动回滚，无副作用。
    """
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "name 必填"})
    if len(body.name.strip()) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"name 不能超过 {MAX_NAME_LEN} 字符"})
    if not (body.content or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "content 必填"})
    if len(body.content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "content 不能超过 10KB"})
    if body.description and len(body.description) > MAX_DESC_LEN:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"description 不能超过 {MAX_DESC_LEN} 字符"})
    if not body.repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})

    scene_ids = body.scene_ids or []
    agent_types = body.agent_types or []

    # 校验 agent_types（内存比对，不查 DB）
    if agent_types:
        invalid = [at for at in agent_types if at not in VALID_AGENT_TYPES]
        if invalid:
            raise HTTPException(status_code=400,
                detail={"code": 400, "message": f"不支持的 Agent 类型: {invalid}"})

    repo_key = _resolve_repo_key(body.repo)
    _check_repo_member(user, repo_key)

    # 校验 scene_ids 存在性（批量查，不循环）
    if scene_ids:
        from src.session_manager import db, db_lock
        with db_lock:
            placeholders = ",".join("?" * len(scene_ids))
            valid_rows = db.cursor().execute(
                f"SELECT scene_id FROM devops_pipeline_scene "
                f"WHERE is_active=1 AND repo_key=? AND scene_id IN ({placeholders})",
                [repo_key] + scene_ids
            ).fetchall()
        valid_ids = {r[0] for r in valid_rows}
        invalid = [sid for sid in scene_ids if sid not in valid_ids]
        if invalid:
            raise HTTPException(status_code=400,
                detail={"code": 400, "message": f"场景不存在: {invalid}"})

    base = _repo_base(repo_key)
    username = str(user.get("username") or user.get("id") or "user")
    filename = f"entry_{int(time.time())}_{uuid.uuid4().hex[:6]}.md"
    member_dir = os.path.join(base, "project-info", "member", username)
    file_path = os.path.join(member_dir, filename)
    _atomic_write(file_path, body.content.strip())

    rel = os.path.relpath(file_path, WORKSPACE_DIR)
    content_hash = hashlib.sha256(body.content.encode("utf-8")).hexdigest()

    obs_key = ""
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            obs_key = f"{knowledge_obs_prefix(repo_key)}/member/{username}/{filename}"
            with open(file_path, "rb") as f:
                storage.save_bytes(obs_key, f.read())
    except Exception as e:
        log.warning(f"[knowledge] entry OBS save failed: {e}")

    # 构建关联列表
    items = [(sid, "") for sid in scene_ids] + [("", at) for at in agent_types]

    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            # 1. INSERT 条目
            c.execute(
                "INSERT INTO agent_shared_knowledge "
                "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, description, name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("member", repo_key, body.agent_type or "", 0, rel, obs_key, content_hash, "accepted", 0, username, "manual", body.description or "", body.name.strip())
            )
            kid = c.lastrowid

            # 2. 绑定关联（幂等，跳过已存在）
            for scene_id, agent_type in items:
                if not scene_id and not agent_type:
                    continue
                existing = c.execute(
                    "SELECT id FROM scene_knowledge_ref "
                    "WHERE knowledge_id=? AND scene_id=? AND agent_type=?",
                    (kid, scene_id, agent_type)
                ).fetchone()
                if not existing:
                    c.execute(
                        "INSERT INTO scene_knowledge_ref "
                        "(knowledge_id, scene_id, agent_type, enabled, created_by) "
                        "VALUES (?, ?, ?, 1, ?)",
                        (kid, scene_id, agent_type, username)
                    )

            # 3. 全部成功才 commit
            db.commit()
    except Exception as e:
        # DB 失败——回滚事务 + 清理本地文件 + OBS 对象，避免残留
        try:
            db.rollback()
        except Exception:
            pass
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception:
            pass
        if obs_key:
            try:
                from src.storage import get_storage
                storage = get_storage()
                if storage is not None:
                    storage.delete_object(obs_key)
            except Exception:
                pass
        log.warning(f"[knowledge] entry create failed, cleaned up {file_path}: {e}")
        raise

    return {"ok": True, "message": "知识已创建", "file_path": rel, "id": kid}


# ===== {id} 路由 =====

@router.get("/{knowledge_id}", operation_id="knowledge_detail", summary="Knowledge detail", description="Returns a single knowledge full text by id.")
def get_knowledge_detail(knowledge_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """按 id 查询单条知识全文。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})

    content = read_entry_content(row["repo_key"], row.get("obs_key", ""), row.get("file_path", ""), row.get("content_hash", ""))

    from src.session_manager import db, db_lock
    with db_lock:
        refs = db.cursor().execute(
            "SELECT r.id, r.scene_id, r.agent_type, r.enabled, r.created_by, r.created_at, "
            "s.name, s.description, s.display_name, s.scene_id, s.nodes_json "
            "FROM scene_knowledge_ref r "
            "LEFT JOIN devops_pipeline_scene s ON r.scene_id = s.scene_id AND s.repo_key = ? AND s.is_active=1 "
            "WHERE r.knowledge_id = ? ORDER BY r.id",
            (row["repo_key"], knowledge_id)
        ).fetchall()

    # Issue #199: 批量查 pipeline 信息（消除 N+1——原来逐个调 get_pipeline_config）
    ref_list = []
    for r in refs:
        scene_name = r[6] or ""
        scene_description = r[7] or ""
        scene_display_name = r[8] or scene_name
        # nodes_json 已在 JOIN 中查出，直接解析
        pipeline_chain = ""
        if r[10]:
            try:
                nodes = json.loads(r[10])
                pipeline_chain = " → ".join(n.get("tag", n.get("id", "")) for n in nodes if n.get("enabled", True))
            except Exception:
                pass
        ref_list.append({
            "id": r[0], "scene_id": r[1], "agent_type": r[2], "enabled": r[3],
            "created_by": r[4], "created_at": r[5],
            "scene_name": scene_name, "scene_display_name": scene_display_name,
            "scene_description": scene_description,
            "scene_pipeline": pipeline_chain,
        })

    return {
        "id": knowledge_id,
        "source": row["source"], "scope": row["scope"],
        "repo_key": row["repo_key"], "agent_type": row["agent_type"],
        "issue_num": row["issue_num"], "file_path": row["file_path"],
        "obs_key": row["obs_key"], "content": content,
        "archived": row["archived"], "created_by": row["created_by"],
        "created_at": row["created_at"], "description": row.get("description", ""),
        "name": row.get("name", ""),
        "refs": ref_list,
    }


@router.put("/{knowledge_id}", operation_id="knowledge_update", summary="Update knowledge", description="Edits knowledge content (only source=manual).")
def update_knowledge(knowledge_id: int, body: KnowledgeUpdateBody,
                     user: dict = Depends(require_no_session_token)):
    """编辑知识内容，仅 source=manual 可编辑。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})
    if row["source"] != "manual":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "系统生成的知识不可编辑"})
    if row["created_by"] != user.get("username"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可编辑"})

    # 校验
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "name 必填"})
    if len(body.name.strip()) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"name 不能超过 {MAX_NAME_LEN} 字符"})
    if not (body.content or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "content 必填"})
    if len(body.content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "content 不能超过 10KB"})
    if body.description and len(body.description) > MAX_DESC_LEN:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"description 不能超过 {MAX_DESC_LEN} 字符"})

    _update_knowledge_content(knowledge_id, row, body.content)

    # 更新描述 + 名称
    if body.description is not None or body.name is not None:
        from src.session_manager import db, db_lock
        with db_lock:
            sets = []
            vals = []
            if body.description is not None:
                sets.append("description=?")
                vals.append(body.description)
            if body.name is not None and body.name.strip():
                sets.append("name=?")
                vals.append(body.name.strip())
            if sets:
                vals.append(knowledge_id)
                db.cursor().execute(
                    f"UPDATE agent_shared_knowledge SET {', '.join(sets)} WHERE id=?",
                    vals
                )
                db.commit()

    return {"ok": True, "message": "知识已更新"}


@router.delete("/{knowledge_id}", operation_id="knowledge_delete", summary="Delete knowledge entry", description="Deletes a source=manual knowledge entry.")
def delete_knowledge_by_id(knowledge_id: int, user: dict = Depends(require_no_session_token)):
    """删除 source=manual 的知识条目。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})
    if row["source"] != "manual":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "系统生成的知识不可删除"})
    if row["created_by"] != user.get("username"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可删除"})

    _delete_knowledge_full(knowledge_id, row)
    return {"ok": True, "message": "知识已删除"}




# ===== 关联管理 API =====

@router.get("/{knowledge_id}/refs", operation_id="knowledge_refs_list", summary="List knowledge refs", description="Lists all refs of a knowledge item.")
def get_knowledge_refs(knowledge_id: int, user: dict = Depends(require_no_session_token)):
    """获取知识的所有关联关系。"""
    _check_ref_permission(knowledge_id, user)
    from src.session_manager import db, db_lock
    with db_lock:
        rows = db.cursor().execute(
            "SELECT id, scene_id, agent_type, enabled, created_by, created_at, updated_at, updated_by "
            "FROM scene_knowledge_ref WHERE knowledge_id = ? ORDER BY id",
            (knowledge_id,)
        ).fetchall()
    return {"refs": [{"id": r[0], "scene_id": r[1], "agent_type": r[2], "enabled": r[3],
                        "created_by": r[4], "created_at": r[5], "updated_at": r[6], "updated_by": r[7]}
                       for r in rows]}


@router.post("/{knowledge_id}/refs", operation_id="knowledge_ref_create", summary="Add knowledge ref", description="Adds a scene/agent ref to a knowledge item.")
def add_knowledge_ref(knowledge_id: int, body: KnowledgeRefBody,
                      user: dict = Depends(require_no_session_token)):
    """为知识新增场景/Agent 关联。"""
    _check_ref_permission(knowledge_id, user)

    # 校验关联数量——同一 scene_id + agent_type 最多 20 条
    from src.session_manager import db, db_lock
    scene_id = body.scene_id or ""
    agent_type = body.agent_type or ""
    # Issue #95：禁止空关联——scene_id 和 agent_type 不能同时为空（否则全局注入）
    if not scene_id and not agent_type:
        raise HTTPException(status_code=400,
            detail={"code": 400, "message": "scene_id 和 agent_type 不能同时为空，必须绑定到场景或 Agent"})
    with db_lock:
        count = db.cursor().execute(
            "SELECT COUNT(*) FROM scene_knowledge_ref "
            "WHERE knowledge_id=? AND scene_id=? AND agent_type=? AND enabled=1",
            (knowledge_id, scene_id, agent_type)
        ).fetchone()[0]
    if count >= 20:
        raise HTTPException(status_code=400,
            detail={"code": 400, "message": "每个场景每个 Agent 节点最多关联 20 条知识"})

    username = str(user.get("username") or "")
    try:
        with db_lock:
            c = db.cursor()
            # 先查有没有已存在（含 enabled=0 软删除）的记录
            existing = c.execute(
                "SELECT id, enabled FROM scene_knowledge_ref "
                "WHERE knowledge_id=? AND scene_id=? AND agent_type=?",
                (knowledge_id, scene_id, agent_type)
            ).fetchone()
            if existing:
                # 已存在 → UPDATE enabled=1（恢复软删除的记录）
                if existing[1] == 0:
                    c.execute(
                        "UPDATE scene_knowledge_ref SET enabled=1, updated_by=? WHERE id=?",
                        (username, existing[0])
                    )
                    db.commit()
                # enabled=1 的记录直接跳过（已绑定）
            else:
                c.execute(
                    "INSERT INTO scene_knowledge_ref "
                    "(knowledge_id, scene_id, agent_type, enabled, created_by) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (knowledge_id, scene_id, agent_type, username)
                )
                db.commit()
    except Exception as e:
        err_msg = str(e).upper()
        if "UNIQUE" in err_msg or "DUPLICATE" in err_msg:
            raise HTTPException(status_code=409,
                detail={"code": 409, "message": "该知识已绑定此场景/Agent 类型"})
        raise
    return {"ok": True}


@router.post("/{knowledge_id}/refs/batch", operation_id="knowledge_add_refs_batch",
             summary="Add knowledge refs (batch)",
             description="Batch-adds scene/agent refs to a knowledge item (idempotent, skips existing).")
def add_knowledge_refs_batch(knowledge_id: int, body: KnowledgeRefBatchBody,
                              user: dict = Depends(require_no_session_token)):
    """批量新增场景/Agent 关联（幂等，跳过已存在的）。"""
    row = _check_ref_permission(knowledge_id, user)
    username = str(user.get("username") or "")
    from src.session_manager import db, db_lock

    scene_ids = body.scene_ids or []
    agent_types = body.agent_types or []

    # Issue #199: 校验 agent_types（内存比对，不查 DB）
    if agent_types:
        invalid = [at for at in agent_types if at not in VALID_AGENT_TYPES]
        if invalid:
            raise HTTPException(status_code=400,
                detail={"code": 400, "message": f"不支持的 Agent 类型: {invalid}"})

    # Issue #199: 校验 scene_ids 存在性（批量查，不循环）
    repo_key = row.get("repo_key", "")
    if scene_ids:
        with db_lock:
            placeholders = ",".join("?" * len(scene_ids))
            valid_rows = db.cursor().execute(
                f"SELECT scene_id FROM devops_pipeline_scene "
                f"WHERE is_active=1 AND repo_key=? AND scene_id IN ({placeholders})",
                [repo_key] + scene_ids
            ).fetchall()
        valid_ids = {r[0] for r in valid_rows}
        invalid = [sid for sid in scene_ids if sid not in valid_ids]
        if invalid:
            raise HTTPException(status_code=400,
                detail={"code": 400, "message": f"场景不存在: {invalid}"})

    added = 0
    items = []
    for sid in scene_ids:
        items.append((sid, ""))
    for at in agent_types:
        items.append(("", at))
    try:
        with db_lock:
            c = db.cursor()
            for scene_id, agent_type in items:
                # 禁止空关联——scene_id 和 agent_type 不能同时为空（否则全局注入）
                if not scene_id and not agent_type:
                    continue
                existing = c.execute(
                    "SELECT id FROM scene_knowledge_ref "
                    "WHERE knowledge_id=? AND scene_id=? AND agent_type=?",
                    (knowledge_id, scene_id, agent_type)
                ).fetchone()
                if not existing:
                    c.execute(
                        "INSERT INTO scene_knowledge_ref "
                        "(knowledge_id, scene_id, agent_type, enabled, created_by) "
                        "VALUES (?, ?, ?, 1, ?)",
                        (knowledge_id, scene_id, agent_type, username)
                    )
                    added += 1
            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        err_msg = str(e).upper()
        if "UNIQUE" in err_msg or "DUPLICATE" in err_msg:
            # 并发竞态：另一个请求已插入同一条记录，视为幂等成功
            try:
                with db_lock:
                    count = db.cursor().execute(
                        "SELECT COUNT(*) FROM scene_knowledge_ref "
                        "WHERE knowledge_id=? AND enabled=1",
                        (knowledge_id,)
                    ).fetchone()[0]
                return {"ok": True, "added": 0, "message": "关联已存在（并发去重）", "total": count}
            except Exception:
                raise HTTPException(status_code=409,
                    detail={"code": 409, "message": "该知识已绑定此场景/Agent 类型"})
        raise
    return {"ok": True, "added": added}


@router.put("/ref/{ref_id}", operation_id="knowledge_ref_update", summary="Update knowledge ref", description="Toggles a ref's enabled switch.")
def update_knowledge_ref(ref_id: int, body: KnowledgeRefUpdateBody,
                         user: dict = Depends(require_no_session_token)):
    """修改关联的 enabled 开关。"""
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute(
            "SELECT k.repo_key FROM scene_knowledge_ref r "
            "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
            "WHERE r.id = ?", (ref_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "关联记录不存在"})
    _check_repo_member(user, row["repo_key"] if isinstance(row, dict) else row[0])
    username = str(user.get("username") or "")
    with db_lock:
        db.cursor().execute(
            "UPDATE scene_knowledge_ref SET enabled = ?, updated_by = ? WHERE id = ?",
            (1 if body.enabled else 0, username, ref_id)
        )
        db.commit()
    return {"ok": True}


@router.delete("/ref/{ref_id}", operation_id="knowledge_ref_delete", summary="Delete knowledge ref", description="Deletes a ref of a knowledge item.")
def delete_knowledge_ref(ref_id: int, user: dict = Depends(require_no_session_token)):
    """删除知识的某条关联。"""
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute(
            "SELECT k.repo_key FROM scene_knowledge_ref r "
            "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
            "WHERE r.id = ?", (ref_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "关联记录不存在"})
    _check_repo_member(user, row["repo_key"] if isinstance(row, dict) else row[0])
    with db_lock:
        db.cursor().execute("DELETE FROM scene_knowledge_ref WHERE id = ?", (ref_id,))
        db.commit()
    return {"ok": True}
