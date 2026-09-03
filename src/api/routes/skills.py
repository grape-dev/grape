"""Skills 路由：列表、上传、删除、个人技能转仓库技能"""
import io as _io
import os, tempfile, zipfile, json, shutil, stat as _stat
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel
from src.user import db as user_db
from src.user.validator import validate_skill as validate_custom_skill, parse_skill_frontmatter
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/skills", tags=["skills"])

# 解压后总大小上限（zip 炸弹防护，见 obs-skill-storage-design.md §7）
MAX_EXTRACT_BYTES = int(os.environ.get("SKILL_MAX_EXTRACT_BYTES", str(100 * 1024 * 1024)))


def _normalize_skill_zip(skill_dir: str, skill_name: str) -> bytes:
    """规范化重打包技能 zip：根 = {skill_name}/，逐文件保留权限位（external_attr），
    丢弃系统目录。返回 zip 字节（OBS 存储正体，见 obs-skill-storage-design.md §2.2）。"""
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git", "__pycache__")]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, skill_dir).replace(os.sep, "/")
                arc = f"{skill_name}/{rel}"
                st = os.stat(full)
                info = zipfile.ZipInfo(arc)
                info.external_attr = (_stat.S_IMODE(st.st_mode)) << 16  # 保留权限位
                with open(full, "rb") as fh:
                    zf.writestr(info, fh.read())
    return buf.getvalue()


@router.get("")
def list_skills(user: dict = Depends(get_current_user), repo: str = "", scope: str = ""):
    """返回技能列表：official 所有用户可见；user 本人可见；repo 指定仓库可见。
    scope 参数：official/repo/user 按维度过滤；不传 = 返回全部（兼容旧逻辑）。
    """
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key

    # repo 参数可能是 fullName（xiaowu2119/hello-skill），转成 repo_key（gitcode:xiaowu2119/hello-skill）
    repo_key = to_repo_key(repo) if repo else ""

    skills = []
    with db_lock:
        c = db.cursor()
        try:
            if scope == "official":
                c.execute(
                    "SELECT id, skill_name, display_name, description, agent_type, scope, owner_id FROM skill WHERE is_active=1 AND scope='official' ORDER BY agent_type, skill_name",
                )
            elif scope == "repo":
                if not repo:
                    return {"skills": []}
                c.execute(
                    "SELECT id, skill_name, display_name, description, agent_type, scope, owner_id FROM skill WHERE is_active=1 AND scope='repo' AND owner_id=? ORDER BY agent_type, skill_name",
                    (repo_key,)
                )
            elif scope == "user":
                c.execute(
                    "SELECT id, skill_name, display_name, description, agent_type, scope, owner_id FROM skill WHERE is_active=1 AND scope='user' AND owner_id=? ORDER BY agent_type, skill_name",
                    (str(user["id"]),)
                )
            elif repo:
                # 官方 + 个人 + 指定仓库技能（兼容旧逻辑）
                c.execute(
                    "SELECT id, skill_name, display_name, description, agent_type, scope, owner_id FROM skill WHERE is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?)) ORDER BY agent_type, skill_name",
                    (str(user["id"]), repo_key)
                )
            else:
                # 官方 + 个人（兼容旧逻辑）
                c.execute(
                    "SELECT id, skill_name, display_name, description, agent_type, scope, owner_id FROM skill WHERE is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?)) ORDER BY agent_type, skill_name",
                    (str(user["id"]),)
                )
            for r in c.fetchall():
                skills.append({
                    "id": r[0],
                    "name": r[1],
                    "display_name": r[2] or r[1],
                    "description": r[3] or "",
                    "agent_type": r[4] or "",
                    "type": "official" if r[5] == "official" else "repo" if r[5] == "repo" else "user-uploaded",
                    "scope": r[5] or "",
                })
        except Exception:
            pass

    return {"skills": skills}


@router.post("/sync-official")
async def sync_official_skills(user: dict = Depends(get_current_user)):
    """从 configs/skill-description.json 同步官方技能到 skill 表，并触发 OBS 同步。

    1. DB 同步：新增 INSERT + 已有 UPDATE 元信息（display_name/description/agent_type）
    2. OBS 同步：计算 content_hash，变更的技能打包 zip 上传 OBS
    返回同步结果。
    """
    from src.session_manager import db, db_lock

    config_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "configs")
    json_path = os.path.join(config_dir, "skill-description.json")
    if not os.path.exists(json_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "skill-description.json 未找到"})

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    added = 0
    # 1. DB 同步（INSERT/UPDATE 元信息）
    with db_lock:
        c = db.cursor()
        for skill_name, info in data.items():
            display_name = info.get("name", skill_name)
            description = info.get("description", "")
            agent_type = info.get("agent_type", "")
            skill_path = info.get("path", skill_name)
            existed = c.execute("SELECT COUNT(*) FROM skill WHERE skill_name=? AND scope='official'", (skill_name,)).fetchone()[0]
            if existed:
                _fp = c.execute("SELECT file_path FROM skill WHERE skill_name=? AND scope='official' AND file_path IS NOT NULL AND file_path!=''", (skill_name,)).fetchone()
                if _fp:
                    c.execute(
                        "UPDATE skill SET display_name=?, description=?, agent_type=? WHERE skill_name=? AND scope='official'",
                        (display_name, description, agent_type, skill_name)
                    )
                else:
                    c.execute(
                        "UPDATE skill SET display_name=?, description=?, agent_type=?, skill_path=? WHERE skill_name=? AND scope='official'",
                        (display_name, description, agent_type, skill_path, skill_name)
                    )
            else:
                c.execute(
                    "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, content_hash) VALUES (?,?,?,?,?,'official','',?)",
                    (skill_name, display_name, description, agent_type, skill_path, "")
                )
            if existed == 0:
                added += 1
        db.commit()

    # 2. OBS 同步（hash 检测 + 上传）
    from src.storage.skill_sync import sync_official_skills_to_obs
    obs_result = await sync_official_skills_to_obs()

    return {
        "message": f"官方技能同步完成，新增 {added} 个，OBS 同步: +{obs_result['added']} ~{obs_result['updated']} 跳过{obs_result['skipped']}",
        "added": added,
        "obs_result": obs_result,
    }


@router.get("/template")
def download_skill_template():
    """下载技能模板 ZIP（skills/_template 目录打包）。免鉴权（token 通过 query 传递）。"""
    from src.config import SKILLS_DIR

    template_dir = os.path.join(SKILLS_DIR, "_template")
    if not os.path.isdir(template_dir):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能模板未找到"})

    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(template_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, template_dir)
                zf.write(full, rel)
    buf.seek(0)
    # ===== OBS 分支（N8）：打包 → 传 OBS → 返回临时签名 URL（前端点击下载时动态获取）=====
    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        key = "skills/template/skill-template.zip"
        storage.save_bytes(key, buf.getvalue())
        url = storage.temporary_url(key)
        return {"url": url, "expires_in": storage.default_expires}
    # ===== local 分支（原逻辑不变）=====
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="skill-template.zip"'},
    )


@router.get("/by-id/{skill_id}/download")
def download_skill_zip_by_id(skill_id: int, user: dict = Depends(get_current_user),
                                   repo: str = ""):
    """下载技能包 zip（点击时动态签发，见 obs-skill-storage-design.md N8 下载模式）。

    按技能 id 精确查询（同名多 scope 记录无歧义）；OBS 模式返回 {url, expires_in}
    临时签名 URL（300s，不落库），浏览器直连 OBS 下载；local 模式返回 zip 文件流。
    官方技能（scope=official）为部署内置物，未转集群不提供下载。
    """
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key, from_repo_key
    repo_key = to_repo_key(repo) if repo else ""
    with db_lock:
        c = db.cursor()
        if repo_key:
            c.execute(
                "SELECT id, skill_path, scope, owner_id, skill_name FROM skill WHERE id=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))",
                (skill_id, str(user["id"]), repo_key)
            )
        else:
            c.execute(
                "SELECT id, skill_path, scope, owner_id, skill_name FROM skill WHERE id=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR scope='repo')",
                (skill_id, str(user["id"]))
            )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能不存在"})
    skill_id, skill_path, scope, owner_id, name = row

    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        from src.user.db import get_skill_object_key
        object_key = get_skill_object_key(skill_id)
        if object_key:
            url = storage.temporary_url(object_key)
            return {"url": url, "expires_in": getattr(storage, "default_expires", 300)}
    # 官方技能未转集群模式：无 OBS 对象，不提供下载
    if scope == "official":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "官方技能为部署内置，未转集群模式不提供下载"})

    # local 分支：本地技能目录打包 zip 流式返回
    from src.config import SKILLS_DIR
    if scope == "user":
        skill_dir = os.path.join(SKILLS_DIR, "custom", owner_id or str(user["id"]), skill_path or name)
    else:
        skill_dir = os.path.join(SKILLS_DIR, "repo", from_repo_key(owner_id), skill_path or name)
    if not os.path.isdir(skill_dir):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能目录不存在"})
    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git", "__pycache__")]
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, os.path.dirname(skill_dir)).replace(os.sep, "/")
                zf.write(full, rel)
    buf.seek(0)
    from fastapi.responses import Response
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@router.get("/by-id/{skill_id}")
def get_skill_detail_by_id(skill_id: int, user: dict = Depends(get_current_user),
                                 repo: str = ""):
    """返回技能详情（含 SKILL.md 内容）——按技能 id 精确查询，查到后校验归属权限。

    权限校验：official 可见；user 需本人；repo 需当前用户托管该仓库。
    """
    from src.session_manager import db, db_lock
    from src.config import SKILLS_DIR

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id, id FROM skill WHERE id=? AND is_active=1",
            (skill_id,)
        )
        row = c.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    scope = row[5]
    owner_id = row[6] or ""

    # 归属校验
    if scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})
    if scope == "repo":
        # 从后端查当前用户托管的仓库列表，校验 skill.owner_id 在列表里
        from src.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})

    skill_dir = row[4] or row[0]
    current_id = row[7]
    # ===== OBS 分支（N5）：两级缓存 + 内存解压提取 + zip_url 临时地址 =====
    from src.storage import get_storage
    storage = get_storage()
    object_key = ""
    if storage is not None:
        from src.user.db import get_skill_object_key
        object_key = get_skill_object_key(current_id)  # 官方技能转集群后 skill_path 也是对象键
    if object_key:
        from src.storage import skill_md_content
        content = skill_md_content(object_key, row[0]) or ""
        zip_url = ""
        try:
            zip_url = storage.temporary_url(object_key)  # 用户点击下载时动态签发
        except Exception:
            zip_url = ""
        return {
            "id": current_id,
            "name": row[0], "display_name": row[1] or row[0], "description": row[2] or "",
            "agent_type": row[3] or "",
            "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
            "scope": scope or "",
            "content": content,
            "zip_url": zip_url,
            "is_obs": True,  # OBS/集群模式（skill_path 为对象键）
        }
    # ===== local 分支 =====
    if scope == "user":
        skill_path = os.path.join(SKILLS_DIR, "custom", owner_id or str(user["id"]), skill_dir, "SKILL.md")
    elif scope == "repo":
        # owner_id 可能带 gitcode: 前缀，目录路径用不带前缀的 owner/repo
        from src.repo_key import from_repo_key
        skill_path = os.path.join(SKILLS_DIR, "repo", from_repo_key(owner_id), skill_dir, "SKILL.md")
    else:
        from src.config import resolve_official_skill_path
        _r = resolve_official_skill_path(row[0])
        skill_path = _r or os.path.join(SKILLS_DIR, skill_dir, "SKILL.md")
    content = ""
    if os.path.exists(skill_path):
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
    return {
        "id": current_id,
        "name": row[0], "display_name": row[1] or row[0], "description": row[2] or "",
        "agent_type": row[3] or "",
        "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
        "scope": scope or "",
        "content": content,
        "is_obs": False,
    }


@router.post("/upload")
async def upload_skill(file: UploadFile = File(...), user: dict = Depends(get_current_user),
                       scope: str = Form("user"), repo: str = Form(""), agent_type: str = Form("")):
    """上传技能 ZIP。scope=user 存个人技能；scope=repo 存仓库技能（需指定 repo）。agent_type 必填（技能类型）。"""
    VALID_AGENT_TYPES = {"vod", "issue", "coding", "qa", "sre", "chat"}
    if scope not in ("user", "repo"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 user 或 repo"})
    if scope == "repo" and not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库技能必须指定 repo"})
    if not agent_type:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须选择技能类型（agent_type）"})
    if agent_type not in VALID_AGENT_TYPES:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"技能类型不合法，可选: {', '.join(sorted(VALID_AGENT_TYPES))}"})

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 文件不能超过 10MB"})

    tmp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(tmp_dir, file.filename or "skill.zip")
        with open(zip_path, "wb") as f:
            f.write(content)

        extract_dir = os.path.join(tmp_dir, "extracted")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # zip 炸弹防护：解压后总大小上限（见设计文档 §7）
                total = sum((i.file_size or 0) for i in zf.infolist())
                if total > MAX_EXTRACT_BYTES:
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"ZIP 解压后总大小超过 {MAX_EXTRACT_BYTES // (1024 * 1024)}MB 上限"})
                zf.extractall(extract_dir)
                # Python 3.12+ extractall 不再恢复 Unix 权限位 → 手动恢复
                # （否则后续规范化重打包读 st_mode 拿不到上传时的权限，见 obs-skill-storage-design.md §7）
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    if mode and not info.is_dir():
                        p = os.path.join(extract_dir, info.filename)
                        if os.path.isfile(p):
                            os.chmod(p, mode & 0o777)
        except HTTPException:
            raise
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "不是有效的 ZIP 文件，请上传 .zip 格式"})

        entries = os.listdir(extract_dir)
        if not entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 文件为空"})

        # 过滤系统杂项目录（macOS __MACOSX、.DS_Store 等），取第一个真实目录
        real_entries = [e for e in entries if e not in ("__MACOSX", ".DS_Store", ".git") and not e.startswith(".")]
        if not real_entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 中未找到技能目录"})

        skill_dir = os.path.join(extract_dir, real_entries[0])
        if not os.path.isdir(skill_dir):
            skill_dir = extract_dir

        skill_name = os.path.basename(skill_dir) if os.path.isdir(skill_dir) else real_entries[0]
        errors = validate_custom_skill(skill_dir, skill_name)
        if errors:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 校验失败", "errors": errors})

        # 从 SKILL.md Front Matter 提取描述（上传后技能列表需要展示）
        fm = parse_skill_frontmatter(skill_dir)
        description = fm.get("description", "") or ""

        import shutil
        from src.session_manager import db, db_lock
        # ===== OBS 分支（N4）：规范化重打包 → 传 OBS 固定 key（原子覆盖）→ DB upsert → 缓存 =====
        from src.config import log
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            from src.storage import skill_key, zip_cache_put, mem_cache_invalidate
            from src.user.db import upsert_skill_obs
            from src.repo_key import to_repo_key
            owner = to_repo_key(repo) if scope == "repo" else str(user["id"])
            db_scope = "repo" if scope == "repo" else "user"
            try:
                # 固定 key（v2.4 去版本化）：putObject 同 key 原子覆盖（最后写者胜）；
                # user 不同 owner 段天然隔离，repo 同仓库同名覆盖不限角色
                object_key = skill_key("custom" if db_scope == "user" else db_scope,
                                       owner, skill_name)
                zip_bytes = _normalize_skill_zip(skill_dir, skill_name)
                storage.save_bytes(object_key, zip_bytes)
                ok = upsert_skill_obs(db_scope, owner, skill_name, agent_type,
                                      description, object_key, str(user["id"]))
                if not ok:
                    raise HTTPException(status_code=409, detail={"code": 409, "message": f"技能 {skill_name} 记录写入失败（同名冲突或并发上传）"})
                zip_cache_put(object_key, zip_bytes)  # put 覆盖同名 key 即天然失效旧缓存
                mem_cache_invalidate(skill_name)      # 覆盖后显式失效内存提取缓存
                log.info(f"skill uploaded to OBS (fixed key): {object_key}")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=502, detail={"code": 502, "message": f"技能上传 OBS 失败: {e}"})
            return {"message": f"技能 {skill_name} 上传成功"}
        # ===== local 分支 =====
        if scope == "repo":
            from src.repo_key import to_repo_key, from_repo_key
            repo_key = to_repo_key(repo)
            target_dir = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", from_repo_key(repo_key), skill_name)
            os.makedirs(os.path.dirname(target_dir), exist_ok=True)
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(skill_dir, target_dir)
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=?", (skill_name, repo_key))
                db.commit()
            user_db.create_skill(user["id"], skill_name, agent_type, description, "", scope="repo", owner_id=repo_key)
        else:
            user_skill_dir = os.path.join(user_db.SKILL_DIR, str(user["id"]), skill_name)
            os.makedirs(os.path.dirname(user_skill_dir), exist_ok=True)
            if os.path.exists(user_skill_dir):
                shutil.rmtree(user_skill_dir)
            shutil.copytree(skill_dir, user_skill_dir)
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE skill_name=? AND scope='user' AND owner_id=?", (skill_name, str(user["id"])))
                db.commit()
            user_db.create_skill(user["id"], skill_name, agent_type, description, "")

        return {"message": f"技能 {skill_name} 上传成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"上传失败: {e}"})
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.delete("/{skill_name}")
def delete_skill(skill_name: str, user: dict = Depends(get_current_user),
                       scope: str = "user", repo: str = ""):
    """删除技能——按 skill_name + scope 查到后做归属校验，再删除。

    权限校验：official 不允许删除；user 需本人；repo 需当前用户托管该仓库。
    """
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key

    with db_lock:
        c = db.cursor()
        # 按 skill_name + scope 查，owner_id 归一化匹配
        if scope == "repo":
            repo_key = to_repo_key(repo) if repo else ""
            c.execute(
                "SELECT id, owner_id, skill_path FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1",
                (skill_name, repo_key)
            )
        else:
            c.execute(
                "SELECT id, owner_id, skill_path FROM skill WHERE skill_name=? AND scope='user' AND owner_id=? AND is_active=1",
                (skill_name, str(user["id"]))
            )
        row = c.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    skill_id = row[0]
    owner_id = row[1] or ""

    # 归属校验
    if scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权删除该技能"})
    if scope == "repo":
        from src.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权删除该技能"})

    # OBS 对象清理
    from src.storage import get_storage
    storage = get_storage()
    object_key = ""
    if storage is not None:
        from src.user.db import get_skill_object_key
        try:
            object_key = get_skill_object_key(skill_id)
        except Exception:
            pass

    # 物理删除
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE id=?", (skill_id,))
        db.commit()

    # local 目录清理（local 模式下 skill 文件在磁盘上）
    if storage is None:
        import shutil
        from src.config import SKILLS_DIR
        from src.repo_key import from_repo_key
        if scope == "user":
            local_dir = os.path.join(SKILLS_DIR, "custom", owner_id, skill_name)
        elif scope == "repo":
            # owner_id 可能带 gitcode: 前缀，目录路径用不带前缀的 owner/repo
            local_dir = os.path.join(SKILLS_DIR, "repo", from_repo_key(owner_id), skill_name)
        else:
            local_dir = None
        if local_dir and os.path.exists(local_dir):
            shutil.rmtree(local_dir, ignore_errors=True)

    if object_key:
        from src.storage import invalidate_skill_caches
        try:
            storage.delete_object(object_key)
        except Exception:
            pass
        invalidate_skill_caches(object_key)

    return {"message": f"技能 {skill_name} 已删除"}


class ConvertSkillRequest(BaseModel):
    skill_name: str
    repo: str  # 目标仓库 repo_key（如 Jason7602/skill-evaluator）


@router.post("/convert")
def convert_skill(body: ConvertSkillRequest, user: dict = Depends(get_current_user)):
    """将个人技能（scope=user）复制为仓库技能（scope=repo）。

    原个人技能记录与目录保留（复制而非移动）。复制后可在此仓库的 Agent 中配置使用。
    """
    skill_name = (body.skill_name or "").strip()
    repo = (body.repo or "").strip()
    if not skill_name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "技能名不能为空"})
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "目标仓库不能为空"})

    from src.repo_key import to_repo_key, from_repo_key
    repo_key = to_repo_key(repo)

    # 1. 查原个人技能（须属于当前用户）
    skill_rec = next((s for s in user_db.list_skills(user["id"]) if s["skill_name"] == skill_name), None)
    if not skill_rec:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "个人技能未找到或不属于当前用户"})

    # 2. 校验目标仓库下无同名仓库技能
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        exists = c.execute("SELECT COUNT(*) FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1", (skill_name, repo_key)).fetchone()[0]
    if exists:
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"目标仓库已存在同名技能「{skill_name}」"})

    # ===== OBS 分支（N6）：copyObject（个人→仓库固定 key）→ DB 回写 =====
    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        from src.storage import skill_key, zip_cache_get, zip_cache_put, mem_cache_invalidate
        from src.user.db import get_skill_object_key, upsert_skill_obs
        src_key = get_skill_object_key(skill_rec["id"])
        if not src_key:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "源个人技能未在 OBS（历史数据请先执行迁移），无法转换"})
        dst_key = skill_key("repo", repo_key, skill_name)
        if not storage.copy_object(src_key, dst_key):
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"OBS 复制技能失败: {src_key} -> {dst_key}"})
        ok = upsert_skill_obs("repo", repo_key, skill_name, skill_rec.get("agent_type", ""),
                              skill_rec.get("description", ""), dst_key, str(user["id"]))
        if not ok:
            storage.delete_object(dst_key)
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"目标仓库已存在同名技能「{skill_name}」"})
        # 源 zip 若在磁盘缓存，复制到目标 key（懒加载兜底亦可）
        src_zip = zip_cache_get(src_key)
        if src_zip:
            zip_cache_put(dst_key, src_zip)
        mem_cache_invalidate(skill_name)
        return {"message": f"技能 {skill_name} 已复制到仓库 {repo}", "skill_name": skill_name, "scope": "repo", "repo": repo}

    # 3. 复制目录 skills/custom/{user_id}/{skill}/ → skills/repo/{owner}/{repo}/{skill}/
    src_dir = os.path.join(user_db.SKILL_DIR, str(user["id"]), skill_name)
    dst_dir = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", from_repo_key(repo_key), skill_name)
    if not os.path.isdir(src_dir):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "个人技能目录不存在"})
    if os.path.exists(dst_dir):
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"目标仓库已存在同名技能「{skill_name}」"})

    os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
    shutil.copytree(src_dir, dst_dir)

    # 4. 创建仓库技能记录（沿用 agent_type / description）
    try:
        ok = user_db.create_skill(user["id"], skill_name, skill_rec.get("agent_type", ""),
                                  skill_rec.get("description", ""), "", scope="repo", owner_id=repo_key)
        if not ok:
            # 若创建失败（如并发冲突），回滚目录
            shutil.rmtree(dst_dir, ignore_errors=True)
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"目标仓库已存在同名技能「{skill_name}」"})
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"复制技能失败: {e}"})

    return {"message": f"技能 {skill_name} 已复制到仓库 {repo}", "skill_name": skill_name, "scope": "repo", "repo": repo}


@router.get("/{skill_id}/sub")
def list_sub_skills(skill_id: int, user: dict = Depends(get_current_user)):
    """返回技能包的子技能列表（从 skill_path 的 references/ 目录读取）"""
    from src.session_manager import db, db_lock
    import re as _re

    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_path FROM skill WHERE id=? AND is_active=1", (skill_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    skill_name, skill_path = row[0], row[1] or ""
    from src.config import SKILLS_DIR, resolve_official_skill_path
    base_dir = os.path.join(SKILLS_DIR, skill_path) if skill_path else ""
    _r = resolve_official_skill_path(skill_name)
    if _r:
        base_dir = os.path.dirname(_r)
    ref_dir = os.path.join(base_dir, "references")

    if not os.path.isdir(ref_dir):
        return {"skill_name": skill_name, "sub_skills": []}

    subs = []
    for entry in sorted(os.listdir(ref_dir)):
        sub_dir = os.path.join(ref_dir, entry)
        if not os.path.isdir(sub_dir):
            continue
        sub_md = os.path.join(sub_dir, "SKILL.md")
        display_name = entry
        description = ""
        file_count = 0
        if os.path.isfile(sub_md):
            try:
                with open(sub_md, encoding="utf-8") as f:
                    front = f.read(500)
                m = _re.search(r'description:\s*["\']?(.*?)["\']?$', front, _re.MULTILINE)
                if m:
                    description = m.group(1)[:200]
                dm = _re.search(r'name:\s*["\']?(.*?)["\']?$', front, _re.MULTILINE)
                if dm:
                    display_name = dm.group(1)
            except Exception:
                pass
        for _, _, files in os.walk(sub_dir):
            file_count += len(files)
        subs.append({
            "name": entry,
            "display_name": display_name,
            "description": description,
            "file_count": file_count,
        })

    return {"skill_name": skill_name, "sub_skills": subs}


@router.get("/{skill_id}/sub/{sub_name}")
def read_sub_skill(skill_id: int, sub_name: str, user: dict = Depends(get_current_user)):
    """读取技能包子技能的 SKILL.md 内容"""
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_path FROM skill WHERE id=? AND is_active=1", (skill_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    from src.config import SKILLS_DIR, resolve_official_skill_path
    base_dir = os.path.join(SKILLS_DIR, row[1] or "") if row[1] else ""
    _r = resolve_official_skill_path(row[0])
    if _r:
        base_dir = os.path.dirname(_r)
    sub_md = os.path.join(base_dir, "references", sub_name, "SKILL.md")

    if not os.path.isfile(sub_md):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"子技能 {sub_name} 未找到"})

    try:
        with open(sub_md, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})

    # 也读取 references/ 下的文件列表
    ref_dir = os.path.join(base_dir, "references", sub_name, "references")
    refs = []
    if os.path.isdir(ref_dir):
        for r in sorted(os.listdir(ref_dir)):
            rp = os.path.join(ref_dir, r)
            if os.path.isfile(rp):
                try:
                    with open(rp, encoding="utf-8") as f:
                        refs.append({"name": r, "content": f.read()[:2000]})
                except Exception:
                    refs.append({"name": r, "content": ""})

    return {"name": sub_name, "content": content, "references": refs}


@router.get("/package-sub/{pkg_id}/{sub_name}")
def package_sub_skill(pkg_id: int, sub_name: str, user: dict = Depends(get_current_user)):
    """返回技能包子技能详情（格式与 skill detail 一致，支持导航到独立详情页）"""
    from src.session_manager import db, db_lock
    import re as _re

    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_path, scope FROM skill WHERE id=? AND is_active=1", (pkg_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "父技能未找到"})

    from src.config import SKILLS_DIR, resolve_official_skill_path
    base_dir = os.path.join(SKILLS_DIR, row[1] or "") if row[1] else ""
    _r = resolve_official_skill_path(row[0])
    if _r:
        base_dir = os.path.dirname(_r)
    sub_md = os.path.join(base_dir, "references", sub_name, "SKILL.md")
    if not os.path.isfile(sub_md):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"子技能 {sub_name} 未找到"})

    try:
        with open(sub_md, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})

    # 从 frontmatter 解析 name 和 description
    display_name = sub_name
    description = ""
    m = _re.search(r'^---\s*\n(.*?)\n---', content, _re.DOTALL)
    if m:
        front = m.group(1)
        dm = _re.search(r'name:\s*["\']?(.*?)["\']?\s*$', front, _re.MULTILINE)
        if dm:
            display_name = dm.group(1)
        desc_m = _re.search(r'description:\s*["\']?(.*?)["\']?\s*$', front, _re.MULTILINE)
        if desc_m:
            description = desc_m.group(1)[:200]

    return {
        "id": f"{pkg_id}/{sub_name}",
        "name": sub_name,
        "display_name": display_name,
        "description": description,
        "agent_type": "sre",
        "scope": row[2] or "official",
        "content": content,
        "type": "package-sub",
    }
