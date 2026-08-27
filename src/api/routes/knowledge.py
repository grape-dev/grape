"""Knowledge 路由：项目知识看板聚合（项目级维度 + DevOps Agent 维度）。

见 docs/new-version2/knowledge-board-design.md：
- GET  /api/knowledge         聚合返回两类维度知识清单（项目级 + Agent 维度）
- GET  /api/knowledge/content 读取单个知识全文（路径穿越防护）
- GET  /api/knowledge/context 返回合并注入上下文文本
- POST /api/knowledge         成员手动上传知识文档（md/txt/json，鉴权当前用户）
- DEL  /api/knowledge         删除成员上传的知识
"""
import os, re
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query
from src.api.deps import get_current_user
from src.config import log
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


def _repo_root_rel(ws_rel: str, repo_key: str) -> str:
    """WORKSPACE_DIR 相对路径 → 仓库根相对路径（剥掉 {owner}/{repo}/ 前缀）。

    agent_shared_knowledge.file_path 存 WORKSPACE 相对（如 owner/repo/project-info/knowledge.md），
    看板统一用「相对仓库根」语义（与 content 接口 / 成员文档 path 一致）。
    """
    rel = (ws_rel or "").replace("\\", "/").lstrip("/")
    owner, rname = _owner_repo(repo_key)
    prefix = f"{owner}/{rname}/"
    if owner and rname and rel.startswith(prefix):
        rel = rel[len(prefix):]
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
    """读 WORKSPACE_DIR 相对路径（agent_shared_knowledge.file_path 语义，防路径穿越）。"""
    from src.config import WORKSPACE_DIR
    rel = _safe_rel(ws_rel)
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
    """
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


def _project_knowledge_entries(repo_key: str) -> list[dict]:
    """agent_shared_knowledge 中 project 级条目（含正文）。"""
    from src.session_manager import db, db_lock
    rows = []
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT scope, agent_type, issue_num, file_path, status, source_seq, created_by, created_at, obs_key "
                "FROM agent_shared_knowledge WHERE repo_key=? AND scope='project' ORDER BY id DESC LIMIT 100",
                (repo_key,))
            rows = c.fetchall()
        except Exception:
            rows = []
    out = []
    for r in rows:
        content = _read_entry_content(repo_key, r[8], r[3])
        out.append({
            "scope": r[0], "agent_type": r[1] or "", "issue_num": r[2] or 0,
            "file_path": r[3] or "", "path": _repo_root_rel(r[3], repo_key),
            "status": r[4] or "", "source_seq": r[5] or 0,
            "created_by": r[6] or "", "created_at": normalize_event_time(r[7] or ""),
            "content": content, "obs_key": r[8] or "",
        })
    return out


def _agent_knowledge_entries(repo_key: str) -> dict[str, list]:
    """agent_shared_knowledge 中 issue / issue_agent 级条目 + Agent 专属成员文档，按 agent_type 分组。"""
    from src.session_manager import db, db_lock
    rows = []
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT scope, agent_type, issue_num, file_path, status, source_seq, created_by, created_at, obs_key "
                "FROM agent_shared_knowledge WHERE repo_key=? AND scope IN ('issue','issue_agent') "
                "ORDER BY agent_type, id DESC LIMIT 500",
                (repo_key,))
            rows = c.fetchall()
        except Exception:
            rows = []
    groups: dict[str, list] = {}
    for r in rows:
        at = r[1] or ""
        content = _read_entry_content(repo_key, r[8], r[3])
        groups.setdefault(at, []).append({
            "scope": r[0], "agent_type": at, "issue_num": r[2] or 0,
            "file_path": r[3] or "", "path": _repo_root_rel(r[3], repo_key),
            "status": r[4] or "", "source_seq": r[5] or 0,
            "created_by": r[6] or "", "created_at": normalize_event_time(r[7] or ""),
            "content": content, "obs_key": r[8] or "",
        })
    # Agent 专属成员上传文档（§六：project-info/agent/{agent_type}/，定向给该 Agent）
    from src.api.knowledge import agent_files
    import os as _os
    base = _os.path.join(_repo_base(repo_key), "project-info", "agent")
    if _os.path.isdir(base):
        for at in sorted(_os.listdir(base)):
            if not at or not _os.path.isdir(_os.path.join(base, at)):
                continue
            for rel, content in agent_files(repo_key, at).items():
                groups.setdefault(at, []).append({
                    "scope": "member_agent", "agent_type": at, "issue_num": 0,
                    "file_path": "", "path": "project-info/" + rel,
                    "status": "accepted", "source_seq": 0,
                    "created_by": "member", "created_at": "", "content": content,
                })
    return groups


@router.get("/")
async def list_knowledge(user: dict = Depends(get_current_user),
                         repo: str = Query("", description="repo_key，如 gitcode:org/test 或 org/test")):
    """聚合返回项目级 + Agent 维度知识清单。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)

    # 项目级：project-info files + meta
    files = {}
    meta = {}
    try:
        from src.runtime.service import load_project_info
        files, meta = load_project_info(repo_key)
    except Exception:
        files, meta = {}, {}
    project_files = [
        {"name": "project-info/project-graph.md", "label": "Project Graph", "updated": meta.get("last_updated", "")}
        for n in ("project-graph.md",) if n in files
    ] + [
        {"name": "project-info/project-capabilities.md", "label": "Project Capabilities", "updated": meta.get("last_updated", "")}
        for n in ("project-capabilities.md",) if n in files
    ]
    if ".metadata.json" in files:
        project_files.append({"name": "project-info/.metadata.json", "label": "metadata",
                              "updated": meta.get("last_updated", "")})

    return {
        "repo_key": repo_key,
        "project": {
            "meta": meta,
            "files": project_files,
            "knowledge": _project_knowledge_entries(repo_key),
            "member_docs": _member_docs(repo_key),
        },
        "agents": _agent_knowledge_entries(repo_key),
    }


@router.get("/content")
async def read_knowledge_content(repo: str = Query(""),
                                 file: str = Query(""),
                                 user: dict = Depends(get_current_user)):
    """读取单个知识全文（相对工作区仓库目录）。"""
    if not repo or not file:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repo 与 file 必填"})
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    return {"repo_key": repo_key, "file": file, "content": _read_file(repo_key, file)}


@router.get("/context")
async def knowledge_context(repo: str = Query(""),
                            agent_type: str = "",
                            issue_num: int = 0,
                            user: dict = Depends(get_current_user)):
    """返回合并注入上下文文本（project-info 摘要 + 共享知识三级 + 成员文档）。"""
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
        k = _build_knowledge_section(repo_key, agent_type=agent_type, issue_num=issue_num)
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


@router.post("/")
async def upload_knowledge(file: UploadFile = File(...),
                           repo: str = Form(""),
                           scope: str = Form("project"),
                           agent_type: str = Form(""),
                           user: dict = Depends(get_current_user)):
    """成员手动上传知识文档（md/txt/json，≤10MB）。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    if scope not in ("project", "agent"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 project 或 agent"})
    if scope == "agent" and not agent_type:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Agent 维度上传必须指定 agent_type"})
    name = os.path.basename((file.filename or "knowledge.md").replace("\\", "/")).strip()
    if not name or not _SAFE_REL.match(name):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "文件名不合法"})
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"仅支持 {sorted(ALLOWED_EXTS)}"})

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "知识文档不能超过 10MB"})
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持 UTF-8 文本"})

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    base = _repo_base(repo_key)
    # §六：Agent 维度上传落盘 project-info/agent/{agent_type}/（仅该 Agent 定向注入）；
    # 项目级维持 project-info/member/{username}/
    if scope == "agent":
        member_dir = os.path.join(base, "project-info", "agent", agent_type)
    else:
        member_dir = os.path.join(base, "project-info", "member", str(user.get("username") or user.get("id") or "user"))
    os.makedirs(member_dir, exist_ok=True)
    # 重名则加时间戳后缀，避免覆盖
    dest = os.path.join(member_dir, name)
    if os.path.exists(dest):
        stem, ext2 = os.path.splitext(name)
        dest = os.path.join(member_dir, f"{stem}-{int(__import__('time').time())}{ext2}")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)
    rel = os.path.relpath(dest, _repo_base(repo_key)).replace("\\", "/")

    # OBS 分支：启用时调 storage.save_bytes + 计算 content_hash
    obs_key = ""
    content_hash = ""
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            from src.api.knowledge import knowledge_storage_key
            obs_key = knowledge_storage_key(repo_key, scope, agent_type=agent_type if scope == "agent" else "")
            storage.save_bytes(obs_key, content)
            import hashlib
            content_hash = hashlib.sha256(content).hexdigest()
    except Exception as e:
        log.warning(f"[knowledge] OBS save failed for {repo_key}/{rel}: {e} — local file unaffected")
        obs_key = ""
        content_hash = ""

    # 登记索引
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, obs_key, content_hash, status, source_seq, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("member", repo_key, agent_type, 0, rel, obs_key, content_hash, "accepted", 0,
                 str(user.get("username") or user.get("id") or "")))
            db.commit()
    except Exception as e:
        log.warning(f"knowledge member index failed {repo_key}: {e}")

    log.info(f"[knowledge] member upload {repo_key} {rel} by {user.get('username','')}")
    return {"message": f"知识文档 {os.path.basename(rel)} 上传成功", "file_path": rel, "scope": scope}


@router.delete("/")
async def delete_knowledge(repo: str = Query(""),
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
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "文件不存在"})
    # 成员目录按用户名子目录校验本人；Agent 目录为团队共享（agent_type 子目录），任一定向可见即可删
    if is_member:
        owner_part = rel.split("/")[2] if len(rel.split("/")) > 2 else ""
        me = str(user.get("username") or user.get("id") or "")
        if owner_part and owner_part != me:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅可删除本人上传的文档"})

    # OBS 分支：先查 DB obs_key，非空先删 OBS 对象——删失败则中止（不清 DB）
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
    if obs_key:
        try:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                if not storage.delete_object(obs_key):
                    raise HTTPException(status_code=500, detail={"code": 500, "message": "OBS 对象删除失败，已中止"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"OBS 删除失败: {e}"})

    try:
        os.remove(full)
    except Exception as e:
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
