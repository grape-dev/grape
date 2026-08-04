"""Skills 路由：列表、上传、删除"""
import os, tempfile, zipfile, json
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from src.user import db as user_db
from src.user.validator import validate_skill as validate_custom_skill
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


@router.get("/{name}")
async def get_skill_detail(name: str, user: dict = Depends(get_current_user),
                           repo: str = ""):
    """返回技能详情（含 SKILL.md 内容）"""
    from src.session_manager import db, db_lock
    from src.config import SKILLS_DIR

    # 查官方技能 + 个人技能 + 指定仓库技能
    with db_lock:
        c = db.cursor()
        if repo:
            c.execute(
                "SELECT skill_name, display_name, description, agent_type, skill_path, scope FROM skill WHERE skill_name=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))",
                (name, str(user["id"]), repo)
            )
        else:
            c.execute(
                "SELECT skill_name, display_name, description, agent_type, skill_path, scope FROM skill WHERE skill_name=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?))",
                (name, str(user["id"]))
            )
        row = c.fetchone()

    if row:
        skill_dir = row[4] or name
        scope = row[5]
        if scope == "user":
            skill_path = os.path.join(SKILLS_DIR, "custom", str(user["id"]), skill_dir, "SKILL.md")
        elif scope == "repo":
            skill_path = os.path.join(SKILLS_DIR, "repo", repo or "", skill_dir, "SKILL.md")
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
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        entries = os.listdir(extract_dir)
        if not entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 文件为空"})

        skill_dir = os.path.join(extract_dir, entries[0])
        if not os.path.isdir(skill_dir):
            skill_dir = extract_dir

        skill_name = os.path.basename(skill_dir) if os.path.isdir(skill_dir) else entries[0]
        errors = validate_custom_skill(skill_dir, skill_name)
        if errors:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"Skill 校验失败: {'; '.join(errors)}"})

        import shutil
        if scope == "repo":
            repo_key = repo
            target_dir = os.path.join(os.path.dirname(user_db.SKILL_DIR), "repo", repo_key, skill_name)
            os.makedirs(os.path.dirname(target_dir), exist_ok=True)
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(skill_dir, target_dir)
            user_db.create_skill(user["id"], skill_name, agent_type, "", "", scope="repo", owner_id=repo_key)
        else:
            user_skill_dir = os.path.join(user_db.SKILL_DIR, str(user["id"]), skill_name)
            os.makedirs(os.path.dirname(user_skill_dir), exist_ok=True)
            if os.path.exists(user_skill_dir):
                shutil.rmtree(user_skill_dir)
            shutil.copytree(skill_dir, user_skill_dir)
            user_db.create_skill(user["id"], skill_name, agent_type, "", "")

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
