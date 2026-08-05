"""Skills 路由：列表、上传、删除、个人技能转仓库技能"""
import os, tempfile, zipfile, json, shutil
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel
from src.user import db as user_db
from src.user.validator import validate_skill as validate_custom_skill, parse_skill_frontmatter
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/")
async def list_skills(user: dict = Depends(get_current_user), repo: str = ""):
    """返回技能列表：official 所有用户可见；user 本人可见；repo 指定仓库可见"""
    from src.session_manager import db, db_lock

    skills = []
    with db_lock:
        c = db.cursor()
        try:
            if repo:
                # 官方 + 个人 + 指定仓库技能
                c.execute(
                    "SELECT skill_name, display_name, description, agent_type, scope, owner_id FROM skill WHERE is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?)) ORDER BY agent_type, skill_name",
                    (str(user["id"]), repo)
                )
            else:
                # 官方 + 个人
                c.execute(
                    "SELECT skill_name, display_name, description, agent_type, scope, owner_id FROM skill WHERE is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?)) ORDER BY agent_type, skill_name",
                    (str(user["id"]),)
                )
            for r in c.fetchall():
                skills.append({
                    "name": r[0],
                    "display_name": r[1] or r[0],
                    "description": r[2] or "",
                    "agent_type": r[3] or "",
                    "type": "official" if r[4] == "official" else "repo" if r[4] == "repo" else "user-uploaded",
                    "scope": r[4] or "",
                })
        except Exception:
            pass

    return {"skills": skills}


@router.post("/sync-official")
async def sync_official_skills(user: dict = Depends(get_current_user)):
    """从 configs/skill-description.json 同步官方技能到 skill 表。
    新增的官方技能会被插入，已有的会更新。返回新增数量。
    """
    from src.session_manager import db, db_lock

    config_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "configs")
    json_path = os.path.join(config_dir, "skill-description.json")
    if not os.path.exists(json_path):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "skill-description.json 未找到"})

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    added = 0
    with db_lock:
        c = db.cursor()
        for skill_name, info in data.items():
            display_name = info.get("name", skill_name)
            description = info.get("description", "")
            agent_type = info.get("agent_type", "")
            skill_path = info.get("path", skill_name)
            # 检查是否已存在
            existed = c.execute("SELECT COUNT(*) FROM skill WHERE skill_name=? AND scope='official'", (skill_name,)).fetchone()[0]
            c.execute(
                "INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,'official','')",
                (skill_name, display_name, description, agent_type, skill_path)
            )
            if existed == 0:
                added += 1
        db.commit()

    return {"message": f"官方技能同步完成，新增 {added} 个", "added": added}


@router.get("/template")
async def download_skill_template():
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
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="skill-template.zip"'},
    )


@router.get("/{name}")
async def get_skill_detail(name: str, user: dict = Depends(get_current_user),
                           repo: str = ""):
    """返回技能详情（含 SKILL.md 内容）"""
    from src.session_manager import db, db_lock
    from src.config import SKILLS_DIR

    # 查官方技能 + 个人技能 + 仓库技能（指定 repo 或用户可访问的全部）
    with db_lock:
        c = db.cursor()
        if repo:
            c.execute(
                "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id FROM skill WHERE skill_name=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))",
                (name, str(user["id"]), repo)
            )
        else:
            c.execute(
                "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id FROM skill WHERE skill_name=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR scope='repo')",
                (name, str(user["id"]))
            )
        row = c.fetchone()

    if row:
        skill_dir = row[4] or name
        scope = row[5]
        owner_id = row[6] or ""
        if scope == "user":
            skill_path = os.path.join(SKILLS_DIR, "custom", owner_id or str(user["id"]), skill_dir, "SKILL.md")
        elif scope == "repo":
            skill_path = os.path.join(SKILLS_DIR, "repo", owner_id, skill_dir, "SKILL.md")
        else:
            skill_path = os.path.join(SKILLS_DIR, skill_dir, "SKILL.md")
        content = ""
        if os.path.exists(skill_path):
            with open(skill_path, encoding="utf-8") as f:
                content = f.read()
        return {
            "name": row[0], "display_name": row[1] or row[0], "description": row[2] or "",
            "agent_type": row[3] or "",
            "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
            "scope": scope or "",
            "content": content,
        }

    raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})


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
                zf.extractall(extract_dir)
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
        if scope == "repo":
            repo_key = repo
            target_dir = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", repo_key, skill_name)
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
async def delete_skill(skill_name: str, user: dict = Depends(get_current_user),
                       scope: str = "user", repo: str = ""):
    if scope == "repo":
        exists = user_db.delete_skill(user["id"], skill_name, scope="repo", owner_id=repo)
    else:
        exists = user_db.delete_skill(user["id"], skill_name)
    if not exists:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})
    return {"message": f"技能 {skill_name} 已删除"}


class ConvertSkillRequest(BaseModel):
    skill_name: str
    repo: str  # 目标仓库 repo_key（如 Jason7602/skill-evaluator）


@router.post("/convert")
async def convert_skill(body: ConvertSkillRequest, user: dict = Depends(get_current_user)):
    """将个人技能（scope=user）复制为仓库技能（scope=repo）。

    原个人技能记录与目录保留（复制而非移动）。复制后可在此仓库的 Agent 中配置使用。
    """
    skill_name = (body.skill_name or "").strip()
    repo = (body.repo or "").strip()
    if not skill_name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "技能名不能为空"})
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "目标仓库不能为空"})

    # 1. 查原个人技能（须属于当前用户）
    skill_rec = next((s for s in user_db.list_skills(user["id"]) if s["skill_name"] == skill_name), None)
    if not skill_rec:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "个人技能未找到或不属于当前用户"})

    # 2. 校验目标仓库下无同名仓库技能
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        exists = c.execute("SELECT COUNT(*) FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1", (skill_name, repo)).fetchone()[0]
    if exists:
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"目标仓库已存在同名技能「{skill_name}」"})

    # 3. 复制目录 skills/custom/{user_id}/{skill}/ → skills/repo/{repo}/{skill}/
    src_dir = os.path.join(user_db.SKILL_DIR, str(user["id"]), skill_name)
    dst_dir = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", repo, skill_name)
    if not os.path.isdir(src_dir):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "个人技能目录不存在"})
    if os.path.exists(dst_dir):
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"目标仓库已存在同名技能「{skill_name}」"})

    os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
    shutil.copytree(src_dir, dst_dir)

    # 4. 创建仓库技能记录（沿用 agent_type / description）
    try:
        ok = user_db.create_skill(user["id"], skill_name, skill_rec.get("agent_type", ""),
                                  skill_rec.get("description", ""), "", scope="repo", owner_id=repo)
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
