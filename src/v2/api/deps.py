"""共享依赖：Token 验证、用户信息获取"""
from fastapi import Header, HTTPException, Depends
from src.user import db as user_db


def get_current_user(authorization: str = Header(None)) -> dict:
    """从 Authorization: Bearer <token> 提取并验证用户"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": 401, "message": "未登录"})
    token = authorization.replace("Bearer ", "").strip()
    uid = user_db.validate_token(token)
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话过期"})
    user = user_db.get_user(uid)
    if not user:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "用户不存在"})
    return {"id": uid, "username": user["username"], "token": token}
