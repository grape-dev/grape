"""Knowledge 路由：项目知识看板聚合（项目级维度 + DevOps Agent 维度）。

见 docs/new-version2/knowledge-board-design.md：
- GET  /api/knowledge         聚合返回两类维度知识清单（项目级 + Agent 维度）
- GET  /api/knowledge/content 读取单个知识全文（路径穿越防护）
- GET  /api/knowledge/context 返回合并注入上下文文本
- POST /api/knowledge         成员手动上传知识文档（md/txt/json，鉴权当前用户）
- DEL  /api/knowledge         删除成员上传的知识
"""
import os, re, time, uuid, hashlib
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.config import log, WORKSPACE_DIR
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

ALLOWED_EXTS = {".md", ".txt", ".json"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_SAFE_REL = re.compile(r"^[\w.\-/]+$")


def _owner_repo(repo_key: str) -> tuple[str, str]:
    from src.repo_key import split_owner_repo
    try:
        return split_owner_repo(repo_key)
    except Exception:
        return "", ""


def _repo_base(repo_key: str) -> str:
    from src.config import WORKSPACE_DIR
    owner, rname = _owner_repo(repo_key)
    return os.path.join(WORKSPACE_DIR, owner, rname)


def _safe_rel(rel: str) -> str:
    """白名单相对路径（防路径穿越，只读工作区仓库目录内）。"""
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel or not _SAFE_REL.match(rel) or rel.startswith(".."):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})
    return rel


def _read_file(repo_key: str, rel: str) -> str:
    rel = _safe_rel(rel)
    ext = os.path.splitext(rel)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"仅支持 {sorted(ALLOWED_EXTS)}"})

    # OBS 分支：查 DB obs_key，非空走 storage.read_bytes
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            row = db.cursor().execute(
                "SELECT obs_key FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? "
                "ORDER BY id DESC LIMIT 1",
                (repo_key, rel)).fetchone()
        if row and row[0]:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                data = storage.read_bytes(row[0])
                if data is not None:
                    return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"OBS read failed for {repo_key}/{rel}: {e} — fallback to local")

    # 本地模式（storage 为 None 或 OBS miss）
    base = _repo_base(repo_key)
    full = os.path.normpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"文件不存在: {rel}"})
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})


def _read_ws_file(repo_key: str, ws_rel: str) -> str:
    """读 WORKSPACE_DIR 相对路径（agent_shared_knowledge.file_path 语义，防路径穿越）。

    OBS 模式：查 DB obs_key → OBS 读取（不依赖本地磁盘）。
    本地模式：open 本地文件（保持原行为）。
    """
    from src.config import WORKSPACE_DIR
    rel = _safe_rel(ws_rel)

    # OBS 分支：查 DB obs_key → OBS 读取
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT obs_key FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? "
                "ORDER BY id DESC LIMIT 1",
                (repo_key, rel)).fetchone()
        if row and row[0]:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                data = storage.read_bytes(row[0])
                if data is not None:
                    return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"OBS read failed for {repo_key}/{rel}: {e} — fallback to local")

    # 本地模式（OBS 未启用 / OBS miss）
    full = os.path.normpath(os.path.join(WORKSPACE_DIR, rel))
    if full != WORKSPACE_DIR and not full.startswith(WORKSPACE_DIR + os.sep):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})
    ext = os.path.splitext(rel)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"仅支持 {sorted(ALLOWED_EXTS)}"})
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"文件不存在: {rel}"})
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})


def _read_entry_content(repo_key: str, obs_key: str, file_path: str) -> str:
    """读取知识条目正文：obs_key 非空走 OBS，否则走本地文件。失败返回空串（不影响列表聚合）。"""
    if obs_key:
        try:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                data = storage.read_bytes(obs_key)
                if data is not None:
                    return data.decode("utf-8", errors="replace")
        except Exception as e:
            log.warning(f"OBS read failed for entry {repo_key}/{obs_key}: {e} — fallback to local")
    # 本地模式（storage 为 None / obs_key 为空 / OBS miss）
    if file_path:
        try:
            return _read_ws_file(repo_key, file_path)
        except HTTPException:
            return ""
    return ""


def _member_docs(repo_key: str) -> list[dict]:
    """扫描成员上传目录 project-info/member/ 与 project-info/agent/，返回文档清单。

    agent 文档带 agent_type 标注（§六：Agent 维度上传落盘 project-info/agent/{agent_type}/）。

    OBS 模式：查 DB scope='member' 记录构建清单（不依赖本地磁盘）。
    本地模式（OBS 未启用）：回退 os.listdir 扫描本地目录（保持原行为）。
    """
    from src.session_manager import db, db_lock
    from src.storage import get_storage

    storage = get_storage()
    if storage is not None:
        # OBS 模式：查 DB 构建清单
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, file_path, agent_type, created_by, created_at, obs_key "
                "FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope='member' "
                "AND obs_key!='' AND obs_key IS NOT NULL "
                "ORDER BY id DESC",
                (repo_key,)
            )
            rows = c.fetchall()
        docs = []
        seen = set()  # file_path 去重（同文件多版本只展示最新）
        for rid, file_path, agent_type, created_by, created_at, obs_key in rows:
            if not file_path or file_path in seen:
                continue
            seen.add(file_path)
            # 从 file_path 解析 kind 和 owner
            # file_path 格式：project-info/member/{username}/{filename} 或 project-info/agent/{agent_type}/{filename}
            parts = file_path.replace("\\", "/").split("/")
            if len(parts) >= 4 and parts[0] == "project-info":
                kind = parts[1]  # "member" 或 "agent"
                owner = parts[2] if kind == "member" else (agent_type or parts[2])
                name = parts[-1]
            else:
                kind = "member"
                owner = created_by or ""
                name = os.path.basename(file_path)
            docs.append({
                "name": name,
                "owner": owner,
                "path": file_path,
                "obs_key": obs_key or "",
                "size": 0,  # OBS 模式不查对象 size（列表展示不需要精确值）
                "updated_at": normalize_event_time(created_at or ""),
                "kind": kind,
            })
        return docs

    # 本地模式（OBS 未启用）：保持原有 os.listdir 逻辑
    docs = []
    for sub in ("member", "agent"):
        base = os.path.join(_repo_base(repo_key), "project-info", sub)
        if not os.path.isdir(base):
            continue
        try:
            for owner_dir in sorted(os.listdir(base)):
                d = os.path.join(base, owner_dir)
                if not os.path.isdir(d):
                    continue
                for f in sorted(os.listdir(d)):
                    fp = os.path.join(d, f)
                    if not os.path.isfile(fp):
                        continue
                    rel = os.path.relpath(fp, _repo_base(repo_key)).replace("\\", "/")
                    docs.append({
                        "name": f,
                        "owner": owner_dir,
                        "path": rel,
                        "obs_key": "",
                        "size": os.path.getsize(fp),
                        "updated_at": _fmt_time(os.path.getmtime(fp)),
                        "kind": sub,
                    })
        except Exception as e:
            log.warning(f"member docs scan failed {repo_key} ({sub}): {e}")
    return docs


def _fmt_time(ts) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _check_repo_member(user: dict, repo_key: str):
    """校验用户是否是当前仓库的成员（托管者或已加入）。"""
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


@router.get("", operation_id="knowledge_list", summary="知识清单（统一列表 + 分页）")
def list_knowledge(user: dict = Depends(get_current_user_or_api_key),
                         repo: str = Query("", description="repo_key"),
                         source: str = Query("", description="按来源筛选：system/agent/manual"),
                         page: int = Query(1, description="页码，从1开始"),
                         page_size: int = Query(50, description="每页条数，默认50")):
    """统一知识列表——只查 DB 返回分页列表（不含统计，统计走独立接口）。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    _check_repo_member(user, repo_key)

    from src.session_manager import db, db_lock

    # 构建 WHERE 条件
    where = "WHERE repo_key=? AND archived=0"
    params = [repo_key]
    if source:
        where += " AND source=?"
        params.append(source)

    with db_lock:
        c = db.cursor()
        total = c.execute(f"SELECT COUNT(*) FROM agent_shared_knowledge {where}", params).fetchone()[0]

        # 分页查询
        offset = (page - 1) * page_size
        rows = c.execute(
            f"SELECT id, scope, repo_key, agent_type, issue_num, file_path, status, "
            f"source_seq, created_by, created_at, obs_key, content_hash, updated_at, source, archived, description, name "
            f"FROM agent_shared_knowledge {where} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
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
            "archived": r[14] or 0,
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
                    f"LEFT JOIN devops_pipeline_scene s ON r.scene_id = s.scene_id AND s.repo_key = ? "
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


@router.get("/stats", operation_id="knowledge_stats", summary="知识统计")
def knowledge_stats(user: dict = Depends(get_current_user_or_api_key),
                     repo: str = Query("")):
    """独立统计接口——返回 system/agent/manual/unlinked 数量。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    _check_repo_member(user, repo_key)

    from src.session_manager import db, db_lock
    with db_lock:
        stats_row = db.cursor().execute(
            "SELECT "
            "SUM(CASE WHEN source='system' AND archived=0 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN source='agent' AND archived=0 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN source='manual' AND archived=0 THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN archived=0 AND id NOT IN (SELECT DISTINCT knowledge_id FROM scene_knowledge_ref WHERE enabled=1) THEN 1 ELSE 0 END) "
            "FROM agent_shared_knowledge WHERE repo_key=?",
            (repo_key,)
        ).fetchone()
    return {
        "system": stats_row[0] or 0,
        "agent": stats_row[1] or 0,
        "manual": stats_row[2] or 0,
        "unlinked": stats_row[3] or 0,
    }


@router.get("/content", operation_id="knowledge_content", summary="读取单个知识全文")
def read_knowledge_content(repo: str = Query(""),
                                 file: str = Query(""),
                                 obs_key: str = Query(""),
                                 user: dict = Depends(get_current_user_or_api_key)):
    """读取单个知识全文（相对工作区仓库目录）或通过 obs_key 直接读 OBS。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    if not file and not obs_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "file 或 obs_key 必填"})

    # obs_key 模式：直接读 OBS，不需要反查 DB
    if obs_key:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            try:
                data = storage.read_bytes(obs_key)
                if data is not None:
                    from src.repo_key import to_repo_key
                    return {"repo_key": to_repo_key(repo), "obs_key": obs_key,
                            "content": data.decode("utf-8", errors="replace")}
            except Exception as e:
                log.warning(f"OBS read failed for obs_key={obs_key}: {e} — fallback to file")
        # OBS miss → 回退走 _read_file（用 obs_key 值作为 file 参数，兼容本地文件模式）

    # file 模式（原有逻辑，兼容非 OBS / OBS miss / 前端未传 file）
    # 如果 file 为空但 obs_key 非空，用 obs_key 作为 file 回退
    _file = file or obs_key
    if not _file:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "file 或 obs_key 必填"})
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    return {"repo_key": repo_key, "file": _file, "content": _read_file(repo_key, _file)}


@router.get("/context", operation_id="knowledge_context", summary="合并注入上下文文本")
def knowledge_context(repo: str = Query(""),
                            agent_type: str = "",
                            issue_num: int = 0,
                            scene_id: str = Query(""),
                            user: dict = Depends(get_current_user_or_api_key)):
    """返回合并注入上下文文本（project-info 摘要 + 共享知识 + 成员文档）。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)

    lines = []
    try:
        from src.prompt_builder import _build_project_info_section, _build_knowledge_section
        pi = _build_project_info_section(repo_key)
        if pi:
            lines.append(pi)
        k, _ = _build_knowledge_section(repo_key, agent_type=agent_type, issue_num=issue_num, scene_id=scene_id)
        if k:
            lines.append(k)
    except Exception:
        pass
    # 成员文档摘要
    docs = _member_docs(repo_key)
    if docs:
        lines.append("### 项目成员上传文档（member）\n" + "\n".join(
            f"- [{d['name']}] {d['path']}（{d['owner']}，{d['updated_at']}）" for d in docs[:20]))
    return {"repo_key": repo_key, "context": "\n\n".join(lines)}


@router.delete("")
def delete_knowledge(repo: str = Query(""),
                           file: str = Query(""),
                           user: dict = Depends(get_current_user)):
    """删除成员上传的知识文档（仅限本人上传）。"""
    if not repo or not file:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repo 与 file 必填"})
    from src.repo_key import to_repo_key, from_repo_key
    repo_key = to_repo_key(repo)
    rel = _safe_rel(file)
    is_member = rel.startswith("project-info/member/")
    is_agent = rel.startswith("project-info/agent/")
    if not (is_member or is_agent):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅可删除成员上传文档"})
    base = _repo_base(repo_key)
    full = os.path.normpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})

    # OBS 模式：先查 DB obs_key，有 obs_key 时不依赖本地文件存在
    obs_key = ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT obs_key FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? AND scope='member' "
                "ORDER BY id DESC LIMIT 1",
                (repo_key, rel)).fetchone()
        if row:
            obs_key = row[0] or ""
    except Exception:
        pass

    from src.storage import get_storage
    storage = get_storage()

    # 本地模式（OBS 未启用）：检查本地文件存在
    if storage is None:
        if not os.path.isfile(full):
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"文件不存在: {rel}"})

    # 成员目录按用户名子目录校验本人；Agent 目录为团队共享（agent_type 子目录），任一定向可见即可删
    if is_member:
        owner_part = rel.split("/")[2] if len(rel.split("/")) > 2 else ""
        me = str(user.get("username") or user.get("id") or "")
        if owner_part and owner_part != me:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅可删除本人上传的文档"})

    # OBS 删除
    if obs_key and storage is not None:
        try:
            if not storage.delete_object(obs_key):
                raise HTTPException(status_code=500, detail={"code": 500, "message": "OBS 对象删除失败，已中止"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"OBS 删除失败: {e}"})

    # 本地文件删除（OBS 模式下本地不存在只 warning 不中止）
    try:
        os.remove(full)
    except Exception as e:
        if obs_key and storage is not None:
            log.warning(f"[knowledge] OBS 已删但本地文件删除失败: {full}: {e}")
        else:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"删除失败: {e}"})
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? AND scope='member'",
                      (repo_key, rel))
            db.commit()
    except Exception:
        pass
    return {"message": f"已删除 {os.path.basename(rel)}"}


# ===== Issue #67：知识 CRUD + 关联管理 + 归档 API =====

class KnowledgeEntryBody(BaseModel):
    repo: str = ""
    content: str = ""
    agent_type: str = ""
    description: str = ""
    name: str = ""

class KnowledgeUpdateBody(BaseModel):
    content: str = ""
    description: str = ""
    name: str = ""

MAX_NAME_LEN = 50
MAX_DESC_LEN = 200
MAX_CONTENT_BYTES = 10 * 1024

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

class ArchiveBody(BaseModel):
    archived: bool = True


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
    base = _repo_base(repo_key)
    full = os.path.normpath(os.path.join(base, file_path))
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(new_content)

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
        base = _repo_base(row["repo_key"])
        full = os.path.normpath(os.path.join(base, file_path))
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

@router.post("/entry")
def create_knowledge_entry(body: KnowledgeEntryBody, user: dict = Depends(get_current_user)):
    """用户直接输入文本创建知识条目（source=manual）。"""
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

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(body.repo)
    _check_repo_member(user, repo_key)

    base = _repo_base(repo_key)
    username = str(user.get("username") or user.get("id") or "user")
    filename = f"entry_{int(time.time())}_{uuid.uuid4().hex[:6]}.md"
    member_dir = os.path.join(base, "project-info", "member", username)
    os.makedirs(member_dir, exist_ok=True)
    file_path = os.path.join(member_dir, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(body.content.strip())

    rel = os.path.relpath(file_path, WORKSPACE_DIR)
    content_hash = hashlib.sha256(body.content.encode("utf-8")).hexdigest()

    obs_key = ""
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            owner, rname = _owner_repo(repo_key)
            obs_key = f"knowledge/{owner}/{rname}/member/{username}/{filename}"
            with open(file_path, "rb") as f:
                storage.save_bytes(obs_key, f.read())
    except Exception as e:
        log.warning(f"[knowledge] entry OBS save failed: {e}")

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_shared_knowledge "
            "(scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by, source, description, name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("member", repo_key, body.agent_type or "", 0, rel, obs_key, content_hash, "accepted", 0, username, "manual", body.description or "", body.name.strip())
        )
        kid = c.lastrowid
        db.commit()

    return {"ok": True, "message": "知识已创建", "file_path": rel, "id": kid}


# ===== {id} 路由 =====

@router.get("/{knowledge_id}")
def get_knowledge_detail(knowledge_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """按 id 查询单条知识全文。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})

    content = _read_entry_content(row["repo_key"], row.get("obs_key", ""), row.get("file_path", ""))

    from src.session_manager import db, db_lock
    from src.api.pipeline_config import get_pipeline_config
    with db_lock:
        refs = db.cursor().execute(
            "SELECT r.id, r.scene_id, r.agent_type, r.enabled, r.created_by, r.created_at, "
            "s.name, s.description, s.display_name, s.scene_id "
            "FROM scene_knowledge_ref r "
            "LEFT JOIN devops_pipeline_scene s ON r.scene_id = s.scene_id AND s.repo_key = ? "
            "WHERE r.knowledge_id = ? ORDER BY r.id",
            (row["repo_key"], knowledge_id)
        ).fetchall()

    # 为每个场景关联补充 pipeline 信息（批量查，不循环加锁）
    ref_list = []
    scene_ids = [r[9] for r in refs if r[9] and row["repo_key"]]
    pipeline_map = {}
    for sid in scene_ids:
        try:
            pc = get_pipeline_config(sid, repo_key=row["repo_key"])
            if pc and pc.get("nodes"):
                pipeline_map[sid] = " → ".join(n.get("tag", n.get("id", "")) for n in pc["nodes"] if n.get("enabled", True))
        except Exception:
            pass
    for r in refs:
        scene_name = r[6] or ""
        scene_description = r[7] or ""
        scene_display_name = r[8] or scene_name
        pipeline_chain = pipeline_map.get(r[9], "")
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


@router.put("/{knowledge_id}")
def update_knowledge(knowledge_id: int, body: KnowledgeUpdateBody,
                     user: dict = Depends(get_current_user)):
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


@router.delete("/{knowledge_id}")
def delete_knowledge_by_id(knowledge_id: int, user: dict = Depends(get_current_user)):
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


@router.put("/{knowledge_id}/archive")
def archive_knowledge(knowledge_id: int, body: ArchiveBody,
                      user: dict = Depends(get_current_user)):
    """标记/取消归档。body.archived=true 标记归档，false 取消归档。"""
    row = _get_knowledge_by_id(knowledge_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "知识不存在"})

    # 权限：manual 仅创建者，system/agent 仓库托管者（当前先放行）
    if row["source"] == "manual" and row["created_by"] != user.get("username"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅创建者可操作"})

    archived = 1 if body.archived else 0
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE agent_shared_knowledge SET archived=? WHERE id=?",
            (archived, knowledge_id)
        )
        c.execute(
            "UPDATE scene_knowledge_ref SET enabled=?, updated_by=? WHERE knowledge_id=?",
            (0 if archived else 1, str(user.get("username") or ""), knowledge_id)
        )
        db.commit()
    return {"ok": True, "archived": archived}


# ===== 关联管理 API =====

@router.get("/{knowledge_id}/refs")
def get_knowledge_refs(knowledge_id: int, user: dict = Depends(get_current_user)):
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


@router.post("/{knowledge_id}/refs")
def add_knowledge_ref(knowledge_id: int, body: KnowledgeRefBody,
                      user: dict = Depends(get_current_user)):
    """为知识新增场景/Agent 关联。"""
    _check_ref_permission(knowledge_id, user)

    # 校验关联数量——同一 scene_id + agent_type 最多 20 条
    from src.session_manager import db, db_lock
    scene_id = body.scene_id or ""
    agent_type = body.agent_type or ""
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


@router.post("/{knowledge_id}/refs/batch", operation_id="knowledge_add_refs_batch")
def add_knowledge_refs_batch(knowledge_id: int, body: KnowledgeRefBatchBody,
                              user: dict = Depends(get_current_user)):
    """批量新增场景/Agent 关联（幂等，跳过已存在的）。"""
    _check_ref_permission(knowledge_id, user)
    username = str(user.get("username") or "")
    from src.session_manager import db, db_lock
    added = 0
    items = []
    for sid in (body.scene_ids or []):
        items.append((sid, ""))
    for at in (body.agent_types or []):
        items.append(("", at))
    with db_lock:
        c = db.cursor()
        for scene_id, agent_type in items:
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
    return {"ok": True, "added": added}


@router.put("/ref/{ref_id}")
def update_knowledge_ref(ref_id: int, body: KnowledgeRefUpdateBody,
                         user: dict = Depends(get_current_user)):
    """修改关联的 enabled 开关。"""
    from src.session_manager import db, db_lock
    username = str(user.get("username") or "")
    with db_lock:
        db.cursor().execute(
            "UPDATE scene_knowledge_ref SET enabled = ?, updated_by = ? WHERE id = ?",
            (1 if body.enabled else 0, username, ref_id)
        )
        db.commit()
    return {"ok": True}


@router.delete("/ref/{ref_id}")
def delete_knowledge_ref(ref_id: int, user: dict = Depends(get_current_user)):
    """删除知识的某条关联。"""
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM scene_knowledge_ref WHERE id = ?", (ref_id,))
        db.commit()
    return {"ok": True}
