
"""Settings 路由：凭据、通知偏好、GitCode Repo、Backend Runtimes"""
import json
from datetime import datetime, timedelta
from typing import Literal
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel
from src.user import db as user_db
from src.api.deps import require_no_session_token, require_org_member
from src.config import log
from src.api.time_utils import normalize_event_time
from src.repo_key import from_repo_key

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _check_login_method_before_unbind(user_id: int, provider: str):
    """P0-4: 手动令牌解绑前检查至少保留一种登录方式。

    与 grape_auth.py 的 unbind_provider 保持一致：
    - 有密码 → 允许解绑
    - 无密码但有其他活跃绑定（identity_count > 1）→ 允许解绑
    - 无密码且只有当前这一条绑定 → 拒绝解绑
    """
    from src.auth.grape_account_db import has_password, count_user_identities
    if not has_password(user_id) and count_user_identities(user_id) <= 1:
        raise HTTPException(
            status_code=400,
            detail={
                "code": 400,
                "message": "至少保留一种登录方式，请先设置 Grape 密码",
                "need_set_password": True,
            },
        )


def _resolve_repo_key(owner_repo: str) -> str:
    """查 DB 确定实际 repo_key 前缀（gitcode: 或 github:）。"""
    from src.repo_key import to_repo_key
    rk = to_repo_key(owner_repo)
    if ":" in owner_repo:
        return rk
    try:
        from src.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            row = c.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?) LIMIT 1",
                            (f"gitcode:{owner_repo}", f"github:{owner_repo}")).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return rk


# ====== Credentials ======

@router.get("/credentials", operation_id="settings_credentials_get", summary="Get third-party credential status", description="Returns the status of third-party credentials (gitcode/github/huawei-cloud/codearts) for the current user. Credential secrets (AK/SK/tokens) are never returned in plaintext; only existence flags are exposed.")
def get_credentials(user: dict = Depends(require_no_session_token), repo_key: str = ""):
    # D32: 从 user_credentials 表 + external_identities 读凭据，不查 user_accounts
    from src.api.deps import get_user_token, get_user_github_token_with_refresh
    from src.auth.grape_account_db import get_user_identities
    gitcode_token = get_user_token(user["username"])
    github_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"])
    identities = {ident["provider"]: ident for ident in get_user_identities(user["id"])}
    hw_cred = user_db.get_credential(user["id"], "huawei_cloud") or {}
    codearts_cred = user_db.get_credential(user["id"], "codearts") or {}

    # R16.2.3: 返回 token 状态（是否过期）
    _gitcode_identity = identities.get("gitcode", {})
    _github_identity = identities.get("github", {})
    _gitcode_status = _gitcode_identity.get("status", 0)
    _github_status = _github_identity.get("status", 0)
    # status: 1=active, 4=expired
    _gitcode_expired = (_gitcode_status == 4)
    _github_expired = (_github_status == 4)

    # R16.2.3: 全局/仓库级 bot token 状态
    _global_token_status = {}
    try:
        from src.user.db import get_meta_value
        _global_token_status = {
            "gitcode_expired": get_meta_value("global_gitcode_bot_token_status") == "expired",
            "github_expired": get_meta_value("global_github_bot_token_status") == "expired",
        }
    except Exception:
        _global_token_status = {"gitcode_expired": False, "github_expired": False}

    return {
        "gitcode": {
            "api_token_exists": bool(gitcode_token),
            "account": user["username"],
            "auto_filled": True,
            "token_expired": _gitcode_expired,
        },
        "github": {
            "token_exists": bool(github_token),
            "login": identities.get("github", {}).get("external_login", ""),
            "token_expired": _github_expired,
        },
        "global_bot_token_status": _global_token_status,
        "huawei_cloud": {
            "ak_exists": bool(hw_cred.get("ak")),
            "sk_exists": bool(hw_cred.get("sk")),
        },
        "codearts": {
            "ak_exists": bool(codearts_cred.get("ak")),
            "sk_exists": bool(codearts_cred.get("sk")),
        },
    }


# ====== Credential Reveal（Issue #474 方案A：按需取回明文）======
# GET /credentials 已收敛为「凭据只回存在性」；需要查看/复制明文时走本接口：
#   二次确认（重新输入 Grape 密码）+ 审计日志 + 明文仅当次返回（Cache-Control: no-store）
#   + 高频取回告警（窗口内计数超阈值 → 429）。
_REVEAL_WINDOW_SECONDS = 600      # 高频窗口：10 分钟
_REVEAL_MAX_PER_WINDOW = 10       # 窗口内最大取回次数（第 11 次起告警并拒绝）


def _count_reveals_in_window(user_id: int) -> int:
    """统计窗口内该用户的凭据取回次数（幂等；表不存在时返回 0）。

    created_at 由 DDL 默认 datetime('now')（UTC）写入；取回日志与审计都走 UTC，
    这里用 UTC 基准计算窗口起点，避免本地时区与库内 UTC 不一致导致误判。
    """
    try:
        from src.session_manager import db as _sdb, db_lock as _sdl
        since = (datetime.utcnow() - timedelta(seconds=_REVEAL_WINDOW_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        with _sdl:
            c = _sdb.cursor()
            c.execute(
                "SELECT COUNT(*) FROM grape_t_credential_reveal_log WHERE user_id=? AND created_at>=?",
                (user_id, since))
            return c.fetchone()[0] or 0
    except Exception:
        return 0


def _record_reveal(user_id: int, provider: str, field: str, ip: str):
    """记录一次凭据取回（供高频告警计数；不存明文）。"""
    try:
        from src.session_manager import db as _sdb, db_lock as _sdl
        with _sdl:
            c = _sdb.cursor()
            c.execute(
                "INSERT INTO grape_t_credential_reveal_log (user_id, provider, field, ip) VALUES (?,?,?,?)",
                (user_id, provider, field, ip))
            _sdb.commit()
    except Exception as e:
        log.warning(f"record reveal failed: {e}")


class CredentialRevealBody(BaseModel):
    provider: str = "huawei_cloud"   # huawei_cloud / codearts
    field: str = "ak"                # ak / sk
    password: str = ""


@router.post("/credentials/reveal", operation_id="settings_credentials_reveal", summary="Reveal a third-party credential plaintext once", description="Returns the plaintext value of one credential field once, after the user re-enters their Grape password. The response carries Cache-Control: no-store and a successful reveal is written to the audit log (provider/field only, never the secret). Repeated reveals within a short window are rate-limited (429).")
def reveal_credential(body: CredentialRevealBody, user: dict = Depends(require_no_session_token),
                      request: Request = None, response: Response = None):
    """按需取回凭据明文（一次性）。

    - 二次确认：必须重新输入当前 Grape 密码（未设密码 → 400 + need_set_password）
    - 审计：credential_reveal，detail 仅含 provider/field，不落明文值
    - 明文仅当次返回：Cache-Control: no-store
    - 高频告警：窗口内取回次数达到阈值 → 429（服务端告警日志 + 用户提示）
    """
    from src.auth.password import verify_password
    from src.auth.grape_account_db import get_grape_account_by_id
    from src.auth import audit as auth_audit

    provider = body.provider.strip().lower()
    field = body.field.strip().lower()
    if provider not in ("huawei_cloud", "codearts"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "不支持的凭据类型"})
    if field not in ("ak", "sk"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "不支持的凭据字段"})

    # 二次确认：重新输入当前密码
    account = get_grape_account_by_id(user["id"]) or {}
    password_hash = account.get("password_hash") or ""
    if not password_hash:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "请先设置 Grape 密码后再取回明文凭据",
            "need_set_password": True,
        })
    if not verify_password(body.password or "", password_hash):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "当前密码错误"})

    # 高频取回告警：窗口内已达阈值 → 拒绝并提示
    if _count_reveals_in_window(user["id"]) >= _REVEAL_MAX_PER_WINDOW:
        log.warning(f"credential reveal high-frequency: user={user['id']} provider={provider} field={field} "
                    f"ip={request.client.host if request and request.client else ''}")
        raise HTTPException(status_code=429, detail={
            "code": 429,
            "message": "明文取回过于频繁，请稍后再试",
        })

    # 取原值（仅当次返回）
    cred = user_db.get_credential(user["id"], provider) or {}
    value = cred.get(field) or ""
    if not value:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "该凭据未配置"})

    # 审计：谁/何时/取了哪个凭据（detail 不含明文）
    ip = request.client.host if request and request.client else ""
    auth_audit.log_audit("credential_reveal", user_id=user["id"], ip=ip,
                         detail={"provider": provider, "field": field})

    _record_reveal(user["id"], provider, field, ip)

    if response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return {"provider": provider, "field": field, "value": value}


class UpdateCredentials(BaseModel):
    huawei_cloud_ak: str = ""
    huawei_cloud_sk: str = ""
    codearts_ak: str = ""
    codearts_sk: str = ""
    github_token: str = ""
    gitcode_token: str = ""
    repo_key: str = ""


# 占位符凭据（2026-08-17 修复，issue #72）：前端曾把 demo 占位符回显进输入框，
# 用户直接保存会把占位符当真实值写库，覆盖真实 AK/SK → 部署任务注入占位符持续 401。
# 后端兜底：任何等于占位符 / demo 前缀 / 含 xxxx 的值一律拒绝写入。
_PLACEHOLDER_VALUES = {
    "AK-demo-access-key-xxxx", "SK-demo-secret-key-xxxx", "ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx",
}


def _is_placeholder(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False  # 空字符串不是占位符（清除语义由调用方处理）
    if v in _PLACEHOLDER_VALUES:
        return True
    if v.startswith(("AK-demo-", "SK-demo-", "ghp_devopsbot_")):
        return True
    return "xxxx" in v.lower()


@router.put("/credentials", operation_id="settings_credentials_update", summary="Update third-party credentials", description="Updates third-party credentials; placeholder/demo values are rejected. Sending an empty string explicitly clears the field; omitting a field leaves it unchanged.")
def update_credentials(body: UpdateCredentials, user: dict = Depends(require_no_session_token)):
    # D32: 写入 user_credentials 表，不写 user_accounts
    explicit = body.model_dump(exclude_unset=True)

    def _apply_ak_sk(provider: str, ak_field: str, sk_field: str):
        """统一 AK/SK 写入逻辑（user_credentials 表）"""
        if ak_field not in explicit and sk_field not in explicit:
            return
        cred = user_db.get_credential(user["id"], provider) or {}
        for field, key in [(ak_field, "ak"), (sk_field, "sk")]:
            if field not in explicit:
                continue
            v = (explicit[field] or "").strip()
            if v and not _is_placeholder(v):
                cred[key] = v
            elif not v:
                cred[key] = ""
        user_db.set_credential(user["id"], provider, cred)

    _apply_ak_sk("huawei_cloud", "huawei_cloud_ak", "huawei_cloud_sk")
    _apply_ak_sk("codearts", "codearts_ak", "codearts_sk")

    # GitHub Token（写入时需验证 token 并获取用户信息；清除时无需验证）
    if "github_token" in explicit:
        _gh_tok = (explicit["github_token"] or "").strip()
        if _gh_tok and not _is_placeholder(_gh_tok):
            # 验证 token 并获取 GitHub 用户信息
            try:
                import requests as _req
                _resp = _req.get("https://api.github.com/user",
                                 headers={"Authorization": f"Bearer {_gh_tok}", "Accept": "application/vnd.github+json"},
                                 timeout=10)
                if _resp.status_code == 200:
                    _gh_user = _resp.json()
                    gh_id = str(_gh_user.get("id", ""))
                    gh_login = _gh_user.get("login", "")
                    # 写入 external_identities（D32: 不再写 user_accounts）
                    from src.auth.grape_account_db import get_external_identity, get_external_identity_any_status, create_external_identity, update_external_identity_tokens, save_binding_ticket
                    existing = get_external_identity("github", gh_id)
                    if existing:
                        if existing["grape_user_id"] != user["id"]:
                            raise HTTPException(
                                status_code=409,
                                detail={"code": 409, "message": "该平台账号已被其他 Grape 账号绑定"},
                            )
                        _ok = update_external_identity_tokens(existing["grape_user_id"], "github",
                                                               access_token=_gh_tok,
                                                               external_name=_gh_user.get("name", ""))
                        if not _ok:
                            raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据保存失败"})
                    else:
                        # 检查是否存在解绑记录（status=0）属于其他用户
                        any_status = get_external_identity_any_status("github", gh_id)
                        if any_status and any_status["status"] == 0 and any_status["grape_user_id"] != user["id"]:
                            # 解绑记录属于其他用户 → 生成转移票据，要求前端确认
                            _ticket = save_binding_ticket(
                                initiating_user_id=user["id"],
                                provider="github",
                                external_id=gh_id,
                                external_login=gh_login,
                                external_name=_gh_user.get("name", ""),
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
                        # 无解绑记录或解绑记录属于当前用户 → 正常创建（create_internal 自动恢复同账号解绑记录）
                        _ok = create_external_identity(
                            grape_user_id=user["id"], provider="github",
                            external_id=gh_id, external_login=gh_login,
                            external_name=_gh_user.get("name", ""),
                            access_token=_gh_tok,
                        )
                        if not _ok:
                            raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据保存失败"})
                    log.info(f"GitHub token saved for user={user['username']}, github_login={gh_login}")
                    # Issue #372 §12.8: 同步写入 unified_user_mapping.github_login
                    # 用 INSERT OR IGNORE + UPDATE（不用 INSERT OR REPLACE，避免覆盖 gitcode_user 等其他平台字段）
                    try:
                        from src.session_manager import db as _um_db, db_lock as _um_dl
                        with _um_dl:
                            _um_c = _um_db.cursor()
                            _um_c.execute(
                                "INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)",
                                (user["id"],)
                            )
                            _um_c.execute(
                                "UPDATE unified_user_mapping SET github_login=?, updated_at=datetime('now') WHERE user_id=?",
                                (gh_login, user["id"])
                            )
                            _um_db.commit()
                        log.info(f"unified_user_mapping synced: user_id={user['id']}, github_login={gh_login}")
                    except Exception as _um_e:
                        log.warning(f"update_credentials: sync unified_user_mapping (github) failed: {_um_e}")
                elif _resp.status_code == 401:
                    # Issue #386: 被校验的第三方 token 无效是客户端输入问题（400），
                    # 不能与调用方 JWT 鉴权失败的 401 同构——否则前端全局 401 拦截器
                    # 会误判登录失效并踢出会话。
                    raise HTTPException(
                        status_code=400,
detail={"code": 400, "error_code": "TOKEN_INVALID", "message": "GitHub token 校验失败，请检查 token 有效性"},
                    )
                else:
                    log.warning(f"GitHub token validation failed: {_resp.status_code}")
                    raise HTTPException(
                        status_code=400,
detail={"code": 400, "error_code": "TOKEN_INVALID", "message": "GitHub token 校验失败，请检查 token 有效性"},
                    )
            except HTTPException:
                raise
            except Exception as _e:
                log.warning(f"GitHub token validation error: {_e}")
                raise HTTPException(
                    status_code=502,
                    detail={"code": 502, "message": f"GitHub token 验证失败: {_e}"},
                )
        elif not _gh_tok:
            # 清除：从 external_identities 删除 GitHub 绑定
            # P0-4: 解绑前检查至少保留一种登录方式
            _check_login_method_before_unbind(user["id"], "github")
            from src.auth.grape_account_db import delete_external_identity
            delete_external_identity(user["id"], "github")

    # GitCode Token（写入时需验证 token 并获取用户信息；清除时无需验证）
    if "gitcode_token" in explicit:
        _gc_tok = (explicit["gitcode_token"] or "").strip()
        if _gc_tok and not _is_placeholder(_gc_tok):
            try:
                import requests as _req
                # GitCode v5 认证用 access_token 查询参数（Authorization header 会 401 "token not found"）
                _resp = _req.get("https://gitcode.com/api/v5/user",
                                 params={"access_token": _gc_tok},
                                 timeout=10)
                if _resp.status_code == 200:
                    _gc_user = _resp.json()
                    gc_id = str(_gc_user.get("id", ""))
                    gc_login = _gc_user.get("login", "")
                    from src.auth.grape_account_db import (
                        get_external_identity, get_external_identity_any_status,
                        create_external_identity, save_binding_ticket,
                        update_external_identity_tokens, delete_external_identity,
                    )
                    existing = get_external_identity("gitcode", gc_id)
                    if existing:
                        if existing["grape_user_id"] != user["id"]:
                            raise HTTPException(
                                status_code=409,
                                detail={"code": 409, "message": "该平台账号已被其他 Grape 账号绑定"},
                            )
                        _ok = update_external_identity_tokens(
                            existing["grape_user_id"], "gitcode",
                            access_token=_gc_tok,
                            external_name=_gc_user.get("name", ""),
                        )
                        if not _ok:
                            raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据保存失败"})
                    else:
                        # 检查是否存在解绑记录（status=0）属于其他用户
                        any_status = get_external_identity_any_status("gitcode", gc_id)
                        if any_status and any_status["status"] == 0 and any_status["grape_user_id"] != user["id"]:
                            # 解绑记录属于其他用户 → 生成转移票据，要求前端确认
                            _ticket = save_binding_ticket(
                                initiating_user_id=user["id"],
                                provider="gitcode",
                                external_id=gc_id,
                                external_login=gc_login,
                                external_name=_gc_user.get("name", ""),
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
                        # 无解绑记录或解绑记录属于当前用户 → 正常创建
                        _ok = create_external_identity(
                            grape_user_id=user["id"], provider="gitcode",
                            external_id=gc_id, external_login=gc_login,
                            external_name=_gc_user.get("name", ""),
                            access_token=_gc_tok,
                        )
                        if not _ok:
                            log.error(f"GitCode bind FAILED: create_external_identity returned False for user={user['id']}, gc_id={gc_id}, gc_login={gc_login}")
                            raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据保存失败"})
                    log.info(f"GitCode token saved for user={user['username']}, gitcode_login={gc_login}")
                    # Issue #372 §12.8: 同步写入 unified_user_mapping
                    # PAT 绑定的用户 username ≠ GitCode login，启动时回填写入的是 username 而非 gc_login
                    # pipeline 用 actor（gc_login）查 unified_user_mapping → 必须写 gc_login 作为 gitcode_user
                    # 用 INSERT OR IGNORE + UPDATE（不用 INSERT OR REPLACE，避免覆盖 github_login 等其他平台字段）
                    try:
                        from src.session_manager import db as _um_db, db_lock as _um_dl
                        with _um_dl:
                            _um_c = _um_db.cursor()
                            _um_c.execute(
                                "INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)",
                                (user["id"],)
                            )
                            _um_c.execute(
                                "UPDATE unified_user_mapping SET gitcode_user=?, gitcode_nickname=?, updated_at=datetime('now') WHERE user_id=?",
                                (gc_login, _gc_user.get("name", ""), user["id"])
                            )
                            _um_db.commit()
                        log.info(f"unified_user_mapping synced: user_id={user['id']}, gitcode_user={gc_login}")
                    except Exception as _um_e:
                        log.warning(f"update_credentials: sync unified_user_mapping failed: {_um_e}")
                elif _resp.status_code == 401:
                    # Issue #386: 同 GitHub 分支——第三方 token 无效返回 400，
                    # 与调用方鉴权 401 区分，避免前端误判登录失效。
                    raise HTTPException(
                        status_code=400,
detail={"code": 400, "error_code": "TOKEN_INVALID", "message": "GitCode token 校验失败，请检查 token 有效性"},
                    )
                else:
                    log.warning(f"GitCode token validation failed: {_resp.status_code}")
                    raise HTTPException(
                        status_code=400,
detail={"code": 400, "error_code": "TOKEN_INVALID", "message": "GitCode token 校验失败，请检查 token 有效性"},
                    )
            except HTTPException:
                raise
            except Exception as _e:
                log.warning(f"GitCode token validation error: {_e}")
                raise HTTPException(
                    status_code=502,
                    detail={"code": 502, "message": f"GitCode token 验证失败: {_e}"},
                )
        elif not _gc_tok:
            # P0-4: 解绑前检查至少保留一种登录方式
            _check_login_method_before_unbind(user["id"], "gitcode")
            from src.auth.grape_account_db import delete_external_identity
            delete_external_identity(user["id"], "gitcode")

    # 绑定/解绑平台后清理 user_repos 缓存——下次 /repos/member 会从 VCS 重新拉取
    if "github_token" in explicit or "gitcode_token" in explicit:
        try:
            from src.session_manager import db as _sm_db, db_lock as _sm_dl
            with _sm_dl:
                _c = _sm_db.cursor()
                _c.execute("DELETE FROM user_repos WHERE user_id=?", (user["id"],))
                _sm_db.commit()
        except Exception as e:
            log.warning(f"update_credentials: clear user_repos failed: {e}")

    # FR-4/FR-10.1: PAT 保存后追加权限探测提示（不阻断保存）
    _scope_warnings = []
    for _provider, _token_field in [("gitcode", "gitcode_token"), ("github", "github_token")]:
        if _token_field in explicit:
            _tok = (explicit[_token_field] or "").strip()
            if _tok and not _is_placeholder(_tok):
                try:
                    import requests as _req
                    if _provider == "gitcode":
                        _hooks_resp = _req.get(
                            f"https://gitcode.com/api/v5/user/repos?per_page=1",
                            params={"access_token": _tok}, timeout=10)
                    else:
                        _hooks_resp = _req.get(
                            f"https://api.github.com/user/repos?per_page=1",
                            headers={"Authorization": f"Bearer {_tok}", "Accept": "application/vnd.github+json"},
                            timeout=10)
                    if _hooks_resp.status_code == 403:
                        _scope_warnings.append(f"{_provider}: Token 缺少 repo 读权限，仓库状态探活/托管校验将不可用")
                    elif _hooks_resp.status_code == 200:
                        _hooks_data = _hooks_resp.json()
                        if isinstance(_hooks_data, list) and not _hooks_data:
                            _scope_warnings.append(f"{_provider}: Token 可能缺少 repo 读权限（返回空列表），探活可能不可用")
                except Exception:
                    pass  # 探测失败不阻断保存
                # FR-10.1: 检测该用户名下 path B 仓库（host_user_id>0），提示 issue 写权限影响
                try:
                    from src.session_manager import db as _sm_db, db_lock as _sm_dl
                    with _sm_dl:
                        _c = _sm_db.cursor()
                        _c.execute("SELECT repo_key FROM managed_repos WHERE host_user_id=? AND host_user_id>0 LIMIT 5",
                                   (user["id"],))
                        _path_b_repos = [r[0] for r in _c.fetchall()]
                    if _path_b_repos:
                        _scope_warnings.append(
                            f"{_provider}: 你是 {len(_path_b_repos)} 个仓库的 host（路径 B），"
                            f"此 Token 将驱动 agent 回帖/建单，缺 issue 写权限将导致回复静默失败")
                except Exception:
                    pass

    if _scope_warnings:
        return {"message": "凭据已更新", "scope_warnings": _scope_warnings}
    return {"message": "凭据已更新"}


# ====== API Keys (Issue #69: grape_ 前缀用户级 key 管理) ======

class CreateApiKeyBody(BaseModel):
    name: str = ""


@router.post("/api-keys", operation_id="settings_key_create", summary="Create API key", description="Generates a grape_-prefixed API key. Plaintext is returned exactly once; the DB stores the SHA-256 hash.")
def create_api_key(body: CreateApiKeyBody, user: dict = Depends(require_no_session_token)):
    """生成 grape_ 前缀 API key——明文仅返回一次，DB 存 SHA-256 hash。

    复用 auth_token.generate_api_token（Issue-45 方式 B 已有后端），
    补齐 Web UI 管理入口（Issue #69 验收标准）。
    """
    from src.api.auth_token import generate_api_token

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Key 名称不能为空"})
    if len(name) > 64:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Key 名称不能超过 64 字符"})

    result = generate_api_token(user["id"], name)
    if not result:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "生成 API Key 失败（同名 key 可能已存在）"})

    log.info(f"API key created: user={user['username']} id={result['id']} name={name}")
    return {
        "id": result["id"],
        "key": result["token"],       # 明文——仅此一次返回
        "name": name,
        "message": "API Key 已生成，明文仅展示此次，请立即保存",
    }


@router.get("/api-keys", operation_id="settings_keys", summary="List API keys", description="Lists the current user's API keys without plaintext (name + status + timestamps only).")
def list_api_keys(user: dict = Depends(require_no_session_token)):
    """列出当前用户的所有 API key（不含明文，仅展示名称+状态+时间）。"""
    from src.api.auth_token import list_api_tokens, API_KEY_PREFIX

    tokens = list_api_tokens(user["id"])
    return {"keys": [
        {
            "id": t["id"],
            "name": t["name"],
            "prefix": API_KEY_PREFIX + "••••",
            "created_at": normalize_event_time(t.get("created_at")),
            "last_used_at": normalize_event_time(t.get("last_used_at")) if t.get("last_used_at") else None,
        }
        for t in tokens
    ]}


@router.delete("/api-keys/{key_id}", operation_id="settings_key_delete", summary="Revoke API key", description="Revokes an API key (soft delete, revoked=1); takes effect immediately and keeps audit.")
def revoke_api_key(key_id: int, user: dict = Depends(require_no_session_token)):
    """吊销 API key——软删除（revoked=1），立即失效，保审计。"""
    from src.api.auth_token import revoke_api_token

    ok = revoke_api_token(user["id"], key_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "API Key 不存在或已吊销"})

    log.info(f"API key revoked: user={user['username']} id={key_id}")
    return {"message": "API Key 已吊销，立即失效"}


# ====== Notifications ======

@router.get("/notifications", operation_id="settings_notifications_get", summary="Get notification preferences", description="Returns notification preferences.")
def get_notifications(user: dict = Depends(require_no_session_token)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT auto_impl_enabled, notify_enabled FROM repo_im_group_binding LIMIT 1")
            row = c.fetchone()
            auto_impl = bool(row[0]) if row else True
            notify = bool(row[1]) if row else True
        except Exception:
            auto_impl, notify = True, True
    return {
        "issues": True,
        "pr": True,
        "build": False,
        "release": True,
        "vod": auto_impl,
        "notifications_enabled": notify,
    }


class NotificationPrefs(BaseModel):
    issues: bool = True
    pr: bool = True
    build: bool = False
    release: bool = True
    vod: bool = False
    notifications_enabled: bool = True


@router.put("/notifications", operation_id="settings_notifications_update", summary="Update notification preferences", description="Updates notification preferences.")
def update_notifications(body: NotificationPrefs, user: dict = Depends(require_no_session_token)):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("UPDATE repo_im_group_binding SET auto_impl_enabled=?, notify_enabled=?",
                      (1 if body.vod else 0, 1 if body.notifications_enabled else 0))
            db.commit()
        except Exception:
            pass
    return {"message": "通知偏好已更新", "prefs": body.model_dump()}


# ====== GitCode Repo ======

@router.get("/gitcode-repo/{owner}/{repo}", operation_id="settings_gitcode_repo_get", summary="Get repo GitCode config", description="Returns GitCode config for a specific repo.")
def get_gitcode_repo_config(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    # R25.7: 用 TokenResolver.resolve() 替换 get_repo_token_with_refresh，返回 level 供前端分级显示
    from src.token_resolver import TokenResolver, create_provider_from_repo_key
    from src.repo_key import to_repo_key
    _rk = to_repo_key(f"{owner}/{repo}")
    _provider = create_provider_from_repo_key(_rk)
    _resolve_result = TokenResolver(_provider).resolve(_rk)
    bot_token = _resolve_result.token
    _resolved_level = _resolve_result.level  # "session"/"repo_bot"/"global_bot"/"user"/""
    bot_username = "devops-bot"
    target_key = _resolve_repo_key(f"{owner}/{repo}")
    repos = user_db.get_managed_repos(user["id"])
    for r in repos:
        if _resolve_repo_key(f'{r.get("repo_owner", "")}/{r.get("repo_name", "")}') == target_key:
            bot_username = r.get("bot_username", "") or "devops-bot"
            break

    # R16.2.3: 返回仓库级 bot token 状态
    _bot_token_status = ""
    _has_repo_bot_token = False
    try:
        from src.session_manager import db, db_lock
        # FR-21.4: 按 repo_key 前缀选列——GitHub 仓查 bot_github_token
        from src.repo_key import parse_repo_key
        _provider_name, _, _ = parse_repo_key(_rk)
        _token_col = "bot_github_token" if _provider_name == "github" else "bot_gitcode_token"
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT bot_token_status, {_token_col} FROM managed_repos WHERE repo_key=? LIMIT 1", (_rk,))
            _row = c.fetchone()
            if _row:
                _bot_token_status = _row[0] or ""
                _has_repo_bot_token = bool(_row[1])
    except Exception:
        pass

    return {
        "devops_bot_token_exists": bool(bot_token),
        "has_repo_bot_token": _has_repo_bot_token,
        "resolved_token_level": _resolved_level,
        "bot_token_status": _bot_token_status,
        "webhook_url": user_db.get_webhook_url() or "",
        "bot_username": bot_username,
    }


class GitCodeRepoConfig(BaseModel):
    bot_token: str = ""
    bot_username: str = "devops-bot"


@router.put("/gitcode-repo/{owner}/{repo}", operation_id="settings_gitcode_repo_update", summary="Update repo GitCode config", description="Updates GitCode config for a specific repo.")
def update_gitcode_repo_config(owner: str, repo: str, body: GitCodeRepoConfig,
                                     user: dict = Depends(require_no_session_token)):
    # 将 Bot Token / Bot Username 存储到 managed_repos（webhook_token 由托管流程自动维护）
    from src.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username
    from src.config import log as _log
    repo_key = _resolve_repo_key(f"{owner}/{repo}")
    # FR-20.5: 托管人/admin 权限校验——托管人(host_user_id)或 user_repos.is_admin 或 grape_accounts.role=admin
    from src.session_manager import db as _sm_db, db_lock as _sm_dl
    _is_authorized = False
    if user.get("role", "") == "admin":
        _is_authorized = True
    else:
        try:
            with _sm_dl:
                _c = _sm_db.cursor()
                # 检查是否为该仓库的托管人
                _c.execute("SELECT host_user_id FROM managed_repos WHERE repo_key=? AND host_user_id=?",
                           (repo_key, user["id"]))
                if _c.fetchone():
                    _is_authorized = True
                else:
                    # 检查 user_repos.is_admin
                    _c.execute("SELECT is_admin FROM user_repos WHERE user_id=? AND repo_key=? AND is_admin=1",
                               (user["id"], repo_key))
                    if _c.fetchone():
                        _is_authorized = True
        except Exception:
            pass
    if not _is_authorized:
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "PERMISSION_DENIED",
            "message": "无权限操作，请联系托管人或管理员"
        })
    if body.bot_token and not _is_placeholder(body.bot_token):
        token = body.bot_token.strip()
        # FR-9.1: verify token 有效性（与 R15 同口径）
        from src.token_resolver import create_provider_from_repo_key
        _vcs_provider = create_provider_from_repo_key(repo_key)
        try:
            _vcs_provider.verify_token(token)
        except Exception:
            raise HTTPException(status_code=400, detail={
                "code": 400, "error_code": "TOKEN_INVALID",
                "message": "Bot Token 无效，请检查后重试"
            })
        _result = update_managed_repo_bot_token(repo_key, token)
        if not _result.get("ok"):
            raise HTTPException(status_code=404, detail={
                "code": 404, "error_code": "REPO_NOT_FOUND",
                "message": "仓库不存在或写入失败"
            })
        _log.info(f"settings: bot token updated for {repo_key} by user {user.get('id')}")
    else:
        _result = {"ok": True, "warning": "", "related_repos": []}
    if body.bot_username:
        # FR-12.3: bot 用户名与真人账号冲突检查
        from src.session_manager import db as _sm_db, db_lock as _sm_dl
        try:
            with _sm_dl:
                _c = _sm_db.cursor()
                _c.execute("SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?) AND status=1 LIMIT 1", (body.bot_username,))
                if _c.fetchone():
                    raise HTTPException(status_code=400, detail={
                        "code": 400, "error_code": "BOT_USERNAME_CONFLICT",
                        "message": f"Bot 用户名 '{body.bot_username}' 与真人账号冲突，该用户的操作将被自循环过滤静默忽略"
                    })
        except HTTPException:
            raise
        except Exception:
            pass  # 查询失败不阻断
        update_managed_repo_bot_username(repo_key, body.bot_username)
    # FR-20.11: 返回 warning + related_repos
    _response = {"message": "GitCode Repo 配置已更新"}
    if _result.get("warning"):
        _response["warning"] = _result["warning"]
    if _result.get("related_repos"):
        _response["scope_warnings"] = [f"该 Token 变更将影响 {len(_result['related_repos'])} 个关联仓库的跨仓信任域"]
    return _response


# ====== OAuth 凭据（管理员配置）======

@router.get("/oauth", operation_id="settings_oauth_get", summary="Get GitCode OAuth config", description="Returns whether GitCode OAuth is configured (client id/redirect uri; secret masked).")
def get_oauth_config(user: dict = Depends(require_no_session_token)):
    from src.user.db import get_meta_value
    return {
        "gitcode_oauth_client_id": bool(get_meta_value("oauth_gitcode_client_id")),
        "gitcode_oauth_redirect_uri": get_meta_value("oauth_gitcode_redirect_uri"),
    }


class OAuthConfig(BaseModel):
    gitcode_oauth_client_id: str = ""
    gitcode_oauth_client_secret: str = ""
    gitcode_oauth_redirect_uri: str = ""


@router.put("/oauth", operation_id="settings_oauth_update", summary="Update GitCode OAuth config", description="Updates the GitCode OAuth client config.")
def update_oauth_config(body: OAuthConfig, user: dict = Depends(require_no_session_token)):
    from src.user.db import set_meta_value
    if body.gitcode_oauth_client_id:
        set_meta_value("oauth_gitcode_client_id", body.gitcode_oauth_client_id)
    if body.gitcode_oauth_client_secret:
        set_meta_value("oauth_gitcode_client_secret", body.gitcode_oauth_client_secret)
    if body.gitcode_oauth_redirect_uri:
        set_meta_value("oauth_gitcode_redirect_uri", body.gitcode_oauth_redirect_uri)
    return {"message": "OAuth 配置已更新"}


class GitHubOAuthConfig(BaseModel):
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_oauth_redirect_uri: str = ""


@router.get("/oauth-github", operation_id="settings_oauth_github_get", summary="Get GitHub OAuth config", description="Returns GitHub OAuth configuration status (secret masked).")
def get_github_oauth_config(user: dict = Depends(require_no_session_token)):
    from src.user.db import get_meta_value
    return {
        "github_oauth_client_id": bool(get_meta_value("oauth_github_client_id")),
        "github_oauth_redirect_uri": get_meta_value("oauth_github_redirect_uri"),
    }


@router.put("/oauth-github", operation_id="settings_oauth_github_update", summary="Update GitHub OAuth config", description="Updates the GitHub OAuth client config.")
def update_github_oauth_config(body: GitHubOAuthConfig, user: dict = Depends(require_no_session_token)):
    from src.user.db import set_meta_value
    if body.github_oauth_client_id:
        set_meta_value("oauth_github_client_id", body.github_oauth_client_id)
    if body.github_oauth_client_secret:
        set_meta_value("oauth_github_client_secret", body.github_oauth_client_secret)
    if body.github_oauth_redirect_uri:
        set_meta_value("oauth_github_redirect_uri", body.github_oauth_redirect_uri)
    return {"message": "GitHub OAuth 配置已更新"}


# ====== Agent Runtimes ======

# Issue #473 范围参数合法值（PR #668 审查修复）：Literal 校验——非法值 422，
# 不再静默回落全量（QA Nice-to-have）。空串=默认全量口径（与前端不传参一致）。
# 参数名 range 遮蔽 Python 内置（QA Nice-to-have）：与既有 type 参数同风格保留，
# 本文件无内置 range() 调用（AST 扫描确认），仅函数内作用域无实际冲突。
RTRange = Literal["", "all", "repo", "user"]

@router.get("/agent-runtimes", operation_id="settings_agent_runtimes", summary="List agent runtimes", description="Lists agent executors from DB with derived health and unique instance_name. Visibility: global + personal (owner=current user) + repo-level of repos the user belongs to. Supports pagination via page/page_size query params. Issue #277: repo param filters repo-scoped engines to only the current repo. Issue #472: q/type/scene/tag params apply search/filters server-side over the full visible set (not just the current page) and the response carries health_stats (total + health distribution over the whole filtered result) so the stats bar reflects the overall search result, decoupled from pagination. Issue #473: range param narrows the viewing scope - empty/all (default, full visibility), repo (repo scope only), user (current user's user-scope + own local engines); invalid range values are rejected with 422.")
def get_agent_runtimes(user: dict = Depends(require_org_member), page: int = 1, page_size: int = 0,
                       scope: str = "", repo: str = "", range: RTRange = "",
                       q: str = "", type: str = "", scene: str = "", tag: str = ""):
    """从 DB 读取 Agent 执行引擎列表，含健康态派生与唯一引擎名（instance_name）。

    可见性（按当前登录用户）：全局 + 个人（owner=当前用户）+ 我所在仓库的仓库级（仓库所有成员可见，不只 owner）。
    派生字段：instance_name / worker_name / worker_ip / scope / owner（见 runtime-admin-two-view-design.md §7）。

    分页（Issue #185）：page/page_size 为可选参数。
    - page_size=0 或不传 → 返回全量（向后兼容旧调用方）
    - page_size>0 → 返回 {runtimes: [...page slice...], total: N, page: P, page_size: S}
    可见性过滤在应用层完成（依赖 credential scope + 用户仓库列表），无法纯 SQL 分页，
    因此先全量过滤再切片——total 为过滤后总数。

    筛选（Issue #472 全局口径）：q/type/scene/tag 在分页切片前对全量可见集过滤
    （谓词与原前端 _filterEngines 一致），health_stats 为筛选后整体搜索结果的
    健康分布（total + online/recently_lost/offline/about_to_gc）——统计栏与分页解耦。

    Issue #277 数据隔离：repo 参数非空时，repo scope 引擎仅返回该仓库的引擎，
    避免跨仓库看到其他仓库的 worker 设备和引擎。

    Issue #338：个人组织视图下，个人归属 worker 设备（scope=user 且 owner=当前用户）
    不受凭据创建时绑定的 org_id 隔离（与 dashboard 个人组织视图语义一致）。

    Issue #473 范围筛选：range 参数收窄查看范围——空/all=全量口径（默认），
    repo=仅 repo scope（配合 repo 参数复用 #277 收窄到当前仓库），
    user=仅当前登录用户名下 user scope（含 owner=当前用户的本机引擎）。
    """
    from src.user import db as user_db
    from src.runtime.instances import instance_name
    from src.session_manager import db, db_lock

    uid = user.get("id") or 0
    uname = user.get("username") or ""
    user_repos = {r["full_name"] for r in (user_db.get_user_repos(uid) if uid else [])}

    # 获取全部有效用户名，用于判断已删除用户的本地引擎
    valid_users = set()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT username FROM grape_accounts")  # D32-补丁
            valid_users = {r[0] for r in c.fetchall()}
        except Exception:
            pass

    # 凭据映射 + 可见性过滤（共享 helper，Issue #185 审查修复）
    # Issue #277 数据隔离：repo 参数非空时，repo scope 仅返回当前仓库的引擎
    cred_map = _build_cred_map(db, db_lock)
    _visible_credential = _make_visible_credential_fn(uname, user_repos, repo_filter=repo)

    cached = user_db.get_agent_runtimes(scope=None)
    # Issue #512：出口统一归一——last_seen_at/updated_at 与心跳链路同为 UTC ISO（带 +00:00）。
    # normalize_event_time 幂等：带 offset 值原样通过；MySQL 存量 naive CST 值按 CST 语义
    # 归一为 UTC（兜底未迁移/残留脏行，避免前端与健康判定 8h 偏差）
    for _rt in cached:
        # 空值保持原样（None/""），非空值归一为 UTC ISO（normalize_event_time 幂等）
        _rt_last = _rt.get("last_seen_at")
        _rt["last_seen_at"] = normalize_event_time(_rt_last) if _rt_last else _rt_last
        _rt_upd = _rt.get("updated_at")
        _rt["updated_at"] = normalize_event_time(_rt_upd) if _rt_upd else _rt_upd
    _org_id = user.get("org_id", 0)
    # 判断当前组织是否是个人组织（个人组织显示 global 引擎，非个人组织严格隔离）
    _is_personal_org = False
    if _org_id:
        from src.user.org_db import is_org_personal
        _is_personal_org = is_org_personal(_org_id)
    _filter_org_id = 0 if _is_personal_org else _org_id
    now = __import__("time").time()
    visible_runtimes = []
    for rt in cached:
        # 组织隔离：
        # 非个人组织：只显示 org_id 匹配的引擎（不含 global org_id=0）
        # 个人组织：只显示 org_id=0（全局/本机）和 org_id=自身个人组织的引擎，
        #           不显示其他非个人组织的引擎
        rt_org_id = rt.get("org_id") or 0
        if _filter_org_id:
            # 非个人组织：严格 org_id 隔离
            # Issue #371：repo scope 引擎豁免——切换为仓库级后 org_id 可能对齐到仓库所属组织
            # （与当前查看组织不同），但仓库级设备对仓库成员可见（#277 仓库隔离口径），
            # 可见性最终由 _visible_credential 的 repo_key in user_repos 判定收敛，不越权。
            if rt_org_id != _filter_org_id:
                _cred_early = cred_map.get(rt.get("credential_id") or 0) or {}
                if _cred_early.get("scope") != "repo":
                    continue
        elif _is_personal_org and rt_org_id and rt_org_id != _org_id:
            # Issue #338：个人归属 worker 引擎（凭据 scope=user 且 owner=当前用户）
            # 不受凭据创建时绑定的 org_id 隔离——归属判断以 _visible_credential 为准
            # （与 dashboard 个人组织视图 "global + 自己的 user scope worker" 语义一致）
            # repo scope 设备只在创建它的组织下可见，不在个人组织下跨组织展示
            _cred_early = cred_map.get(rt.get("credential_id") or 0) or {}
            _own_user_worker = (_cred_early.get("scope") == "user"
                                and (_cred_early.get("owner_name") or "") == uname)
            if not _own_user_worker:
                # 个人组织：跳过其他非个人组织（org_id>0 且不是自己）的引擎
                # 但需要检查该 org 是否是个人组织——如果是个人组织则不跳过
                from src.user.org_db import is_org_personal
                if not is_org_personal(rt_org_id):
                    continue
        rt["health"] = _runtime_health(rt, now)
        cid = rt.get("credential_id") or 0
        cred = cred_map.get(cid) or {"name": "", "device_name": "", "scope": "global",
                                     "owner_name": "", "owner_id": "", "last_ip": ""}
        if not _visible_credential(cid, cred["scope"], cred["owner_name"], cred["owner_id"], from_repo_key(cred.get("repo_key", ""))):
            continue
        if cid == 0:
            # 本机引擎行可能绑定了已删除用户的 owner_id，需校验用户是否仍存在
            rt_owner = (rt.get("owner_id") or "").strip()
            if rt_owner and rt_owner not in valid_users:
                # 对应用户已删除 → 引擎不展示，标记 health=user_deleted 供排查
                log.info(f"agent-runtimes: skipping local engine '{rt['name']}' owned by deleted user '{rt_owner}'")
                continue
        if cred.get("status") == "revoked":
            # 设备已吊销 → 引擎不再展示（与 Workers 视图只列 active 一致）；
            # DB 行保留 offline，避免历史会话/任务绑定的 runtime_id 悬空
            continue
        rt["instance_name"] = instance_name(rt["name"], cred["device_name"], cid)
        rt["worker_name"] = cred["name"]
        # AIShell 显示名（2026-10-13）：出口统一转换 name/display_name，
        # 前端读 rt.name 的所有位置一次生效；内部路由键（DB 行本身）不变
        from src.runtime.instances import to_display
        rt["display_name"] = to_display(rt.get("display_name") or rt["name"])
        rt["name"] = to_display(rt["name"])
        rt["worker_ip"] = cred["last_ip"]
        # 本机直连（cid=0）：owner/scope 取引擎自身 DB 值，不继承空凭据默认值
        if cid == 0:
            rt["owner"] = cred["owner_name"]
            rt["scope"] = rt.get("scope", cred["scope"]) or "global"
            # 本机直连引擎的 worker_name/device_name 用 host_name（区分多台主控机）
            _hn = rt.get("host_name") or ""
            if _hn:
                rt["worker_name"] = _hn
                rt["worker_ip"] = "127.0.0.1"
        else:
            rt["owner"] = cred["owner_name"]
            rt["scope"] = cred["scope"]          # 引擎 scope 随 worker（凭据）一致
            rt["repo_key"] = from_repo_key(cred.get("repo_key", ""))
        rt["is_local"] = (cid == 0)          # 本机直连（credential_id=0，恒 direct），前端分组/标签用
        rt["spec"] = cred.get("spec") or ""
        # B5 (Issue #257): worker 版本是否落后（服务端最新版本 vs worker 上报版本）
        try:
            from src.worker_package import worker_code_version, normalize_worker_version
            _latest_wv = worker_code_version()
            _worker_wv = rt.get("worker_version", "")
            if _latest_wv and _worker_wv:
                rt["worker_version_stale"] = normalize_worker_version(_latest_wv) != normalize_worker_version(_worker_wv)
            else:
                rt["worker_version_stale"] = False
        except Exception:
            rt["worker_version_stale"] = False
        # 引擎健康分（Issue #134 展示增强）
        # Issue #498: 透出统一异常口径（health_abnormal/health_reason）与自愈留痕
        # （最近失败时间/衰减窗口有效失败数），前端两口径一致呈现、异常原因可见
        try:
            from src.runtime.health_scorer import get_health_score
            _hd = get_health_score(rt["id"])
            rt["health_score"] = _hd.get("health_score")
            rt["health_total_tasks"] = _hd.get("total_tasks", 0)
            rt["health_failed_tasks"] = _hd.get("failed_tasks", 0)
            rt["health_failed_tasks_effective"] = _hd.get("failed_tasks_effective", 0)
            rt["health_consecutive_failures"] = _hd.get("consecutive_failures", 0)
            rt["health_heartbeat_alive"] = _hd.get("heartbeat_alive")
            rt["health_last_failure_at"] = _hd.get("last_failure_at", "")
            rt["health_abnormal"] = _hd.get("health_abnormal", False)
            rt["health_reason"] = _hd.get("health_reason", "")
        except Exception:
            rt["health_score"] = None
        # 引擎标签（event-dispatch-redesign-v2.md §2.5）
        try:
            with db_lock:
                _tc = db.cursor()
                _tc.execute("SELECT tag, priority, enabled FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag", (rt["id"],))
                rt["tags"] = [{"tag": r[0], "priority": r[1], "enabled": bool(r[2])} for r in _tc.fetchall()]
        except Exception:
            rt["tags"] = []
        visible_runtimes.append(rt)

    # 设备级视图（共享 helper，Issue #185 审查修复：消除重复代码）
    # org_id 过滤 cred_map 和 cached（与 runtime-devices 端点一致，防止闪现其他组织设备）
    # Issue #371：非个人组织严格隔离分支为 repo scope 增加豁免（与 #338 个人组织分支同口径），
    # repo scope 设备对仓库成员可见，可见性由 _visible_credential 的 repo_key in user_repos 收敛。
    _devices_cred_map = cred_map
    _devices_cached = cached
    if _filter_org_id:
        _devices_cred_map = {k: v for k, v in cred_map.items()
                             if (v.get("org_id") or 0) == _filter_org_id
                             or v.get("scope") == "repo"}
        _devices_cached = [rt for rt in cached if (rt.get("org_id") or 0) == _filter_org_id
                           or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
    elif _is_personal_org:
        # 个人组织：只保留 org_id=0（全局）和 org_id=自身 的凭据/引擎；
        # Issue #338：个人归属 worker 设备（scope=user 且 owner=当前用户）不受
        # 凭据创建时绑定的 org_id 隔离（可见性由 _visible_credential 的 owner 判定收敛）
        # Issue #430：repo scope 设备豁免（与 #371 非个人组织分支同口径）——
        # 切换为仓库级后 org_id 对齐到仓库所属组织（可能是其他用户的个人组织），
        # 仓库级设备对仓库成员可见，可见性由 _visible_credential 的 repo_key in
        # user_repos 判定收敛，不越权。
        _devices_cred_map = {k: v for k, v in cred_map.items()
                             if (v.get("org_id") or 0) in (0, _org_id)
                             or v.get("scope") == "repo"
                             or (v.get("scope") == "user" and (v.get("owner_name") or "") == uname)}
        _devices_cached = [rt for rt in cached if (rt.get("org_id") or 0) in (0, _org_id)
                           or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
    devices = _build_runtime_devices(db, db_lock, _devices_cred_map, _visible_credential,
                                     valid_users, _devices_cached, now)

    # Issue #473 范围筛选：全部（默认，现有全量口径）/ 仓库（仅 repo scope，配合 repo 参数
    # 复用 #277 口径收窄到当前仓库）/ 个人（仅当前用户 user scope + 本人本机引擎）。
    # 先范围再 scope 标签再分页；吊销设备在可见性阶段已排除（#417）。
    if range:
        visible_runtimes = [rt for rt in visible_runtimes
                            if _rt_in_range(rt.get("scope") or "global", rt.get("owner") or "",
                                            bool(rt.get("is_local")), range, uname,
                                            local_owner=rt.get("owner_id") or "")]
        devices = [d for d in devices
                   if _rt_in_range(d.get("scope") or "global", d.get("owner") or "",
                                   (d.get("credential_id") or 0) == 0, range, uname,
                                   local_owner=d.get("local_owner_id") or "")]

    # scope 筛选（服务端过滤）
    if scope:
        if scope == "local":
            visible_runtimes = [rt for rt in visible_runtimes if rt.get("is_local")]
        else:
            visible_runtimes = [rt for rt in visible_runtimes
                                if (rt.get("scope") or "global") == scope]

    # 返回所有可见引擎的 scope 分布（供前端生成筛选标签，不依赖当前页数据）
    _avail_scopes = list(set(
        ("local" if rt.get("is_local") else (rt.get("scope") or "global"))
        for rt in visible_runtimes
    ))

    # Issue #472 全局口径：筛选标签（类型/场景/标签）从全量可见集派生（分页/筛选后不消失）
    _avail_types = list(set(_engine_type_of(rt) for rt in visible_runtimes if _engine_type_of(rt)))
    _avail_scenes = sorted({s for rt in visible_runtimes for s in (rt.get("scene_tags") or []) if s})
    _avail_tags = sorted({t for rt in visible_runtimes for t in (rt.get("agent_tags") or []) if t})

    # Issue #472 全局口径：q/type/scene/tag 在分页切片前对全量可见集过滤
    # （谓词与原前端 _filterEngines 一致，搜索作用于整体结果而非当前页）
    _q = (q or "").strip().lower()
    if _q:
        visible_runtimes = [rt for rt in visible_runtimes if _q in " ".join([
            str(rt.get("alias") or ""), str(rt.get("instance_name") or ""),
            str(rt.get("name") or ""), str(rt.get("worker_name") or ""),
            str(rt.get("owner") or ""), str(rt.get("host_name") or ""),
            " ".join(rt.get("agent_tags") or []),
            " ".join(rt.get("local_skills") or []),
        ]).lower()]
    if type:
        # 类型过滤用 _engine_type_of 规范化后精确匹配（原 `_tlow in name` 子串
        # 匹配会让 "aishell" 筛选带出 aishell-dsh——与派发侧 includes 误命中
        # 同族问题，2026-11-06 页面验收发现）。type 值本身即 _engine_type_of 派生。
        _tlow = type.lower()
        visible_runtimes = [rt for rt in visible_runtimes
                            if _engine_type_of(rt).lower() == _tlow]
    if scene:
        _scenes = [s.strip().lower() for s in scene.split(",") if s.strip()]
        visible_runtimes = [rt for rt in visible_runtimes
                            if any((s in [str(x).lower() for x in (rt.get("scene_tags") or [])])
                                   for s in _scenes)]
    if tag:
        _tags = [t.strip().lower() for t in tag.split(",") if t.strip()]
        visible_runtimes = [rt for rt in visible_runtimes
                            if any((t in [str(x).lower() for x in (rt.get("agent_tags") or [])]
                                    or "all" in [str(x).lower() for x in (rt.get("agent_tags") or [])])
                                   for t in _tags)]

    # Issue #472 全局口径：统计栏数据源 = 筛选后整体搜索结果的健康分布（与分页解耦）
    health_stats = _health_stats(visible_runtimes)

    # Issue #185: 分页（page_size>0 时对 visible_runtimes 切片，total 为过滤后总数）
    # Issue #865：仅对当前页批量附加模型字段（单条 GROUP BY，无 N+1）
    total_runtimes = len(visible_runtimes)
    if page_size and page_size > 0:
        _page = max(page, 1)
        _offset = (_page - 1) * page_size
        paged_runtimes = visible_runtimes[_offset:_offset + page_size]
        _attach_engine_models(paged_runtimes)
        return {"runtimes": paged_runtimes, "devices": devices,
                "total": total_runtimes, "page": _page, "page_size": page_size,
                "avail_scopes": _avail_scopes, "avail_types": _avail_types,
                "avail_scenes": _avail_scenes, "avail_tags": _avail_tags,
                "health_stats": health_stats}
    _attach_engine_models(visible_runtimes)
    return {"runtimes": visible_runtimes, "devices": devices, "total": total_runtimes,
            "avail_scopes": _avail_scopes, "avail_types": _avail_types,
            "avail_scenes": _avail_scenes, "avail_tags": _avail_tags,
            "health_stats": health_stats}


def _engine_type_of(rt: dict) -> str:
    """从引擎名派生类型标签——走 canonical_engine 查表后转展示类型名
    （canonical 是后端键：aishell/hwcloud → hwcloud；本函数是页面类型：
    旧 AI Shell 显示 aishell、新引擎 aishell-dsh，与前端 _availTypes 一致）。
    非前缀形态的历史名保留 includes 兜底（旧轨判定不变）。"""
    n = (rt.get("instance_name") or rt.get("name") or "").lower()
    if not n:
        return ""
    from src.runtime.instances import canonical_engine

    c = canonical_engine(n)
    if c == "aishell-dsh":
        return "aishell-dsh"
    if c == "hwcloud":
        return "aishell"
    if c != "opencode":
        return c
    # 旧轨兜底：名字中段包含引擎词的历史形态（与改造前 includes 判定一致）
    if "aishell-dsh" in n:
        return "aishell-dsh"
    if "aishell" in n or "hwcloud" in n:
        return "aishell"
    if "hermes" in n:
        return "hermes"
    if "opencode" in n:
        return "opencode"
    if "codearts" in n:
        return "codearts"
    if "codex" in n:
        return "codex"
    return ""


_HEALTH_STAT_KEYS = ("online", "recently_lost", "offline", "about_to_gc")


def _health_stats(items: list) -> dict:
    """Issue #472: 整体搜索结果的健康分布（total + 四态计数），统计栏全局口径数据源。"""
    counts = {k: 0 for k in _HEALTH_STAT_KEYS}
    for it in items:
        h = it.get("health") or it.get("status") or "offline"
        if h in counts:
            counts[h] += 1
    counts["total"] = len(items)
    return counts


# ====== Issue #865：引擎模型展示（配置模型 + 最近实际使用模型）======

_RECENT_MODELS_TOP_N = 3


def _recent_models_by_runtime(runtime_ids: list[int]) -> dict[int, list[dict]]:
    """按 runtime_ids 批量聚合 obs_llm_usage 的最近实际使用模型（top N）。

    单条 GROUP BY（engine_id × model 维度一次查询，避免 N+1），
    top N 在应用层排序（count desc → last_used desc）。
    返回 {runtime_id: [{model, count, last_used_at}, ...]}；无用量/异常返回空 dict。
    安全约束（Issue #865 Spec）：仅聚合模型名字段，不取 base_url/api_key。
    """
    ids = [int(rid) for rid in (runtime_ids or []) if rid]
    if not ids:
        return {}
    from src.session_manager import db, db_lock
    out: dict[int, list[dict]] = {}
    try:
        with db_lock:
            c = db.cursor()
            ph = ",".join("?" * len(ids))
            c.execute(
                f"SELECT engine_id, model_key, model_raw, COUNT(*) AS cnt, MAX(created_at) AS last_used "
                f"FROM obs_llm_usage WHERE engine_id IN ({ph}) GROUP BY engine_id, model_key, model_raw",
                ids)
            for eng_id, m_key, m_raw, cnt, last_used in c.fetchall():
                model = str(m_key or m_raw or "").strip()
                if not model:
                    continue
                out.setdefault(int(eng_id or 0), []).append(
                    {"model": model[:120], "count": int(cnt or 0), "last_used_at": last_used or ""})
    except Exception as e:
        log.warning(f"recent_models aggregation failed: {e}")
        return {}
    for rows in out.values():
        rows.sort(key=lambda r: (-r["count"], r["last_used_at"]))
        del rows[_RECENT_MODELS_TOP_N:]
    return out


def _attach_engine_models(runtimes: list[dict]) -> None:
    """为列表/详情的引擎行附加模型字段（Issue #865）。

    - recent_models：obs_llm_usage 聚合（批量单查，无 N+1）
    - 配置模型：DB model_name（worker 上报）；本机直连（credential_id=0）且未上报时
      服务端读本机配置兜底（model_source=local_config，复用 worker 同一解析器）
    仅附加模型名字段，绝不输出 base_url/api_key/环境变量值。
    """
    from src.runtime.engine_model import detect_engine_model
    recent = _recent_models_by_runtime([rt.get("id") for rt in runtimes])
    for rt in runtimes:
        rt["recent_models"] = recent.get(rt.get("id"), [])
        if not (rt.get("model_name") or "") and (rt.get("credential_id") or 0) == 0:
            try:
                _m = detect_engine_model(rt.get("name") or "")
            except Exception:
                _m = ""
            if _m:
                rt["model_name"] = _m
                rt["model_source"] = "local_config"


def _runtime_health(rt: dict, now: float) -> str:
    """单引擎健康态：
    - credential_id>0（远端边端）：status+last_seen 四态
    - credential_id=0（本地引擎）：installed/healthy 派生（就绪=online，未装=offline）
    """
    from src.runtime.health import derive_runtime_health, derive_local_runtime_health
    if rt.get("credential_id"):
        return derive_runtime_health(rt.get("status") or "offline", rt.get("last_seen_at"), now)
    # Issue #592：本机直连口径收敛到 health.py（与 dashboard 弹窗共用，防两处漂移）
    return derive_local_runtime_health(bool(rt.get("installed")), bool(rt.get("healthy")))


_DEVICE_ONLINE_WINDOW = 60  # 心跳间隔 15s，容差 60s 内视为在线


def _device_health(cstatus: str, last_seen_at: str | None, now: float) -> str:
    """设备（凭据维度）健康态：由心跳新鲜度派生。

    凭据 `status` 恒为 'active'（不表达在线），不能像 runtime 行那样按 `status='online'` 判定；
    在线 = 最近心跳在 _DEVICE_ONLINE_WINDOW 内；失联 <5min = 刚失联；>6d = 待回收；其余离线。
    """
    from src.runtime.health import parse_dt
    ts = parse_dt(last_seen_at)
    if ts is None:
        return "offline"
    offline_for = now - ts
    if offline_for < _DEVICE_ONLINE_WINDOW:
        return "online"
    if offline_for < 5 * 60:
        return "recently_lost"
    if offline_for > 6 * 24 * 3600:
        return "about_to_gc"
    return "offline"


# ====== 共享 helper（Issue #185 审查修复：消除 agent-runtimes / runtime-devices 重复代码 + N+1 查询）======

def _build_cred_map(db, db_lock) -> dict:
    """构建 credential_id -> 凭据信息映射，两个端点共用。"""
    cred_map = {}
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, last_ip, repo_key, org_id, spec FROM runtime_credentials")
        for r in c.fetchall():
            cid, cname, cdev, cscope, cowner_id, cowner_name, cstatus, clast, clast_ip, crepo_key, corg_id, cspec = r
            cred_map[cid] = {"name": cname, "device_name": cdev or "", "scope": cscope,
                             "owner_id": cowner_id or "", "owner_name": cowner_name or "",
                             "status": cstatus, "last_seen_at": clast, "last_ip": clast_ip or "",
                             "repo_key": crepo_key or "", "org_id": corg_id or 0,
                             "spec": cspec or ""}
    return cred_map


def _make_visible_credential_fn(uname: str, user_repos: set, repo_filter: str = ""):
    """返回 _visible_credential 闭包，两个端点共用。
    uname: 当前登录用户名
    user_repos: 用户可访问的仓库 full_name 集合（无前缀 owner/repo）
    repo_filter: 当前仓库 full_name（无前缀），非空时 repo scope 仅允许该仓库（Issue #277 数据隔离）
    """
    def _visible_credential(cid: int, cscope: str, cowner_name: str, cowner_id: str,
                            crepo_key: str = "") -> bool:
        if not cid:                      # credential_id=0（历史本机引擎）
            return True
        if cscope == "global":
            return True
        if cscope == "user":
            return cowner_name == uname
        if cscope == "repo":
            # owner_id 为空（历史转换入口没写）时回退用 cred 的 repo_key（防御性，
            # 2026-10-16：转换后设备对仓库成员不可见的根因）
            _rk = from_repo_key(cowner_id) or from_repo_key(crepo_key)
            # repo_filter 非空（Issue #277 数据隔离）：仅允许当前仓库的设备/引擎
            if repo_filter:
                return _rk == repo_filter
            if cowner_name == uname:
                return True
            # repo_key 存储为 gitcode:owner/repo（带前缀），user_repos 无前缀
            # 统一用 from_repo_key 剥前缀后比对（Issue #248）
            return _rk in user_repos
        return False
    return _visible_credential


def _rt_in_range(scope: str, owner: str, is_local: bool, rt_range: str, uname: str,
                 local_owner: str = "") -> bool:
    """Issue #473 范围筛选判定（列表查看范围，与 scope 标签筛选互补）。

    - 空 / all / global → True（现有全量口径兜底：全局 + 个人 + 我所在仓库的仓库级）
    - repo  → 仅 repo scope（配合 #277 repo 参数时进一步收窄到当前仓库）
    - user  → 仅当前登录用户名下 user scope（含 owner=当前用户的本机引擎）

    PR #668 审查修复（Required）：本机引擎（credential_id=0）的输出 owner 恒为空
    （cred_map 无 id=0 行，落默认 owner_name=""），is_local 条目以输出 owner 判定
    归属不可达；改用引擎自身 DB 行 owner_id（agent_runtimes.owner_id，
    local_owner 参数）判定「owner=当前用户的本机引擎」。远端条目（cid>0）不受影响。
    local_owner 缺省为 ""（无主/未传）→ 不属于任何用户，不误入个人范围。
    """
    _r = (rt_range or "").strip().lower()
    if not _r or _r in ("all", "global"):
        return True
    if _r == "repo":
        return (scope or "global") == "repo"
    if _r == "user":
        if is_local:
            return (local_owner or "").strip() == uname
        return (owner or "") == uname and (scope or "global") == "user"
    return True


def _build_runtime_devices(db, db_lock, cred_map, visible_credential, valid_users: set,
                           local_runtimes: list, now: float) -> list:
    """构建设备级视图列表（按 credential_id 分组），agent-runtimes / runtime-devices 共用。

    修复 N+1 查询：本机直连引擎（credential_id=0）直接从 local_runtimes 参数传入，
    不再在 runtime-devices 端点中二次全量查询 get_agent_runtimes(scope=None)。

    参数:
      db / db_lock: session_manager 的 db 和 db_lock
      cred_map: _build_cred_map 返回的凭据映射
      visible_credential: _make_visible_credential_fn 返回的闭包
      valid_users: 有效用户名集合（用于过滤已删除用户的本机引擎）
      local_runtimes: credential_id=0 的本机引擎列表（get_agent_runtimes(scope=None) 的结果，
                      两个端点均已查询，传入复用避免二次全量扫描）
      now: 当前时间戳
    """
    from src.runtime.health import derive_runtime_health
    from src.runtime.instances import instance_name
    import json as _json

    devices = []
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM runtime_credentials WHERE status='active' ORDER BY id DESC")
        for (cid,) in c.fetchall():
            cred = cred_map.get(cid)
            if not cred:
                continue  # cred_map 已被组织过滤 — 不在 map 中的凭据跳过
            if not visible_credential(cid, cred.get("scope", "global"), cred.get("owner_name", ""), cred.get("owner_id", ""), from_repo_key(cred.get("repo_key", ""))):
                continue
            c.execute("SELECT id, name, version, healthy, installed, status, last_seen_at, local_skills, skill_scan_result, worker_version FROM agent_runtimes WHERE credential_id=?", (cid,))
            # AIShell 显示名（2026-10-13 收口补漏）：worker 设备视图的承载引擎列表
            from src.runtime.instances import to_display as _td2
            runtimes = [
                {"id": x[0], "name": _td2(x[1]), "version": x[2], "healthy": bool(x[3]),
                 "installed": bool(x[4]),
                 "instance_name": instance_name(x[1], cred.get("device_name", ""), cid),
                 # Issue #1310：承载引擎 chip 显示「引擎名（凭据名）」——补凭据名 worker_name
                 "worker_name": cred.get("name", ""),
                 "health": derive_runtime_health(x[5], x[6], now),
                 "local_skills": _json.loads(x[7] or "[]") if x[7] else [],
                 "skill_scan_result": _json.loads(x[8] or "[]") if x[8] else [],
                 "worker_version": x[9] or "" if len(x) > 9 else ""}
                for x in c.fetchall()
            ]
            for rt in runtimes:
                _tc = db.cursor()
                _tc.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rt["id"],))
                rt["agent_tags"] = [r[0] for r in _tc.fetchall()]
            # B5: worker 版本落后标记（设备级，取首个引擎的 worker_version）
            _dev_wv = runtimes[0].get("worker_version", "") if runtimes else ""
            _dev_wv_stale = False
            if _dev_wv:
                try:
                    from src.worker_package import worker_code_version, normalize_worker_version
                    _dev_wv_stale = normalize_worker_version(worker_code_version()) != normalize_worker_version(_dev_wv)
                except Exception:
                    pass
            devices.append({"credential_id": cid, "name": cred.get("name", ""), "device_name": cred.get("device_name", ""),
                            "credential_status": cred.get("status", "active"), "last_seen_at": cred.get("last_seen_at"),
                            "worker_ip": cred.get("last_ip", ""), "scope": cred.get("scope", "global"),
                            "owner": cred.get("owner_name", ""),
                            "repo_key": from_repo_key(cred.get("repo_key", "")),
                            "health": _device_health(cred.get("status", "active"), cred.get("last_seen_at"), now),
                            "worker_version": _dev_wv,
                            "worker_version_stale": _dev_wv_stale,
                            "runtimes": runtimes})

    # 本机直连引擎（credential_id=0）：关联到 runtime_credentials 中已有的主控机设备
    import socket as _sock
    _local_hostname = ""
    try:
        _local_hostname = _sock.gethostname()
    except Exception:
        pass
    _device_by_name = {d.get("device_name", ""): d for d in devices}

    for rt in local_runtimes:
        if (rt.get("credential_id") or 0) != 0:
            continue
        # 已删除用户的本地引擎不展示
        rt_owner = (rt.get("owner_id") or "").strip()
        if rt_owner and rt_owner not in valid_users:
            continue
        # 健康态 + instance_name 派生（本机引擎 cid=0 用 host_name 作为 device_name）
        rt["health"] = _runtime_health(rt, now)
        _hn = rt.get("host_name") or _local_hostname or ""
        rt["instance_name"] = instance_name(rt["name"], _hn, 0)
        from src.runtime.instances import to_display as _td
        rt["name"] = _td(rt["name"])
        target_device = _device_by_name.get(_hn)
        if target_device:
            # Issue #473 范围筛选：本机引擎挂到已有主控机凭据设备时补充归属
            if not target_device.get("local_owner_id"):
                target_device["local_owner_id"] = rt_owner
            target_device["runtimes"].append({
                "id": rt["id"], "name": rt["name"], "version": rt.get("version", ""),
                "healthy": rt.get("healthy", False), "installed": rt.get("installed", False),
                "instance_name": rt.get("instance_name", rt.get("name", "")),
                # Issue #1310：本机直连按 host_name 兜底凭据名
                "worker_name": _hn,
                "health": rt.get("health", "offline"),
                "local_skills": rt.get("local_skills", []),
                "skill_scan_result": rt.get("skill_scan_result", []),
                "agent_tags": rt.get("agent_tags", []),
                "scene_tags": rt.get("scene_tags", ["default"]),
                "alias": rt.get("alias", "")})
        else:
            _local_dev_key = "__local__"
            if _local_dev_key not in _device_by_name:
                _local_dev = {"credential_id": 0, "name": _hn or "本机直连",
                    "device_name": _hn or "本机直连",
                    "credential_status": "active", "last_seen_at": None,
                    "worker_ip": "127.0.0.1", "scope": "local",
                    "owner": "", "health": "online", "runtimes": [],
                    "local_owner_id": rt_owner}
                devices.append(_local_dev)
                _device_by_name[_local_dev_key] = _local_dev
            elif not _device_by_name[_local_dev_key].get("local_owner_id"):
                # 首个有主本机引擎补充归属（Issue #473 范围筛选判定用）
                _device_by_name[_local_dev_key]["local_owner_id"] = rt_owner
            _device_by_name[_local_dev_key]["runtimes"].append({
                "id": rt["id"], "name": rt["name"], "version": rt.get("version", ""),
                "healthy": rt.get("healthy", False), "installed": rt.get("installed", False),
                "instance_name": rt.get("instance_name", rt.get("name", "")),
                # Issue #1310：本机直连按 host_name 兜底凭据名
                "worker_name": _hn,
                "health": rt.get("health", "offline"),
                "local_skills": rt.get("local_skills", []),
                "skill_scan_result": rt.get("skill_scan_result", []),
                "agent_tags": rt.get("agent_tags", []),
                "scene_tags": rt.get("scene_tags", ["default"]),
                "alias": rt.get("alias", "")})

    # 过滤空设备（无引擎且非 active 凭据）
    devices = [d for d in devices if d.get("runtimes") or (d.get("credential_id", 0) > 0 and d.get("credential_status") == "active")]

    # 本机设备 health 修正
    for d in devices:
        if d.get("device_name") == _local_hostname:
            d["health"] = "online" if any(rt.get("health") == "online" for rt in d.get("runtimes", [])) else "offline"

    return devices


# ====== Runtime Devices（设备级分页端点，Issue #185）======

@router.get("/runtime-devices", operation_id="settings_runtime_devices", summary="List runtime devices (paginated)", description="Lists devices grouped by credential_id with hosted engines. Supports pagination via page/page_size and filtering via scope/type/q. total reflects the filtered count so the footer pager stays consistent with the displayed list (Issue #245). Issue #277: repo param filters repo-scoped devices to only the current repo. Issue #473: range param narrows the viewing scope - empty/all (default), repo (repo scope only), user (current user's user-scope + own local engines); invalid range values are rejected with 422.")
def get_runtime_devices(user: dict = Depends(require_org_member), page: int = 1, page_size: int = 20,
                        scope: str = "", type: str = "", q: str = "", repo: str = "",
                        range: RTRange = ""):
    """设备级视图：按 credential_id 分组，返回每个设备及其承载的引擎列表。

    分页（Issue #185）：page/page_size 参数对设备列表切片，total 为过滤后设备总数。
    筛选（Issue #245）：scope/type/q 参数在分页切片前过滤，total 始终为筛选后总数，
    保证底部页码总条数与实际展示数据一致（修复前端二次过滤导致 total 不更新）。
    可见性过滤与 agent-runtimes 端点一致（global + personal + repo-level）。

    Issue #277 数据隔离：repo 参数非空时，repo scope 设备仅返回当前仓库的设备，
    避免跨仓库看到其他仓库的 worker 设备和引擎。

    Issue #338：个人组织视图下，个人归属 worker 设备（scope=user 且 owner=当前用户）
    不受凭据创建时绑定的 org_id 隔离（与 dashboard 个人组织视图语义一致）。
    """
    from src.user import db as user_db
    from src.session_manager import db as _db, db_lock as _dl
    import time as _time

    uid = user.get("id") or 0
    uname = user.get("username") or ""
    user_repos = {r["full_name"] for r in (user_db.get_user_repos(uid) if uid else [])}
    now = _time.time()

    # 凭据映射 + 可见性过滤（共享 helper，Issue #185 审查修复）
    # Issue #277 数据隔离：repo 参数非空时，repo scope 仅返回当前仓库的设备
    cred_map = _build_cred_map(_db, _dl)
    _visible_credential = _make_visible_credential_fn(uname, user_repos, repo_filter=repo)

    # 有效用户集合（用于过滤已删除用户的本机引擎）
    valid_users = set()
    with _dl:
        c = _db.cursor()
        try:
            c.execute("SELECT username FROM grape_accounts")  # D32-补丁
            valid_users = {r[0] for r in c.fetchall()}
        except Exception:
            pass

    # 本机引擎列表（一次性查询，传入 _build_runtime_devices 复用，消除 N+1 二次全量查询）
    _local_runtimes = user_db.get_agent_runtimes(scope=None)

    # 组织隔离：非个人组织只显示 org_id 匹配的引擎/凭据
    from src.user.org_db import is_org_personal
    _org_id = user.get("org_id", 0)
    _is_personal_org = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = 0 if _is_personal_org else _org_id
    if _filter_org_id:
        # Issue #371：非个人组织严格隔离分支为 repo scope 增加豁免（与 #338 个人组织分支同口径），
        # repo scope 设备对仓库成员可见，可见性由 _visible_credential 的 repo_key in user_repos 收敛。
        _local_runtimes = [rt for rt in _local_runtimes if (rt.get("org_id") or 0) == _filter_org_id
                           or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
        cred_map = {k: v for k, v in cred_map.items()
                    if (v.get("org_id") or 0) == _filter_org_id
                    or v.get("scope") == "repo"}
    elif _is_personal_org:
        # 个人组织：只保留 org_id=0（全局）和 org_id=自身 的凭据/引擎；
        # Issue #338：个人归属 worker 设备（scope=user 且 owner=当前用户）不受
        # 凭据创建时绑定的 org_id 隔离（与 dashboard 个人组织视图语义一致）
        # Issue #430：repo scope 设备豁免（与 #371 非个人组织分支同口径）——
        # 仓库级设备对仓库成员可见，可见性由 _visible_credential 的 repo_key in
        # user_repos 判定收敛，不越权。
        _local_runtimes = [rt for rt in _local_runtimes if (rt.get("org_id") or 0) == 0
                           or (rt.get("org_id") or 0) == _org_id
                           or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
        cred_map = {k: v for k, v in cred_map.items()
                    if (v.get("org_id") or 0) in (0, _org_id)
                    or v.get("scope") == "repo"
                    or (v.get("scope") == "user" and (v.get("owner_name") or "") == uname)}

    # 构建设备列表（共享 helper）
    devices = _build_runtime_devices(_db, _dl, cred_map, _visible_credential,
                                     valid_users, _local_runtimes, now)

    # Issue #245: 筛选从迁移到后端——前端二次过滤会让 _rtDevTotal 与展示数据不一致
    # avail_scopes / avail_types 取自全部可见设备（不依赖当前筛选/页），供前端生成筛选标签，
    # 避免后端过滤后当前页只剩单一 scope/type 导致筛选标签消失无法切换
    _avail_scopes = list(set(
        ("local" if d.get("credential_id") == 0 else (d.get("scope") or "global"))
        for d in devices
    ))
    _avail_types = list(set(
        _t for d in devices for _t in _device_engine_types(d)
    ))

    # Issue #473 范围筛选：全部（默认）/ 仓库 / 个人，先于 type/q/scope 与分页（total 为组合筛选后总数）
    # PR #668 审查修复：本机设备（credential_id=0）归属按 local_owner_id（引擎 DB 行 owner_id）
    if range:
        devices = [d for d in devices
                   if _rt_in_range(d.get("scope") or "global", d.get("owner") or "",
                                   (d.get("credential_id") or 0) == 0, range, uname,
                                   local_owner=d.get("local_owner_id") or "")]

    # type 筛选：设备任一引擎名 includes(type)（小写，与前端 _availTypes 逻辑一致）
    if type:
        _tlow = type.lower()
        devices = [d for d in devices if any(
            _tlow in (r.get("name") or "").lower() for r in d.get("runtimes", [])
        )]

    # q 搜索：匹配 name/device_name/owner/worker_ip/runtimes[].name（与前端原 haystack 一致）
    if q:
        _qlow = q.lower()
        devices = [d for d in devices if _qlow in " ".join([
            d.get("name") or "", d.get("device_name") or "", d.get("owner") or "",
            d.get("worker_ip") or "",
            " ".join((r.get("name") or "") for r in d.get("runtimes", []))
        ]).lower()]

    # scope 筛选（credential_id=0 → local，否则取 d.scope；与 renderWorkers 原前端逻辑一致）
    if scope:
        devices = [d for d in devices if (
            ("local" if d.get("credential_id") == 0 else (d.get("scope") or "global")) == scope
        )]

    # 分页切片（total 为筛选后总数）
    total = len(devices)
    # Issue #472 全局口径：统计栏数据源 = 筛选后整体搜索结果的健康分布（与分页解耦）
    health_stats = _health_stats(devices)
    if page_size and page_size > 0:
        _page = max(page, 1)
        _offset = (_page - 1) * page_size
        paged_devices = devices[_offset:_offset + page_size]
        return {"devices": paged_devices, "total": total, "page": _page, "page_size": page_size,
                "avail_scopes": _avail_scopes, "avail_types": _avail_types,
                "health_stats": health_stats}
    return {"devices": devices, "total": total,
            "avail_scopes": _avail_scopes, "avail_types": _avail_types,
            "health_stats": health_stats}


def _device_engine_types(d: dict) -> list:
    """从设备的引擎列表派生引擎类型标签（与前端 _availTypes 的 includes 判定一致）。"""
    _KNOWN = [("hermes", "hermes"), ("opencode", "opencode"),
              ("codearts", "codearts"), ("codex", "codex")]
    found = []
    for rt in d.get("runtimes", []):
        n = (rt.get("name") or "").lower()
        for _kw, _label in _KNOWN:
            if _kw in n and _label not in found:
                found.append(_label)
    return found


# 兼容别名：旧路径 /backend-agents（≥2 发布周期，见 agent-runtime-design.md §6.2）
@router.get("/backend-agents", operation_id="settings_backend_agents", summary="List backend agents (legacy alias)", description="Legacy alias for listing agent runtimes.")
def get_backend_agents(user: dict = Depends(require_no_session_token)):
    cached = user_db_get_backend_agents_alias()
    return {"backends": cached}


def user_db_get_backend_agents_alias():
    from src.user import db as user_db
    return user_db.get_backend_agents()


@router.get("/agent-runtimes/{rid}", operation_id="settings_agent_runtime_detail", summary="Get agent runtime detail", description="Returns a single agent runtime by id with full engine-modal fields (health/health_score/tags/local_skills/alias/worker_name/worker_ip/scope/owner). Visibility check reuses the agent-runtimes list logic (credential scope + org isolation + repo filter); invisible or missing ids return 404. Issue #364: frontend fallback for engines visible in the Workers device view but not in the Engines list page.")
def get_agent_runtime_detail(rid: int, user: dict = Depends(require_org_member), repo: str = ""):
    """单引擎详情（Issue #364）：按 agent_runtimes.id 精确返回。

    复用 get_agent_runtimes 的全量可见性过滤与字段派生（page_size=0 全量口径），
    保证详情与 Engines 列表可见性 100% 一致（credential scope + org 隔离 +
    repo 过滤 + 已删除用户/已吊销凭据过滤），未命中/不可见统一 404。
    """
    result = get_agent_runtimes(user=user, page=1, page_size=0, scope="", repo=repo)
    for rt in result.get("runtimes", []):
        if rt.get("id") == rid:
            return rt
    raise HTTPException(status_code=404, detail={"code": 404, "message": "引擎不存在或不可见"})


@router.post("/agent-runtimes/{rid}/health-reset", operation_id="settings_agent_runtime_health_reset", summary="Reset runtime health score", description="Resets a single runtime engine's health score (Issue #394). Writes agent_runtimes.health_reset_at as the pardon point so the health-score window restarts from the reset moment; previously accumulated failures are pardoned. Only repo-scope (repo members) and personal-scope (owner) worker engines are resettable; global engines return 403. Invisible or missing ids return 404.")
def reset_agent_runtime_health(rid: int, user: dict = Depends(require_org_member), repo: str = ""):
    """重置单个引擎的健康分（Issue #394）。

    权限：
    - 复用 get_agent_runtimes 的全量可见性过滤（与详情端点同口径），引擎不可见 → 404；
    - 仅仓库级（scope=repo）与个人级（scope=user）worker 设备引擎开放重置，
      全局引擎（scope=global）不开放 → 403。
    操作：写入 health_reset_at 赦免点 → 清缓存 → 强制重算（health_scorer.reset_health），
    返回重置后的健康分数据；log 审计留痕（operator / rid / 重置点 / 新健康分）。
    """
    result = get_agent_runtimes(user=user, page=1, page_size=0, scope="", repo=repo)
    rt = None
    for item in result.get("runtimes", []):
        if item.get("id") == rid:
            rt = item
            break
    if rt is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "引擎不存在或不可见"})
    if (rt.get("scope") or "global") not in ("user", "repo"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅仓库级/个人级 worker 设备的引擎支持重置健康分，全局引擎不开放"})
    from src.runtime.health_scorer import reset_health
    data = reset_health(rid)
    if data is None:
        log.warning(f"health-reset failed for runtime={rid} operator={user.get('username') or user.get('id')}")
        raise HTTPException(status_code=503, detail={"code": 503, "message": "重置健康分失败，请稍后重试"})
    log.info(f"health-reset ok runtime={rid} operator={user.get('username') or user.get('id')} "
             f"health_reset_at={data.get('updated_at')} health_score={data.get('health_score')}")
    return {"runtime_id": rid, "health_reset_at": data.get("health_reset_at"),
            "health_score": data.get("health_score")}


@router.post("/agent-runtimes/refresh", operation_id="settings_agent_runtimes_refresh", summary="Refresh agent runtimes", description="Manually refreshes executors: discovers local engines, child-process probe, and persists to DB.")
def refresh_agent_runtimes(user: dict = Depends(require_org_member)):
    """手动刷新：发现本地引擎 + 子进程检测 + 写入 DB（探测统一走 local_probe，方案 C）

    P0 修复：save_agent_runtimes 可能因 MySQL Lock wait timeout (1205) 抛异常，
    这里接住返回 500 JSON 而非让异常冒泡导致 uvicorn 进程崩溃（→ 502）。
    """
    from src.runtime.registry import discover_runtimes
    from src.runtime.local_probe import probe_local_engines
    from src.runtime.agent import _scan_local_skills
    from src.user import db as user_db
    from src.config import log
    import socket as _sock
    _hostname = _sock.gethostname()
    try:
        discovered = discover_runtimes()
        results = probe_local_engines(discovered)

        # 扫描本机 local_skills 详情并注入
        from src.runtime.agent import _scan_local_skills_with_detail
        for r in results:
            _name = (r.get("name") or "").strip().lower()
            r["local_skills_detail"] = _scan_local_skills_with_detail(_name)
            r["local_skills"] = [s["name"] for s in r["local_skills_detail"]]

        # Only persist runtimes that are actually installed
        results = [r for r in results if r["installed"]]
        user_db.save_agent_runtimes(results, host_name=_hostname)
        return {"runtimes": results}
    except Exception as e:
        log.warning(f"refresh_agent_runtimes failed: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=f"刷新引擎状态失败: {e}")

# 兼容别名：旧路径 /backend-agents/refresh
@router.post("/backend-agents/refresh", operation_id="settings_backend_agents_refresh", summary="Refresh backend agents (legacy alias)", description="Legacy alias for refreshing agent runtimes.")
async def refresh_backend_agents(user: dict = Depends(require_org_member)):
    return refresh_agent_runtimes(user)


# ====== Runtime Credentials（rta_ token，见 agent-runtime-event-full-design.md §4.1 / §10）======

_SCOPE_ALIAS = {"user": "user", "个人": "user", "repo": "repo", "仓库": "repo", "global": "global", "全局": "global"}


class RuntimeCredBody(BaseModel):
    name: str
    scope: str = "user"       # user(个人)/repo(仓库)/global(全局)，默认个人
    repo_key: str = ""        # scope=repo 时必填（目标仓库全名，如 owner/repo）


def _normalize_scope(scope: str) -> str:
    s = _SCOPE_ALIAS.get((scope or "").strip().lower())
    if not s:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 user/repo/global（个人/仓库/全局）"})
    return s


@router.post("/runtime-credentials", operation_id="settings_runtime_credential_create", summary="Create runtime credential", description="Creates an rta_ credential; plaintext shown once, DB stores sha256. owner = current user (not selectable); scope defaults to personal.")
def create_runtime_credential(body: RuntimeCredBody, user: dict = Depends(require_org_member)):
    """生成 rta_ token：仅展示一次明文，DB 存 sha256。owner=当前登录用户（不可选），scope 默认个人。

    组织隔离：凭据创建时绑定当前组织 org_id。
    """
    import hashlib, secrets
    from src.session_manager import db, db_lock
    from src.user.org_db import is_org_personal
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "凭据名称不能为空"})
    scope = _normalize_scope(body.scope)
    owner = user.get("username") or ""
    repo_key = ""
    if scope in ("repo", "user"):
        raw_key = (body.repo_key or "").strip()
        if scope == "repo" and not raw_key:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库范围凭据必须指定 repo_key"})
        if raw_key:
            repo_key = _resolve_repo_key(raw_key)
    # 解析当前组织 org_id
    _org_id = user.get("org_id", 0)
    _is_personal = is_org_personal(_org_id) if _org_id else True
    _cred_org_id = 0 if _is_personal else _org_id
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT INTO runtime_credentials (token_hash, name, scope, owner_id, owner_name, repo_key, org_id) VALUES (?,?,?,?,?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), name, scope, repo_key if scope == "repo" else "", owner, repo_key, _cred_org_id))
            cred_id = c.lastrowid
            db.commit()
        except Exception as e:
            msg = str(e)
            if ("UNIQUE" in msg or "1062" in msg or "Duplicate" in msg) and "runtime_credentials.name" in msg:
                # sqlite: "UNIQUE constraint failed: ..."; MySQL: (1062, "Duplicate entry ... for key 'runtime_credentials.name'")
                # 只认 "UNIQUE" 字样在 MySQL 下匹配不到 → 误报 500（2026-08-17 实测）
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"凭据名称已存在（全局唯一）: {name}"})
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"生成失败: {e}"})
    # 预构建 worker 安装包（异步）：凭据生成后即可一键安装，避免首次下载才打包
    # （首次构建含依赖 wheels 下载，耗时数秒至数分钟，放后台不阻塞响应）
    try:
        from src.worker_package import ensure_package_ready
        ensure_package_ready()
    except Exception as _e:
        log.warning(f"预构建 worker 安装包失败: {_e}")
    return {"token": token, "id": cred_id, "name": name, "scope": scope, "owner": owner, "message": "token 仅展示一次，请立即保存"}


@router.put("/runtime-credentials/{cid}", operation_id="settings_runtime_credential_rename", summary="Rename runtime credential", description="Renames a credential (worker name); globally unique, duplicates rejected. owner/scope unchanged.")
def rename_runtime_credential(cid: int, body: RuntimeCredBody, user: dict = Depends(require_no_session_token)):
    """改名凭据（worker 名）：全局唯一，重名拒绝。owner/scope 不可改。"""
    from src.session_manager import db, db_lock
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "凭据名称不能为空"})
    with db_lock:
        c = db.cursor()
        try:
            c.execute("UPDATE runtime_credentials SET name=? WHERE id=? AND status='active'", (name, cid))
            if c.rowcount == 0:
                row = c.execute("SELECT id FROM runtime_credentials WHERE id=?", (cid,)).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
                raise HTTPException(status_code=400, detail={"code": 400, "message": "仅 active 凭据可改名"})
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            msg = str(e)
            if ("UNIQUE" in msg or "1062" in msg or "Duplicate" in msg) and "runtime_credentials.name" in msg:
                # sqlite: "UNIQUE constraint failed: ..."; MySQL: (1062, "Duplicate entry ... for key 'runtime_credentials.name'")
                # 只认 "UNIQUE" 字样在 MySQL 下匹配不到 → 误报 500（2026-08-17 实测）
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"凭据名称已存在（全局唯一）: {name}"})
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"改名失败: {e}"})
    return {"message": f"凭据已改名为 {name}"}


class CredScopeBody(BaseModel):
    scope: str = "user"
    repo_key: str = ""


@router.put("/runtime-credentials/{cid}/scope", operation_id="settings_runtime_credential_scope", summary="Change credential scope", description="Switches a credential scope (repo/personal). Owner-only, active-only; global is not allowed.")
def change_credential_scope(cid: int, body: CredScopeBody, user: dict = Depends(require_org_member)):
    """切换凭据 scope（仓库/个人）。仅 owner 可操作，仅 active 凭据可改。不允许设为 global。"""
    from src.session_manager import db, db_lock
    scope = _normalize_scope(body.scope)
    if scope == "global":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "不允许设为全局，仅可切换仓库/个人"})
    raw_key = (body.repo_key or "").strip()
    if scope == "repo" and not raw_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库 scope 需填写 repo_key"})
    repo_key = _resolve_repo_key(raw_key) if raw_key else ""
    # Issue #371：切换 scope 时同步 org_id，消除「scope 已改但 org_id 仍是旧归属」的脏数据。
    # - repo scope：org_id 对齐到仓库所属组织（managed_repos.org_id 权威单一来源），无组织归属时置 0；
    #   这样非个人组织视图按 org_id 过滤时 repo scope 引擎不会被错误滤除。
    # - user scope：org_id 对齐到当前操作者所在组织（用户在该组织下操作，设备应在该组织下可见）；
    #   个人组织操作时 org_id=个人组织 ID，非个人组织操作时 org_id=该组织 ID。
    _new_org_id = 0
    if scope == "repo" and repo_key:
        try:
            from src.user.org_db import get_org_id_by_repo_key
            _new_org_id = get_org_id_by_repo_key(repo_key)
        except Exception as _e:
            log.warning(f"change_credential_scope: resolve org_id for repo_key={repo_key} failed: {_e}")
            _new_org_id = 0
    elif scope == "user":
        _new_org_id = user.get("org_id") or 0
    with db_lock:
        c = db.cursor()
        c.execute("SELECT owner_name, status FROM runtime_credentials WHERE id=?", (cid,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
        _owner, _status = row[0] or "", row[1] or ""
        if _status != "active":
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仅 active 凭据可改 scope"})
        _uname = user.get("username") or ""
        if _owner != _uname:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅凭据 owner 可修改 scope"})
        # Issue #371：同步 scope/repo_key/owner_id/org_id 一次性落库。
        # - repo scope：owner_id=repo_key（与 worker 注册路径一致，Workers 页对 repo 设备的可见性
        #   判断走 from_repo_key(owner_id) in user_repos，owner_id 为空 → 同仓库其他成员永远看不到）；
        # - user scope：owner_id 清空（user scope 注册时按 owner_name 从 grape_accounts 重算，不依赖此字段）。
        _new_owner_id = repo_key if (scope == "repo" and repo_key) else ""
        # Issue #654：user scope 引擎行 owner_id 与 register 同源（按 owner_name 解析
        # grape_accounts.id），消除「切换置空、注册重算」两条路径的语义漂移；
        # 解析不到回退空串（兼容老数据，register 侧同样回退）。
        _engine_owner_id = _new_owner_id
        if scope == "user" and _owner:
            try:
                c.execute("SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?) LIMIT 1", (_owner,))
                _urow = c.fetchone()
                if _urow:
                    _engine_owner_id = str(_urow[0])
            except Exception as _e:
                log.warning(f"change_credential_scope: resolve owner_id for user scope failed: {_e}")
        c.execute("UPDATE runtime_credentials SET scope=?, repo_key=?, owner_id=?, org_id=? WHERE id=?",
                  (scope, repo_key or None, _new_owner_id, _new_org_id, cid))
        # 切换 scope 后同步更新引擎行（保留引擎信息，避免 worker 需要重新注册）。
        # Issue #654：先按 (credential_id, lower(name)) 去重合并（保留 MAX(id)）：
        # 被替换行的 engine_capabilities / tags_user_set 迁移到存活行（priority/标签不回默认），
        # 引用旧引擎 id 的配置（场景 engine_runtime_ids / agent backend）由 remap 改指向存活行。
        from src.runtime.engine_select import merge_duplicate_engine_rows, remap_engine_references
        _rid_map = merge_duplicate_engine_rows(cid, c)
        c.execute(
            "UPDATE agent_runtimes SET scope=?, owner_id=?, org_id=?, repo_key=? WHERE credential_id=?",
            (scope, _engine_owner_id, _new_org_id, repo_key or None, cid)
        )
        db.commit()
    # Issue #654：引擎行替换时统一 remap 引用（自身事务，锁外执行避免交叉提交），
    # 并输出结构化变更事件（cid、影响引擎数、旧→新 id 映射）。
    if _rid_map:
        try:
            remap_engine_references(_rid_map, cid, reason="credential_scope_change")
        except Exception as _e:
            log.warning(f"change_credential_scope: remap references failed: {_e}")
    import json as _json654
    log.info("engine_change_event: " + _json654.dumps({
        "event": "credential_scope_changed", "credential_id": cid, "scope": scope,
        "repo_key": repo_key, "org_id": _new_org_id,
        "engines_affected": len(_rid_map),
        "id_mapping": {str(k): v for k, v in _rid_map.items()},
    }, ensure_ascii=False))
    log.info(f"Credential scope changed: cid={cid} scope={scope} repo_key={repo_key} org_id={_new_org_id}")
    return {"scope": scope, "repo_key": repo_key}


@router.get("/runtime-credentials", operation_id="settings_runtime_credentials", summary="List runtime credentials", description="Lists runtime credentials without token_hash/plaintext.")
def list_runtime_credentials(user: dict = Depends(require_org_member)):
    """凭据列表（不含 token_hash / 明文），按组织过滤。

    非个人组织只显示 org_id 匹配的凭据（不含 global）；
    个人组织显示全部（兼容现有行为）。
    """
    from src.session_manager import db, db_lock
    from src.user.org_db import is_org_personal
    org_id = user.get("org_id", 0)
    _is_personal = is_org_personal(org_id) if org_id else True
    with db_lock:
        c = db.cursor()
        if org_id and not _is_personal:
            # 非个人组织：严格隔离，只显示 org_id 匹配的凭据
            c.execute("SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, created_at, repo_key FROM runtime_credentials WHERE org_id=? ORDER BY id DESC", (org_id,))
        else:
            # 个人组织或未指定：显示全部（兼容现有行为）
            c.execute("SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, created_at, repo_key FROM runtime_credentials ORDER BY id DESC")
        return {"credentials": [
            {"id": r[0], "name": r[1], "device_name": r[2] or "", "scope": r[3],
             "owner_id": r[4], "owner": r[5] or "", "status": r[6], "last_seen_at": normalize_event_time(r[7]), "created_at": normalize_event_time(r[8]),
             "repo_key": from_repo_key(r[9] or "")}
            for r in c.fetchall()
        ]}


def _cleanup_scene_engine_runtime_ids(rid_list: list):
    """清理场景配置中引用了即将被删除的 runtime_id 的 engine_runtime_ids（Issue #250）。

    扫描 devops_pipeline_scene 所有活跃场景的 nodes_json，
    从每个 node 的 engine_runtime_ids 中移除在 rid_list 中的 ID，回写 DB。

    引擎绑定真实性同步：实现已抽至 engine_select.cleanup_scene_engine_bindings（与
    离线超阈值解绑共用），本函数保留为薄包装（调用点不变，行为等价）。
    """
    from src.runtime.engine_select import cleanup_scene_engine_bindings
    cleanup_scene_engine_bindings(rid_list)


@router.post("/runtime-credentials/{cid}/revoke", operation_id="settings_runtime_credential_revoke", summary="Revoke runtime credential", description="Revokes and deletes a worker credential: removes the credential row and cascades its agent_runtimes; runtime_session bindings are reset.")
def revoke_runtime_credential(cid: int, user: dict = Depends(require_no_session_token)):
    """吊销并删除 worker 记录：删 runtime_credentials 行 + 级联删其 agent_runtimes 行。

    2026-08-17 用户指示「吊销=删除」：删除后 token 即刻失效（_auth_credential
    查不到 → 401）、凭据名释放可复用（全局唯一键不再被 revoked 行占用）。
    级联清理：
    - agent_runtimes：该凭据注册的引擎行一并删除
    - devops_agent_session.runtime_id：置 0（dashboard 红字「无 engine」，如实反映）
    - devops_pipeline_scene.nodes_json：清理 engine_runtime_ids 中对被删 runtime 的悬空引用（Issue #250）
    历史 agent_tasks 绑定旧 runtime_id 的任务恢复走 /api/events/{seq}/redispatch
    （pick_engine 重新绑定后 requeue，见 engine-selection-claim-pool-2026-08.md）。
    """
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM runtime_credentials WHERE id=?", (cid,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
        c.execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid,))
        rid_list = [r[0] for r in c.fetchall()]
        if rid_list:
            ph = ",".join("?" for _ in rid_list)
            try:
                c.execute(f"UPDATE devops_agent_session SET runtime_id=0 WHERE runtime_id IN ({ph})", rid_list)
            except Exception as _e:
                log.warning(f"revoke {cid}: 清理 session 失败: {_e}")
        # Issue #250: 物理删除 agent_runtimes 前清理场景配置 engine_runtime_ids 悬空引用
        _cleanup_scene_engine_runtime_ids(rid_list)
        # Issue #417：级联清理 engine_capabilities（否则悬空能力标签残留）
        if rid_list:
            try:
                _ph_ec = ",".join("?" for _ in rid_list)
                c.execute(f"DELETE FROM engine_capabilities WHERE runtime_id IN ({_ph_ec})", rid_list)
            except Exception as _ece:
                log.warning(f"revoke {cid}: 清理 engine_capabilities 失败: {_ece}")
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()
    return {"message": f"凭据 {cid} 已吊销并删除"}


class ExecModeBody(BaseModel):
    mode: str


@router.get("/execution-mode", operation_id="settings_execution_mode_get", summary="Get execution mode", description="Returns the current execution mode.")
def get_execution_mode_api(user: dict = Depends(require_no_session_token)):
    from src.runtime.service import get_execution_mode
    return {"mode": get_execution_mode()}


@router.get("/pipeline-mode", operation_id="settings_pipeline_mode_get", summary="Get pipeline mode", description="Returns the current pipeline mode.")
def get_pipeline_mode_api(user: dict = Depends(require_no_session_token)):
    from src.api.event_pipeline import is_plugin_mode
    return {"mode": "plugin" if is_plugin_mode() else "legacy"}


@router.put("/pipeline-mode", operation_id="settings_pipeline_mode_set", summary="Set pipeline mode", description="Sets the pipeline mode.")
def set_pipeline_mode_api(body: dict, user: dict = Depends(require_no_session_token)):
    from src.user.db import set_meta_value
    mode = (body.get("mode") or "").strip().lower()
    if mode not in ("plugin", "legacy"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "mode 必须为 plugin 或 legacy"})
    set_meta_value("pipeline_mode", mode)
    return {"message": f"管线模式已切换为 {'插件模式' if mode == 'plugin' else '传统模式'}", "mode": mode}


@router.put("/execution-mode", operation_id="settings_execution_mode_set", summary="Set execution mode", description="Sets the execution mode.")
def set_execution_mode_api(body: ExecModeBody, user: dict = Depends(require_no_session_token)):
    from src.runtime.service import set_execution_mode
    if body.mode not in ("auto", "direct", "queue_remote"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "mode 必须为 auto/direct/queue_remote（queue_local 已废除）"})
    set_execution_mode(body.mode)
    return {"message": f"执行模式已切换为 {body.mode}", "mode": body.mode}


# ===== 引擎标签管理（event-dispatch-redesign-v2.md §2.4）=====

class EngineTagBody(BaseModel):
    tag: str
    priority: int = 50


class EngineTagUpdateBody(BaseModel):
    priority: int = 50
    enabled: int = 1


def _sync_agent_tags(rid: int, cursor):
    """engine_capabilities 变更后，回写 agent_runtimes.agent_tags（JSON 快照）。

    agent_tags 是 engine_capabilities 中 enabled=1 的 tag 列表的 JSON 表示，
    供前端展示用。此函数确保两表一致。
    """
    cursor.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag", (rid,))
    tags = [r[0] for r in cursor.fetchall()]
    cursor.execute("UPDATE agent_runtimes SET agent_tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), rid))


@router.get("/agent-runtimes/{rid}/tags", operation_id="settings_engine_tags", summary="List engine tags", description="Lists an engine's tags.")
def get_engine_tags(rid: int, user: dict = Depends(require_no_session_token)):
    """获取引擎的标签列表"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT id, tag, priority, enabled, created_at, updated_at "
                "FROM engine_capabilities WHERE runtime_id=? ORDER BY tag",
                (rid,)
            )
            rows = c.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    return {"tags": [
        {"id": r[0], "tag": r[1], "priority": r[2],
         "enabled": bool(r[3]), "created_at": r[4], "updated_at": r[5]}
        for r in rows
    ]}


@router.post("/agent-runtimes/{rid}/tags", operation_id="settings_engine_tag_create", summary="Add engine tag", description="Adds an engine tag (idempotent: updates if present).")
def add_engine_tag(rid: int, body: EngineTagBody, user: dict = Depends(require_no_session_token)):
    """为引擎添加标签（幂等：已有则更新）"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    tag = (body.tag or "").strip().lower()
    if tag not in ("vod", "issue", "coding", "qa", "sre", "chat"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"无效标签: {tag}，可选: vod/issue/coding/qa/sre/chat"})
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT 1 FROM agent_runtimes WHERE id=?", (rid,))
            if not c.fetchone():
                raise HTTPException(status_code=404, detail={"code": 404, "message": "引擎不存在"})
            c.execute(
                "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                "VALUES (?,?,1,?,1,?,?) "
                "ON DUPLICATE KEY UPDATE priority=VALUES(priority), updated_at=VALUES(updated_at)",
                (rid, tag, body.priority, now, now)
            )
            _sync_agent_tags(rid, c)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            # MySQL ON DUPLICATE KEY may not work with SQLite syntax; fallback
            try:
                c.execute(
                    "REPLACE INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                    "VALUES (?,?,1,?,1,?,?)",
                    (rid, tag, body.priority, now, now)
                )
                _sync_agent_tags(rid, c)
                db.commit()
            except Exception as e2:
                db.rollback()
                raise HTTPException(status_code=500, detail={"code": 500, "message": str(e2)})
    log.info(f"Engine tag added: runtime={rid} tag={tag} priority={body.priority}")
    return {"message": f"标签 {tag} 已添加", "tag": tag}


@router.put("/agent-runtimes/{rid}/tags/{tag}", operation_id="settings_engine_tag_update", summary="Update engine tag", description="Updates an engine tag configuration.")
def update_engine_tag(rid: int, tag: str, body: EngineTagUpdateBody,
                            user: dict = Depends(require_no_session_token)):
    """修改引擎标签配置"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    tag = tag.strip().lower()
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "UPDATE engine_capabilities SET priority=?, enabled=?, updated_at=? "
                "WHERE runtime_id=? AND tag=?",
                (body.priority, body.enabled, now, rid, tag)
            )
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "标签不存在"})
            _sync_agent_tags(rid, c)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    return {"message": f"标签 {tag} 已更新"}


@router.delete("/agent-runtimes/{rid}/tags/{tag}", operation_id="settings_engine_tag_delete", summary="Delete engine tag", description="Deletes an engine tag.")
def delete_engine_tag(rid: int, tag: str, user: dict = Depends(require_no_session_token)):
    """删除引擎标签"""
    from src.session_manager import db, db_lock
    tag = tag.strip().lower()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag=?", (rid, tag))
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "标签不存在"})
            _sync_agent_tags(rid, c)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    log.info(f"Engine tag deleted: runtime={rid} tag={tag}")
    return {"message": f"标签 {tag} 已删除"}


# ===== Issue #49：Webhook 事件 action 过滤规则（event_action_filter 管理）=====

class EventActionFilterBody(BaseModel):
    event_type: str
    action: str
    allow: int = 0              # 0=过滤 1=放行 2=副作用-only
    actor: str = "any"          # user / bot / any
    require_changes: str = ""   # JSON 数组字符串，如 '["description"]'
    block_changes: str = ""     # JSON 数组字符串，命中即过滤，如 '["assignees","labels"]'
    handle_side_effect: str = ""
    priority: int = 0
    enabled: int = 1


class EventActionFilterUpdateBody(BaseModel):
    allow: int | None = None
    actor: str | None = None
    require_changes: str | None = None
    block_changes: str | None = None
    handle_side_effect: str | None = None
    priority: int | None = None
    enabled: int | None = None


@router.get("/event-action-filters", operation_id="settings_event_action_filters", summary="List event action filter rules", description="Lists all event action filter rules (hot-reloadable).")
def list_event_action_filters(user: dict = Depends(require_no_session_token)):
    """查看全部事件 action 过滤规则（可配置热生效）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            # 兼容旧表（无 block_changes）
            cols = [r[1] for r in c.execute("PRAGMA table_info(event_action_filter)").fetchall()]
            if "block_changes" in cols:
                c.execute("SELECT id, event_type, action, allow, actor, require_changes, block_changes, handle_side_effect, "
                          "priority, enabled FROM event_action_filter ORDER BY event_type, action")
            else:
                c.execute("SELECT id, event_type, action, allow, actor, require_changes, '', handle_side_effect, "
                          "priority, enabled FROM event_action_filter ORDER BY event_type, action")
            rows = c.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    return {"rules": [
        {"id": r[0], "event_type": r[1], "action": r[2], "allow": r[3], "actor": r[4] or "any",
         "require_changes": r[5] or "", "block_changes": r[6] or "", "handle_side_effect": r[7] or "",
         "priority": r[8], "enabled": r[9]}
        for r in rows
    ]}


@router.post("/event-action-filters", operation_id="settings_event_action_rule_create", summary="Add event action filter rule", description="Adds an event action filter rule (idempotent: skips an existing event_type x action combo).")
def add_event_action_rule(body: EventActionFilterBody, user: dict = Depends(require_no_session_token)):
    """新增事件 action 过滤规则（幂等：同 event_type×action 已存在则跳过）。"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT 1 FROM event_action_filter WHERE event_type=? AND action=?", (body.event_type, body.action))
            if c.fetchone():
                raise HTTPException(status_code=409, detail={"code": 409, "message": "该事件/动作规则已存在"})
            c.execute(
                "INSERT INTO event_action_filter (event_type, action, allow, actor, require_changes, block_changes, handle_side_effect, priority, enabled, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (body.event_type, body.action, body.allow, body.actor, body.require_changes,
                 body.block_changes, body.handle_side_effect, body.priority, body.enabled, now, now))
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    return {"message": "规则已添加", "event_type": body.event_type, "action": body.action}


@router.put("/event-action-filters/{rule_id}", operation_id="settings_event_action_rule_update", summary="Update event action filter rule", description="Updates an event action filter rule (allow/actor/changes/side-effect/priority/enabled; hot within 30s).")
def update_event_action_rule(rule_id: int, body: EventActionFilterUpdateBody,
                             user: dict = Depends(require_no_session_token)):
    """修改事件 action 过滤规则（改 allow/actor/changes/副作用/优先级/启用，热生效 ≤30s）。"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    sets, params = [], []
    for field in ("allow", "actor", "require_changes", "block_changes", "handle_side_effect", "priority", "enabled"):
        val = getattr(body, field)
        if val is not None:
            sets.append(f"{field}=?")
            params.append(val)
    if not sets:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无可更新字段"})
    sets.append("updated_at=?")
    params.append(now)
    params.append(rule_id)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"UPDATE event_action_filter SET {', '.join(sets)} WHERE id=?", params)
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "规则不存在"})
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    # 失效缓存，让新规则立即生效
    try:
        from src.api import event_rule
        event_rule.load_event_action_rule(force=True)
    except Exception:
        pass
    return {"message": "规则已更新"}


@router.delete("/event-action-filters/{rule_id}", operation_id="settings_event_action_rule_delete", summary="Delete event action filter rule", description="Deletes an event action filter rule (no rules = allow all).")
def delete_event_action_rule(rule_id: int, user: dict = Depends(require_no_session_token)):
    """删除事件 action 过滤规则（删除后无规则=放行）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM event_action_filter WHERE id=?", (rule_id,))
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "规则不存在"})
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    try:
        from src.api import event_rule
        event_rule.load_event_action_rule(force=True)
    except Exception:
        pass
    return {"message": "规则已删除"}


# ===== 内测申请代理（转发到 Admin Console）=====
class BetaApplyBody(BaseModel):
    accountType: str
    accountName: str
    email: str = ""
    identityType: str = ""
    phone: str = ""
    reason: str = ""
    surveyData: dict = {}


@router.post("/beta-apply/submit")
def beta_apply_submit(body: BetaApplyBody):
    """第三方内测申请提交——转发到 Admin Console 的 /api/admin/beta-apply/submit。"""
    import httpx
    from src.config import ADMIN_CONSOLE_URL, ADMIN_BETA_APPLY_API_KEY
    if not ADMIN_CONSOLE_URL:
        raise HTTPException(status_code=503, detail={"code": 503, "message": "内测申请服务未配置"})
    if not ADMIN_BETA_APPLY_API_KEY:
        raise HTTPException(status_code=503, detail={"code": 503, "message": "内测申请服务未配置"})
    try:
        resp = httpx.post(
            f"{ADMIN_CONSOLE_URL}/api/admin/beta-apply/submit",
            headers={"X-API-Key": ADMIN_BETA_APPLY_API_KEY, "Content-Type": "application/json"},
            json={
                "accountType": body.accountType,
                "accountName": body.accountName,
                "email": body.email,
                "identityType": body.identityType,
                "phone": body.phone,
                "reason": body.reason,
                "surveyData": body.surveyData,
            },
            timeout=10.0
        )
        return resp.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail={"code": 504, "message": "内测申请服务超时"})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"内测申请服务不可达: {e}"})
