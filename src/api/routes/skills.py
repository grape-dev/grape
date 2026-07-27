"""Skills 路由：列表、上传、删除"""
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from src.user import db as user_db
from src.user.validator import validate_skill as validate_custom_skill
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/skills", tags=["skills"])

AGENT_CATEGORIES = {
    "vod": ["event-monitor", "signal-analysis", "anomaly-detection"],
    "issue": ["issue-triage", "label-manager", "auto-assign"],
    "coding": ["code-gen", "pr-creator", "branch-manager"],
    "qa": ["code-review", "test-gen", "quality-check"],
    "sre": ["change-log", "deploy-monitor", "incident-response"],
}


@router.get("/")
async def list_skills(user: dict = Depends(get_current_user)):
    official = []
    for agent_type, skill_names in AGENT_CATEGORIES.items():
        for sname in skill_names:
            official.append({
                "name": sname,
                "agent_type": agent_type,
                "description": f"DevOps {agent_type.upper()} skill: {sname}",
                "type": "official",
            })

    custom = []
    user_skills_raw = user_db.get_user_skills(user["id"])
    for s in user_skills_raw:
        custom.append({
            "name": s.get("name", "unknown"),
            "agent_type": s.get("agent_type", ""),
            "description": s.get("description", "")[:100],
            "type": "user-uploaded",
        })

    return {"skills": official + custom}


@router.post("/upload")
async def upload_skill(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 文件不能超过 10MB"})

    import base64, yaml, tempfile, os, zipfile
    try:
        b64_content = base64.b64encode(content).decode()
        result = validate_custom_skill(b64_content)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"Skill 校验失败: {e}"})

    return {"message": "技能上传成功", "skill": result}


@router.delete("/{skill_name}")
async def delete_skill(skill_name: str, user: dict = Depends(get_current_user)):
    skills = user_db.get_user_skills(user["id"])
    for s in skills:
        if s.get("name") == skill_name:
            user_db.delete_user_skill(s["id"])
            return {"message": f"技能 {skill_name} 已删除"}
    raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})
