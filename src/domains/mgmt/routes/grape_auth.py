"""Grape 独立账号体系路由——注册/登录/忘记密码/修改密码/设置密码/邮箱变更/注销/绑定/解绑。

与现有 auth.py 共存：
- auth.py 保留现有 OAuth 登录（gitcode/github）+ 密码登录 + refresh + profile + logout
- grape_auth.py 新增 Grape 独立账号体系路由
- 前端按需调用新旧路由，迁移完成后逐步废弃旧路由
"""

import os

import re

import hashlib

import time

from fastapi import APIRouter, HTTPException, Request, Response, Depends

from pydantic import BaseModel

from src.kernel.config import log

from src.api.deps import require_no_session_token, require_jwt_only, invalidate_user_cache

from src.domains.mgmt.auth import password as auth_password

from src.domains.mgmt.auth import verification as auth_verification

from src.domains.mgmt.auth import audit as auth_audit

from src.domains.mgmt.auth import signup_policy

from src.domains.mgmt.auth import login_throttle

from src.domains.mgmt.auth import grape_account_db

from src.domains.mgmt.auth.grape_account_db import (
    create_grape_account, create_grape_account_oauth,
    get_grape_account_by_identifier, get_grape_account_by_email,
    get_grape_account_by_id, verify_grape_account,
    update_password, set_password as db_set_password, update_email,
    update_profile, get_token_version, increment_token_version,
    is_email_registered, is_username_taken,
    get_user_identities, create_external_identity,
    delete_external_identity, has_password,
    get_external_identity, get_external_identity_any_status,
    update_external_identity_tokens,
    _sync_um_platform_col,
    update_last_login_at,
    # D22: 软删除/恢复绑定支持
    get_unbound_identity, restore_external_identity,
    # 绑定转移支持
    save_binding_ticket, validate_binding_ticket, consume_binding_ticket,
    transfer_external_identity,
    transfer_external_identity_in_tx, transfer_business_data,
)

from src.domains.mgmt.auth.auth_guards import (
    AccountDeactivationCascade,
    ChannelGate,
    LoginThrottlePolicy,
    OAuthStateStore,
    UnbindGuard,
)

from src.kernel.auth_sdk.cookie_utils import (
    set_refresh_cookie as _set_refresh_cookie,
    clear_refresh_cookie as _clear_refresh_cookie,
    app_base as _app_base,
)

router = APIRouter(prefix="/api/auth", tags=["grape-auth"])

def _get_client_ip(request: Request) -> str:
    # P2-1: 仅在配置了可信代理时才信任 X-Forwarded-For
    trusted_proxies = os.environ.get("TRUSTED_PROXY_IPS", "").strip()
    xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if xff and trusted_proxies:
        proxy_ips = [p.strip() for p in trusted_proxies.split(",") if p.strip()]
        if request.client and request.client.host in proxy_ips:
            return xff
    return request.client.host if request.client else ""

def _get_request_lang(request: Request, body_lang: str = "") -> str:
    """Extract user's language preference from request body or Accept-Language header."""
    lang = (body_lang or "").strip().lower()
    if not lang:
        al = (request.headers.get("Accept-Language", "") or "").split(",")[0].strip().lower()
        if al:
            lang = al
    if lang.startswith("en"):
        return "en"
    return "zh"

def _safe_decrypt_state_token(state_data: dict) -> str:
    """P2 加固：从 state_data 解密 access_token（加密存储，安全消费）。

    解密失败时返回空字符串——调用方通过 _get_state_access_token 包装检查。
    """
    enc = state_data.get("oauth_access_token_enc", "")
    if not enc:
        return ""
    try:
        from src.domains.mgmt.auth.credential_crypto import decrypt
        return decrypt(enc)
    except Exception as e:
        log.warning(f"state access_token decrypt failed: {e}")
        return ""

def _get_state_access_token(state_data: dict):
    """从 state_data 获取 access_token，解密失败时返回 None 标记失败。

    返回 (token_str, ok_bool)：
    - 无加密值 → ("", True)  旧 state 兼容
    - 解密成功 → (token, True)
    - 解密失败 → ("", False)  调用方应走失败路径
    """
    enc = state_data.get("oauth_access_token_enc", "")
    if not enc:
        return "", True
    token = _safe_decrypt_state_token(state_data)
    if not token:
        return "", False
    return token, True

def _clear_user_repos_cache(user_id: str):
    """D43: 清理 user_repos 缓存——绑定/解绑新平台后旧缓存不完整，必须清除。
    统一入口，所有绑定/解绑路径调用此函数。
    """
    try:
        from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
        with _sm_dl:
            _c = _sm_db.cursor()
            _c.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
            _sm_db.commit()
    except Exception as e:
        log.warning(f"_clear_user_repos_cache failed for user={user_id}: {e}")

def _issue_jwt(user_id: str, username: str, token_version: int,
               login_provider: str = "grape"):
    """签发 JWT 双 token。"""
    from src.kernel.auth_sdk.jwt_utils import create_access_token, create_refresh_token
    access = create_access_token(user_id, username, token_version,
                                  login_provider=login_provider)
    refresh = create_refresh_token(user_id, username, token_version,
                                    login_provider=login_provider)
    return access, refresh

class RegisterSendCodeBody(BaseModel):
    username: str
    email: str
    password: str = ""  # 可选——send-code 阶段不校验密码，verify 阶段校验

class RegisterVerifyBody(BaseModel):
    username: str
    email: str
    password: str
    code: str
    lang: str = ""

class LoginBody(BaseModel):
    identifier: str = ""
    username: str = ""
    password: str
    lang: str = ""

class ForgotPasswordSendCodeBody(BaseModel):
    email: str

class ForgotPasswordResetBody(BaseModel):
    email: str
    code: str
    new_password: str

class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str

class SetPasswordSendCodeBody(BaseModel):
    pass  # 无请求体，验证码发到当前用户邮箱

class SetPasswordBody(BaseModel):
    code: str
    new_password: str

class ChangeEmailSendCodeBody(BaseModel):
    new_email: str

class ChangeEmailBody(BaseModel):
    new_email: str
    code: str

class DeleteAccountBody(BaseModel):
    password: str = ""
    code: str = ""
    oauth_state: str = ""  # 需求 14: 无密码无邮箱用户通过 OAuth 重新授权
    force: bool = False  # P1-3: 有未完成资源时用户确认强制注销

class DeleteAccountSendCodeBody(BaseModel):
    pass  # 无请求体，验证码发到当前用户邮箱

class UnlockSendCodeBody(BaseModel):
    identifier: str

class UnlockBody(BaseModel):
    identifier: str
    code: str

_RESERVED_USERNAMES = frozenset({
    "admin", "root", "system", "grape", "api", "bot", "devops",
    "support", "help", "administrator", "superuser",
})

def _validate_username(username: str) -> str | None:
    """校验用户名规则，返回错误信息或 None。"""
    if len(username) < 3:
        return "用户名至少 3 个字符"
    if len(username) > 32:
        return "用户名最多 32 个字符"
    if not all(c.isascii() and (c.isalnum() or c == "_") for c in username):
        return "用户名仅允许字母、数字、下划线"
    if username.lower() in _RESERVED_USERNAMES:
        return "该用户名为保留字，不可使用"
    return None

@router.post("/register/send-code")
def register_send_code(body: RegisterSendCodeBody, request: Request):
    """发送注册验证码。"""
    email = body.email.lower().strip()
    ip = _get_client_ip(request)

    # R7: 无邮箱配置降级——跳过验证码发送，提示前端直接注册
    from src.domains.mgmt.auth.email_service import is_email_configured
    if not is_email_configured():
        return {"error_code": "EMAIL_NOT_CONFIGURED", "message": "Email service not configured, please register with username and password directly"}

    # 用户名校验
    username_err = _validate_username(body.username)
    if username_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_INVALID", "message": username_err})

    # 用户名唯一性
    if is_username_taken(body.username):
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_TAKEN", "message": "用户名已被占用"})

    # 邮箱格式
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "EMAIL_INVALID", "message": "邮箱格式不正确"})

    # 注册策略
    if not signup_policy.check_signup_allowed(email):
        raise HTTPException(status_code=403, detail={"code": 403, "error_code": "EMAIL_NOT_ALLOWED", "message": "该邮箱不在允许注册范围内"})

    # 防枚举：已注册邮箱统一返回"验证码已发送"，不暴露账号存在性
    # 已注册邮箱不实际发送验证码，但发通知邮件提醒原账号持有人
    # 明确告知"已注册"放到 register/verify 阶段（需验证码，枚举成本高）
    already_registered = is_email_registered(email)

    if not already_registered:
        result = auth_verification.send_code(email, "register", ip)
        if not result["ok"]:
            raise HTTPException(status_code=429, detail={"code": 429, "message": result["message"]})
    else:
        # 已注册邮箱：模拟限流检查，使 429 vs 200 不可区分
        rl_result = auth_verification.check_rate_limit(email, "register", ip)
        if not rl_result["ok"]:
            raise HTTPException(status_code=429, detail={"code": 429, "message": rl_result["message"]})
        # 通知原账号持有人（不发送验证码）
        try:
            from src.domains.mgmt.auth.email_service import get_email_service
            _svc = get_email_service()
            _svc.send_notification(
                to=email,
                subject="您的邮箱注册提醒",
                body="有人尝试使用您的邮箱在本系统注册。如非本人操作请忽略此邮件；如是您本人，请直接登录或找回密码。",
            )
        except Exception as _e:
            log.warning(f"Failed to send already-registered notification to {email}: {_e}")

    return {"message": "验证码已发送"}

@router.post("/register/verify")
def register_verify(body: RegisterVerifyBody, request: Request, response: Response):
    """验证注册验证码并创建账号。"""
    email = body.email.lower().strip()
    ip = _get_client_ip(request)

    # R7: 无邮箱配置降级——跳过验证码校验，email 允许为空，直接创建账号
    from src.domains.mgmt.auth.email_service import is_email_configured
    email_configured = is_email_configured()

    # 再次校验用户名和密码
    username_err = _validate_username(body.username)
    if username_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_INVALID", "message": username_err})
    if is_username_taken(body.username):
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_TAKEN", "message": "用户名已被占用"})
    pwd_err = auth_password.check_password_strength(body.password, body.username, email)
    if pwd_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "PASSWORD_WEAK", "message": pwd_err})

    # 校验验证码（无邮箱配置时跳过）
    if email_configured:
        result = auth_verification.verify_code(email, "register", body.code)
        if not result["ok"]:
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "CODE_INVALID", "message": result["message"]})

    # 创建账号（R7: 无邮箱配置时 email_verified=False）
    account = create_grape_account(body.username, email, body.password,
                                   email_verified=email_configured)
    if not account:
        # D8 修复：并发注册竞态——数据库 UNIQUE 兜底，检查具体冲突原因
        if is_email_registered(email):
            raise HTTPException(status_code=409, detail={"code": 409, "error_code": "EMAIL_ALREADY_REGISTERED", "message": "该邮箱已注册，请直接登录或重置密码"})
        if is_username_taken(body.username):
            raise HTTPException(status_code=409, detail={"code": 409, "error_code": "USERNAME_TAKEN", "message": "用户名已被占用"})
        raise HTTPException(status_code=500, detail={"code": 500, "error_code": "ACCOUNT_CREATE_FAILED", "message": "创建账号失败"})

    # 创建个人组织（根据用户语言偏好命名）
    _ensure_personal_org(account["id"], body.username, _get_request_lang(request, body.lang))

    # 签发 JWT
    token_version = get_token_version(account["id"])
    access, refresh = _issue_jwt(account["id"], account["username"], token_version)
    _set_refresh_cookie(response, refresh)

    # D11: 更新 last_login_at
    update_last_login_at(account["id"])

    # 审计日志
    auth_audit.log_audit("register", user_id=account["id"], ip=ip,
                         detail={"source": "email", "email": email})

    return {"token": access, "user": {"id": account["id"], "username": account["username"], "email": email}}

@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    """统一密码登录（identifier + password）。

    identifier 自动识别：包含 @ 为邮箱，否则为用户名。
    兼容旧客户端发送 username 字段。
    """
    # P1-2 修复：identifier 归一化（.lower()），防止大小写绕过账号锁定
    identifier = (body.identifier or body.username).strip().lower()
    ip = _get_client_ip(request)

    # 限流检查
    throttle = login_throttle.check_throttle(identifier, ip)
    if not throttle["allowed"]:
        # P1-3: 账号锁定时记录审计日志（需求 16）
        if throttle.get("locked") or throttle.get("ip_locked"):
            try:
                auth_audit.log_audit("account_locked",
                                     ip=ip,
                                     detail={"type": "ip_locked" if throttle.get("ip_locked") else "account_locked",
                                             "identifier": auth_audit.mask_identifier(identifier)})
            except Exception:
                pass  # 审计日志失败不影响限流响应
        if throttle.get("captcha"):
            raise HTTPException(status_code=429, detail={"code": 429, "error_code": throttle.get("error_code", ""), "message": throttle["message"], "captcha": True, **({"email_configured": throttle["email_configured"]} if "email_configured" in throttle else {})})
        raise HTTPException(status_code=429, detail={"code": 429, "error_code": throttle.get("error_code", ""), "message": throttle["message"], **({"email_configured": throttle["email_configured"]} if "email_configured" in throttle else {})})

    # 验证账号密码（D32: 只查 grape_accounts，不 fallback 旧表）
    account = verify_grape_account(identifier, body.password)

    if not account:
        login_throttle.record_failure(identifier, ip)
        auth_audit.log_audit("login_failed", ip=ip,
                             detail={"identifier": auth_audit.mask_identifier(identifier)})
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "CREDENTIAL_INCORRECT", "message": "账号或密码不正确，请重新输入"})

    # 密码正确但账号被禁用 → 才暴露禁用状态
    if account.get("status", 1) != 1:
        auth_audit.log_audit("login_failed", ip=ip,
                             detail={"identifier": auth_audit.mask_identifier(identifier),
                                     "reason": "account_disabled"})
        # BUG-9 修复：区分冷静期(status=3)和禁用(status=0)
        if account.get("status") == 3:
            raise HTTPException(status_code=403, detail={"code": 403, "error_code": "ACCOUNT_DEACTIVATING", "message": "该账号正在注销中，暂时无法登录"})
        raise HTTPException(status_code=403, detail={"code": 403, "error_code": "ACCOUNT_DEACTIVATED", "message": "该账号已被停用，请联系管理员"})

    # 登录成功，清除失败记录
    login_throttle.clear_failures(identifier, ip)

    # 补建个人组织（注册时可能因 slug 冲突失败，登录时幂等补建）
    _ensure_personal_org(account["id"], account["username"], _get_request_lang(request, body.lang))

    # 签发 JWT
    token_version = account.get("token_version", 0)
    access, refresh = _issue_jwt(account["id"], account["username"], token_version)
    _set_refresh_cookie(response, refresh)

    # D11: 更新 last_login_at
    update_last_login_at(account["id"])

    # 审计日志
    auth_audit.log_audit("login_success", user_id=account["id"], ip=ip,
                         detail={"method": "password"})

    return {
        "token": access,
        "user": {"id": account["id"], "username": account["username"], "email": account.get("email", "")},
    }

@router.post("/forgot-password/send-code")
def forgot_password_send_code(body: ForgotPasswordSendCodeBody, request: Request):
    """发送密码重置验证码。

    安全：统一返回 200 "验证码已发送"，不管邮箱是否已注册。
    邮箱不存在时不实际发送邮件，但返回相同响应，防用户枚举。
    """
    # R7: 无邮箱配置降级——返回 503，提示联系管理员
    from src.domains.mgmt.auth.email_service import is_email_configured
    if not is_email_configured():
        raise HTTPException(status_code=503, detail={"code": 503, "error_code": "EMAIL_NOT_CONFIGURED", "message": "Email service not configured, please contact administrator to reset password"})

    email = body.email.lower().strip()
    ip = _get_client_ip(request)

    # 检查邮箱是否已注册（不暴露结果给调用方）
    account = get_grape_account_by_email(email) if email and "@" in email else None

    # BUG-8 修复：对未注册邮箱也执行限流检查，使 429 vs 200 不可区分。
    if account:
        result = auth_verification.send_code(email, "reset_password", ip)
        if not result["ok"]:
            raise HTTPException(status_code=429, detail={"code": 429, "message": result["message"]})
    else:
        # 未注册邮箱：模拟限流检查，返回与已注册邮箱一致的响应
        rl_result = auth_verification.check_rate_limit(email, "reset_password", ip)
        if not rl_result["ok"]:
            raise HTTPException(status_code=429, detail={"code": 429, "message": rl_result["message"]})

    # 统一返回成功（邮箱不存在时也返回成功，不实际发送）
    return {"message": "验证码已发送"}

@router.post("/forgot-password/reset")
def forgot_password_reset(body: ForgotPasswordResetBody, request: Request):
    """验证码重置密码。"""
    email = body.email.lower().strip()
    ip = _get_client_ip(request)

    # 校验验证码
    result = auth_verification.verify_code(email, "reset_password", body.code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "CODE_INVALID", "message": result["message"]})

    # 查找账号（grape_accounts 优先，旧表无 email 字段无需 fallback）
    account = get_grape_account_by_email(email)
    if not account:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "ACCOUNT_NOT_FOUND", "message": "账号不存在"})

    # 校验新密码强度
    pwd_err = auth_password.check_password_strength(body.new_password, account["username"], email)
    if pwd_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "PASSWORD_WEAK", "message": pwd_err})

    # 更新密码
    update_password(account["id"], body.new_password)
    # token_version +1，全设备下线
    increment_token_version(account["id"])
    invalidate_user_cache(account["id"])

    auth_audit.log_audit("password_reset", user_id=account["id"], ip=ip)

    # 提示账号状态（非正常状态给用户明确信息）
    if account.get("status", 1) == 0:
        return {"message": "密码重置成功。账号当前已被禁用，请联系管理员恢复后登录"}
    if account.get("status", 1) == 3:
        return {"message": "密码重置成功。账号处于注销冷静期，请联系管理员恢复后登录"}
    return {"message": "密码重置成功"}

@router.post("/change-password")
def change_password(body: ChangePasswordBody, user: dict = Depends(require_jwt_only),
                    request: Request = None, response: Response = None):
    """修改密码（需登录态）。"""
    ip = _get_client_ip(request) if request else ""

    # 校验当前密码（D32: 只查 grape_accounts，不 fallback 旧表）
    account = get_grape_account_by_id(user["id"])
    if not account or not auth_password.verify_password(body.current_password, account["password_hash"]):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "当前密码错误"})

    # 校验新密码
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "新密码不可与当前密码相同"})
    pwd_err = auth_password.check_password_strength(body.new_password, account["username"], account.get("email", ""))
    if pwd_err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": pwd_err})

    # 更新密码（D32: 只走 grape_accounts）
    update_password(user["id"], body.new_password)
    increment_token_version(user["id"])
    invalidate_user_cache(user["id"])

    # 当前设备签发新 token
    token_version = get_token_version(user["id"])
    access, refresh = _issue_jwt(user["id"], user["username"], token_version)

    auth_audit.log_audit("password_change", user_id=user["id"], ip=ip)

    # 设置新 refresh cookie
    if response:
        _set_refresh_cookie(response, refresh)

    return {"token": access}

@router.post("/set-password/send-code")
def set_password_send_code(user: dict = Depends(require_jwt_only), request: Request = None):
    """发送设置密码验证码（无密码用户）。"""
    # R14-2: 无邮箱配置降级——返回 503，对齐 forgot_password_send_code 模式
    from src.domains.mgmt.auth.email_service import is_email_configured
    if not is_email_configured():
        raise HTTPException(status_code=503, detail={"code": 503, "error_code": "EMAIL_NOT_CONFIGURED", "message": "Email service not configured, please contact administrator"})

    ip = _get_client_ip(request) if request else ""

    account = get_grape_account_by_id(user["id"])
    if not account:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "账号不存在"})

    if has_password(user["id"]):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "已设置密码，请使用修改密码"})

    if not account["email"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请先绑定邮箱"})

    result = auth_verification.send_code(account["email"], "set_password", ip)
    if not result["ok"]:
        raise HTTPException(status_code=429, detail={"code": 429, "message": result["message"]})

    return {"message": "验证码已发送"}

@router.post("/set-password")
def set_password(body: SetPasswordBody, user: dict = Depends(require_jwt_only),
                 request: Request = None, response: Response = None):
    """设置密码（无密码用户）。"""
    ip = _get_client_ip(request) if request else ""

    account = get_grape_account_by_id(user["id"])
    if not account:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "账号不存在"})

    # BUG-10 修复：TOCTOU — send-code 阶段检查了 has_password，set_password 阶段需重新检查。
    # 用户可能在两步之间通过 change_password 设置了密码，此时 set_password 应拒绝覆盖。
    if has_password(user["id"]):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "密码已设置，如需修改请使用修改密码功能"})

    if not account["email"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请先绑定邮箱"})

    # 校验验证码
    result = auth_verification.verify_code(account["email"], "set_password", body.code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": result["message"]})

    # 校验新密码强度
    pwd_err = auth_password.check_password_strength(body.new_password, account["username"], account["email"])
    if pwd_err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": pwd_err})

    # 设置密码
    db_set_password(user["id"], body.new_password)

    # P2-16: 遵循需求12"设置密码后不触发其他设备下线"
    # set_password 是无密码用户主动增加登录方式，不是密码变更。
    # 不调 increment_token_version（会让所有已登录设备 token 失效）。
    # 仅刷新缓存确保密码哈希及时生效。
    from src.api.deps import invalidate_user_cache
    invalidate_user_cache(user["id"])

    # 给当前设备签发新 token（不带 login_provider，因为是密码设置的）
    token_version = get_token_version(user["id"])
    access, refresh = _issue_jwt(user["id"], user["username"], token_version)
    if response:
        _set_refresh_cookie(response, refresh)

    auth_audit.log_audit("set_password", user_id=user["id"], ip=ip)

    return {"message": "密码设置成功", "token": access}

@router.post("/change-email/send-code")
def change_email_send_code(body: ChangeEmailSendCodeBody, user: dict = Depends(require_jwt_only),
                           request: Request = None):
    """发送邮箱变更验证码。"""
    # R14-2: 无邮箱配置降级——返回 503，对齐 forgot_password_send_code 模式
    from src.domains.mgmt.auth.email_service import is_email_configured
    if not is_email_configured():
        raise HTTPException(status_code=503, detail={"code": 503, "error_code": "EMAIL_NOT_CONFIGURED", "message": "Email service not configured, please contact administrator"})

    ip = _get_client_ip(request) if request else ""
    new_email = body.new_email.lower().strip()

    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "邮箱格式不正确"})

    # BUG-12 修复：校验新旧邮箱不同
    account = get_grape_account_by_id(user["id"])
    old_email = account["email"] if account and account["email"] else ""
    if old_email and new_email == old_email:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "新邮箱与当前邮箱相同"})

    if is_email_registered(new_email):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "该邮箱已被占用"})

    # 限流归属当前用户账号（非新邮箱），防止用他人邮箱触发验证码发送
    # 首次设置邮箱时（old_email 为空）用用户 ID 作为限流 key
    rl_key = old_email if old_email else f"user:{user['id']}"

    result = auth_verification.send_code(new_email, "change_email", ip, rate_limit_key=rl_key)
    if not result["ok"]:
        raise HTTPException(status_code=429, detail={"code": 429, "message": result["message"]})

    return {"message": "验证码已发送"}

@router.post("/change-email")
def change_email(body: ChangeEmailBody, user: dict = Depends(require_jwt_only),
                 request: Request = None):
    """邮箱变更（或首次设置邮箱）。"""
    ip = _get_client_ip(request) if request else ""
    new_email = body.new_email.lower().strip()

    account = get_grape_account_by_id(user["id"])
    old_email = account["email"] if account and account["email"] else ""

    # 验证码验证：verify_code 必须用与 send_code 相同的 rl_key 查找
    # send_code 将验证码存储在 email=rl_key 的记录中
    rl_key = old_email if old_email else f"user:{user['id']}"
    result = auth_verification.verify_code(rl_key, "change_email", body.code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": result["message"]})

    # 向旧邮箱发送通知
    if old_email:
        from src.domains.mgmt.auth.email_service import get_email_service
        try:
            get_email_service().send_notification(
                old_email, "邮箱已变更",
                "您的 Grape 账号邮箱已被更换。若非本人操作，请联系管理员。"
            )
        except Exception:
            pass

    # 更新邮箱
    result = update_email(user["id"], new_email)
    if result == "conflict":
        raise HTTPException(status_code=409, detail={"code": 409, "message": "该邮箱已被占用"})
    if not result:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "邮箱更新失败"})

    auth_audit.log_audit("email_change", user_id=user["id"], ip=ip,
                         detail={"old_email": auth_audit.mask_email(old_email),
                                 "new_email": auth_audit.mask_email(new_email)})

    return {"message": "邮箱变更成功"}

@router.post("/delete-account/send-code")
def delete_account_send_code(user: dict = Depends(require_jwt_only), request: Request = None):
    """发送账号注销验证码。"""
    # R14-2: 无邮箱配置降级——返回 503，对齐 forgot_password_send_code 模式
    from src.domains.mgmt.auth.email_service import is_email_configured
    if not is_email_configured():
        raise HTTPException(status_code=503, detail={"code": 503, "error_code": "EMAIL_NOT_CONFIGURED", "message": "Email service not configured, please contact administrator"})

    ip = _get_client_ip(request) if request else ""

    account = get_grape_account_by_id(user["id"])
    if not account:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "账号不存在"})

    if not account["email"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请先绑定邮箱"})

    result = auth_verification.send_code(account["email"], "delete_account", ip)
    if not result["ok"]:
        raise HTTPException(status_code=429, detail={"code": 429, "message": result["message"]})

    return {"message": "验证码已发送"}

@router.get("/delete-account/oauth/{provider}")
def delete_account_via_oauth(provider: str, popup: bool = False, prompt: str = "",
                             user: dict = Depends(require_jwt_only),
                             request: Request = None):
    """需求 14 修正：无密码无邮箱用户通过 OAuth 重新授权发起注销认证。

    发起 OAuth 授权，state 携带 delete_account_user_id。
    回调后校验 external_id 与用户已有绑定匹配 → 认证通过 → 完成注销。
    popup=True 时 state 携带 popup 标记，回调重定向到中间页走 postMessage。
    prompt=login 时强制第三方平台重新认证（高危操作不允许复用浏览器会话）。
    """
    from src.domains.mgmt.auth.providers import get_provider, build_auth_url, get_redirect_uri
    import hashlib as _hashlib
    import os as _os

    cfg = get_provider(provider)
    if not cfg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的平台: {provider}"})


    # 检查用户是否已绑定该平台
    from src.domains.mgmt.auth.grape_account_db import get_user_identities
    identities = get_user_identities(user["id"])
    bound_providers = {ident["provider"] for ident in identities}
    if provider not in bound_providers:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"未绑定 {provider} 平台"})

    redirect_uri = get_redirect_uri(request, provider)
    state = _hashlib.sha256(_os.urandom(32)).hexdigest()

    extra_state = {"delete_account_user_id": user["id"]}
    if popup:
        extra_state["popup"] = True
    # 通过 extra_state_data 传递 delete_account_user_id（build_auth_url 内部一次 save_oauth_state）
    result = build_auth_url(provider, state, redirect_uri,
                           extra_state_data=extra_state,
                           prompt=prompt)
    if not result:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "构建 OAuth 授权 URL 失败"})

    return {"redirect_url": result["redirect_url"]}

@router.post("/delete-account")
def delete_account(body: DeleteAccountBody, user: dict = Depends(require_jwt_only),
                   request: Request = None):
    """账号注销（软删除，进入 30 天冷静期）。"""
    ip = _get_client_ip(request) if request else ""

    account = get_grape_account_by_id(user["id"])
    if not account:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "账号不存在"})

    # 需求14: 管理员账号需先转移管理员权限才可注销
    from src.domains.mgmt.admin import db as admin_db
    admin = admin_db.get_admin_by_username(account["username"])
    if admin:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "该账号是管理员，请先转移管理员权限后再注销",
        })

    # 重新认证（需求 14: 认证方式动态选择）
    has_password = bool(account["password_hash"])
    has_email = bool(account["email"])
    if body.password:
        if not has_password or not auth_password.verify_password(body.password, account["password_hash"]):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "密码错误"})
    elif body.code and has_email:
        result = auth_verification.verify_code(account["email"], "delete_account", body.code)
        if not result["ok"]:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "验证码错误"})
    elif body.oauth_state:
        # 需求 14 修正：无密码无邮箱用户通过 OAuth 重新授权认证
        from src.domains.mgmt.user import db as user_db
        # 校验 OAuth state 中的 delete_account_user_id 是否匹配当前用户
        state_data = user_db.validate_oauth_state(body.oauth_state, "")
        if state_data is None:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "OAuth 授权会话已过期，请重新操作"})
        if str(state_data.get("delete_account_user_id") or "") != str(user["id"] or ""):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "OAuth 授权用户不匹配"})
        # OAuth 重新授权成功 → 认证通过
    else:
        # 无密码无邮箱 → 检查是否有 OAuth 绑定，引导用户走 OAuth 重新授权
        if not has_password and not has_email:
            from src.domains.mgmt.auth.grape_account_db import get_user_identities
            identities = get_user_identities(user["id"])
            if identities:
                # 有 OAuth 绑定 → 返回需要 OAuth 重新授权的提示
                providers = list({ident["provider"] for ident in identities})
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": "请通过第三方平台重新授权完成注销认证",
                    "need_oauth": True,
                    "providers": providers,
                })
            else:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": "无法完成注销认证：请先绑定邮箱或设置密码",
                })
        else:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "请提供密码或验证码"})

    # BUG-11 修复：需求14 — 检查未完成资源
    # 1. 检查运行中的 Agent 任务
    from src.domains.mgmt.user import db as user_db
    user_repos = user_db.get_user_repos(user["id"])
    if user_repos:
        repo_keys = [r.get("repo_key") or r.get("full_name", "") for r in user_repos]
        repo_keys = [k for k in repo_keys if k]
        if repo_keys:
            # `agent_tasks` 归 runtime：在制任务数走 owner 域原语（任意拓扑），
            # 不再拿 kernel 进程主库句柄直发（三库拆分后 mgmt 库里没有该表）。
            from src.domains.runtime import public as _runtime_public
            try:
                from src.kernel.gsb.provider import get_service as _gs
                running_count = _gs("runtime").get_inflight_task_count(repo_keys)
            except Exception as _ge:  # noqa: BLE001
                from src.kernel.config import log as _lg
                _lg.debug(f"grape_auth: provider inflight count failed, fallback: {_ge}")
                running_count = _runtime_public.inflight_task_count_any(repo_keys)
            if running_count > 0:
                # P1-3: 用户确认强制注销时放行，否则提示先处理
                if not body.force:
                    raise HTTPException(status_code=409, detail={
                        "code": 409,
                        "message": f"有 {running_count} 个进行中的任务，请等待完成后再注销，或确认强制注销",
                        "running_tasks": running_count,
                        "need_force": True,
                    })

    # 2. 检查是否是组织唯一管理员（非个人组织）
    from src.domains.mgmt.user.org_db import list_user_orgs, get_org_members
    for org in list_user_orgs(user["id"]):
        if org.get("is_personal"):
            continue  # 个人组织不阻止注销
        if org.get("my_role") == "owner":
            members = get_org_members(org["id"])
            owners = [m for m in members if m.get("role") == "owner"]
            if len(owners) <= 1:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"您是组织「{org.get('name', '')}」的唯一管理员，请先转移管理员权限后再注销",
                })

    # 软删除（P3-3：级联失效身份行/清 token，并回收 token_version 使 JWT 立即失效）
    AccountDeactivationCascade.soft_delete(user["id"])
    invalidate_user_cache(user["id"])

    # D43: 清理关联数据——注销后这些缓存/映射不应残留
    # 1. user_repos 缓存
    _clear_user_repos_cache(user["id"])
    # 2. unified_user_mapping（飞书用户映射）——清列保行不 DELETE
    # 铁律 1：DELETE 整行后下次重启 backfill 按 user_id 重灌伪身份；
    # 清列保行后 INSERT OR IGNORE 命中已有行不重灌
    from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
    with _sm_dl:
        _c = _sm_db.cursor()
        _c.execute("UPDATE unified_user_mapping SET gitcode_user='', github_login='', feishu_open_id='', feishu_name='', app_id='' WHERE user_id=?",
                   (user["id"],))
        _c.execute("DELETE FROM org_member WHERE user_id=?", (user["id"],))
        _sm_db.commit()

    auth_audit.log_audit("account_deleted", user_id=user["id"], ip=ip)

    return {"message": "账号已注销，进入 30 天冷静期。冷静期内可联系管理员恢复账号，30 天后数据将永久删除"}

@router.post("/unlock/send-code")
def unlock_send_code(body: UnlockSendCodeBody, request: Request):
    """发送账号解锁验证码。"""
    ip = _get_client_ip(request)

    # P44: 无邮箱配置降级——返回 503，与 forgot-password/send-code 一致
    from src.domains.mgmt.auth.email_service import is_email_configured
    if not is_email_configured():
        raise HTTPException(status_code=503, detail={"code": 503, "error_code": "EMAIL_NOT_CONFIGURED", "message": "Email service not configured, please contact administrator"})

    # BUG-5 修复：防枚举——账号不存在时不返回 404，统一返回"验证码已发送"。
    # P1-4 修复：账号不存在时也调 check_rate_limit，防止 200 vs 429 枚举 + DoS。
    account = get_grape_account_by_identifier(body.identifier)
    if not account or not account["email"]:
        # 不存在账号：模拟限流检查，使 429 vs 200 不可区分
        rl_key = body.identifier.strip().lower() if body.identifier else ""
        rl_result = auth_verification.check_rate_limit(rl_key, "unlock_account", ip)
        if not rl_result["ok"]:
            raise HTTPException(status_code=429, detail={"code": 429, "message": rl_result["message"]})
        return {"message": "验证码已发送"}

    result = auth_verification.send_code(account["email"], "unlock_account", ip)
    if not result["ok"]:
        raise HTTPException(status_code=429, detail={"code": 429, "message": result["message"]})

    return {"message": "验证码已发送"}

@router.post("/unlock")
def unlock(body: UnlockBody, request: Request):
    """验证码解锁账号。"""
    ip = _get_client_ip(request)

    account = get_grape_account_by_identifier(body.identifier)
    if not account or not account["email"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "验证码无效或已过期"})

    result = auth_verification.verify_code(account["email"], "unlock_account", body.code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": result["message"]})

    # 清除失败计数和锁定状态
    login_throttle.clear_failures(body.identifier, ip)

    auth_audit.log_audit("account_unlocked", user_id=account["id"], ip=ip,
                         detail={"method": "verification_code"})

    return {"message": "账号解锁成功"}

@router.get("/bind/{provider}")
def bind_provider(provider: str, popup: bool = False, prompt: str = "",
                 user: dict = Depends(require_jwt_only), request: Request = None):
    """发起第三方绑定 OAuth。

    redirect_uri 使用分路径回调地址 /api/auth/callback/{provider}（需求 3）。
    state 携带 binding_user_id，回调时通过 state 分流走绑定逻辑。
    popup=True 时 state 携带 popup 标记，回调重定向到中间页走 postMessage。
    prompt=login 时强制第三方平台重新认证（用于已绑定场景切换账号，避免复用浏览器会话）。
    """
    from src.domains.mgmt.auth.providers import get_provider, build_auth_url, get_redirect_uri
    import hashlib as _hashlib
    import os as _os

    cfg = get_provider(provider)
    if not cfg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的平台: {provider}"})

    redirect_uri = get_redirect_uri(request, provider)

    state = _hashlib.sha256(_os.urandom(32)).hexdigest()

    extra_state = {"binding_user_id": user["id"]}
    if popup:
        extra_state["popup"] = True
    result = build_auth_url(provider, state, redirect_uri,
                            extra_state_data=extra_state,
                            prompt=prompt)
    if not result:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "构建授权 URL 失败"})

    return {"redirect_url": result["redirect_url"]}

@router.delete("/bind/{provider}")
def unbind_provider(provider: str, confirm: bool = False, user: dict = Depends(require_jwt_only), request: Request = None):
    """解绑第三方平台。"""
    ip = _get_client_ip(request) if request else ""

    # F6 加固：provider 白名单——um 清列/仓库 LIKE 前缀都以 provider 为常量、
    # 无通配符为安全前提。
    # D39/S59（领域建模）：合法平台集合从目录派生，不写字面量——
    # 解绑的 external_identities 来源 = 登录渠道目录 ∪ 仓库平台目录
    # （PAT 绑定 S52 双写 external_identities）。gitcode/github/gitlab
    # 在内置卡/内置渠道中恒在；config-only 平台须已配卡。
    try:
        from src.domains.mgmt.auth.providers import list_providers as _list_channels
        from src.domains.runtime.public import list_vcs_platforms
        _allowed = set(_list_channels() or []) | set(list_vcs_platforms() or [])
    except Exception as _e:
        from src.kernel.config import log as _alog
        _alog.warning(f"unbind: provider catalog load failed, fail-closed: {_e}")
        _allowed = set()
    import re as _re
    if (not _re.fullmatch(r"[a-z0-9_]+", provider or "")) or (provider not in _allowed):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的平台: {provider}"})

    # 需求17: 解绑前检查该平台下是否有正在使用的仓库（已建立 webhook 的托管仓库）
    # R19.3.1: 检查路径 B 仓库依赖（host_user_id 指向该用户的仓库）
    # managed_repos 无 user_id 列，通过 user_repos 关联查询
    if not confirm:
        from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
        affected_warehouses = []
        path_b_warehouses = []
        with _sm_dl:
            _c = _sm_db.cursor()
            _c.execute(
                "SELECT m.repo_key FROM managed_repos m "
                "INNER JOIN user_repos u ON m.repo_key = u.repo_key "
                "WHERE u.user_id=? AND substr(m.repo_key, 1, ?) = ? "
                "AND m.webhook_id != '' AND m.webhook_id IS NOT NULL",
                (user["id"], len(f"{provider}:"), f"{provider}:")
            )
            rows = _c.fetchall()
            affected_warehouses = [r[0] if isinstance(r, tuple) else r["repo_key"] for r in rows]
            # R19.3.1: 查路径 B 仓库（host_user_id == user.id）
            try:
                _c.execute(
                    "SELECT repo_key FROM managed_repos "
                    "WHERE host_user_id=? AND substr(repo_key, 1, ?) = ?",
                    (user["id"], len(f"{provider}:"), f"{provider}:")
                )
                path_b_rows = _c.fetchall()
                path_b_warehouses = [r[0] if isinstance(r, tuple) else r["repo_key"] for r in path_b_rows]
            except Exception as e:
                # C1: 用户决策数据路径——失败必须可见，不得静默漏报 409 确认列表
                log.warning(f"unbind path-B warehouse query failed (user={user['id']}, provider={provider}): {e}")
                path_b_warehouses = []
        if affected_warehouses or path_b_warehouses:
            _all_affected = list(set(affected_warehouses + path_b_warehouses))
            raise HTTPException(status_code=409, detail={
                "code": 409,
                "message": "该平台下有正在使用的仓库，确认解绑将影响以下仓库",
                "warehouses": _all_affected,
                "path_b_warehouses": path_b_warehouses,
                "need_confirm": True,
            })

    # 检查至少保留一种登录方式（判据唯一来源：auth_guards.LoginMethodPolicy）
    from src.domains.mgmt.auth.auth_guards import LoginMethodPolicy

    if LoginMethodPolicy.would_lock_out(user["id"]):
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "至少保留一种登录方式，请先设置 Grape 密码",
            "need_set_password": True,
        })

    # 删除绑定关系（P3-1：原子「至少保留一种登录方式」由单条条件 UPDATE 裁决；
    # 并发下另一端会拿到 keep-one 文案而不是「未绑定该平台」）
    if not UnbindGuard.remove(user["id"], provider):
        raise UnbindGuard.rejection(user["id"])

    # 需求 10：解绑后清理 placeholder 邮箱（走数据访问层，不绕过 grape_account_db）
    from src.domains.mgmt.auth.grape_account_db import clear_placeholder_email
    clear_placeholder_email(user["id"], provider)

    # FR-21.7: 清 um 平台列——解绑后 um.gitcode_user/github_login 残留会导致飞书反查命中旧身份
    # D39/S59：无 um 列的平台（gitee 等）跳过，不折叠清 gitcode_user（误清别人的列）
    from src.kernel.repo_key import um_column_for_provider
    _um_col = um_column_for_provider(provider)
    from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
    if _um_col:
        with _sm_dl:
            _c = _sm_db.cursor()
            _c.execute(f"UPDATE unified_user_mapping SET {_um_col}='' WHERE user_id=?", (user["id"],))
            _sm_db.commit()

    # R30: 先计算受影响仓库数量（在删除 user_repos 之前）
    affected_count = 0
    try:
        with _sm_dl:
            _c2 = _sm_db.cursor()
            _c2.execute(
                "SELECT COUNT(*) FROM managed_repos m "
                "INNER JOIN user_repos u ON m.repo_key = u.repo_key "
                "WHERE u.user_id=? AND substr(m.repo_key, 1, ?) = ? "
                "AND m.webhook_id != '' AND m.webhook_id IS NOT NULL",
                (user["id"], len(f"{provider}:"), f"{provider}:")
            )
            row = _c2.fetchone()
            affected_count = row[0] if row else 0
    except Exception as e:
        # C1: 用户决策数据——查询失败不得静默归零冒充"无影响"，-1 表示未知
        log.warning(f"unbind affected-count query failed (user={user['id']}, provider={provider}): {e}")
        affected_count = -1

    # OMIT-1: 清理旧表 user_credentials 中的 OAuth 凭据
    # D40/S60：走凭据撤销域服务——键族规则（{p}_oauth/{p}_pat）集中身份域
    # 一处，解绑/吊销单入口传播（此前只清 _oauth，S52 写入的 _pat 残留）
    from src.domains.mgmt.auth.grape_account_db import revoke_provider_credentials
    try:
        revoke_provider_credentials(user["id"], provider)
    except Exception as _e:
        log.warning(f"unbind: revoke credentials failed (user={user['id']}, "
                    f"provider={provider}): {_e}")  # loud fail：留痕供排查

    # D42: 清理 user_repos 中该平台的仓库缓存
    # F3: 清理失败不得留"绑定已删、缓存残留、审计缺失"的静默半途状态——
    # 失败必须可见（响应带 partial 标记），审计仍然记录
    user_repos_cleanup_failed = False
    try:
        with _sm_dl:
            _c = _sm_db.cursor()
            _pfx = f"{provider}:"
            _c.execute("DELETE FROM user_repos WHERE user_id=? AND substr(repo_key, 1, ?) = ?",
                       (user["id"], len(_pfx), _pfx))
            _sm_db.commit()
    except Exception as e:
        log.error(f"unbind user_repos cleanup failed (user={user['id']}, provider={provider}): {e}")
        user_repos_cleanup_failed = True

    auth_audit.log_audit("unbind", user_id=user["id"], ip=ip,
                         detail={"provider": provider})

    # D42: 返回更新后的 bindings，供前端刷新 localStorage.user
    remaining_identities = get_user_identities(user["id"])
    remaining_bindings = [ident["provider"] for ident in remaining_identities]

    try:
        from src.domains.platform.public import on_oauth_unbound
        on_oauth_unbound(str(user["id"]), str(provider))
    except Exception as _hook_err:
        log.warning(f"platform-link hook (oauth unbound) skipped: {_hook_err}")
    return {"message": "解绑成功", "bindings": remaining_bindings, "affected_repos": affected_count,
            "partial": user_repos_cleanup_failed}

@router.get("/identities")
def get_identities(user: dict = Depends(require_no_session_token)):
    """获取已绑定的第三方列表。"""
    identities = get_user_identities(user["id"])
    return {"identities": identities}

@router.get("/me", operation_id="grape_auth_me")
def get_me(user: dict = Depends(require_no_session_token)):
    """获取当前 Grape 账号信息（含绑定状态、是否有密码、token 是否过期）。"""
    account = get_grape_account_by_id(user["id"])
    if not account:
        # D32: 不 fallback 旧表
        raise HTTPException(status_code=404, detail={"code": 404, "message": "账号不存在"})

    identities = get_user_identities(user["id"])
    bindings = [ident["provider"] for ident in identities]
    # Issue #372 R10: 检查 token 过期状态（status=4），返回结构化 error_code 供前端映射
    expired_providers = []
    try:
        from src.domains.mgmt.auth.grape_account_db import is_identity_expired
        # S96：遍历用户实际绑定平台（bindings 来自 identities），替代字面二元组
        for _p in dict.fromkeys(bindings):
            if is_identity_expired(user["id"], _p):
                expired_providers.append({
                    "provider": _p,
                    "error_code": "TOKEN_EXPIRED",
                    "message": "user token expired or revoked",
                    "rebind_url": "/#account",
                })
    except Exception:
        pass
    return {
        "username": account["username"],
        "email": account["email"],
        "nickname": account["nickname"],
        "avatar_url": account["avatar_url"],
        "has_password": has_password(user["id"]),
        "identities": identities,
        "bindings": bindings,
        "token_expired_providers": expired_providers,
    }

class UpdateProfileBody(BaseModel):
    nickname: str = ""
    avatar_url: str = ""

@router.put("/profile", operation_id="grape_auth_update_profile")
def update_profile(body: UpdateProfileBody, user: dict = Depends(require_no_session_token)):
    """更新昵称/头像。"""
    update_profile_fn = grape_account_db.update_profile
    ok = update_profile_fn(user["id"], nickname=body.nickname, avatar_url=body.avatar_url)
    if not ok:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "更新失败"})
    return {"message": "更新成功"}

def _personal_org_name(username: str, lang: str = "zh") -> str:
    """Return localized personal org name based on user's language preference."""
    if lang and lang.startswith("en"):
        return f"{username}'s Workspace"
    return f"{username}的个人空间"

def _ensure_personal_org(user_id: str, username: str, lang: str = "zh"):
    """P1-3: 幂等补建个人组织——提取公共函数，消除 3 处重复代码。"""
    from src.domains.mgmt.user.org_db import get_personal_org_id, create_org, get_org_by_slug
    if not get_personal_org_id(user_id):
        slug = f"personal-{user_id}"
        if get_org_by_slug(slug):
            slug = f"personal-{user_id}-{username[:8]}"
        create_org(
            name=_personal_org_name(username, lang), slug=slug,
            owner_id=user_id, is_personal=1,
        )
        log.info(f"Created personal org for user {user_id} ({username}, lang={lang})")

def _check_whitelist(provider: str, external_login: str) -> bool:
    """P1-4: OAuth 白名单校验——提取公共函数，消除重复。
    2026-11-12 行式化（weekly-11）：直接按渠道 key 行查（is_user_allowed
    行式双读：新行优先→老列兜底），不再读渠道卡 whitelist_column（字段已退役）。
    返回 True 表示通过。
    """
    from src.kernel.config import is_user_allowed
    return is_user_allowed(provider, external_login)

def _build_redirect_url(request: Request, path: str = "/", params: dict | None = None,
                        hash_frag: str = "", popup: bool = False) -> str:
    """P1-5: 统一构造重定向 URL——消除 scheme/host/lang/app_base 重复拼装。

    所有 OAuth 回调重定向统一用此函数，保证 URL 构造一致。
    popup=True 时重定向到轻量中间页 /oauth-popup-callback.html，
    由中间页通过 postMessage 通知父窗口再关闭弹窗。
    """
    from src.domains.mgmt.auth.providers import _get_trusted_host
    from src.domains.mgmt.auth.email_service import is_production_env

    # 优先用 browser_callback_url 配置（nginx 反代场景 Host header 不可靠）
    base = ""
    try:
        from src.domains.mgmt.user.db import get_browser_callback_url
        base = get_browser_callback_url()
        if base and ("localhost" in base or "127.0.0.1" in base):
            base = ""
    except Exception:
        base = ""

    if base:
        # 配置模式：直接用配置的 base_url 构造
        scheme = "https" if is_production_env() else ("https" if request.headers.get("X-Forwarded-Proto") == "https" else "http")
        lang = request.cookies.get("grapeLang", "zh")
        prefix = f"/{lang}" if lang != "zh" else ""
        import urllib.parse as _up
        if popup:
            real_path = "/oauth-popup-callback.html"
        else:
            real_path = path
        qs = f"?{_up.urlencode(params)}" if params else ""
        frag = f"#{hash_frag}" if hash_frag else ""
        return f"{base.rstrip('/')}{_app_base()}{prefix}{real_path}{qs}{frag}"
    else:
        # fallback：从 Host header 构造（开发环境直连）
        if is_production_env():
            scheme = "https"
        else:
            scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
        host = _get_trusted_host(request)
        lang = request.cookies.get("grapeLang", "zh")
        prefix = f"/{lang}" if lang != "zh" else ""
        import urllib.parse as _up
        if popup:
            real_path = "/oauth-popup-callback.html"
        else:
            real_path = path
        qs = f"?{_up.urlencode(params)}" if params else ""
        frag = f"#{hash_frag}" if hash_frag else ""
        return f"{scheme}://{host}{_app_base()}{prefix}{real_path}{qs}{frag}"


def _oauth_error_redirect(request: Request, message: str, popup: bool = False):
    """OAuth 回调失败 → 重定向到前端登录页并带错误信息。

    P0-3（Issue #3054）：只做一次编码——_build_redirect_url 里的 urlencode 已经负责
    转义，这里再 quote 一遍会变成双重编码（前端被迫二次 decode，遇裸 % 就抛 URIError）。
    """
    from fastapi.responses import RedirectResponse
    url = _build_redirect_url(request, "/", {"oauth_error": message}, popup=popup)
    return RedirectResponse(url=url, status_code=302)

def _bind_error_redirect(request: Request, message: str, popup: bool = False):
    """绑定流程失败 → 重定向到用户中心并带错误提示。（P0-3：编码口径同 _oauth_error_redirect）"""
    from fastapi.responses import RedirectResponse
    url = _build_redirect_url(request, "/", {"bind_error": message}, hash_frag="account", popup=popup)
    return RedirectResponse(url=url, status_code=302)

# P0-1（Issue #3054）：错误跳转里允许出现的 provider key 形态——渠道 key 只可能是
# 小写标识符，第三方可控字符串（含 HTML/JS 元字符）一律不往 URL 里放。
_WL_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# D35 保留字列表
_USERNAME_RESERVED = {"admin", "root", "system", "default", "api", "auth", "grape"}

def _generate_username(base: str, provider: str = "") -> str:
    """从第三方平台用户名自动生成 Grape 用户名。

    规则：
    - 清理非 [a-zA-Z0-9_] 字符
    - P2: 中文/非拉丁用户名清洗后为空时，用 provider 前缀 + 原始用户名 hash 生成可读用户名
    - 截断到 32 字符
    - 不足 3 字符用 "user" 兜底
    - 保留字加 _user 后缀
    - 冲突加数字后缀（_2, _3, ...）
    """
    import re
    name = re.sub(r'[^a-zA-Z0-9_]', '', base or "")[:32]
    # P2: 中文用户名清洗后为空——用 provider 前缀 + hash 生成可读用户名
    if not name and base:
        import hashlib as _hl
        short_hash = _hl.md5(base.encode()).hexdigest()[:8]
        prefix = provider[:4] if provider else "u"
        name = f"{prefix}_{short_hash}"
    if len(name) < 3:
        name = (name + "user")[:32] if name else "user"
    if name.lower() in _USERNAME_RESERVED:
        name = (name + "_user")[:32]
    candidate = name
    suffix = 2
    while is_username_taken(candidate):
        s = str(suffix)
        candidate = f"{name[:32-len(s)-1]}_{s}"
        suffix += 1
    return candidate

def _oauth_login_redirect(request: Request, access_token: str, first_login: bool,
                          refresh_token: str = "", user_id: str = "",
                          username: str = "", provider: str = "",
                          token_version: int = 0, popup: bool = False):
    """P0-3: OAuth 登录成功 → 生成一次性 exchange code → 重定向到前端。

    不再通过 URL 传递 access_token，改为生成 60 秒过期的 exchange code（#535 UC14，
    原 10s 与 SPA 首载耗时冲突），
    前端用 POST /auth/exchange 换取 JWT。
    refresh_token 仍通过 HttpOnly cookie 传递（调用方设置）。
    popup=True 时重定向到中间页，由中间页 postMessage 通知父窗口。
    """
    import hashlib as _h, os as _os
    from fastapi.responses import RedirectResponse
    from src.domains.mgmt.user import db as user_db
    code = _h.sha256(_os.urandom(32)).hexdigest()
    user_db.save_exchange_code(code, user_id, username, provider,
                               token_version, first_login)
    url = _build_redirect_url(request, "/", {"exchange_code": code}, popup=popup)
    return RedirectResponse(url=url, status_code=302)

def _oauth_associate_redirect(request: Request, provider: str, external_id: str,
                               external_login: str, external_name: str,
                               external_avatar: str, oauth_token: str,
                               email: str = "", popup: bool = False):
    """OAuth 未绑定 → 重定向到前端关联页面。

    supp-1: 删除 associate_token（user_id=0 JWT）——关联 API 认 state+密码，不需要 JWT。
    OAuth 用户信息存入 oauth_states，关联时通过 state 取出。
    popup=True 时重定向到中间页，由中间页 postMessage 通知父窗口展示关联页。
    """
    import urllib.parse as _up
    from fastapi.responses import RedirectResponse
    import hashlib as _h, os as _os, time as _t
    state = _h.sha256(_os.urandom(32)).hexdigest()
    from src.domains.mgmt.user import db as user_db
    # P2 加固：access_token 加密存储，避免 DB 泄露时明文暴露
    from src.domains.mgmt.auth.credential_crypto import encrypt as _encrypt
    user_db.save_oauth_state(state, provider, {
        "external_id": external_id,
        "external_login": external_login,
        "external_name": external_name,
        "external_avatar": external_avatar,
        "oauth_access_token_enc": _encrypt(oauth_token) if oauth_token else "",
        "email": email,
        "expires": _t.time() + 600,  # 10 分钟有效
    })

    url = _build_redirect_url(request, "/register", {
        "oauth_associate": "1",
        "provider": provider,
        "state": state,
        "external_login": external_login,
        "external_name": external_name,
        "email": email,
    }, popup=popup)
    return RedirectResponse(url=url, status_code=302)

@router.get("/login/{provider}")
def oauth_login(provider: str, popup: bool = False, request: Request = None, lang: str = ""):
    """统一 OAuth 登录入口——跳转到第三方平台授权页。

    P0-2: 已登录态保护——如果请求携带有效 JWT，自动转为绑定流程，
    state 里带 binding_user_id，回调时走绑定逻辑而非登录逻辑。
    未登录或 JWT 无效时走正常登录流程。
    popup=True 时 state 携带 popup 标记，回调重定向到中间页走 postMessage。

    需求 1（login CSRF 防护）：login flow 生成 nonce 存入 state + Set-Cookie，
    回调时校验 cookie nonce 与 state nonce 一致。绑定/注销流程不需要此机制。
    """
    from src.domains.mgmt.auth.providers import get_provider, build_auth_url, get_redirect_uri, _get_client_id
    import hashlib as _h, os as _os, secrets as _secrets
    from fastapi.responses import JSONResponse

    # supp-2: IP 限流
    ip = _get_client_ip(request) if request else ""
    if not login_throttle.check_oauth_rate_limit(ip, login_throttle.OAUTH_LOGIN_LIMIT, login_throttle.OAUTH_LOGIN_WINDOW):
        raise HTTPException(status_code=429, detail={"code": 429, "message": "请求过于频繁，请稍后再试"})

    cfg = get_provider(provider)
    if not cfg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的平台: {provider}"})

    # P2-4：enabled 必须 fail-closed —— 登录目录按 enabled_only 过滤，
    # 但直达 /login/<provider> 不能绕过开关（原实现关掉渠道仍可用直达链接登录）。
    from src.domains.mgmt.auth.providers import is_channel_enabled as _ch_enabled

    if not _ch_enabled(provider):
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "CHANNEL_DISABLED",
            "message": f"{cfg.get('name') or provider} 登录渠道已停用，请联系管理员"})

    # D28：策略能力分派——路由不比 type 字符串（needs_oauth_client / post_callback 均为
    # 协议属性，见 LoginChannelStrategy）。构建即校验（S45：缺字段报字段名）。
    from src.kernel.auth_sdk.channel_strategy import build_channel_strategy, ChannelAuthError
    try:
        _eng = build_channel_strategy(provider)
    except ChannelAuthError as _ce:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"渠道配置无效: {_ce}"})

    # 检查 client_id 是否已配置（SAML 类协议无 OAuth 应用，策略声明跳过）
    if _eng.needs_oauth_client and not _get_client_id(provider):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"{cfg['name']} OAuth 未配置"})

    redirect_uri = get_redirect_uri(request, provider)

    # P0-2: 可选 JWT 认证——已登录则转绑定流程
    extra_state = {"login_flow": True}
    is_login_flow = True
    if request:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            from src.kernel.auth_sdk.jwt_utils import verify_access_token
            payload = verify_access_token(token)
            if payload:
                uid = str(payload.get("sub") or "").strip()   # D105 第二批：sub 字符串化（老 int 兼容）
                token_ver = int(payload.get("ver", 0))
                if uid:
                    from src.api.deps import get_user_status_cached
                    db_ver, status, _ = get_user_status_cached(uid)
                    if token_ver == db_ver and status == 1:
                        # 已登录且有效 → 转绑定流程
                        extra_state = {"binding_user_id": uid}
                        is_login_flow = False

    if popup:
        extra_state["popup"] = True

    # Store user's language preference in state for localized org name on auto-create
    if request:
        _oauth_lang = _get_request_lang(request, lang)
        extra_state["lang"] = _oauth_lang

    # 需求 1：login flow 生成 nonce 绑定浏览器 session
    nonce = ""
    if is_login_flow:
        nonce = _secrets.token_hex(32)
        extra_state["session_nonce"] = nonce

    state = _h.sha256(_os.urandom(32)).hexdigest()
    # D28：统一走策略 initiate_auth——oauth2 委托 build_auth_url（行为零改动），
    # SAML 由策略自行落 state（RelayState 带回，校验链与 OAuth 同源）。
    try:
        _r = _eng.initiate_auth(state, redirect_uri,
                                {**cfg, "provider_key": provider},
                                extra_state=extra_state) or {}
    except Exception as _e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"构建授权 URL 失败: {_e}"})
    result = {"redirect_url": str(_r.get("redirect_url") or "")}
    if not result["redirect_url"]:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "构建授权 URL 失败"})

    # 需求 1：login flow 通过 Set-Cookie 返回 nonce
    # P3-4：多流程并存——追加而非覆盖，双标签页各持自己的 nonce
    resp = JSONResponse({"redirect_url": result["redirect_url"]})
    if is_login_flow:
        OAuthStateStore.add(resp, request, nonce)
    return resp

def _handle_oauth_delete(request, provider, external_id, user_info, delete_account_user_id, popup=False):
    """注销 OAuth 认证流程（需求 14 修正）。"""
    from src.domains.mgmt.auth.grape_account_db import get_external_identity
    # Issue #3054 P2-1：status=4（token 过期）仍属有效身份，注销 OAuth 校验不得误判 mismatch
    existing = get_external_identity(provider, external_id, statuses=(1, 4))
    if not existing or str(existing["grape_user_id"] or "") != str(delete_account_user_id or ""):
        return _oauth_error_redirect(request, "delete_mismatch", popup=popup)

    # 原 state 已被消费，生成新 state 给前端 delete_account 用
    import hashlib as _hl, os as _os
    new_state = _hl.sha256(_os.urandom(32)).hexdigest()
    from src.domains.mgmt.user import db as user_db
    user_db.save_oauth_state(new_state, "", {"delete_account_user_id": delete_account_user_id})

    from fastapi.responses import RedirectResponse
    url = _build_redirect_url(request, "/", {"delete_oauth_state": new_state}, hash_frag="account", popup=popup)
    return RedirectResponse(url=url, status_code=302)

def _handle_oauth_bind(request, provider, external_id, user_info, access_token, refresh_token, binding_user_id, popup=False, expires_in=0):
    """绑定流程——检查重复绑定 → 写入 external_identities → 重定向回用户中心。

    需求 6：占位账号合并与新绑定创建在同一个 with db_lock 事务内执行。
    执行顺序：unbind 旧绑定 → create 新绑定 → mark 占位账号 status=4。
    """
    from src.domains.mgmt.auth.grape_account_db import (
        get_external_identity, get_external_identity_any_status, save_binding_ticket,
        is_placeholder_account_mergeable,
        unbind_placeholder_binding, mark_account_merged,
        restore_binding_in_tx,
        get_user_identities,
    )
    existing = get_external_identity(provider, external_id)
    if existing and str(existing["grape_user_id"] or "") != str(binding_user_id or ""):
        # 活跃绑定属于其他用户 → 检查对方是否是可合并的占位账号
        conflict_user_id = existing["grape_user_id"]
        if is_placeholder_account_mergeable(conflict_user_id):
            # 需求 6：检查当前绑定用户是否已有该 provider 的活跃记录
            current_identities = get_user_identities(binding_user_id)
            bound_providers = {ident["provider"] for ident in current_identities}
            if provider in bound_providers:
                return _bind_error_redirect(request, "bind_conflict", popup=popup)

            # 对方是纯占位账号 → 单事务执行 unbind → create → mark
            from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
            try:
                with _sm_dl:
                    c = _sm_db.cursor()
                    # Step 1: unbind 旧绑定
                    if not unbind_placeholder_binding(c, conflict_user_id, provider):
                        _sm_db.rollback()
                        return _bind_error_redirect(request, "bind_failed", popup=popup)
                    # Step 2: restore 新绑定（只写不读，MySQL 安全）
                    if not restore_binding_in_tx(
                        c, old_user_id=conflict_user_id,
                        new_user_id=binding_user_id, provider=provider,
                        external_id=external_id,
                        external_login=user_info.get("external_login", ""),
                        external_name=user_info.get("external_name", ""),
                        external_avatar=user_info.get("external_avatar", ""),
                        access_token=access_token,
                        refresh_token=refresh_token,
                        expires_in=expires_in,
                    ):
                        _sm_db.rollback()
                        return _bind_error_redirect(request, "bind_failed", popup=popup)
                    # Step 3: 标记占位账号 status=4 + 清邮箱
                    if not mark_account_merged(c, conflict_user_id):
                        _sm_db.rollback()
                        return _bind_error_redirect(request, "bind_failed", popup=popup)
                    _sm_db.commit()
                # 审计日志（commit 后）
                auth_audit.log_audit("placeholder_merge", user_id=binding_user_id,
                                     ip=_get_client_ip(request),
                                     detail={"provider": provider, "external_id": external_id,
                                             "merged_account": conflict_user_id})
                log.info(f"Auto-merged placeholder account {conflict_user_id} → {binding_user_id} "
                         f"(provider={provider}, external_id={external_id})")
                auth_audit.log_audit("bind", user_id=binding_user_id,
                                     ip=_get_client_ip(request),
                                     detail={"provider": provider, "external_id": external_id})
                _clear_user_repos_cache(binding_user_id)
                _hook_platform_link(binding_user_id, provider)  # D109：绑定（占位合并路径）也写关系
                from fastapi.responses import RedirectResponse
                url = _build_redirect_url(request, "/", {"bind_success": provider}, hash_frag="repos", popup=popup)
                return RedirectResponse(url=url, status_code=302)
            except Exception as e:
                try:
                    _sm_db.rollback()
                except Exception:
                    pass
                log.warning(f"_handle_oauth_bind merge transaction failed: {e}")
                return _bind_error_redirect(request, "bind_failed", popup=popup)
        else:
            # 不可自动合并 → 检查是否是占位账号（有业务数据）还是真实用户
            conflict_account = get_grape_account_by_id(conflict_user_id)
            if conflict_account and conflict_account.get("email_is_placeholder") and conflict_account.get("created_by_oauth"):
                # 占位账号但有业务数据 → 走转移确认流程（用户确认后转移绑定+业务数据）
                _ticket = save_binding_ticket(
                    initiating_user_id=binding_user_id,
                    provider=provider,
                    external_id=external_id,
                    external_login=user_info.get("external_login", ""),
                    external_name=user_info.get("external_name", ""),
                    external_avatar=user_info.get("external_avatar", ""),
                    original_user_id=conflict_user_id,
                )
                if _ticket:
                    from fastapi.responses import RedirectResponse
                    if popup:
                        url = _build_redirect_url(request, "/", {
                            "need_transfer": provider,
                        }, hash_frag="account", popup=popup)
                    else:
                        url = _build_redirect_url(request, "/", {
                            "need_transfer": provider,
                            "binding_ticket": _ticket,
                        }, hash_frag="account")
                    return RedirectResponse(url=url, status_code=302)
                return _bind_error_redirect(request, "bind_failed", popup=popup)
            else:
                # 真实用户活跃绑定 → 冲突错误
                return _bind_error_redirect(request, "bind_conflict", popup=popup)

    # 检查历史绑定（status=0 解绑 / status=4 token 过期）是否属于其他用户 → 需要转移确认
    existing_any = get_external_identity_any_status(provider, external_id)
    if existing_any and existing_any["status"] in (0, 4) and str(existing_any["grape_user_id"] or "") != str(binding_user_id or ""):
        # Issue #3054 P2-1：status=4 与 status=0 同等可发票据（单次消费、10 分钟
        # TTL、绑定 session 与目标 external_id），不再落 bind_failed 死路。
        # 生成转移票据，通过 URL 参数传递给前端展示确认页
        _ticket = save_binding_ticket(
            initiating_user_id=binding_user_id,
            provider=provider,
            external_id=external_id,
            external_login=user_info.get("external_login", ""),
            external_name=user_info.get("external_name", ""),
            external_avatar=user_info.get("external_avatar", ""),
            original_user_id=existing_any["grape_user_id"],
        )
        if _ticket:
            # 重定向到账号设置页，前端检测 URL 参数展示转移确认
            # popup 模式：只传 need_transfer + provider，不传 binding_ticket（票据留在服务端）
            from fastapi.responses import RedirectResponse
            if popup:
                url = _build_redirect_url(request, "/", {
                    "need_transfer": provider,
                }, hash_frag="account", popup=popup)
            else:
                url = _build_redirect_url(request, "/", {
                    "need_transfer": provider,
                    "binding_ticket": _ticket,
                }, hash_frag="account")
            return RedirectResponse(url=url, status_code=302)
        return _bind_error_redirect(request, "bind_failed", popup=popup)

    bind_ok = create_external_identity(
        grape_user_id=binding_user_id,
        provider=provider,
        external_id=external_id,
        external_login=user_info.get("external_login", ""),
        external_name=user_info.get("external_name", ""),
        external_avatar=user_info.get("external_avatar", ""),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    if not bind_ok:
        return _bind_error_redirect(request, "bind_failed", popup=popup)

    # FR-18.6: 补写 um 平台列
    _sync_um_platform_col(binding_user_id, provider, user_info.get("external_login", ""))

    auth_audit.log_audit("bind", user_id=binding_user_id,
                         ip=_get_client_ip(request),
                         detail={"provider": provider, "external_id": external_id})
    _clear_user_repos_cache(binding_user_id)
    _hook_platform_link(binding_user_id, provider)  # D109：绑定路径也写关系（此前只在登录路径写）

    from fastapi.responses import RedirectResponse
    url = _build_redirect_url(request, "/", {"bind_success": provider}, hash_frag="repos", popup=popup)
    return RedirectResponse(url=url, status_code=302)

def _handle_oauth_login(request, provider, external_id, user_info, access_token, refresh_token, popup=False, state_data=None, expires_in=0):
    """登录流程——已绑定直接登录 / 未绑定自动创建或跳关联页。"""
    from src.domains.mgmt.auth.grape_account_db import get_external_identity
    # Issue #3054 P2-1：status=4（token 过期）仍是「已绑定」，必须进登录分支——
    # 只看 status=1 会让过期用户走建号/绑定失败，产出 status=3 孤儿账号。
    binding = get_external_identity(provider, external_id, statuses=(1, 4))

    if binding:
        # 已绑定 → 直接登录（P0-2: 已绑定用户不再检查白名单，需求4"已绑定的用户授权后直接登录成功"）
        grape_user_id = binding["grape_user_id"]
        account = get_grape_account_by_id(grape_user_id)
        if not account:
            return _oauth_error_redirect(request, "account_not_found", popup=popup)
        if account.get("status", 1) != 1:
            # P3-3：注销冷静期 / 已禁用 / 已删除 三分（与历史绑定分支同口径）
            return _oauth_error_redirect(
                request, AccountDeactivationCascade.oauth_error_code(int(account.get("status") or 0)),
                popup=popup)

        from src.domains.mgmt.auth.grape_account_db import update_external_identity_tokens
        # P2-8 修复：检查 token 更新返回值，失败时记录日志（不阻断登录）
        _token_updated = update_external_identity_tokens(grape_user_id, provider,
                                        access_token=access_token,
                                        refresh_token=refresh_token,
                                        expires_in=expires_in,
                                        external_name=user_info.get("external_name", ""),
                                        external_avatar=user_info.get("external_avatar", ""))
        if not _token_updated:
            log.warning(f"OAuth login: token refresh failed for user={grape_user_id}, provider={provider}")

        # Issue #497: 登录名变更同步（external_id 不变、login 变）——检测到不一致时四表一致化
        _oauth_new_login = (user_info.get("external_login") or "").strip()
        _oauth_old_login = (binding.get("external_login") or "").strip()
        if _oauth_new_login and _oauth_new_login != _oauth_old_login:
            from src.domains.mgmt.auth.grape_account_db import sync_external_login_on_rename
            _renamed = sync_external_login_on_rename(grape_user_id, provider,
                                                     _oauth_new_login, _oauth_old_login)
            if _renamed:
                log.info(f"OAuth login: external_login synced {_oauth_old_login} -> {_oauth_new_login} for user={grape_user_id}")
                # username 可能已同步更新，刷新 account 供 JWT/重定向使用
                _fresh_account = get_grape_account_by_id(grape_user_id)
                if _fresh_account:
                    account = _fresh_account

        token_version = account.get("token_version", 0)
        jwt_access, jwt_refresh = _issue_jwt(account["id"], account["username"],
                                              token_version, login_provider=provider)
        from src.domains.mgmt.user import db as user_db
        has_repos = bool(user_db.get_managed_repos(account["id"]))
        _oauth_lang = (state_data or {}).get("lang", "") if isinstance(state_data, dict) else ""
        _ensure_personal_org(account["id"], account["username"], _get_request_lang(request, _oauth_lang))
        update_last_login_at(account["id"])

        resp = _oauth_login_redirect(request, jwt_access, not has_repos,
                                     refresh_token=jwt_refresh,
                                     user_id=account["id"],
                                     username=account["username"],
                                     provider=provider,
                                     token_version=token_version,
                                     popup=popup)
        _set_refresh_cookie(resp, jwt_refresh)
        auth_audit.log_audit("login_success", user_id=account["id"],
                             ip=_get_client_ip(request),
                             detail={"method": "oauth", "provider": provider})
        _hook_platform_link(account["id"], provider)
        return resp

    # 未绑定 → 查历史绑定（D22: 软删除记录）
    unbound = get_unbound_identity(provider, external_id)
    _via_tombstone = False
    if not unbound:
        # Issue #3054 P2-2：换绑（旧 id 只在 metadata 墓碑）/ status=4 解绑后，
        # 旧平台账号再登录必须回到原账号——查墓碑而不是新建第二个 Grape 号。
        from src.domains.mgmt.auth.grape_account_db import find_identity_by_tombstone
        _tomb = find_identity_by_tombstone(provider, external_id)
        if _tomb:
            unbound = {
                "grape_user_id": _tomb["grape_user_id"],
                "external_login": _tomb.get("external_login", ""),
                "external_name": _tomb.get("external_name", ""),
                "external_avatar": _tomb.get("external_avatar", ""),
                "unbound_at": _tomb.get("unbound_at", ""),
            }
            _via_tombstone = True
    if unbound:
        # 找到历史绑定 → 检查原账号状态
        original_user_id = unbound["grape_user_id"]
        original_account = get_grape_account_by_id(original_user_id)
        if not original_account:
            return _oauth_error_redirect(request, "original_account_not_found", popup=popup)
        if original_account.get("status", 1) != 1:
            if original_account.get("status") == 0:
                return _oauth_error_redirect(request, "original_account_disabled", popup=popup)
            if original_account.get("status") == 3:
                # P3-3：冷静期≠已删除——冷静期内可由管理员恢复
                return _oauth_error_redirect(request, "original_account_deactivating", popup=popup)
            return _oauth_error_redirect(request, "original_account_deleted", popup=popup)

        # 原账号正常 → 自动恢复绑定并登录
        if _via_tombstone:
            from src.domains.mgmt.auth.grape_account_db import restore_tombstoned_identity
            restored = restore_tombstoned_identity(
                grape_user_id=original_user_id,
                provider=provider,
                external_id=external_id,
                access_token=access_token,
                refresh_token=refresh_token,
                external_name=user_info.get("external_name", ""),
                external_avatar=user_info.get("external_avatar", ""),
                expires_in=expires_in,
            )
        else:
            restored = restore_external_identity(
                grape_user_id=original_user_id,
                provider=provider,
                access_token=access_token,
                refresh_token=refresh_token,
                external_name=user_info.get("external_name", ""),
                external_avatar=user_info.get("external_avatar", ""),
                expires_in=expires_in,
            )
        if not restored:
            return _oauth_error_redirect(request, "rebind_failed", popup=popup)

        # Issue #497: rebind 路径同样同步登录名变更（external_id 不变、login 变）
        _oauth_new_login = (user_info.get("external_login") or "").strip()
        _oauth_old_login = (unbound.get("external_login") or "").strip()
        if _oauth_new_login and _oauth_new_login != _oauth_old_login:
            from src.domains.mgmt.auth.grape_account_db import sync_external_login_on_rename
            _renamed = sync_external_login_on_rename(original_user_id, provider,
                                                     _oauth_new_login, _oauth_old_login)
            if _renamed:
                log.info(f"OAuth rebind: external_login synced {_oauth_old_login} -> {_oauth_new_login} for user={original_user_id}")
                _fresh_account = get_grape_account_by_id(original_user_id)
                if _fresh_account:
                    original_account = _fresh_account

        _clear_user_repos_cache(original_user_id)
        _rebind_lang = (state_data or {}).get("lang", "") if isinstance(state_data, dict) else ""
        _ensure_personal_org(original_account["id"], original_account["username"], _get_request_lang(request, _rebind_lang))
        update_last_login_at(original_account["id"])

        from src.domains.mgmt.user import db as user_db
        token_version = original_account.get("token_version", 0)
        jwt_access, jwt_refresh = _issue_jwt(
            original_account["id"], original_account["username"],
            token_version, login_provider=provider,
        )
        has_repos = bool(user_db.get_managed_repos(original_account["id"]))

        resp = _oauth_login_redirect(request, jwt_access, not has_repos,
                                     refresh_token=jwt_refresh,
                                     user_id=original_account["id"],
                                     username=original_account["username"],
                                     provider=provider,
                                     token_version=token_version,
                                     popup=popup)
        _set_refresh_cookie(resp, jwt_refresh)
        auth_audit.log_audit("rebind", user_id=original_account["id"],
                             ip=_get_client_ip(request),
                             detail={"provider": provider,
                                     **({"via": "tombstone"} if _via_tombstone else {})})
        auth_audit.log_audit("login_success", user_id=original_account["id"],
                             ip=_get_client_ip(request),
                             detail={"method": "oauth", "provider": provider, "via": "rebind"})
        _hook_platform_link(original_account["id"], provider)
        log.info(f"OAuth rebind: user {original_account['id']} ({original_account['username']}) restored via {provider}")
        return resp

    # 无历史绑定 → 查邮箱冲突（现有逻辑不变）
    external_login = user_info.get("external_login", "")
    if not _check_whitelist(provider, external_login):
        # P0-1（Issue #3054）：错误跳转只放错误码，不放第三方用户数据。
        # external_login 来自第三方（可任意构造），拼进跳转 URL 后会被前端读取渲染，
        # 构成反射型 DOM XSS 面；provider 只接受渠道 key 形态。
        _wl_code = "WHITELIST:" + provider if _WL_PROVIDER_KEY_RE.match(provider or "") else "WHITELIST"
        return _oauth_error_redirect(request, _wl_code, popup=popup)

    oauth_email = user_info.get("email", "")
    # Issue #3054：只有**已验证**的邮箱才允许进入「注册新账号 / 关联已有账号」二选一。
    # 未验证邮箱可被任意构造（provider 不返回 email_verified 时也是 False），当归属证据
    # 既会把用户送进二选一死路，也会被用来探测邮箱是否已注册。未验证 → 走正常建号/登录。
    if oauth_email and user_info.get("email_verified") and is_email_registered(oauth_email):
        return _oauth_associate_redirect(
            request, provider, external_id,
            external_login, user_info.get("external_name", ""),
            user_info.get("external_avatar", ""), access_token,
            email=oauth_email,
            popup=popup,
        )

    # D102-C: 可读用户名派生——OIDC 纯登录渠道（华为等）可能把 sub（长随机
    # base64url）当 external_login 下发，建号名不可读。有昵称时优先用昵称派生
    # （_generate_username 自带清洗/冲突后缀），sub 仅作兜底。
    _name_for_uname = user_info.get("external_name", "") or ""
    if _name_for_uname and (not external_login or len(external_login) > 24):
        username = _generate_username(_name_for_uname, provider)
    else:
        username = _generate_username(external_login, provider)
    oauth_email_verified = user_info.get("email_verified", False)
    # P3-6（Issue #3054 QA）：自动建号是注册行为，必须与 /register 同受注册策略约束。
    # 原实现只对「邮箱已注册 → 二选一页」两分支做了 check_signup_allowed，
    # 自动建号这条主路径直接 create_grape_account_oauth → 关注册/域白名单形同虚设。
    from src.domains.mgmt.auth import signup_policy as _signup_policy

    if not _signup_policy.check_oauth_signup_allowed(oauth_email, bool(oauth_email_verified)):
        log.warning(f"OAuth auto-create blocked by signup policy: provider={provider}")
        return _oauth_error_redirect(request, "signup_closed", popup=popup)
    account = create_grape_account_oauth(
        username=username,
        email=oauth_email or "",
        nickname=user_info.get("external_name", ""),
        avatar_url=user_info.get("external_avatar", ""),
        email_verified=oauth_email_verified,
        provider=provider,
    )
    if not isinstance(account, dict):
        # None=其他失败, "username_conflict"/"email_conflict"=UNIQUE 冲突
        if account == "email_conflict":
            return _oauth_error_redirect(request, "email_conflict", popup=popup)
        if account == "username_conflict":
            return _oauth_error_redirect(request, "username_conflict", popup=popup)
        log.warning(f"OAuth auto-create failed: username={username}, provider={provider}")
        return _oauth_error_redirect(request, "create_failed", popup=popup)

    # Issue #455: GitHub OAuth 落库读回校验——实测曾出现「日志称创建成功但库中查不到账号/绑定」（落库无痕）。
    # 创建后立即读回，读不到则按数据一致性优先返回失败（不再静默 302）；GitCode 语义保持不变。
    # S87/D63：落库读回校验全渠道化（原 github 特判——一致性校验与平台无关，
    # 纯加固零危害：仅真实落库失败时触发回滚+失败响应）
    _readback_account = get_grape_account_by_id(account["id"])
    if not _readback_account:
        log.error(f"OAuth auto-create READBACK FAILED: account {account['id']} ({username}) not persisted for provider={provider}; returning failure")
        AccountDeactivationCascade.rollback_signup(account["id"])
        return _oauth_error_redirect(request, "create_failed", popup=popup)

    bind_ok = create_external_identity(
        grape_user_id=account["id"],
        provider=provider,
        external_id=external_id,
        external_login=external_login,
        external_name=user_info.get("external_name", ""),
        external_avatar=user_info.get("external_avatar", ""),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    if not bind_ok:
        log.warning(f"OAuth auto-create: bind failed for user {account['id']}, provider={provider}, rolling back")
        AccountDeactivationCascade.rollback_signup(account["id"])
        return _oauth_error_redirect(request, "bind_failed", popup=popup)

    # S87/D63：绑定读回校验全渠道化（原 github 特判；一致性校验与平台无关）
    _readback_binding = get_external_identity(provider, external_id)
    if not _readback_binding:
        log.error(f"OAuth auto-create READBACK FAILED: binding for user {account['id']} ({username}) not persisted for provider={provider}; rolling back and returning failure")
        AccountDeactivationCascade.rollback_signup(account["id"])
        return _oauth_error_redirect(request, "bind_failed", popup=popup)

    # FR-18.6: 补写 um 平台列
    _sync_um_platform_col(account["id"], provider, external_login)
    _clear_user_repos_cache(account["id"])
    _oauth_lang = (state_data or {}).get("lang", "") if isinstance(state_data, dict) else ""
    _ensure_personal_org(account["id"], account["username"], _get_request_lang(request, _oauth_lang))

    token_version = account.get("token_version", 0)
    jwt_access, jwt_refresh = _issue_jwt(account["id"], account["username"],
                                          token_version, login_provider=provider)
    update_last_login_at(account["id"])

    resp = _oauth_login_redirect(request, jwt_access, True,
                                 refresh_token=jwt_refresh,
                                 user_id=account["id"],
                                 username=account["username"],
                                 provider=provider,
                                 token_version=token_version,
                                 popup=popup)
    _set_refresh_cookie(resp, jwt_refresh)
    auth_audit.log_audit("register", user_id=account["id"], ip=_get_client_ip(request),
                         detail={"source": "oauth", "provider": provider, "username": username})
    auth_audit.log_audit("login_success", user_id=account["id"], ip=_get_client_ip(request),
                         detail={"method": "oauth", "provider": provider, "via": "auto_create"})
    _hook_platform_link(account["id"], provider)
    log.info(f"OAuth auto-create: user {account['id']} ({account['username']}) created via {provider}")
    return resp

def _hook_platform_link(user_id, provider: str) -> None:
    """D109：登录成功 → 写 user_platform_link（fail-safe，绝不阻断登录）。"""
    try:
        from src.domains.platform.public import on_oauth_login

        on_oauth_login(str(user_id), str(provider))
    except Exception as e:  # 钩子失败只告警
        log.warning(f"platform-link hook (oauth login) skipped: {e}")


@router.get("/callback/{provider}")
def oauth_callback(request: Request, provider: str, code: str = "", state: str = "", error: str = ""):
    """OAuth 回调——分路径，所有平台 + 登录/绑定共用。

    需求 3：回调从统一 /api/auth/oauth/callback 改为分路径 /api/auth/callback/{provider}。
    provider 从 URL 路径参数获取，同时与 state 中的 provider 二次校验。

    P1-2: 拆分为公共前置 + 三个分支函数（delete/bind/login）。

    流程：
    1. 校验 state（原子消费）+ provider 一致 + nonce（D28 提取为 _validate_channel_callback）
    2. 用 code 换 access_token
    3. 获取第三方用户信息
    4. 分流：delete_account_user_id → 注销 / binding_user_id → 绑定 / login_flow → 登录

    popup 模式：state_data 中 popup=True 时，回调重定向到轻量中间页，
    由中间页通过 postMessage 通知父窗口再关闭弹窗。
    """
    state_data, popup, is_login_flow, err_resp = _validate_channel_callback(
        request, state, provider, error=error)
    if err_resp is not None:
        return err_resp
    if state_data is None:          # 契约上不可达（state 无效必有 err_resp）
        return _oauth_error_redirect(request, "state_invalid", popup=popup)

    # P3-7：state 签发后渠道被停用 → 本次登录不得再完成（与 /login 同源判据）
    if ChannelGate.is_disabled(provider):
        log.warning(f"oauth_callback: channel disabled, reject (provider={provider})")
        return _oauth_error_redirect(request, "channel_disabled", popup=popup)

    if error:
        resp = _oauth_error_redirect(request, "auth_failed", popup=popup)
        if is_login_flow:
            OAuthStateStore.consume(resp, request, state_data.get("session_nonce", ""))
        return resp

    binding_user_id = state_data.get("binding_user_id")
    delete_account_user_id = state_data.get("delete_account_user_id")

    from src.domains.mgmt.auth.providers import exchange_token, get_user_info, get_redirect_uri
    # 加固项 §10.6：从 state 恢复 redirect_uri（与发起时绝对一致）
    redirect_uri = state_data.get("redirect_uri", "")
    if not redirect_uri:
        # 旧 state 兼容（升级时已清空 oauth_states，正常不会走到）
        redirect_uri = get_redirect_uri(request, provider)
    pkce_verifier = state_data.get("pkce_verifier", "")

    # 换 token + 获取用户信息（公共前置）
    token_data, token_err = exchange_token(provider, code, redirect_uri, pkce_verifier)
    if not token_data:
        err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect
        resp = err_fn(request, token_err or "token_failed", popup=popup)
        if is_login_flow:
            OAuthStateStore.consume(resp, request, state_data.get("session_nonce", ""))
        return resp

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 0)
    if not access_token:
        err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect
        resp = err_fn(request, "token_failed", popup=popup)
        if is_login_flow:
            OAuthStateStore.consume(resp, request, state_data.get("session_nonce", ""))
        return resp

    user_info = get_user_info(provider, access_token)
    if not user_info:
        # D102-E（实弹热修语义重建）：oidc 渠道 userinfo 失败 → 引擎兜底
        # （id_token claims 解码，_tokens 复用已换 token——code 一次性不可重换）。
        # 非 oidc 渠道维持原 user_info_failed 语义（D28 渐进迁移：oidc 先行）。
        _channel_type = ""
        try:
            from src.domains.mgmt.auth.providers import get_provider as _gp
            from src.domains.mgmt.auth.providers import is_oidc_channel as _is_oidc
            _cfg = _gp(provider) or {}
            _channel_type = "oidc" if _is_oidc(_cfg) else ""
        except Exception:
            pass
        if _channel_type == "oidc":
            try:
                from src.kernel.auth_sdk.channel_strategy import OidcChannelStrategy
                _claims = OidcChannelStrategy().handle_callback(
                    {"_tokens": token_data if isinstance(token_data, dict) else {},
                     "code": code, "redirect_uri": redirect_uri},
                    {"provider_key": provider})
                user_info = {
                    "external_id": _claims.external_id,
                    "external_login": _claims.login,
                    "external_name": _claims.name,
                    "external_avatar": "",
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "email": _claims.email,
                    "email_verified": False,
                }
                log.info(f"oauth_callback: oidc engine fallback produced identity "
                         f"(provider={provider}, sub={_claims.external_id[:8]}...)")
            except Exception as _oe:
                log.warning(f"oauth_callback: oidc engine fallback failed: {_oe}")
        if not user_info:
            err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect
            resp = err_fn(request, "user_info_failed", popup=popup)
            if is_login_flow:
                OAuthStateStore.consume(resp, request, state_data.get("session_nonce", ""))
            return resp

    external_id = user_info["external_id"]
    if not external_id:
        err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect
        resp = err_fn(request, "no_external_id", popup=popup)
        if is_login_flow:
            OAuthStateStore.consume(resp, request, state_data.get("session_nonce", ""))
        return resp

    # 分流
    if delete_account_user_id:
        return _handle_oauth_delete(request, provider, external_id, user_info, delete_account_user_id, popup=popup)
    if binding_user_id:
        return _handle_oauth_bind(request, provider, external_id, user_info, access_token, refresh_token, binding_user_id, popup=popup, expires_in=expires_in)

    # login flow：_handle_oauth_login 返回的 RedirectResponse 上清除 nonce cookie
    login_resp = _handle_oauth_login(request, provider, external_id, user_info, access_token, refresh_token, popup=popup, state_data=state_data, expires_in=expires_in)
    if is_login_flow and hasattr(login_resp, "delete_cookie"):
        OAuthStateStore.consume(login_resp, request, state_data.get("session_nonce", ""))
    return login_resp

class ExchangeCodeBody(BaseModel):
    """P0-3: 一次性交换码换 JWT。"""
    code: str

@router.post("/exchange")
def exchange_token(body: ExchangeCodeBody, request: Request, response: Response):
    """P0-3: 用一次性 exchange_code 换取 JWT access_token。

    exchange_code 由 OAuth 回调生成，60 秒过期（#535 UC14），一次性消费（查+删）。
    防止 access_token 泄露到 URL/日志/Referer。
    """
    # supp-2: IP 限流
    ip = _get_client_ip(request)
    if not login_throttle.check_oauth_rate_limit(ip, login_throttle.OAUTH_EXCHANGE_LIMIT, login_throttle.OAUTH_EXCHANGE_WINDOW):
        raise HTTPException(status_code=429, detail={"code": 429, "message": "请求过于频繁，请稍后再试"})

    from src.domains.mgmt.user import db as user_db
    data = user_db.validate_exchange_code(body.code)
    if not data:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "exchange_code_invalid"})

    # 验证账号仍然有效
    account = get_grape_account_by_id(data["user_id"])
    if not account or account.get("status", 1) != 1:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "账号不存在或已被禁用"})

    # 校验 token_version（exchange code 存储时的版本与当前一致）
    current_ver = account.get("token_version", 0)
    if data["token_version"] != current_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话已失效"})

    # 签发新的 JWT
    jwt_access, jwt_refresh = _issue_jwt(
        account["id"], account["username"], current_ver,
        login_provider=data["provider"],
    )
    _set_refresh_cookie(response, jwt_refresh)

    auth_audit.log_audit("login_success", user_id=account["id"],
                         ip=_get_client_ip(request),
                         detail={"method": "oauth", "provider": data["provider"], "via": "exchange_code"})
    _hook_platform_link(account["id"], data["provider"])

    return {
        "token": jwt_access,
        "first_login": data["is_new"],
    }

class OAuthAssociateSendCodeBody(BaseModel):
    """关联页发送邮箱验证码。"""
    email: str

@router.post("/login/{provider}/associate/send-code")
def oauth_associate_send_code(provider: str, body: OAuthAssociateSendCodeBody, request: Request):
    """关联页发送邮箱验证码——用于 OAuth 关联时验证所有权。

    安全：统一返回"验证码已发送"，不暴露邮箱是否已注册。
    邮箱不存在时不实际发送，但返回相同响应。
    """
    email = body.email.lower().strip()
    ip = _get_client_ip(request)

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "邮箱格式不正确"})

    # 检查邮箱是否已注册（不暴露结果给调用方）
    # P2-5 修复：未注册邮箱也调 check_rate_limit，防止 200 vs 429 枚举
    account = get_grape_account_by_email(email) if email else None
    if account:
        result = auth_verification.send_code(email, "oauth_associate", ip)
        if not result["ok"]:
            raise HTTPException(status_code=429, detail={"code": 429, "message": result["message"]})
    else:
        rl_result = auth_verification.check_rate_limit(email, "oauth_associate", ip)
        if not rl_result["ok"]:
            raise HTTPException(status_code=429, detail={"code": 429, "message": rl_result["message"]})

    return {"message": "验证码已发送"}

class OAuthAssociateBody(BaseModel):
    """OAuth 关联已有 Grape 账号。支持密码或邮箱验证码验证。"""
    state: str
    identifier: str = ""       # 用户名或邮箱（密码验证时必填）
    password: str = ""         # 密码验证方式
    code: str = ""             # 邮箱验证码验证方式

@router.post("/login/{provider}/associate")
def oauth_associate(provider: str, body: OAuthAssociateBody, request: Request, response: Response):
    """OAuth 未绑定用户关联已有 Grape 账号。

    支持两种验证方式（任选其一）：
    1. 密码验证：identifier + password
    2. 邮箱验证码验证：identifier（邮箱）+ code

    验证通过 → 写入 external_identities → 登录。
    """

    # P3-2：与 /login 同口径限流——原实现直调 verify_grape_account，被锁账号可
    # 用「错几次 + 正确密码走 associate」绕过锁定拿 JWT。
    ip = _get_client_ip(request)
    identifier = (body.identifier or "").strip().lower()
    LoginThrottlePolicy.check_or_raise(identifier, ip)

    # P3-7：渠道开关复查
    ChannelGate.raise_if_disabled(provider)

    # P3-5：先 peep 不消费——密码输错一次不能让关联会话失效（原实现一进来就烧 state）
    state_data = OAuthStateStore.peek(body.state, provider)
    if state_data is None:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "关联会话已过期，请重新登录"})

    import time as _t
    if _t.time() > state_data.get("expires", 0):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "关联会话已过期，请重新登录"})

    # 验证方式选择：有 code 走邮箱验证码，有 password 走密码
    if body.code:
        # 邮箱验证码验证
        email = (body.identifier or "").lower().strip()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "请输入邮箱"})
        result = auth_verification.verify_code(email, "oauth_associate", body.code)
        if not result["ok"]:
            LoginThrottlePolicy.record_failure(identifier or email, ip)
            raise HTTPException(status_code=400, detail={"code": 400, "message": result["message"]})
        # 验证码通过 → 按邮箱查账号
        account = get_grape_account_by_email(email)
        if not account:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "账号不存在"})
    elif body.password:
        # 密码验证
        if not body.identifier:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "请输入用户名或邮箱"})
        account = verify_grape_account(body.identifier, body.password)
        if not account:
            # P3-2：失败计数与 /login 同源（同一 identifier 维度锁定）
            LoginThrottlePolicy.record_failure(identifier, ip)
            auth_audit.log_audit("login_failed", ip=ip,
                                 detail={"identifier": auth_audit.mask_identifier(identifier),
                                         "via": "oauth_associate"})
            raise HTTPException(status_code=401, detail={"code": 401, "error_code": "CREDENTIAL_INCORRECT", "message": "账号或密码不正确，请重新输入"})
    else:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请提供密码或验证码"})

    # 检查账号状态（P3-3：冷静期/禁用/已删除 三分）
    if account.get("status", 1) != 1:
        raise AccountDeactivationCascade.http_error(int(account.get("status") or 0))

    # 检查该第三方账号是否已被其他 Grape 账号绑定（仅活跃绑定 status=1）
    # 解绑记录（status=0）属于其他用户时 → 生成转移票据，要求前端确认
    external_id = state_data.get("external_id", "")
    existing = get_external_identity(provider, external_id)
    if existing and str(existing["grape_user_id"] or "") != str(account["id"] or ""):
        raise HTTPException(status_code=409, detail={"code": 409, "message": "该平台账号已被其他账号绑定"})

    # 检查是否存在解绑记录（status=0）属于其他用户
    any_status = get_external_identity_any_status(provider, external_id)
    if any_status and any_status["status"] in (0, 4) and str(any_status["grape_user_id"] or "") != str(account["id"] or ""):
        # 解绑记录（status=0）或 token 过期记录（status=4）属于其他用户 → 生成转移票据，要求前端确认
        _ticket = save_binding_ticket(
            initiating_user_id=account["id"],
            provider=provider,
            external_id=external_id,
            external_login=state_data.get("external_login", ""),
            external_name=state_data.get("external_name", ""),
            external_avatar=state_data.get("external_avatar", ""),
            original_user_id=any_status["grape_user_id"],
        )
        if not _ticket:
            raise HTTPException(status_code=500, detail={"code": 500, "message": "生成转移票据失败"})
        raise HTTPException(
            status_code=409,
            detail={
                "code": 409,
                "message": "该平台账号曾绑定过其他账号，是否转移到当前账号？",
                "binding_ticket": _ticket,
                "need_transfer": True,
            },
        )

    # 写入绑定关系（create_external_identity 内部会自动恢复同账号的解绑记录）
    # P3-5：所有校验通过后才真正消费 state（此前的失败可原地重试）
    if OAuthStateStore.consume_state(body.state, provider) is None:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "关联会话已过期，请重新登录"})

    # P2 加固：access_token 从 state 解密，解密失败则走失败路径
    _state_token, _token_ok = _get_state_access_token(state_data)
    if not _token_ok:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据解密失败，请重新发起绑定"})
    associate_ok = create_external_identity(
        grape_user_id=account["id"],
        provider=provider,
        external_id=external_id,
        external_login=state_data.get("external_login", ""),
        external_name=state_data.get("external_name", ""),
        external_avatar=state_data.get("external_avatar", ""),
        access_token=_state_token,
    )
    if not associate_ok:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "关联失败，请重试"})

    # FR-18.6: 补写 um 平台列
    _sync_um_platform_col(account["id"], provider, state_data.get("external_login", ""))

    # D43: 绑定成功后清理 user_repos 缓存
    _clear_user_repos_cache(account["id"])

    # 签发 JWT
    token_version = account.get("token_version", 0)
    access, refresh = _issue_jwt(account["id"], account["username"],
                                  token_version, login_provider=provider)
    _set_refresh_cookie(response, refresh)

    auth_audit.log_audit("bind", user_id=account["id"], ip=_get_client_ip(request),
                         detail={"provider": provider, "external_id": external_id, "via": "associate"})
    auth_audit.log_audit("login_success", user_id=account["id"], ip=_get_client_ip(request),
                         detail={"method": "oauth", "provider": provider, "via": "associate"})
    # P3-2：成功即清失败记录（与 /login 同口径）
    LoginThrottlePolicy.clear(identifier, ip)
    _hook_platform_link(account["id"], provider)

    return {
        "token": access,
        "user": {"id": account["id"], "username": account["username"], "email": account.get("email", "")},
    }

class OAuthRegisterBody(BaseModel):
    """OAuth 未绑定用户注册新 Grape 账号。"""
    state: str
    username: str
    email: str = ""
    password: str = ""
    lang: str = ""

@router.post("/login/{provider}/register")
def oauth_register(provider: str, body: OAuthRegisterBody, request: Request, response: Response):
    """OAuth 未绑定用户注册新 Grape 账号并绑定。

    有密码（邮箱注册）或无密码（纯 OAuth 注册）。
    """

    # P3-7：渠道开关复查（state 签发后禁用渠道不得再完成注册）
    ChannelGate.raise_if_disabled(provider)

    # P3-2：注册入口接入同口径 IP 限流
    ip = _get_client_ip(request)
    LoginThrottlePolicy.enforce_ip_rate_limit(ip)

    # P3-5：先 peep 不消费——校验失败（用户名占用等）可原地改参数重试
    state_data = OAuthStateStore.peek(body.state, provider)
    if state_data is None:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "OAUTH_STATE_EXPIRED", "message": "关联会话已过期，请重新登录"})

    import time as _t
    if _t.time() > state_data.get("expires", 0):
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "OAUTH_STATE_EXPIRED", "message": "关联会话已过期，请重新登录"})

    # P3-2：注册失败计数键 = 渠道 + IP（不拿用户名/邮箱做键，避免锁他人账号）

    # 用户名校验
    username_err = _validate_username(body.username)
    if username_err:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_INVALID", "message": username_err})
    if is_username_taken(body.username):
        # P3-2：注册入口失败计入同一限流维度（与 /login 同源表）
        LoginThrottlePolicy.record_registration_failure(provider, ip)
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "USERNAME_TAKEN", "message": "用户名已被占用"})

    external_id = state_data.get("external_id", "")
    external_login = state_data.get("external_login", "")

    # P0-3: 纯 OAuth 注册路径也需检查白名单（需求4"首次自动创建需通过OAuth白名单校验"）
    if not _check_whitelist(provider, external_login):
        raise HTTPException(status_code=403, detail={"code": 403, "error_code": "OAUTH_NOT_WHITELISTED", "message": "您不在平台白名单中，无法注册"})

    # 检查该第三方账号是否已被活跃绑定（status=1）
    # 解绑记录（status=0）不阻止新用户注册绑定——解绑意味着放弃绑定关系
    # Issue #3054 P2-1：status=4（token 过期）仍占 UNIQUE(provider, external_id)，
    # 注册前就认作「已绑定」直接 409，避免建号后绑定失败 → status=3 孤儿账号。
    existing = get_external_identity(provider, external_id, statuses=(1, 4))
    if existing:
        raise HTTPException(status_code=409, detail={"code": 409, "error_code": "OAUTH_ALREADY_BOUND", "message": "该平台账号已绑定其他账号"})

    oauth_email = body.email or state_data.get("email", "")
    has_pwd = bool(body.password)

    if has_pwd:
        # 邮箱密码注册
        if not oauth_email or "@" not in oauth_email:
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "EMAIL_INVALID", "message": "邮箱格式不正确"})
        pwd_err = auth_password.check_password_strength(body.password, body.username, oauth_email)
        if pwd_err:
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "PASSWORD_WEAK", "message": pwd_err})
        if not signup_policy.check_signup_allowed(oauth_email):
            raise HTTPException(status_code=403, detail={"code": 403, "error_code": "EMAIL_NOT_ALLOWED", "message": "该邮箱不在允许注册范围内"})
        if oauth_email and is_email_registered(oauth_email):
            raise HTTPException(status_code=409, detail={"code": 409, "error_code": "EMAIL_ALREADY_REGISTERED", "message": "该邮箱已注册，请直接登录或关联账号"})
        # P3-5：所有校验通过后才真正消费 state（可原地改参数重试）
        if OAuthStateStore.consume_state(body.state, provider) is None:
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "OAUTH_STATE_EXPIRED", "message": "关联会话已过期，请重新登录"})
        account = create_grape_account(body.username, oauth_email, body.password)
    else:
        # 纯 OAuth 注册（无密码）
        # P3-6：与密码注册共用注册策略——原实现纯 OAuth 分支完全跳过
        # signup_policy.check_signup_allowed 与 is_email_registered。
        # 有邮箱才有策略判据；无邮箱（平台不下发 email）保持既有占位账号行为，
        # 准入仍由上方 OAuth 白名单闸控制。
        if oauth_email:
            if not signup_policy.check_signup_allowed(oauth_email):
                raise HTTPException(status_code=403, detail={"code": 403, "error_code": "EMAIL_NOT_ALLOWED", "message": "该邮箱不在允许注册范围内"})
            if is_email_registered(oauth_email):
                raise HTTPException(status_code=409, detail={"code": 409, "error_code": "EMAIL_ALREADY_REGISTERED", "message": "该邮箱已注册，请直接登录或关联账号"})
        # P3-5：所有校验通过后才真正消费 state
        if OAuthStateStore.consume_state(body.state, provider) is None:
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "OAUTH_STATE_EXPIRED", "message": "关联会话已过期，请重新登录"})
        account = create_grape_account_oauth(
            username=body.username,
            email=oauth_email,
            nickname=state_data.get("external_name", ""),
            avatar_url=state_data.get("external_avatar", ""),
            provider=provider,
        )

    if not isinstance(account, dict):
        # None=其他失败, "username_conflict"/"email_conflict"=UNIQUE 冲突
        if account == "email_conflict":
            raise HTTPException(status_code=409, detail={"code": 409, "error_code": "EMAIL_ALREADY_REGISTERED", "message": "该邮箱已注册，请直接登录或关联账号"})
        if account == "username_conflict":
            LoginThrottlePolicy.record_registration_failure(provider, ip)
            raise HTTPException(status_code=409, detail={"code": 409, "error_code": "USERNAME_TAKEN", "message": "用户名已被占用"})
        raise HTTPException(status_code=500, detail={"code": 500, "error_code": "ACCOUNT_CREATE_FAILED", "message": "创建账号失败"})

    # P2-7 修复：_ensure_personal_org 移到 bind 成功之后，防止绑定失败时遗留孤儿组织
    # P2 加固：access_token 从 state 解密，解密失败则走失败路径
    _state_token, _token_ok = _get_state_access_token(state_data)
    if not _token_ok:
        AccountDeactivationCascade.rollback_signup(account["id"])
        raise HTTPException(status_code=500, detail={"code": 500, "error_code": "OAUTH_TOKEN_DECRYPT_FAILED", "message": "凭据解密失败，请重新发起"})
    # 写入绑定关系（失败时回滚 grape_accounts——软删除）
    bind_ok = create_external_identity(
        grape_user_id=account["id"],
        provider=provider,
        external_id=external_id,
        external_login=state_data.get("external_login", ""),
        external_name=state_data.get("external_name", ""),
        external_avatar=state_data.get("external_avatar", ""),
        access_token=_state_token,
    )
    if not bind_ok:
        log.warning(f"OAuth register: create_external_identity failed for user {account['id']}, rolling back")
        AccountDeactivationCascade.rollback_signup(account["id"])
        LoginThrottlePolicy.record_registration_failure(provider, ip)
        raise HTTPException(status_code=409, detail={"code": 409, "error_code": "OAUTH_ALREADY_BOUND", "message": "该平台账号已被绑定"})

    # FR-18.6: 补写 um 平台列
    _sync_um_platform_col(account["id"], provider, state_data.get("external_login", ""))

    # 创建个人组织（slug 冲突时加后缀，登录时幂等补建兜底）
    _ensure_personal_org(account["id"], body.username, _get_request_lang(request, body.lang))

    # D43: 绑定成功后清理 user_repos 缓存
    _clear_user_repos_cache(account["id"])

    # 签发 JWT
    token_version = get_token_version(account["id"])
    access, refresh = _issue_jwt(account["id"], account["username"],
                                  token_version, login_provider=provider)
    _set_refresh_cookie(response, refresh)

    auth_audit.log_audit("register", user_id=account["id"], ip=_get_client_ip(request),
                         detail={"source": "oauth", "provider": provider, "external_id": external_id})
    auth_audit.log_audit("bind", user_id=account["id"], ip=_get_client_ip(request),
                         detail={"provider": provider, "external_id": external_id, "via": "oauth_register"})

    return {
        "token": access,
        "user": {"id": account["id"], "username": account["username"], "email": account.get("email", "")},
    }

@router.get("/providers")
def list_oauth_providers():
    """列出已配置的 OAuth provider（供前端动态渲染登录按钮）。"""
    from src.domains.mgmt.auth.providers import providers_catalog
    # R7: 返回 email_configured 供前端判断无邮箱配置降级
    from src.domains.mgmt.auth.email_service import is_email_configured
    return {"providers": providers_catalog(), "email_configured": is_email_configured()}

@router.get("/transfer-context", operation_id="grape_auth_transfer_context")
def get_transfer_context(user: dict = Depends(require_jwt_only)):
    """获取当前用户的待转移绑定信息（popup 模式专用）。

    popup 模式下 binding_ticket 不通过 URL 传递，
    父窗口收到 postMessage {need_transfer: true} 后调用此接口，
    用 JWT 从服务端获取待转移信息展示确认页。
    票据留在服务端，与用户 JWT 绑定，不暴露到 URL/Referer/浏览器历史。
    """
    from src.domains.mgmt.auth.grape_account_db import get_pending_transfer_context
    ctx = get_pending_transfer_context(user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail={
            "code": 404,
            "message": "无待转移的绑定信息，请重新发起绑定",
        })
    provider = ctx["provider"]
    # D102-J: 显示名查表化（对齐前端 _PROV_NAMES——gitee 此前显示原文 'gitee'）
    p_name = {'github': 'GitHub', 'gitcode': 'GitCode', 'gitee': 'Gitee',
              'gitlab': 'GitLab'}.get(provider, provider)
    return {
        "provider": provider,
        "provider_name": p_name,
        "external_login": ctx["external_login"],
        "external_name": ctx["external_name"],
        "external_avatar": ctx["external_avatar"],
        "binding_ticket": ctx["binding_ticket"],
    }

class TransferBindBody(BaseModel):
    """绑定转移确认请求体。

    access_token/refresh_token 可选：票据按设计不携带平台令牌（凭据不出库），
    因此正常的"确认转移"请求里它们是空的——此时服务端把新绑定落成
    status=4（已绑定、待重新授权）并在响应里回 needs_reauth，前端据此直接把
    用户送去再授权一次；不再产出"status=1 但没有 token"的僵尸绑定。
    """
    binding_ticket: str
    confirm: bool = False
    access_token: str = ""
    refresh_token: str = ""
    expires_in: int = 0

@router.post("/transfer-bind")
def transfer_bind(body: TransferBindBody, user: dict = Depends(require_jwt_only), request: Request = None):
    """转移第三方绑定到当前登录账号。

    前置条件：前端已通过 token 绑定 / oauth_associate 路径收到 409 + binding_ticket，
    用户确认后携带票据调用此接口完成转移。

    流程：
    1. 校验 binding_ticket（校验 initiating_user_id == 当前用户；票据在业务事务成功后才消费）
    2. 用户未确认 → 400
    3. 检查原账号是否还有其他登录方式（密码或其他第三方绑定）
    4. 原账号无其他登录方式 → 400（防止原账号无法访问）
    5. transfer_external_identity 原子转移
    6. 审计日志 + 清理缓存
    """
    ip = _get_client_ip(request) if request else ""

    # 1. 校验票据（Issue #376：只校验不消费——消费移到业务事务成功后，
    #    失败重试不再连带作废票据导致必然 410）
    ticket_data = validate_binding_ticket(body.binding_ticket, user["id"], consume=False)
    if not ticket_data:
        raise HTTPException(status_code=410, detail={
            "code": 410,
            "message": "绑定票据无效或已过期，请重新发起绑定",
        })

    # 2. 用户确认检查
    if not body.confirm:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "请确认转移操作",
        })

    provider = ticket_data["provider"]
    external_id = ticket_data["external_id"]
    original_user_id = ticket_data.get("original_user_id", 0)

    if not original_user_id:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "票据数据异常，缺少原账号信息",
        })

    # 3. 检查原账号是否还有其他登录方式
    # 占位账号（有业务数据）转移后标记为 merged，不需要保留登录方式
    # 非占位账号需要保留至少 1 种登录方式
    original_account = get_grape_account_by_id(original_user_id)
    is_placeholder = (original_account
                      and original_account.get("email_is_placeholder")
                      and original_account.get("created_by_oauth"))
    if not is_placeholder:
        # 原账号需要至少还有 1 种其他登录方式（密码 / 其他第三方绑定，含 status=4 可恢复）。
        # 判据唯一来源：auth_guards.LoginMethodPolicy（与解绑前置闸同口径）。
        from src.domains.mgmt.auth.auth_guards import LoginMethodPolicy

        if LoginMethodPolicy.would_lock_out(original_user_id):
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": "原账号无其他登录方式，转移会导致账号无法访问，请联系客服",
            })

    # 4. 检查目标用户是否已绑定同 provider（防止 UNIQUE 冲突）
    current_identities = get_user_identities(user["id"])
    bound_providers = {ident["provider"] for ident in current_identities}
    if provider in bound_providers:
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "message": f"您已绑定 {provider}，请先解绑后再转移",
        })

    # 5. 执行转移：业务数据转移 + 绑定转移 + 占位账号标记（同一事务）
    from src.domains.mgmt.auth.grape_account_db import db as _ga_db, db_lock as _ga_dl
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _ga_dl:
            c = _ga_db.cursor()
            # 4a. 转移业务数据（user_repos / agent_tasks / chat 等）
            if not transfer_business_data(original_user_id, user["id"]):
                _ga_db.rollback()
                raise HTTPException(status_code=500, detail={
                    "code": 500,
                    "message": "业务数据转移失败，请稍后重试",
                })
            # 4b. 转移绑定关系（内联，不加锁）
            if not transfer_external_identity_in_tx(
                c, new_user_id=user["id"], provider=provider, external_id=external_id,
                external_login=ticket_data.get("external_login", ""),
                external_name=ticket_data.get("external_name", ""),
                external_avatar=ticket_data.get("external_avatar", ""),
                access_token=body.access_token,
                refresh_token=body.refresh_token,
                expires_in=body.expires_in,
                original_user_id=original_user_id,
            ):
                _ga_db.rollback()
                raise HTTPException(status_code=500, detail={
                    "code": 500,
                    "message": "绑定转移失败，请稍后重试",
                })
            # 4c. 占位账号标记为 merged（status=4）+ 清邮箱（需求 7）
            if is_placeholder:
                c.execute(
                    "UPDATE grape_accounts SET status=4, username=?, "
                    "email=NULL, email_source=NULL, email_is_placeholder=0, "
                    "updated_at=? WHERE id=?",
                    (f"merged_{original_user_id}", now, original_user_id),
                )
            _ga_db.commit()
        # 5. 消费票据（Issue #376）：业务事务 commit 成功后才删——
        #    失败路径（上方任一 raise/rollback）票据保留剩余有效期，重试不再 410。
        #    并发窗口内同一票据二次提交时，validate 阶段 user/origin 校验仍生效。
        consume_binding_ticket(body.binding_ticket)
    except HTTPException:
        raise
    except Exception as e:
        try:
            _ga_db.rollback()
        except Exception:
            pass
        log.warning(f"transfer_bind transaction failed: {e}")
        raise HTTPException(status_code=500, detail={
            "code": 500,
            "message": "转移失败，请稍后重试",
        })

    # 5. 审计日志
    auth_audit.log_audit("transfer_bind", user_id=user["id"], ip=ip,
                         detail={
                             "provider": provider,
                             "external_id": external_id,
                             "original_user_id": original_user_id,
                         })

    # 6. 清理当前用户和原账号的 user_repos 缓存
    _clear_user_repos_cache(user["id"])
    _clear_user_repos_cache(original_user_id)

    # 6b. D109：转移也是一种"绑定"，关系行要跟着补（此前只在普通绑定路径调）
    try:
        _hook_platform_link(user["id"], provider)
    except Exception as _hook_err:
        log.warning(f"transfer_bind: platform link hook skipped: {_hook_err}")

    log.info(f"Transfer bind: provider={provider}, external_id={external_id}, "
             f"from user={original_user_id} to user={user['id']}, "
             f"token={'yes' if body.access_token else 'no (needs_reauth)'}")

    # 7. 没有令牌的转移 → 明确告诉前端"还需要授权一次"（新绑定是 status=4，
    #    不是可用的 status=1）。此前这里返回"绑定转移成功"就结束了，用户看到的
    #    是一个已绑定但永远拉不到仓库的平台。
    if not str(body.access_token or "").strip():
        return {
            "message": "绑定转移成功，请再授权一次以启用仓库列表",
            "needs_reauth": [provider],
            "hint": f"请重新授权 {provider}：转移操作不携带平台令牌",
        }
    return {"message": "绑定转移成功"}

from fastapi import APIRouter, HTTPException, Request, Response, Depends, Form

from src.domains.mgmt.auth import password as auth_password

from src.domains.mgmt.auth import verification as auth_verification

from src.domains.mgmt.auth import audit as auth_audit

from src.domains.mgmt.auth import signup_policy

from src.domains.mgmt.auth import login_throttle

from src.domains.mgmt.auth import grape_account_db

def _validate_channel_callback(request: Request, state: str, provider: str,
                               error: str = ""):
    """D28/S48：回调公共前置——GET(OAuth) 与 POST(SAML ACS) 两入口共用。

    职责（消灭两入口各自手写一遍的重复校验链）：
    1. state 原子消费（查+删，validate_oauth_state）
    2. URL provider 与 state _provider 一致性
    3. login flow 的 nonce cookie 校验（防 login CSRF）

    返回 (state_data, popup, is_login_flow, err_resp)：
    err_resp 非 None 时调用方直接 return err_resp（state_data 为 None）。
    GET 入口的 error 参数语义保留：state 无效且带 error → auth_failed。
    """
    from src.domains.mgmt.user import db as user_db

    # P3-5（Issue #3054 QA）：先 peek（不消费）→ provider / nonce 全部通过后才
    # 真正 DELETE。原实现 validate_oauth_state(state) 默认 consume=True，错平台
    # 回调或 nonce 失配会把**本可成功的合法 state 烧掉**（线上 state_invalid 的
    # 一个来源），与 user/db.py 自己写的「先比 provider 再 DELETE」口径相反。
    # peek 不传 provider：provider 比对留在这里做，才能区分 provider_mismatch 与
    # state_invalid（两码前端文案不同），且两者都不消费 state。
    state_data = user_db.validate_oauth_state(state, "", consume=False)
    if state_data is None:
        # state 无效——无法判断 popup 模式，统一走中间页。
        if error:
            return None, True, False, _oauth_error_redirect(request, "auth_failed", popup=True)
        return None, True, False, _oauth_error_redirect(request, "state_invalid", popup=True)

    popup = bool(state_data.get("popup"))
    state_provider = state_data.get("_provider", "")
    if not state_provider:
        return None, popup, False, _oauth_error_redirect(request, "provider_unknown", popup=popup)
    if provider != state_provider:
        log.warning(f"channel callback: provider mismatch (url={provider}, state={state_provider})")
        return None, popup, False, _oauth_error_redirect(request, "provider_mismatch", popup=popup)

    is_login_flow = bool(state_data.get("login_flow"))
    if is_login_flow:
        # P3-4：nonce cookie 是多槽集合，命中即通过；不匹配时**不清 cookie**
        # （只有自己那槽失配，清整条会把另一标签页的登录打挂）。
        state_nonce = state_data.get("session_nonce", "")
        if not OAuthStateStore.nonce_ok(request, state_nonce):
            log.warning("channel callback: nonce mismatch (state=%s..., cookie_slots=%d)",
                        state[:12], len(OAuthStateStore.tokens(request)))
            return None, popup, is_login_flow, _oauth_error_redirect(
                request, "state_mismatch", popup=popup)

    # 校验链全通过 → 原子消费（rowcount 裁决；被并发抢走按 state_invalid 处理）。
    consumed = user_db.validate_oauth_state(state, provider, consume=True)
    if consumed is None:
        log.warning(f"channel callback: state consumed concurrently (state={state[:12]}...)")
        return None, popup, False, _oauth_error_redirect(request, "state_invalid", popup=popup)
    return consumed, popup, is_login_flow, None

@router.post("/callback/{provider}")
def saml_callback(request: Request, provider: str,
                  SAMLResponse: str = Form(""), RelayState: str = Form("")):
    """D28/S48：SAML ACS 回调（POST binding）——仅 type=saml 渠道走此入口。

    校验链与 OAuth GET 回调同源：state（RelayState）原子消费 →
    URL provider 与 state provider 一致 → login flow nonce 校验 →
    策略层 handle_callback（断言验签 + 属性归一）→ 复用
    delete/bind/login 三分支（S43：不新发明绑定逻辑）。

    断言验签未装配（真实 IdP 元数据，S38/S41 实弹件）时策略层安全默认
    拒绝——本入口对未验签断言不可用于登录。
    """
    from src.domains.mgmt.auth.providers import get_provider
    from src.kernel.auth_sdk.channel_strategy import build_channel_strategy, ChannelAuthError

    cfg = get_provider(provider) or {}
    # D28：能力分派（不比 type 字符串）——POST binding 是协议属性
    try:
        _eng = build_channel_strategy(provider)
    except ChannelAuthError as _ce:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": f"渠道配置无效: {_ce}"})
    if not _eng.post_callback:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": f"{provider} 渠道回调不是 POST binding（POST 回调仅限 SAML ACS）"})

    # D28：公共前置校验链（state 原子消费 / provider 一致 / nonce）与 GET 入口共用
    state_data, popup, is_login_flow, err_resp = _validate_channel_callback(
        request, RelayState, provider)
    if err_resp is not None:
        return err_resp
    if state_data is None:          # 契约上不可达（state 无效必有 err_resp）
        return _oauth_error_redirect(request, "state_invalid", popup=popup)

    # P3-7：渠道开关复查（POST 入口与 GET 同源判据）
    if ChannelGate.is_disabled(provider):
        log.warning(f"saml_callback: channel disabled, reject (provider={provider})")
        return _oauth_error_redirect(request, "channel_disabled", popup=popup)

    if not SAMLResponse:
        return _oauth_error_redirect(request, "saml_response_missing", popup=popup)

    try:
        claims = _eng.handle_callback({"SAMLResponse": SAMLResponse},
                                      {**cfg, "provider_key": provider})
    except ChannelAuthError as _e:
        log.warning(f"saml_callback: rejected ({_e})")
        resp = _oauth_error_redirect(request, "saml_verify_failed", popup=popup)
        if is_login_flow:
            OAuthStateStore.consume(resp, request, state_data.get("session_nonce", ""))
        return resp

    # 归一声明 → 既有 user_info 形态（SAML 无 token，空值语义与 S42 一致）
    user_info = {
        "external_id": claims.external_id,
        "external_login": claims.login,
        "external_name": claims.name,
        "external_avatar": "",
        "access_token": "",
        "refresh_token": "",
        "email": claims.email,
        "email_verified": False,
    }

    binding_user_id = state_data.get("binding_user_id")
    delete_account_user_id = state_data.get("delete_account_user_id")

    if delete_account_user_id:
        return _handle_oauth_delete(request, provider, claims.external_id,
                                    user_info, delete_account_user_id, popup=popup)
    if binding_user_id:
        return _handle_oauth_bind(request, provider, claims.external_id,
                                  user_info, "", "", binding_user_id,
                                  popup=popup, expires_in=0)

    login_resp = _handle_oauth_login(request, provider, claims.external_id,
                                      user_info, "", "", popup=popup,
                                      state_data=state_data, expires_in=0)
    if is_login_flow and hasattr(login_resp, "delete_cookie"):
        OAuthStateStore.consume(login_resp, request, state_data.get("session_nonce", ""))
    return login_resp
