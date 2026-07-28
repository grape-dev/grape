"""Skills 路由：列表、上传、删除"""
import os, tempfile, zipfile, json
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from src.user import db as user_db
from src.user.validator import validate_skill as validate_custom_skill
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/")
async def list_skills(user: dict = Depends(get_current_user)):
    """返回技能列表：official 从 agent_type_config 读取，custom 从用户上传"""
    from src.session_manager import db, db_lock

    official = []
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT agent_type, display_name, description, parent_agent_type FROM agent_type_config WHERE parent_agent_type!='' ORDER BY parent_agent_type, agent_type"
            )
            for r in c.fetchall():
                official.append({
                    "name": r[0],
                    "display_name": r[1],
                    "description": r[2] or "",
                    "agent_type": r[3],
                    "type": "official",
                })
        except Exception:
            pass

    custom = []
    user_skills_raw = user_db.list_skills(user["id"])
    for s in user_skills_raw:
        custom.append({
            "name": s.get("skill_name", "unknown"),
            "display_name": s.get("skill_name", "unknown"),
            "description": f"User-uploaded skill: {s.get('skill_name', '')}",
            "agent_type": "",
            "type": "user-uploaded",
        })

    return {"skills": official + custom}


@router.post("/upload")
async def upload_skill(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
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
        user_skill_dir = os.path.join(user_db.SKILL_DIR, str(user["id"]), skill_name)
        os.makedirs(os.path.dirname(user_skill_dir), exist_ok=True)
        if os.path.exists(user_skill_dir):
            shutil.rmtree(user_skill_dir)
        shutil.copytree(skill_dir, user_skill_dir)

        user_db.create_skill(user["id"], skill_name, "")
        user_db.save_skill_md(user["id"], skill_name)

        return {"message": f"技能 {skill_name} 上传成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"上传失败: {e}"})
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.delete("/{skill_name}")
async def delete_skill(skill_name: str, user: dict = Depends(get_current_user)):
    skills = user_db.list_skills(user["id"])
    exists = any(s.get("skill_name") == skill_name for s in skills)
    if not exists:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})
    user_db.delete_skill(user["id"], skill_name)
    return {"message": f"技能 {skill_name} 已删除"}
