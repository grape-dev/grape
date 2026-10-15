"""Grape 独立账号体系路由——注册/登录/忘记密码/修改密码/设置密码/邮箱变更/注销/绑定/解绑。

与现有 auth.py 共存：
- auth.py 保留现有 OAuth 登录（gitcode/github）+ 密码登录 + refresh + profile + logout
- grape_auth.py 新增 Grape 独立账号体系路由
- 前端按需调用新旧路由，迁移完成后逐步废弃旧路由
"""
import os
import hashlib
import time

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel
from src.config import log
from src.api.deps import require_no_session_token, require_jwt_only, invalidate_user_cache
from src.auth import password as auth_password
from src.auth import verification as auth_verification
from src.auth import audit as auth_audit
from src.auth import signup_policy
from src.auth import login_throttle
from src.auth import grape_account_db
from src.auth.grape_account_db import (
    create_grape_account, create_grape_account_oauth,
    get_grape_account_by_identifier, get_grape_account_by_email,
    get_grape_account_by_id, verify_grape_account,
    update_password, set_password as db_set_password, update_email,
    update_profile, get_token_version, increment_token_version,
    is_email_registered, is_username_taken, soft_delete_account,
    get_user_identities, create_external_identity,
    delete_external_identity, count_user_identities, has_password,
    get_external_identity, get_external_identity_any_status,
    update_external_identity_tokens,
    update_last_login_at,
    # D22: 软删除/恢复绑定支持
    get_unbound_identity, restore_external_identity,
    # 绑定转移支持
    save_binding_ticket, validate_binding_ticket, transfer_external_identity,
    transfer_external_identity_in_tx, transfer_business_data,
)
from src.auth.cookie_utils import (
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
        from src.auth.credential_crypto import decrypt
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


def _clear_user_repos_cache(user_id: int):
    """D43: 清理 user_repos 缓存——绑定/解绑新平台后旧缓存不完整，必须清除。
    统一入口，所有绑定/解绑路径调用此函数。
    """
    try:
        from src.session_manager import db as _sm_db, db_lock as _sm_dl
        with _sm_dl:
            _c = _sm_db.cursor()
            _c.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
            _sm_db.commit()
    except Exception as e:
        log.warning(f"_clear_user_repos_cache failed for user={user_id}: {e}")


def _issue_jwt(user_id: int, username: str, token_version: int,
               login_provider: str = "grape"):
    """签发 JWT 双 token。"""
    from src.api.jwt_utils import create_access_token, create_refresh_token
    access = create_access_token(user_id, username, token_version,
                                  login_provider=login_provider)
    refresh = create_refresh_token(user_id, username, token_version,
                                    login_provider=login_provider)
    return access, refresh


# ===========================================================================
# 请求体定义
# ===========================================================================

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


# ===========================================================================
# 用户名校验
# ===========================================================================

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


# ===========================================================================
# 1. 邮箱注册
# ===========================================================================

@router.post("/register/send-code")
def register_send_code(body: RegisterSendCodeBody, request: Request):
    """发送注册验证码。"""
    email = body.email.lower().strip()
    ip = _get_client_ip(request)

    # 用户名校验
    username_err = _validate_username(body.username)
    if username_err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": username_err})

    # 用户名唯一性
    if is_username_taken(body.username):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "用户名已被占用"})

    # 邮箱格式
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "邮箱格式不正确"})

    # 注册策略
    if not signup_policy.check_signup_allowed(email):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "该邮箱不在允许注册范围内"})

    # BUG-2 修复：防枚举——已注册邮箱不返回 409，统一返回"验证码已发送"。
    # 已注册邮箱不实际发送验证码，但响应与未注册邮箱一致。
    # P1-1 修复：已注册邮箱也调 check_rate_limit，防止 200 vs 429 枚举 + DoS。
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
        # P0-1: 对已注册邮箱发送"您的邮箱已注册"通知邮件（不发送验证码）
        try:
            from src.auth.email_service import get_email_service
            _svc = get_email_service()
            _svc.send_notification(
                to=email,
                subject="您的邮箱已注册",
                body="您的邮箱已在本系统注册，如非本人操作请忽略此邮件。",
            )
        except Exception as _e:
            log.warning(f"Failed to send already-registered notification to {email}: {_e}")

    return {"message": "验证码已发送"}


@router.post("/register/verify")
def register_verify(body: RegisterVerifyBody, request: Request, response: Response):
    """验证注册验证码并创建账号。"""
    email = body.email.lower().strip()
    ip = _get_client_ip(request)

    # 再次校验用户名和密码
    username_err = _validate_username(body.username)
    if username_err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": username_err})
    if is_username_taken(body.username):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "用户名已被占用"})
    pwd_err = auth_password.check_password_strength(body.password, body.username, email)
    if pwd_err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": pwd_err})

    # 校验验证码
    result = auth_verification.verify_code(email, "register", body.code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": result["message"]})

    # 创建账号
    account = create_grape_account(body.username, email, body.password)
    if not account:
        # D8 修复：并发注册竞态——数据库 UNIQUE 兜底，检查具体冲突原因
        if is_email_registered(email):
            raise HTTPException(status_code=409, detail={"code": 409, "message": "该邮箱已注册，请直接登录或重置密码"})
        if is_username_taken(body.username):
            raise HTTPException(status_code=409, detail={"code": 409, "message": "用户名已被占用"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": "创建账号失败"})

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


# ===========================================================================
# 2. 统一密码登录
# ===========================================================================

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
            raise HTTPException(status_code=429, detail={"code": 429, "message": throttle["message"], "captcha": True})
        raise HTTPException(status_code=429, detail={"code": 429, "message": throttle["message"]})

    # 验证账号密码（D32: 只查 grape_accounts，不 fallback 旧表）
    account = verify_grape_account(identifier, body.password)

    if not account:
        login_throttle.record_failure(identifier, ip)
        auth_audit.log_audit("login_failed", ip=ip,
                             detail={"identifier": auth_audit.mask_identifier(identifier)})
        raise HTTPException(status_code=401, detail={"code": 401, "message": "账号或密码错误"})

    # 密码正确但账号被禁用 → 才暴露禁用状态
    if account.get("status", 1) != 1:
        auth_audit.log_audit("login_failed", ip=ip,
                             detail={"identifier": auth_audit.mask_identifier(identifier),
                                     "reason": "account_disabled"})
        # BUG-9 修复：区分冷静期(status=3)和禁用(status=0)
        if account.get("status") == 3:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "账号已注销，处于冷静期"})
        raise HTTPException(status_code=403, detail={"code": 403, "message": "账号已被禁用"})

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


# ===========================================================================
# 3. 忘记密码
# ===========================================================================

@router.post("/forgot-password/send-code")
def forgot_password_send_code(body: ForgotPasswordSendCodeBody, request: Request):
    """发送密码重置验证码。

    安全：统一返回 200 "验证码已发送"，不管邮箱是否已注册。
    邮箱不存在时不实际发送邮件，但返回相同响应，防用户枚举。
    """
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
        raise HTTPException(status_code=400, detail={"code": 400, "message": result["message"]})

    # 查找账号（grape_accounts 优先，旧表无 email 字段无需 fallback）
    account = get_grape_account_by_email(email)
    if not account:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "账号不存在"})

    # 校验新密码强度
    pwd_err = auth_password.check_password_strength(body.new_password, account["username"], email)
    if pwd_err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": pwd_err})

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


# ===========================================================================
# 4. 修改密码
# ===========================================================================

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


# ===========================================================================
# 5. 设置密码（无密码用户）
# ===========================================================================

@router.post("/set-password/send-code")
def set_password_send_code(user: dict = Depends(require_jwt_only), request: Request = None):
    """发送设置密码验证码（无密码用户）。"""
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


# ===========================================================================
# 6. 邮箱变更
# ===========================================================================

@router.post("/change-email/send-code")
def change_email_send_code(body: ChangeEmailSendCodeBody, user: dict = Depends(require_jwt_only),
                           request: Request = None):
    """发送邮箱变更验证码。"""
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
        from src.auth.email_service import get_email_service
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


# ===========================================================================
# 7. 账号注销
# ===========================================================================

@router.post("/delete-account/send-code")
def delete_account_send_code(user: dict = Depends(require_jwt_only), request: Request = None):
    """发送账号注销验证码。"""
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
    from src.auth.providers import get_provider, build_auth_url, get_redirect_uri
    import hashlib as _hashlib
    import os as _os

    cfg = get_provider(provider)
    if not cfg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的平台: {provider}"})

    # 检查用户是否已绑定该平台
    from src.auth.grape_account_db import get_user_identities
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
    from src.admin import db as admin_db
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
        from src.user import db as user_db
        # 校验 OAuth state 中的 delete_account_user_id 是否匹配当前用户
        state_data = user_db.validate_oauth_state(body.oauth_state, "")
        if state_data is None:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "OAuth 授权会话已过期，请重新操作"})
        if state_data.get("delete_account_user_id") != user["id"]:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "OAuth 授权用户不匹配"})
        # OAuth 重新授权成功 → 认证通过
    else:
        # 无密码无邮箱 → 检查是否有 OAuth 绑定，引导用户走 OAuth 重新授权
        if not has_password and not has_email:
            from src.auth.grape_account_db import get_user_identities
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
    from src.user import db as user_db
    user_repos = user_db.get_user_repos(user["id"])
    if user_repos:
        repo_keys = [r.get("repo_key") or r.get("full_name", "") for r in user_repos]
        repo_keys = [k for k in repo_keys if k]
        if repo_keys:
            from src.session_manager import db as _sdb, db_lock as _sdl
            with _sdl:
                _c = _sdb.cursor()
                placeholders = ",".join("?" * len(repo_keys))
                _c.execute(
                    f"SELECT COUNT(*) FROM agent_tasks WHERE repo_key IN ({placeholders}) "
                    f"AND status IN ('queued','dispatched','running')",
                    repo_keys,
                )
                running_count = _c.fetchone()[0]
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
    from src.user.org_db import list_user_orgs, get_org_members
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

    # 软删除
    soft_delete_account(user["id"])
    increment_token_version(user["id"])
    invalidate_user_cache(user["id"])

    # D43: 清理关联数据——注销后这些缓存/映射不应残留
    # 1. user_repos 缓存
    _clear_user_repos_cache(user["id"])
    # 2. unified_user_mapping（飞书用户映射）
    from src.session_manager import db as _sm_db, db_lock as _sm_dl
    with _sm_dl:
        _c = _sm_db.cursor()
        _c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (user["id"],))
        _c.execute("DELETE FROM org_member WHERE user_id=?", (user["id"],))
        _sm_db.commit()

    auth_audit.log_audit("account_deleted", user_id=user["id"], ip=ip)

    return {"message": "账号已注销，进入 30 天冷静期。冷静期内可联系管理员恢复账号，30 天后数据将永久删除"}


# ===========================================================================
# 8. 账号解锁
# ===========================================================================

@router.post("/unlock/send-code")
def unlock_send_code(body: UnlockSendCodeBody, request: Request):
    """发送账号解锁验证码。"""
    ip = _get_client_ip(request)

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


# ===========================================================================
# 9. 第三方绑定/解绑
# ===========================================================================

@router.get("/bind/{provider}")
def bind_provider(provider: str, popup: bool = False, prompt: str = "",
                 user: dict = Depends(require_jwt_only), request: Request = None):
    """发起第三方绑定 OAuth。

    redirect_uri 使用分路径回调地址 /api/auth/callback/{provider}（需求 3）。
    state 携带 binding_user_id，回调时通过 state 分流走绑定逻辑。
    popup=True 时 state 携带 popup 标记，回调重定向到中间页走 postMessage。
    prompt=login 时强制第三方平台重新认证（用于已绑定场景切换账号，避免复用浏览器会话）。
    """
    from src.auth.providers import get_provider, build_auth_url, get_redirect_uri
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

    # 需求17: 解绑前检查该平台下是否有正在使用的仓库（已建立 webhook 的托管仓库）
    # managed_repos 无 user_id 列，通过 user_repos 关联查询
    if not confirm:
        from src.session_manager import db as _sm_db, db_lock as _sm_dl
        affected_warehouses = []
        with _sm_dl:
            _c = _sm_db.cursor()
            _c.execute(
                "SELECT m.repo_key FROM managed_repos m "
                "INNER JOIN user_repos u ON m.repo_key = u.repo_key "
                "WHERE u.user_id=? AND m.repo_key LIKE ? "
                "AND m.webhook_id != '' AND m.webhook_id IS NOT NULL",
                (user["id"], f"{provider}:%")
            )
            rows = _c.fetchall()
            affected_warehouses = [r[0] if isinstance(r, tuple) else r["repo_key"] for r in rows]
        if affected_warehouses:
            raise HTTPException(status_code=409, detail={
                "code": 409,
                "message": "该平台下有正在使用的仓库，确认解绑将影响以下仓库",
                "warehouses": affected_warehouses,
                "need_confirm": True,
            })

    # 检查至少保留一种登录方式
    identity_count = count_user_identities(user["id"])
    user_has_password = has_password(user["id"])

    if not user_has_password and identity_count <= 1:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "至少保留一种登录方式，请先设置 Grape 密码",
            "need_set_password": True,
        })

    # 删除绑定关系
    deleted = delete_external_identity(user["id"], provider)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "未绑定该平台"})

    # 需求 10：解绑后清理 placeholder 邮箱（走数据访问层，不绕过 grape_account_db）
    from src.auth.grape_account_db import clear_placeholder_email
    clear_placeholder_email(user["id"], provider)

    # R30: 先计算受影响仓库数量（在删除 user_repos 之前）
    from src.session_manager import db as _sm_db, db_lock as _sm_dl
    affected_count = 0
    try:
        with _sm_dl:
            _c2 = _sm_db.cursor()
            _c2.execute(
                "SELECT COUNT(*) FROM managed_repos m "
                "INNER JOIN user_repos u ON m.repo_key = u.repo_key "
                "WHERE u.user_id=? AND m.repo_key LIKE ? "
                "AND m.webhook_id != '' AND m.webhook_id IS NOT NULL",
                (user["id"], f"{provider}:%")
            )
            row = _c2.fetchone()
            affected_count = row[0] if row else 0
    except Exception:
        pass

    # OMIT-1: 清理旧表 user_credentials 中的 OAuth 凭据
    from src.user import db as user_db
    provider_key = provider + "_oauth"
    try:
        user_db.delete_credential(user["id"], provider_key)
    except Exception:
        pass  # 旧表可能没有此记录，静默

    # D42: 清理 user_repos 中该平台的仓库缓存
    with _sm_dl:
        _c = _sm_db.cursor()
        _c.execute("DELETE FROM user_repos WHERE user_id=? AND repo_key LIKE ?",
                   (user["id"], f"{provider}:%"))
        _sm_db.commit()

    auth_audit.log_audit("unbind", user_id=user["id"], ip=ip,
                         detail={"provider": provider})

    # D42: 返回更新后的 bindings，供前端刷新 localStorage.user
    remaining_identities = get_user_identities(user["id"])
    remaining_bindings = [ident["provider"] for ident in remaining_identities]

    return {"message": "解绑成功", "bindings": remaining_bindings, "affected_repos": affected_count}


@router.get("/identities")
def get_identities(user: dict = Depends(require_no_session_token)):
    """获取已绑定的第三方列表。"""
    identities = get_user_identities(user["id"])
    return {"identities": identities}


# ===========================================================================
# 10. 账号信息 + 资料更新
# ===========================================================================

@router.get("/me", operation_id="grape_auth_me")
def get_me(user: dict = Depends(require_no_session_token)):
    """获取当前 Grape 账号信息（含绑定状态、是否有密码）。"""
    account = get_grape_account_by_id(user["id"])
    if not account:
        # D32: 不 fallback 旧表
        raise HTTPException(status_code=404, detail={"code": 404, "message": "账号不存在"})

    identities = get_user_identities(user["id"])
    bindings = [ident["provider"] for ident in identities]
    return {
        "username": account["username"],
        "email": account["email"],
        "nickname": account["nickname"],
        "avatar_url": account["avatar_url"],
        "has_password": has_password(user["id"]),
        "identities": identities,
        "bindings": bindings,
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


# ===========================================================================
# 11. OAuth 统一登录入口 + 回调（替代 auth.py 的硬编码路由）
# ===========================================================================

def _personal_org_name(username: str, lang: str = "zh") -> str:
    """Return localized personal org name based on user's language preference."""
    if lang and lang.startswith("en"):
        return f"{username}'s Workspace"
    return f"{username}的个人空间"


def _ensure_personal_org(user_id: int, username: str, lang: str = "zh"):
    """P1-3: 幂等补建个人组织——提取公共函数，消除 3 处重复代码。"""
    from src.user.org_db import get_personal_org_id, create_org, get_org_by_slug
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
    返回 True 表示通过（或在无白名单列时直接通过）。
    """
    from src.auth.providers import get_provider as _get_prov_cfg
    cfg = _get_prov_cfg(provider)
    wl_col = cfg.get("whitelist_column") if cfg else None
    if wl_col:
        from src.config import is_user_allowed
        return is_user_allowed(wl_col, external_login)
    return True


def _build_redirect_url(request: Request, path: str = "/", params: dict | None = None,
                        hash_frag: str = "", popup: bool = False) -> str:
    """P1-5: 统一构造重定向 URL——消除 scheme/host/lang/app_base 重复拼装。

    所有 OAuth 回调重定向统一用此函数，保证 URL 构造一致。
    popup=True 时重定向到轻量中间页 /oauth-popup-callback.html，
    由中间页通过 postMessage 通知父窗口再关闭弹窗。
    """
    from src.auth.providers import _get_trusted_host
    from src.auth.email_service import is_production_env
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
    """OAuth 回调失败 → 重定向到前端登录页并带错误信息。"""
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up
    url = _build_redirect_url(request, "/", {"oauth_error": _up.quote(message)}, popup=popup)
    return RedirectResponse(url=url, status_code=302)


def _bind_error_redirect(request: Request, message: str, popup: bool = False):
    """绑定流程失败 → 重定向到用户中心并带错误提示。"""
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up
    url = _build_redirect_url(request, "/", {"bind_error": _up.quote(message)}, hash_frag="account", popup=popup)
    return RedirectResponse(url=url, status_code=302)


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
                          refresh_token: str = "", user_id: int = 0,
                          username: str = "", provider: str = "",
                          token_version: int = 0, popup: bool = False):
    """P0-3: OAuth 登录成功 → 生成一次性 exchange code → 重定向到前端。

    不再通过 URL 传递 access_token，改为生成 10 秒过期的 exchange code，
    前端用 POST /auth/exchange 换取 JWT。
    refresh_token 仍通过 HttpOnly cookie 传递（调用方设置）。
    popup=True 时重定向到中间页，由中间页 postMessage 通知父窗口。
    """
    import hashlib as _h, os as _os
    from fastapi.responses import RedirectResponse
    from src.user import db as user_db
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
    from src.user import db as user_db
    # P2 加固：access_token 加密存储，避免 DB 泄露时明文暴露
    from src.auth.credential_crypto import encrypt as _encrypt
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
    from src.auth.providers import get_provider, build_auth_url, get_redirect_uri, _get_client_id
    import hashlib as _h, os as _os, secrets as _secrets
    from fastapi.responses import JSONResponse

    # supp-2: IP 限流
    ip = _get_client_ip(request) if request else ""
    if not login_throttle.check_oauth_rate_limit(ip, login_throttle.OAUTH_LOGIN_LIMIT, login_throttle.OAUTH_LOGIN_WINDOW):
        raise HTTPException(status_code=429, detail={"code": 429, "message": "请求过于频繁，请稍后再试"})

    cfg = get_provider(provider)
    if not cfg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的平台: {provider}"})

    # 检查 client_id 是否已配置
    if not _get_client_id(provider):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"{cfg['name']} OAuth 未配置"})

    redirect_uri = get_redirect_uri(request, provider)

    # P0-2: 可选 JWT 认证——已登录则转绑定流程
    extra_state = {"login_flow": True}
    is_login_flow = True
    if request:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            from src.api.jwt_utils import verify_access_token
            payload = verify_access_token(token)
            if payload:
                uid = int(payload.get("sub", 0))
                token_ver = int(payload.get("ver", 0))
                if uid:
                    from src.api.deps import get_user_status_cached
                    db_ver, status = get_user_status_cached(uid)
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
    result = build_auth_url(provider, state, redirect_uri,
                            extra_state_data=extra_state)
    if not result:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "构建授权 URL 失败"})

    # 需求 1：login flow 通过 Set-Cookie 返回 nonce
    from src.auth.security_config import is_production
    resp = JSONResponse({"redirect_url": result["redirect_url"]})
    if is_login_flow:
        resp.set_cookie(
            key="oauth_nonce",
            value=nonce,
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=is_production(),
            path="/",
        )
    return resp


# P1-2: oauth_callback 拆分——三个分支提取为独立函数

def _handle_oauth_delete(request, provider, external_id, user_info, delete_account_user_id, popup=False):
    """注销 OAuth 认证流程（需求 14 修正）。"""
    from src.auth.grape_account_db import get_external_identity
    existing = get_external_identity(provider, external_id)
    if not existing or existing["grape_user_id"] != delete_account_user_id:
        return _oauth_error_redirect(request, "delete_mismatch", popup=popup)

    # 原 state 已被消费，生成新 state 给前端 delete_account 用
    import hashlib as _hl, os as _os
    new_state = _hl.sha256(_os.urandom(32)).hexdigest()
    from src.user import db as user_db
    user_db.save_oauth_state(new_state, "", {"delete_account_user_id": delete_account_user_id})

    from fastapi.responses import RedirectResponse
    url = _build_redirect_url(request, "/", {"delete_oauth_state": new_state}, hash_frag="account", popup=popup)
    return RedirectResponse(url=url, status_code=302)


def _handle_oauth_bind(request, provider, external_id, user_info, access_token, refresh_token, binding_user_id, popup=False):
    """绑定流程——检查重复绑定 → 写入 external_identities → 重定向回用户中心。

    需求 6：占位账号合并与新绑定创建在同一个 with db_lock 事务内执行。
    执行顺序：unbind 旧绑定 → create 新绑定 → mark 占位账号 status=4。
    """
    from src.auth.grape_account_db import (
        get_external_identity, get_external_identity_any_status, save_binding_ticket,
        is_placeholder_account_mergeable,
        unbind_placeholder_binding, mark_account_merged,
        restore_binding_in_tx,
        get_user_identities,
    )
    existing = get_external_identity(provider, external_id)
    if existing and existing["grape_user_id"] != binding_user_id:
        # 活跃绑定属于其他用户 → 检查对方是否是可合并的占位账号
        conflict_user_id = existing["grape_user_id"]
        if is_placeholder_account_mergeable(conflict_user_id):
            # 需求 6：检查当前绑定用户是否已有该 provider 的活跃记录
            current_identities = get_user_identities(binding_user_id)
            bound_providers = {ident["provider"] for ident in current_identities}
            if provider in bound_providers:
                return _bind_error_redirect(request, "bind_conflict", popup=popup)

            # 对方是纯占位账号 → 单事务执行 unbind → create → mark
            from src.session_manager import db as _sm_db, db_lock as _sm_dl
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

    # 检查历史绑定（status=0）是否属于其他用户 → 需要转移确认
    existing_any = get_external_identity_any_status(provider, external_id)
    if existing_any and existing_any["status"] == 0 and existing_any["grape_user_id"] != binding_user_id:
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
    )
    if not bind_ok:
        return _bind_error_redirect(request, "bind_failed", popup=popup)

    auth_audit.log_audit("bind", user_id=binding_user_id,
                         ip=_get_client_ip(request),
                         detail={"provider": provider, "external_id": external_id})
    _clear_user_repos_cache(binding_user_id)

    from fastapi.responses import RedirectResponse
    url = _build_redirect_url(request, "/", {"bind_success": provider}, hash_frag="repos", popup=popup)
    return RedirectResponse(url=url, status_code=302)


def _handle_oauth_login(request, provider, external_id, user_info, access_token, refresh_token, popup=False, state_data=None):
    """登录流程——已绑定直接登录 / 未绑定自动创建或跳关联页。"""
    from src.auth.grape_account_db import get_external_identity
    binding = get_external_identity(provider, external_id)

    if binding:
        # 已绑定 → 直接登录（P0-2: 已绑定用户不再检查白名单，需求4"已绑定的用户授权后直接登录成功"）
        grape_user_id = binding["grape_user_id"]
        account = get_grape_account_by_id(grape_user_id)
        if not account:
            return _oauth_error_redirect(request, "account_not_found", popup=popup)
        if account.get("status", 1) != 1:
            return _oauth_error_redirect(request, "account_disabled", popup=popup)

        from src.auth.grape_account_db import update_external_identity_tokens
        # P2-8 修复：检查 token 更新返回值，失败时记录日志（不阻断登录）
        _token_updated = update_external_identity_tokens(grape_user_id, provider,
                                        access_token=access_token,
                                        refresh_token=refresh_token,
                                        external_name=user_info.get("external_name", ""),
                                        external_avatar=user_info.get("external_avatar", ""))
        if not _token_updated:
            log.warning(f"OAuth login: token refresh failed for user={grape_user_id}, provider={provider}")

        token_version = account.get("token_version", 0)
        jwt_access, jwt_refresh = _issue_jwt(account["id"], account["username"],
                                              token_version, login_provider=provider)
        from src.user import db as user_db
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
        return resp

    # 未绑定 → 查历史绑定（D22: 软删除记录）
    unbound = get_unbound_identity(provider, external_id)
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
                return _oauth_error_redirect(request, "original_account_deleted", popup=popup)
            return _oauth_error_redirect(request, "original_account_unavailable", popup=popup)

        # 原账号正常 → 自动恢复绑定并登录
        restored = restore_external_identity(
            grape_user_id=original_user_id,
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
            external_name=user_info.get("external_name", ""),
            external_avatar=user_info.get("external_avatar", ""),
        )
        if not restored:
            return _oauth_error_redirect(request, "rebind_failed", popup=popup)

        _clear_user_repos_cache(original_user_id)
        _rebind_lang = (state_data or {}).get("lang", "") if isinstance(state_data, dict) else ""
        _ensure_personal_org(original_account["id"], original_account["username"], _get_request_lang(request, _rebind_lang))
        update_last_login_at(original_account["id"])

        from src.user import db as user_db
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
                             detail={"provider": provider})
        auth_audit.log_audit("login_success", user_id=original_account["id"],
                             ip=_get_client_ip(request),
                             detail={"method": "oauth", "provider": provider, "via": "rebind"})
        log.info(f"OAuth rebind: user {original_account['id']} ({original_account['username']}) restored via {provider}")
        return resp

    # 无历史绑定 → 查邮箱冲突（现有逻辑不变）
    external_login = user_info.get("external_login", "")
    if not _check_whitelist(provider, external_login):
        return _oauth_error_redirect(request, f"WHITELIST:{provider}:{external_login}", popup=popup)

    oauth_email = user_info.get("email", "")
    if oauth_email and is_email_registered(oauth_email):
        return _oauth_associate_redirect(
            request, provider, external_id,
            external_login, user_info.get("external_name", ""),
            user_info.get("external_avatar", ""), access_token,
            email=oauth_email,
            popup=popup,
        )

    username = _generate_username(external_login, provider)
    oauth_email_verified = user_info.get("email_verified", False)
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

    bind_ok = create_external_identity(
        grape_user_id=account["id"],
        provider=provider,
        external_id=external_id,
        external_login=external_login,
        external_name=user_info.get("external_name", ""),
        external_avatar=user_info.get("external_avatar", ""),
        access_token=access_token,
        refresh_token=refresh_token,
    )
    if not bind_ok:
        log.warning(f"OAuth auto-create: bind failed for user {account['id']}, provider={provider}, rolling back")
        soft_delete_account(account["id"])
        return _oauth_error_redirect(request, "bind_failed", popup=popup)
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
    log.info(f"OAuth auto-create: user {account['id']} ({account['username']}) created via {provider}")
    return resp


@router.get("/callback/{provider}")
def oauth_callback(request: Request, provider: str, code: str = "", state: str = "", error: str = ""):
    """OAuth 回调——分路径，所有平台 + 登录/绑定共用。

    需求 3：回调从统一 /api/auth/oauth/callback 改为分路径 /api/auth/callback/{provider}。
    provider 从 URL 路径参数获取，同时与 state 中的 provider 二次校验。

    P1-2: 拆分为公共前置 + 三个分支函数（delete/bind/login）。

    流程：
    1. 校验 state（原子消费）
    2. 校验 URL provider 与 state provider 一致
    3. 用 code 换 access_token
    4. 获取第三方用户信息
    5. 分流：delete_account_user_id → 注销 / binding_user_id → 绑定 / login_flow → 登录

    popup 模式：state_data 中 popup=True 时，回调重定向到轻量中间页，
    由中间页通过 postMessage 通知父窗口再关闭弹窗。
    """
    from src.user import db as user_db

    state_data = user_db.validate_oauth_state(state)
    if state_data is None:
        # state 无效——无法判断 popup 模式，统一走中间页。
        # 中间页根据 window.opener 判断：有 opener postMessage，无 opener 显示静态提示。
        # 不在服务端猜 popup=False 跳主站——popup + opener 被切断时会在弹窗里加载主站重现原问题。
        if error:
            return _oauth_error_redirect(request, "auth_failed", popup=True)
        return _oauth_error_redirect(request, "state_invalid", popup=True)

    # popup 模式标记——提前提取，确保所有后续分支都能正确传递
    popup = bool(state_data.get("popup"))

    # 需求 3：校验 URL 路径中的 provider 与 state 中的 provider 一致
    state_provider = state_data.get("_provider", "")
    if not state_provider:
        return _oauth_error_redirect(request, "provider_unknown", popup=popup)
    if provider != state_provider:
        log.warning(f"oauth_callback: provider mismatch (url={provider}, state={state_provider})")
        return _oauth_error_redirect(request, "provider_mismatch", popup=popup)

    # 需求 1：login flow 校验 nonce cookie（防 login CSRF）
    is_login_flow = bool(state_data.get("login_flow"))
    if is_login_flow:
        cookie_nonce = request.cookies.get("oauth_nonce", "")
        state_nonce = state_data.get("session_nonce", "")
        if not cookie_nonce or not state_nonce or cookie_nonce != state_nonce:
            resp = _oauth_error_redirect(request, "state_mismatch", popup=popup)
            resp.delete_cookie("oauth_nonce", path="/")
            return resp

    if error:
        resp = _oauth_error_redirect(request, "auth_failed", popup=popup)
        if is_login_flow:
            resp.delete_cookie("oauth_nonce", path="/")
        return resp

    binding_user_id = state_data.get("binding_user_id")
    delete_account_user_id = state_data.get("delete_account_user_id")

    from src.auth.providers import exchange_token, get_user_info, get_redirect_uri
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
            resp.delete_cookie("oauth_nonce", path="/")
        return resp

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect
        resp = err_fn(request, "token_failed", popup=popup)
        if is_login_flow:
            resp.delete_cookie("oauth_nonce", path="/")
        return resp

    user_info = get_user_info(provider, access_token)
    if not user_info:
        err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect
        resp = err_fn(request, "user_info_failed", popup=popup)
        if is_login_flow:
            resp.delete_cookie("oauth_nonce", path="/")
        return resp

    external_id = user_info["external_id"]
    if not external_id:
        err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect
        resp = err_fn(request, "no_external_id", popup=popup)
        if is_login_flow:
            resp.delete_cookie("oauth_nonce", path="/")
        return resp

    # 分流
    if delete_account_user_id:
        return _handle_oauth_delete(request, provider, external_id, user_info, delete_account_user_id, popup=popup)
    if binding_user_id:
        return _handle_oauth_bind(request, provider, external_id, user_info, access_token, refresh_token, binding_user_id, popup=popup)

    # login flow：_handle_oauth_login 返回的 RedirectResponse 上清除 nonce cookie
    login_resp = _handle_oauth_login(request, provider, external_id, user_info, access_token, refresh_token, popup=popup, state_data=state_data)
    if is_login_flow and hasattr(login_resp, "delete_cookie"):
        login_resp.delete_cookie("oauth_nonce", path="/")
    return login_resp


class ExchangeCodeBody(BaseModel):
    """P0-3: 一次性交换码换 JWT。"""
    code: str


@router.post("/exchange")
def exchange_token(body: ExchangeCodeBody, request: Request, response: Response):
    """P0-3: 用一次性 exchange_code 换取 JWT access_token。

    exchange_code 由 OAuth 回调生成，10 秒过期，一次性消费（查+删）。
    防止 access_token 泄露到 URL/日志/Referer。
    """
    # supp-2: IP 限流
    ip = _get_client_ip(request)
    if not login_throttle.check_oauth_rate_limit(ip, login_throttle.OAUTH_EXCHANGE_LIMIT, login_throttle.OAUTH_EXCHANGE_WINDOW):
        raise HTTPException(status_code=429, detail={"code": 429, "message": "请求过于频繁，请稍后再试"})

    from src.user import db as user_db
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
    from src.user import db as user_db
    state_data = user_db.validate_oauth_state(body.state, provider)
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
            raise HTTPException(status_code=401, detail={"code": 401, "message": "账号或密码错误"})
    else:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请提供密码或验证码"})

    # 检查账号状态
    if account.get("status", 1) != 1:
        if account.get("status") == 0:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "账号已被禁用"})
        if account.get("status") == 3:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "账号已注销"})
        raise HTTPException(status_code=403, detail={"code": 403, "message": "账号状态异常，请联系客服"})

    # 检查该第三方账号是否已被其他 Grape 账号绑定（仅活跃绑定 status=1）
    # 解绑记录（status=0）属于其他用户时 → 生成转移票据，要求前端确认
    external_id = state_data.get("external_id", "")
    existing = get_external_identity(provider, external_id)
    if existing and existing["grape_user_id"] != account["id"]:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "该平台账号已被其他账号绑定"})

    # 检查是否存在解绑记录（status=0）属于其他用户
    any_status = get_external_identity_any_status(provider, external_id)
    if any_status and any_status["status"] == 0 and any_status["grape_user_id"] != account["id"]:
        # 解绑记录属于其他用户 → 生成转移票据，要求前端确认
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
    from src.user import db as user_db
    state_data = user_db.validate_oauth_state(body.state, provider)
    if state_data is None:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "关联会话已过期，请重新登录"})

    import time as _t
    if _t.time() > state_data.get("expires", 0):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "关联会话已过期，请重新登录"})

    # 用户名校验
    username_err = _validate_username(body.username)
    if username_err:
        raise HTTPException(status_code=400, detail={"code": 400, "message": username_err})
    if is_username_taken(body.username):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "用户名已被占用"})

    external_id = state_data.get("external_id", "")
    external_login = state_data.get("external_login", "")

    # P0-3: 纯 OAuth 注册路径也需检查白名单（需求4"首次自动创建需通过OAuth白名单校验"）
    if not _check_whitelist(provider, external_login):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "您不在平台白名单中，无法注册"})

    # 检查该第三方账号是否已被活跃绑定（status=1）
    # 解绑记录（status=0）不阻止新用户注册绑定——解绑意味着放弃绑定关系
    existing = get_external_identity(provider, external_id)
    if existing:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "该平台账号已绑定其他账号"})

    oauth_email = body.email or state_data.get("email", "")
    has_pwd = bool(body.password)

    if has_pwd:
        # 邮箱密码注册
        if not oauth_email or "@" not in oauth_email:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "邮箱格式不正确"})
        pwd_err = auth_password.check_password_strength(body.password, body.username, oauth_email)
        if pwd_err:
            raise HTTPException(status_code=400, detail={"code": 400, "message": pwd_err})
        if not signup_policy.check_signup_allowed(oauth_email):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "该邮箱不在允许注册范围内"})
        if oauth_email and is_email_registered(oauth_email):
            raise HTTPException(status_code=409, detail={"code": 409, "message": "该邮箱已注册，请直接登录或关联账号"})
        account = create_grape_account(body.username, oauth_email, body.password)
    else:
        # 纯 OAuth 注册（无密码）
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
            raise HTTPException(status_code=409, detail={"code": 409, "message": "该邮箱已注册，请直接登录或关联账号"})
        if account == "username_conflict":
            raise HTTPException(status_code=409, detail={"code": 409, "message": "用户名已被占用"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": "创建账号失败"})

    # P2-7 修复：_ensure_personal_org 移到 bind 成功之后，防止绑定失败时遗留孤儿组织
    # P2 加固：access_token 从 state 解密，解密失败则走失败路径
    _state_token, _token_ok = _get_state_access_token(state_data)
    if not _token_ok:
        soft_delete_account(account["id"])
        raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据解密失败，请重新发起"})
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
        soft_delete_account(account["id"])
        raise HTTPException(status_code=409, detail={"code": 409, "message": "该平台账号已被绑定"})

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
    from src.auth.providers import list_providers, get_provider
    from src.user.db import get_meta_value

    result = []
    for key in list_providers():
        cfg = get_provider(key) or {}
        client_id = get_meta_value(f"oauth_{key}_client_id") or ""

        result.append({
            "provider": key,
            "name": cfg.get("name", key),
            "configured": bool(client_id),
            "brand_color": cfg.get("brand_color", ""),
        })
    return {"providers": result}


# ===========================================================================
# 10. 绑定转移（第三方账号从原账号转移到当前账号）
# ===========================================================================

@router.get("/transfer-context", operation_id="grape_auth_transfer_context")
def get_transfer_context(user: dict = Depends(require_jwt_only)):
    """获取当前用户的待转移绑定信息（popup 模式专用）。

    popup 模式下 binding_ticket 不通过 URL 传递，
    父窗口收到 postMessage {need_transfer: true} 后调用此接口，
    用 JWT 从服务端获取待转移信息展示确认页。
    票据留在服务端，与用户 JWT 绑定，不暴露到 URL/Referer/浏览器历史。
    """
    from src.auth.grape_account_db import get_pending_transfer_context
    ctx = get_pending_transfer_context(user["id"])
    if not ctx:
        raise HTTPException(status_code=404, detail={
            "code": 404,
            "message": "无待转移的绑定信息，请重新发起绑定",
        })
    provider = ctx["provider"]
    p_name = provider == 'github' and 'GitHub' or provider == 'gitcode' and 'GitCode' or provider
    return {
        "provider": provider,
        "provider_name": p_name,
        "external_login": ctx["external_login"],
        "external_name": ctx["external_name"],
        "external_avatar": ctx["external_avatar"],
        "binding_ticket": ctx["binding_ticket"],
    }


class TransferBindBody(BaseModel):
    """绑定转移确认请求体。"""
    binding_ticket: str
    confirm: bool = False
    access_token: str = ""


@router.post("/transfer-bind")
def transfer_bind(body: TransferBindBody, user: dict = Depends(require_jwt_only), request: Request = None):
    """转移第三方绑定到当前登录账号。

    前置条件：前端已通过 token 绑定 / oauth_associate 路径收到 409 + binding_ticket，
    用户确认后携带票据调用此接口完成转移。

    流程：
    1. 校验 binding_ticket（一次性消费，校验 initiating_user_id == 当前用户）
    2. 用户未确认 → 400
    3. 检查原账号是否还有其他登录方式（密码或其他第三方绑定）
    4. 原账号无其他登录方式 → 400（防止原账号无法访问）
    5. transfer_external_identity 原子转移
    6. 审计日志 + 清理缓存
    """
    ip = _get_client_ip(request) if request else ""

    # 1. 校验并消费票据
    ticket_data = validate_binding_ticket(body.binding_ticket, user["id"])
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
        original_has_password = has_password(original_user_id)
        original_identity_count = count_user_identities(original_user_id)
        # count_user_identities 统计 status=1 的活跃绑定，包含正在转移的这个
        # 原账号需要至少还有 1 种其他登录方式：密码 或 其他第三方绑定（identity_count > 1）
        if not original_has_password and original_identity_count <= 1:
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
    from src.auth.grape_account_db import db as _ga_db, db_lock as _ga_dl
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

    log.info(f"Transfer bind: provider={provider}, external_id={external_id}, "
             f"from user={original_user_id} to user={user['id']}")

    return {"message": "绑定转移成功"}



