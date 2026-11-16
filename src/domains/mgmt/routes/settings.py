"""Settings 路由：凭据、通知偏好、GitCode Repo、Backend Runtimes"""

import json

from datetime import datetime, timedelta

from typing import Literal

from fastapi import APIRouter, HTTPException, Depends, Request, Response

from pydantic import BaseModel

from src.kernel.host_identity import engine_host  # Issue #2094: 引擎/设备身份（GRAPE_HOSTNAME 可覆盖）
from src.domains.mgmt.user import db as user_db

from src.api.deps import require_no_session_token, require_org_member

from src.kernel.config import log

from src.api.time_utils import normalize_event_time

from src.kernel.repo_key import from_repo_key
# 运行域表（引擎/凭据/能力/群绑定）的读写一律走 owner 域 public 的具名查询与原语
from src.domains.runtime import public as _rt

router = APIRouter(prefix="/api/settings", tags=["settings"])

def _check_login_method_before_unbind(user_id: str, provider: str):
    """P0-4: 手动令牌解绑前检查至少保留一种登录方式。

    与 grape_auth.py 的 unbind_provider 保持一致：
    - 有密码 → 允许解绑
    - 无密码但有其他活跃绑定（identity_count > 1）→ 允许解绑
    - 无密码且只有当前这一条绑定 → 拒绝解绑
    """
    from src.domains.mgmt.auth.grape_account_db import has_password, count_user_identities
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
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(owner_repo)
    if ":" in owner_repo:
        return rk
    try:
        from src.kernel.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            from src.kernel.repo_key import repo_key_candidates
            _rks = tuple(repo_key_candidates(owner_repo))
            row = c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({','.join('?' * len(_rks))}) LIMIT 1",
                            _rks).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return rk

@router.get("/credentials", operation_id="settings_credentials_get", summary="Get third-party credential status", description="Returns the status of third-party credentials (VCS platform PATs from the platform-card catalog, plus huawei-cloud/codearts) for the current user. Credential secrets (AK/SK/tokens) are never returned in plaintext; only existence flags are exposed.")
def get_credentials(user: dict = Depends(require_no_session_token), repo_key: str = ""):
    # D32: 从 user_credentials 表 + external_identities 读凭据，不查 user_accounts
    from src.api.deps import get_user_token_for
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms as _lvcs
    _vcs_platform_list = list(_lvcs())  # D102-P 统一：全平台走泛型 <p>_pat 存在性键
    # S63 守卫收口（LABEL_COLOR 同款契约开关派生）：存量专用字段平台（表单键
    # <p>_token，provider 模块声明 SETTINGS_TOKEN_FIELD_SUFFIX="token"）的 PAT
    # 存在性回退不按平台名比较——按开关派生在下方 _legacy_dedicated_pat 里
    # 现取（get_user_token_for 按平台卡派生 provider），未声明平台恒空。
    from src.domains.runtime.vcs.providers import module_for as _vcs_module_for

    def _legacy_dedicated_pat(p: str) -> str:
        try:
            if getattr(_vcs_module_for(p), "SETTINGS_TOKEN_FIELD_SUFFIX", "") == "token":
                # 数据驱动取数：统一走 get_user_token_for 泛型链（按 provider 查
                # 凭据域——对 gitcode 同样成立，get_user_token 与其同链仅多刷新）。
                return get_user_token_for(user["username"], user_id=user["id"], provider=p)
        except Exception:
            pass
        return ""
    hw_cred = user_db.get_credential(user["id"], "huawei_cloud") or {}
    codearts_cred = user_db.get_credential(user["id"], "codearts") or {}

    # 存量契约键（前端凭据页 + tests/api 在用）：本批卡驱动改造不动响应契约。
    try:
        from src.domains.mgmt.auth.grape_account_db import get_user_identities
        _identities = {ident["provider"]: ident for ident in (get_user_identities(user["id"]) or [])}
    except Exception:
        _identities = {}
    _global_token_status = {}
    try:
        from src.domains.mgmt.user.db import get_meta_value as _gmv
        _global_token_status = {
            "gitcode_expired": _gmv("global_gitcode_bot_token_status") == "expired",
            "github_expired": _gmv("global_github_bot_token_status") == "expired",
        }
    except Exception:
        _global_token_status = {"gitcode_expired": False, "github_expired": False}

    return {
        # ── legacy 兼容键（勿删：前端与既有测试消费；新代码请用 <p>_pat 泛型键）──
        "gitcode": {
            "api_token_exists": bool(_legacy_dedicated_pat("gitcode")),
            "account": user["username"],
            "auto_filled": True,
            "token_expired": (_identities.get("gitcode", {}).get("status", 0) == 4),
        },
        "github": {
            "token_exists": bool(get_user_token_for(user["username"], user_id=user["id"], provider="github")),
            "login": _identities.get("github", {}).get("external_login", ""),
            "token_expired": (_identities.get("github", {}).get("status", 0) == 4),
        },
        "global_bot_token_status": _global_token_status,
        # D102-P：泛型平台 PAT 存在性（gitee/gitlab 等平台卡驱动平台）——
        # 只回存在性+login，不回 token 值（凭据 tab 清除按钮的判定数据源）。
        **{
            f"{_p}_pat": (
                {"exists": True, "login": (_cred or {}).get("login", ""),
                 **({"workflows_write": _cred["workflows_write"]}
                    if isinstance(_cred, dict) and "workflows_write" in _cred else {})}
                if (_cred := user_db.get_credential(user["id"], _p, kind="pat"))
                # 存量专用字段平台（契约开关派生）：PAT 走专用字段（token 存 ei），
                # 存在性对齐专用键——泛型 <p>_pat 凭据域未命中时回退
                else (
                    {"exists": True, "login": ""}
                    if _legacy_dedicated_pat(_p)
                    else {}
                )
            )
            for _p in _vcs_platform_list
        },
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

def _count_reveals_in_window(user_id: str) -> int:
    """统计窗口内该用户的凭据取回次数（幂等；表不存在时返回 0）。

    created_at 由 DDL 默认 datetime('now')（UTC）写入；取回日志与审计都走 UTC，
    这里用 UTC 基准计算窗口起点，避免本地时区与库内 UTC 不一致导致误判。
    """
    try:
        from src.kernel.session_manager import db as _sdb, db_lock as _sdl
        since = (datetime.utcnow() - timedelta(seconds=_REVEAL_WINDOW_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        with _sdl:
            c = _sdb.cursor()
            c.execute(
                "SELECT COUNT(*) FROM grape_t_credential_reveal_log WHERE user_id=? AND created_at>=?",
                (user_id, since))
            return c.fetchone()[0] or 0
    except Exception:
        return 0

def _record_reveal(user_id: str, provider: str, field: str, ip: str):
    """记录一次凭据取回（供高频告警计数；不存明文）。"""
    try:
        from src.kernel.session_manager import db as _sdb, db_lock as _sdl
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
    from src.domains.mgmt.auth.password import verify_password
    from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
    from src.domains.mgmt.auth import audit as auth_audit

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
    # D32/S52：config-only 平台 PAT 绑定入口（泛型 {provider: token}）。
    # github/gitcode 走上方既有专用字段，路径零改动；新平台（gitee 等）
    # 验证后双写 user_credentials <p>_pat（UserToken 级）+ external_identities（归属源）。
    platform_tokens: dict = {}
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
                    # A 批对象模型（2026-11-13 用户裁定）：PAT = 仓库执行凭据，
                    # 不是身份绑定——只落凭据域 github_pat，不建/不激活身份行。
                    # 身份行（external_identities）只由 OAuth 首登或设置页 OAuth
                    # 绑定产生，因此这里不再有 409 / 转移票据分支。
                    from src.domains.mgmt.user.db import set_credential as _sc
                    # 写失败必须 loud：静默 False 会留下「前端看到成功、凭据没落库」
                    if not _sc(user["id"], "github", {"token": _gh_tok, "login": gh_login}, kind="pat"):
                        raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据保存失败"})
                    log.info(f"GitHub token saved for user={user['username']}, github_login={gh_login}")
                    # Issue #372 §12.8: 同步写入 unified_user_mapping.github_login
                    # 用 INSERT OR IGNORE + UPDATE（不用 INSERT OR REPLACE，避免覆盖 gitcode_user 等其他平台字段）
                    try:
                        from src.kernel.session_manager import db as _um_db, db_lock as _um_dl
                        with _um_dl:
                            _um_c = _um_db.cursor()
                            _um_c.execute(
                                "INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)",
                                (user["id"],)
                            )
                            # S92/D68：um 平台归属列停写（身份域+启动回填喂养）
                            _um_c.execute(
                                "UPDATE unified_user_mapping SET updated_at=datetime('now') WHERE user_id=?",
                                (user["id"],)
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
            # 清除 PAT：级联按 OAuth 共存与否分流（D105k 四象限）
            _cascade_pat_clear(user, "github")

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
                    # A 批对象模型（2026-11-13 用户裁定）：PAT = 仓库执行凭据，
                    # 不是身份绑定——只落凭据域 gitcode_pat，不建/不激活身份行。
                    # 身份行只由 OAuth 首登或设置页 OAuth 绑定产生，故无 409/票据。
                    from src.domains.mgmt.user.db import set_credential as _sc
                    # 写失败必须 loud（同 github 分支口径）
                    if not _sc(user["id"], "gitcode", {"token": _gc_tok, "login": gc_login}, kind="pat"):
                        raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据保存失败"})
                    log.info(f"GitCode token saved for user={user['username']}, gitcode_login={gc_login}")
                    # Issue #372 §12.8: 同步写入 unified_user_mapping
                    # PAT 绑定的用户 username ≠ GitCode login，启动时回填写入的是 username 而非 gc_login
                    # pipeline 用 actor（gc_login）查 unified_user_mapping → 必须写 gc_login 作为 gitcode_user
                    # 用 INSERT OR IGNORE + UPDATE（不用 INSERT OR REPLACE，避免覆盖 github_login 等其他平台字段）
                    try:
                        # S99/D74：昵称写收敛 channel_directory（单一权威访问层）
                        from src.domains.runtime.im.channel_directory import set_gitcode_nickname
                        set_gitcode_nickname(user["id"], _gc_user.get("name", ""))
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
            # 清除 PAT：级联按 OAuth 共存与否分流（D105k 四象限）
            _cascade_pat_clear(user, "gitcode")

    # D32/S52：config-only 平台 PAT 绑定（泛型 platform_tokens）。github/gitcode
    # 走上方既有专用字段路径（零改动）；新平台验证后双写
    # user_credentials <p>_pat（UserToken 级）+ external_identities（D31 归属源）。
    # 边界（A 批 2026-11-13）：PAT = 仓库执行凭据，不写身份行、不参与归属冲突，
    # 因此没有 409/转移票据；空值=清除 PAT 凭据（identity 解绑走既有解绑流程）。
    _platform_tokens = explicit.get("platform_tokens") or {}
    if _platform_tokens:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms
        _known_platforms = set(list_vcs_platforms())
        from src.domains.mgmt.user.db import set_credential
        for _p, _raw_tok in (_platform_tokens.items() if isinstance(_platform_tokens, dict) else []):
            _p = str(_p or "").strip()
            # D105-18：值支持对象形态 {"token":..., "workflows_write": bool}
            #（凭据级能力三态显式声明；字符串形态向后兼容=未声明）
            _wf_declared = False
            _wf_write = False
            if isinstance(_raw_tok, dict):
                _wf_declared = "workflows_write" in _raw_tok
                _wf_write = bool(_raw_tok.get("workflows_write") or False)
                _raw_tok = str(_raw_tok.get("token", "") or "")
            _tok = str(_raw_tok or "").strip()
            if not _p:
                continue
            # D102-P 统一（口径 v6 终态）：泛型路径全平台生效——存量
            # gitcode/github 的 <p>_pat 与专用字段语义等价（同账号同 token），
            # 写入走卡验证 + ei 归属（同专用分支形态）；空串 → 走既有解绑路径
            # （登录方式保底检查在 delete_external_identity 调用链）。
            # 专用字段分支保留仅为旧前端兼容（新前端统一发 platform_tokens）。
            if _p not in _known_platforms:
                raise HTTPException(status_code=400, detail={
                    "code": 400, "message": f"未注册的平台: {_p}（先在管理面配置平台卡）"})
            if _tok and not _is_placeholder(_tok):
                try:
                    from src.kernel.token_resolver import create_provider
                    _info = create_provider(_p).verify_token(_tok)
                except HTTPException:
                    raise
                except Exception as _ve:
                    raise HTTPException(status_code=502, detail={
                        "code": 502, "message": f"{_p} token 验证失败: {_ve}"})
                if not isinstance(_info, dict) or not _info.get("login") or not _info.get("id"):
                    raise HTTPException(status_code=400, detail={
                        "code": 400, "error_code": "TOKEN_INVALID",
                        "message": f"{_p} token 校验失败（无用户信息），请检查 token 有效性"})
                _ext_id = str(_info.get("id"))
                _ext_login = str(_info.get("login", ""))
                # A 批对象模型（2026-11-13 用户裁定）：PAT = 仓库执行凭据，不是
                # 身份绑定。external_identities 只由 OAuth 首登或设置页 OAuth
                # 绑定产生；这里不再建/改身份行，于是不会出现「身份留在 A 账号、
                # 凭据写进 B 账号」的半转移态，也就不再需要 409 与转移票据。
                # 平台登录名来源 = 凭据域 config.login（list_platform_links 并集读）。
                _pat_cfg = {"token": _tok, "login": _ext_login}
                if _wf_declared:
                    _pat_cfg["workflows_write"] = _wf_write  # 显式 False 也落库（三态）
                # 写失败必须 loud（2026-11-13 复核）：原实现丢弃返回值，凭据没写成
                # 也照样打 "PAT bound" 成功日志。PAT 不碰身份行后无需补偿身份。
                if not set_credential(user["id"], _p, _pat_cfg, kind="pat"):
                    raise HTTPException(status_code=500, detail={
                        "code": 500, "message": "凭据保存失败"})
                log.info(f"platform_tokens: {_p} PAT bound for user={user['id']} login={_ext_login}")
            elif not _tok:
                # D105k：清除 = 级联按 OAuth 共存与否分流（四象限）——
                # 无 OAuth 平台级解绑（ei 归属行+凭据+user_repos 全清，
                # 修复"清 PAT 后仓库列表残留"）；有 OAuth 只清 PAT 供给 ② 接管
                set_credential(user["id"], _p, {}, kind="pat")
                _cascade_pat_clear(user, _p)

    # 绑定/解绑平台后清理 user_repos 缓存——下次 /repos/member 会从 VCS 重新拉取
    # 卡驱动：任意平台凭据键变更都失效缓存，无平台名分支。
    # 2026-11-13 补泛型入口：新平台 PAT 走 body.platform_tokens（复数），键名结尾
    # 是 `_tokens` 不是 `_token` —— 只看旧键会让「配完 PAT 项目页看不到列表」复现
    # （缓存 30min 内直接返回旧的、不含新平台的快照）。清除路径用的是同一个泛型键。
    if any(k.endswith(("_token", "_pat")) for k in explicit) or explicit.get("platform_tokens"):

        try:
            from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
            with _sm_dl:
                _c = _sm_db.cursor()
                _c.execute("DELETE FROM user_repos WHERE user_id=?", (user["id"],))
                _sm_db.commit()
        except Exception as e:
            log.warning(f"update_credentials: clear user_repos failed: {e}")

    # FR-4/FR-10.1: PAT 保存后追加权限探测提示（不阻断保存）
    # 卡驱动：探测范围 = 全部平台卡目录；token 字段名走 provider 模块契约开关
    # SETTINGS_TOKEN_FIELD_SUFFIX（LABEL_COLOR 同款模式）：声明 "token" 的平台
    # （gitcode/github 存量专用字段）用 <p>_token，其余平台走 <p>_pat 凭据键
    # （与凭据域 D31 存储口径一致）；模块缺失/未声明时回退 <p>_pat 泛型口径。
    _scope_warnings = []
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms as _lvp
        _probe_platforms = _lvp()
    except Exception:
        # 卡目录不可用 → 不探测（不做平台名兜底：探测范围必须来自卡目录）
        _probe_platforms = []
    for _provider in _probe_platforms:
        try:
            from src.domains.runtime.vcs.providers import module_for as _module_for
            _suffix = getattr(_module_for(_provider), "SETTINGS_TOKEN_FIELD_SUFFIX", "pat")
        except Exception:
            _suffix = "pat"
        _token_field = f"{_provider}_{_suffix}"
        if _token_field in explicit:
            _tok = (explicit[_token_field] or "").strip()
            if _tok and not _is_placeholder(_tok):
                try:
                    # D43/S63：探测 URL/鉴权形态按平台卡派生（query_token→参数、其余→头）
                    import requests as _req
                    from src.domains.runtime.vcs.platform_card import get_platform_card
                    _card = get_platform_card(_provider)
                    if _card is None:
                        raise RuntimeError(f"platform card missing: {_provider}")
                    if _card.api_auth == "query_token":
                        _hooks_resp = _req.get(
                            f"{_card.api_base}/user/repos?per_page=1",
                            params={"access_token": _tok}, timeout=10)
                    else:
                        _hooks_resp = _req.get(
                            f"{_card.api_base}/user/repos?per_page=1",
                            headers={"Authorization": f"Bearer {_tok}", "Accept": "application/json"},
                            timeout=10)
                    if _hooks_resp.status_code == 403:
                        _scope_warnings.append(f"{_provider}: Token 缺少 repo 读权限，仓库状态探活/托管校验将不可用")
                    elif _hooks_resp.status_code == 200:
                        _hooks_data = _hooks_resp.json()
                        if isinstance(_hooks_data, list) and not _hooks_data:
                            _scope_warnings.append(f"{_provider}: Token 可能缺少 repo 读权限（返回空列表），探活可能不可用")
                except Exception:
                    pass  # 探测失败不阻断保存
                # FR-10.1: 检测该用户名下 path B 仓库（str(host_user_id or "") not in ("", "0")），提示 issue 写权限影响
                try:
                    from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
                    with _sm_dl:
                        _c = _sm_db.cursor()
                        _c.execute("SELECT repo_key FROM managed_repos WHERE host_user_id=? AND (host_user_id IS NOT NULL AND host_user_id != '' AND host_user_id != '0') LIMIT 5",
                                   (user["id"],))
                        _path_b_repos = [r[0] for r in _c.fetchall()]
                    if _path_b_repos:
                        _scope_warnings.append(
                            f"{_provider}: 你是 {len(_path_b_repos)} 个仓库的 host（路径 B），"
                            f"此 Token 将驱动 agent 回帖/建单，缺 issue 写权限将导致回复静默失败")
                except Exception:
                    pass

    # D105k：返回最新 bindings（对齐解绑路由 D42 口径）——清除级联可能删了
    # ei 归属行，前端凭 localStorage.user.bindings 快照判定绑定态（S12f 引导页
    # 触发依赖），不同步则"清 PAT 后空项目页而非引导页"
    from src.domains.mgmt.auth.grape_account_db import get_user_identities
    remaining = [i["provider"] for i in get_user_identities(user["id"])]
    if _scope_warnings:
        return {"message": "凭据已更新", "scope_warnings": _scope_warnings,
                "bindings": remaining}
    return {"message": "凭据已更新", "bindings": remaining}

class CreateApiKeyBody(BaseModel):
    name: str = ""

@router.post("/api-keys", operation_id="settings_key_create", summary="Create API key", description="Generates a grape_-prefixed API key. Plaintext is returned exactly once; the DB stores the SHA-256 hash.")
def create_api_key(body: CreateApiKeyBody, user: dict = Depends(require_no_session_token)):
    """生成 grape_ 前缀 API key——明文仅返回一次，DB 存 SHA-256 hash。

    复用 auth_token.generate_api_token（Issue-45 方式 B 已有后端），
    补齐 Web UI 管理入口（Issue #69 验收标准）。
    """
    from src.kernel.auth_sdk.auth_token import generate_api_token

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
    from src.kernel.auth_sdk.auth_token import list_api_tokens, API_KEY_PREFIX

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
    from src.kernel.auth_sdk.auth_token import revoke_api_token

    ok = revoke_api_token(user["id"], key_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "API Key 不存在或已吊销"})

    log.info(f"API key revoked: user={user['username']} id={key_id}")
    return {"message": "API Key 已吊销，立即失效"}

@router.get("/notifications", operation_id="settings_notifications_get", summary="Get notification preferences", description="Returns notification preferences.")
def get_notifications(user: dict = Depends(require_no_session_token)):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            # P4：repo_im_group_binding 在 runtime 域，删视图后走 lib 读
            from src.kernel.domain_read import runtime_lib as _rt_lib_imf
            _imf = _rt_lib_imf(foreign_only=True)
            if _imf is not None:
                row = _imf.read_one("repo_im_flags_latest", ())
            else:
                # repo_im_group_binding 归运行域：具名查询（任意拓扑）
                row = _rt.read_one_any("repo_im_flags_latest")
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
    from src.kernel.session_manager import db, db_lock
    _ai = 1 if body.vod else 0
    _nt = 1 if body.notifications_enabled else 0
    # P3-5h：repo_im_group_binding 在 runtime 域
    from src.kernel.domain_read import runtime_lib as _rt_lib_np
    from src.kernel import cross_write as _xw_np
    _np_done = False
    _rt_np = _rt_lib_np(foreign_only=True)
    if _rt_np is not None:
        _rt_np_ok = _rt_np
        _np_done = _xw_np.submit_if_lib(
            _rt_np_ok, "runtime", "repo_im_notifications_set", "repo-im-notify",
            {"auto_impl_enabled": _ai, "notify_enabled": _nt},
            lambda: _rt_np_ok.set_repo_im_notifications(_ai, _nt), log)
    if not _np_done:
        try:
            # repo_im_group_binding 归运行域：写走具名查询（任意拓扑，自提交）
            _rt.exec_any("repo_im_set_notifications", (_ai, _nt))
        except Exception as _e_np:
            log.warning(f"update_notifications: repo_im flags write failed: {_e_np}")
    return {"message": "通知偏好已更新", "prefs": body.model_dump()}

@router.get("/gitcode-repo/{owner}/{repo}", operation_id="settings_gitcode_repo_get", summary="Get repo GitCode config", description="Returns GitCode config for a specific repo.")
def get_gitcode_repo_config(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    # R25.7: 用 TokenResolver.resolve() 替换 get_repo_token_with_refresh，返回 level 供前端分级显示
    from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key
    from src.kernel.repo_key import to_repo_key
    _rk = to_repo_key(f"{owner}/{repo}")
    _provider = create_provider_from_repo_key(_rk)
    _resolve_result = TokenResolver(_provider).resolve(_rk)
    bot_token = _resolve_result.token
    _resolved_level = _resolve_result.level  # "session"/"repo_bot"/"global_bot"/"user"/""
    # D99：bot_username 默认值走来源链（平台卡 bot_identity/global bot/托管行），
    # 消灭 'devops-bot' 硬编码兜底（gitee 等平台配置页显示错误身份）
    from src.domains.mgmt.user.repo_manager import resolve_bot_username as _rbu_settings
    bot_username = _rbu_settings(_provider.provider_name) or ""
    target_key = _resolve_repo_key(f"{owner}/{repo}")
    repos = user_db.get_managed_repos(user["id"])
    for r in repos:
        if _resolve_repo_key(f'{r.get("repo_owner", "")}/{r.get("repo_name", "")}') == target_key:
            bot_username = r.get("bot_username", "") or bot_username
            break

    # R16.2.3: 返回仓库级 bot token 状态
    _bot_token_status = ""
    _has_repo_bot_token = False
    try:
        from src.kernel.session_manager import db, db_lock
        # FR-21.4: 按 repo_key 前缀选列（D43/S63：列名卡派生）
        from src.kernel.repo_key import parse_repo_key
        from src.kernel.token_resolver import create_provider as _cp_cfg
        _provider_name, _, _ = parse_repo_key(_rk)
        _token_col = _cp_cfg(_provider_name).token_storage_field()
        if _token_col:
            with db_lock:
                c = db.cursor()
                c.execute(f"SELECT bot_token_status, {_token_col} FROM managed_repos WHERE repo_key=? LIMIT 1", (_rk,))
                _row = c.fetchone()
                if _row:
                    _bot_token_status = _row[0] or ""
                    _has_repo_bot_token = bool(_row[1])
        else:
            # F4/D106：卡列空平台（gitee/gitlab 终态）→ 状态列 + rc 权威行
            from src.domains.mgmt.user.db import get_repo_credential_value
            with db_lock:
                c = db.cursor()
                c.execute("SELECT bot_token_status FROM managed_repos WHERE repo_key=? LIMIT 1", (_rk,))
                _row = c.fetchone()
            if _row:
                _bot_token_status = _row[0] or ""
                _has_repo_bot_token = bool(get_repo_credential_value(_rk, "bot"))
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
    from src.domains.mgmt.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username
    from src.kernel.config import log as _log
    repo_key = _resolve_repo_key(f"{owner}/{repo}")
    # FR-20.5: 托管人/admin 权限校验——托管人(host_user_id)或 user_repos.is_admin 或 grape_accounts.role=admin
    from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
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
        from src.kernel.token_resolver import create_provider_from_repo_key
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
        from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
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

@router.get("/oauth", operation_id="settings_oauth_get", summary="Get GitCode OAuth config", description="Returns whether GitCode OAuth is configured (client id/redirect uri; secret masked).")
def get_oauth_config(user: dict = Depends(require_no_session_token)):
    from src.domains.mgmt.user.db import get_meta_value
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
    from src.domains.mgmt.user.db import set_meta_value
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
    from src.domains.mgmt.user.db import get_meta_value
    return {
        "github_oauth_client_id": bool(get_meta_value("oauth_github_client_id")),
        "github_oauth_redirect_uri": get_meta_value("oauth_github_redirect_uri"),
    }

@router.put("/oauth-github", operation_id="settings_oauth_github_update", summary="Update GitHub OAuth config", description="Updates the GitHub OAuth client config.")
def update_github_oauth_config(body: GitHubOAuthConfig, user: dict = Depends(require_no_session_token)):
    from src.domains.mgmt.user.db import set_meta_value
    if body.github_oauth_client_id:
        set_meta_value("oauth_github_client_id", body.github_oauth_client_id)
    if body.github_oauth_client_secret:
        set_meta_value("oauth_github_client_secret", body.github_oauth_client_secret)
    if body.github_oauth_redirect_uri:
        set_meta_value("oauth_github_redirect_uri", body.github_oauth_redirect_uri)
    return {"message": "GitHub OAuth 配置已更新"}

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
    from src.domains.mgmt.user import db as user_db
    from src.domains.runtime.engine.instances import instance_name
    from src.kernel.session_manager import db, db_lock

    uid = str(user.get("id") or "")
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
    _org_id = user.get("org_id", "")
    # 判断当前组织是否是个人组织（个人组织显示 global 引擎，非个人组织严格隔离）
    _is_personal_org = False
    if _org_id:
        from src.domains.mgmt.user.org_db import is_org_personal
        _is_personal_org = is_org_personal(_org_id)
    _filter_org_id = "" if _is_personal_org else _org_id
    now = __import__("time").time()
    visible_runtimes = []
    for rt in cached:
        # 组织隔离：
        # 非个人组织：只显示 org_id 匹配的引擎（不含 global org_id=''）
        # 个人组织：只显示 org_id=''（全局/本机）和 org_id=自身个人组织的引擎，
        #           不显示其他非个人组织的引擎
        rt_org_id = rt.get("org_id") or ""
        # 注册链把无组织写成 int 0 → TEXT 列落 '0'（与本机引擎行的 '' 同语义）。
        # 不归一化时个人组织视图把 '0' 当「别的组织」跳过 → 沙箱云引擎全部不可见
        # （aw11 E-02 实测：个人组织视图下 13-18 行全被 org 过滤吃掉）。
        if rt_org_id in ("0", 0):
            rt_org_id = ""
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
                # 个人组织：跳过其他非个人组织（org_id != '' 且不是自己）的引擎
                # 但需要检查该 org 是否是个人组织——如果是个人组织则不跳过
                from src.domains.mgmt.user.org_db import is_org_personal
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
        from src.domains.runtime.engine.instances import to_display
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
        # Issue #2133：spawn_failed 时间窗降级 / 场景解绑恢复入口（前端 degraded 高亮 + 一键加回）
        rt["spawn_fail_degraded"] = bool(rt.get("spawn_fail_degraded_at"))
        rt["detached_from_scenes"] = bool(rt.get("detached_from_scenes_at"))
        # [沙箱迁移 i7] 沙箱 worker 提供的引擎：附平台/模板（卡片与详情展示+筛选用）。
        # 判定（三路取或）：① 凭据 cred_type='sandbox' ② runtime_info.platform 非空
        # ③ 引擎名解析平台（四段制 {engine}:{template}:{platform}:{provider_id} 第3段 /
        #    存量三段式 {engine}:{platform}:{provider_id} 第2段）。
        _sb_cred = cred_map.get(cid) or {}
        try:
            import json as _json_sb
            _ri = _json_sb.loads(rt.get("runtime_info") or "{}") if isinstance(rt.get("runtime_info"), str) else (rt.get("runtime_info") or {})
        except Exception:
            _ri = {}
        _ri_platform = (_ri.get("platform") or "").strip() if isinstance(_ri, dict) else ""
        _name_platform = ""
        _name_parts = (rt.get("name") or "").split(":")
        if _name_parts and _name_parts[0] in ("hermes", "opencode", "codearts", "codex", "aishell"):
            if len(_name_parts) >= 4:
                _name_platform = _name_parts[2]
            elif len(_name_parts) == 3:
                _name_platform = _name_parts[1]
        if (_sb_cred.get("cred_type") or "worker") == "sandbox" or _ri_platform or _name_platform:
            rt["is_sandbox"] = True
            # [展示语义 i7 用户裁定] sandbox_platform = Provider 名（runtime_info.provider_name 优先）
            _ri_provider = (_ri.get("provider_name") or "").strip() if isinstance(_ri, dict) else ""
            rt["sandbox_platform"] = _ri_provider or _ri_platform or _name_platform or (_sb_cred.get("platform") or "")
            rt["sandbox_platform_tech"] = _ri_platform or _name_platform or (_sb_cred.get("platform") or "")
            rt["platform_enabled"] = _sb_cred.get("platform_enabled", None)
            rt["sandbox_template"] = (_ri.get("template_name") or _ri.get("template_id") or "") if isinstance(_ri, dict) else ""
        else:
            rt["is_sandbox"] = False
            rt["sandbox_platform"] = ""
            rt["sandbox_platform_tech"] = ""
            rt["sandbox_template"] = ""
        # B5 (Issue #257): worker 版本是否落后（服务端最新版本 vs worker 上报版本）
        try:
            from src.kernel.worker_package import worker_code_version, normalize_worker_version
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
            from src.domains.runtime.engine.health_scorer import get_health_score
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
            # engine_capabilities 归运行域：具名查询（任意拓扑）
            rt["tags"] = [{"tag": r[0], "priority": r[1], "enabled": bool(r[2])} for r in
                          _rt.read_all_any("engine_cap_flags", (rt["id"],))]
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
                             if (v.get("org_id") or "") == _filter_org_id
                             or v.get("scope") == "repo"}
        _devices_cached = [rt for rt in cached if (rt.get("org_id") or "") == _filter_org_id
                           or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
    elif _is_personal_org:
        # 个人组织：只保留 org_id=''（全局）和 org_id=自身 的凭据/引擎；
        # Issue #338：个人归属 worker 设备（scope=user 且 owner=当前用户）不受
        # 凭据创建时绑定的 org_id 隔离（可见性由 _visible_credential 的 owner 判定收敛）
        # Issue #430：repo scope 设备豁免（与 #371 非个人组织分支同口径）——
        # 切换为仓库级后 org_id 对齐到仓库所属组织（可能是其他用户的个人组织），
        # 仓库级设备对仓库成员可见，可见性由 _visible_credential 的 repo_key in
        # user_repos 判定收敛，不越权。
        _devices_cred_map = {k: v for k, v in cred_map.items()
                             if (v.get("org_id") or "") in ("", _org_id)
                             or v.get("scope") == "repo"
                             or (v.get("scope") == "user" and (v.get("owner_name") or "") == uname)}
        _devices_cached = [rt for rt in cached if (rt.get("org_id") or "") in ("", _org_id)
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
    total_runtimes = len(visible_runtimes)
    if page_size and page_size > 0:
        _page = max(page, 1)
        _offset = (_page - 1) * page_size
        paged_runtimes = visible_runtimes[_offset:_offset + page_size]
        _attach_engine_models(paged_runtimes)  # Issue #865：附加配置模型/最近使用模型
        return {"runtimes": paged_runtimes, "devices": devices,
                "total": total_runtimes, "page": _page, "page_size": page_size,
                "avail_scopes": _avail_scopes, "avail_types": _avail_types,
                "avail_scenes": _avail_scenes, "avail_tags": _avail_tags,
                "health_stats": health_stats}
    _attach_engine_models(visible_runtimes)  # Issue #865：附加配置模型/最近使用模型
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
    from src.domains.runtime.engine.instances import canonical_engine

    c = canonical_engine(n)
    if c == "aishell-dsh":
        return "aishell-dsh"
    if c == "hwcloud":
        return "aishell"
    # [云四段名] engine:template:platform:provider——canonical 已按首段解析出权威类型。
    # 不得落入下方旧轨 includes 兜底：模板名段可含任意引擎词（hermes-git-a-ode），
    # 旧序 hermes 先于 opencode → opencode:hermes-* 被误判为 hermes，引擎页
    # hermes 筛选出现 opencode 云引擎（aw11 E-02 实测 2026-11-16；前端
    # DashboardView.engEngineTypeOf 2026-11-12 已同族修复，此处对齐）。
    if ":" in n:
        return c
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
    if "atomcode" in n:
        return "atomcode"
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

def _runtime_health(rt: dict, now: float) -> str:
    """单引擎健康态：
    - credential_id>0（远端边端）：status+last_seen 四态
    - credential_id=0（本地引擎）：installed/healthy 派生（就绪=online，未装=offline）
    """
    from src.domains.runtime.engine.health import derive_runtime_health, derive_local_runtime_health
    if rt.get("credential_id"):
        return derive_runtime_health(rt.get("status") or "offline", rt.get("last_seen_at"), now)
    if rt.get("installed") and rt.get("healthy"):
        return "online"
    return "offline"

_DEVICE_ONLINE_WINDOW = 60  # 心跳间隔 15s，容差 60s 内视为在线

def _device_health(cstatus: str, last_seen_at: str | None, now: float) -> str:
    """设备（凭据维度）健康态：由心跳新鲜度派生。

    凭据 `status` 恒为 'active'（不表达在线），不能像 runtime 行那样按 `status='online'` 判定；
    在线 = 最近心跳在 _DEVICE_ONLINE_WINDOW 内；失联 <5min = 刚失联；>6d = 待回收；其余离线。
    """
    from src.domains.runtime.engine.health import parse_dt
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

def _creds_via_lib():
    """GRAPE_CREDENTIALS_VIA_LIB=1 且 runtime lib 可用时返回 lib 模块（三库三机模式）。"""
    import os as _os

    from src.kernel import domain_read as _dr  # noqa: PLC0415
    if not _dr.switch_on("GRAPE_CREDENTIALS_VIA_LIB", "runtime"):
        return None
    try:
        from src.domains.runtime import public as _rt
        if not _rt.available():
            return None
        # 注意：不能整体换成 provider 实例 —— 调用方用的是 public 原语名
        # （list_active_credential_ids 等），provider 契约里没有这些方法，
        # 换过去会 AttributeError 500。只对已命名的两个方法就近走 provider。
        return _rt
    except Exception as _exc:  # noqa: BLE001
        log.warning(f"credentials via lib unavailable: {_exc}")
        return None


def _build_cred_map(db, db_lock) -> dict:
    """构建 credential_id -> 凭据信息映射，两个端点共用。

    只读取**密文/元数据列**（不涉及解密）；lib 模式下由 runtime 自己的连接取数。
    """
    _rt_lib = _creds_via_lib()
    if _rt_lib is not None:
        try:
            from src.kernel.gsb.provider import get_service as _gs_cr
            rows = _gs_cr("runtime").list_credential_rows()
        except Exception as _ge_cr:  # noqa: BLE001
            log.debug(f"settings: provider list_credential_rows failed, fallback: {_ge_cr}")
            rows = _rt_lib.list_credential_rows()
    else:
        with db_lock:
            c = db.cursor()
            from src.kernel.domain_read import runtime_lib as _ru_lib_5b448f_f
            _ru_lib_5b448f = _ru_lib_5b448f_f(foreign_only=True)
            if _ru_lib_5b448f is not None:
                rows = _ru_lib_5b448f.read_all("runtime_credentials_5b448f", ())
            else:
                # runtime_credentials 归运行域：具名查询（任意拓扑）
                rows = _rt.read_all_any("runtime_credentials_all")
    cred_map = {}
    for r in rows:
        cid, cname, cdev, cscope, cowner_id, cowner_name, cstatus, clast, clast_ip, crepo_key, corg_id, cspec, ccred_type, cplatform = r
        cred_map[cid] = {"name": cname, "device_name": cdev or "", "scope": cscope,
                         "owner_id": cowner_id or "", "owner_name": cowner_name or "",
                         "status": cstatus, "last_seen_at": clast, "last_ip": clast_ip or "",
                         "repo_key": crepo_key or "", "org_id": corg_id or "",
                         "spec": cspec or "",
                         "cred_type": ccred_type or "worker", "platform": cplatform or ""}

    # [沙箱迁移 i7] 沙箱凭据关联的平台 id + 启用态（设备卡片禁用徽标 / 跳 Provider 详情用）
    # sandbox_provider_config 归 mgmt：mgmt 进程本地游标
    try:
        with db_lock:
            c = db.cursor()
            _prov_rows = c.execute(
                "SELECT id, enabled, runtime_credential_id FROM sandbox_provider_config "
                "WHERE runtime_credential_id > 0").fetchall()
        for _pid, _pen, _pcid in _prov_rows:
            if _pcid in cred_map:
                cred_map[_pcid]["provider_id"] = _pid
                cred_map[_pcid]["platform_enabled"] = bool(_pen)
    except Exception:
        pass
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
    from src.domains.runtime.engine.health import derive_runtime_health
    from src.domains.runtime.engine.instances import instance_name
    import json as _json

    devices = []
    # 凭据 id 源：lib 优先（三库独立后无视图可读）；同时准备本地 cursor 供兜底 SQL 使用
    _ids_lib = _creds_via_lib()
    if _ids_lib is not None:
        _cred_ids = [(cid,) for cid in _ids_lib.list_active_credential_ids()]
        with db_lock:
            c = db.cursor()
    else:
        # runtime_credentials 归运行域：具名查询（任意拓扑）
        _cred_ids = _rt.read_all_any("runtime_credentials_active_ids")
    for (cid,) in _cred_ids:
        cred = cred_map.get(cid)
        if not cred:
            continue  # cred_map 已被组织过滤 — 不在 map 中的凭据跳过
        if not visible_credential(cid, cred.get("scope", "global"), cred.get("owner_name", ""), cred.get("owner_id", ""), from_repo_key(cred.get("repo_key", ""))):
            continue
        # P4：删视图后 agent_runtimes 走 lib 读
        from src.kernel.domain_read import runtime_lib as _rt_lib_lr
        _lr_lib = _rt_lib_lr(foreign_only=True)
        if _lr_lib is not None:
            _lr_rows = _lr_lib.read_all("runtimes_by_credential", (cid,))
        else:
            # agent_runtimes 归运行域：具名查询（任意拓扑）
            _lr_rows = _rt.read_all_any("runtimes_by_credential_full", (cid,))
        # AIShell 显示名（2026-10-13 收口补漏）：worker 设备视图的承载引擎列表
        from src.domains.runtime.engine.instances import to_display as _td2
        runtimes = [
            {"id": x[0], "name": _td2(x[1]), "version": x[2], "healthy": bool(x[3]),
             "installed": bool(x[4]),
             "instance_name": instance_name(x[1], cred.get("device_name", ""), cid),
             "health": derive_runtime_health(x[5], x[6], now),
             "local_skills": _json.loads(x[7] or "[]") if x[7] else [],
             "skill_scan_result": _json.loads(x[8] or "[]") if x[8] else [],
             "worker_version": x[9] or "" if len(x) > 9 else ""}
            for x in _lr_rows
        ]
        from src.kernel.domain_read import runtime_lib as _runtime_lib_tags
        _rtl = _runtime_lib_tags()
        for rt in runtimes:
            if _rtl is not None:
                try:
                    from src.kernel.gsb.provider import get_service as _gs_et
                    rt["agent_tags"] = _gs_et("runtime").list_engine_tags(rt["id"], enabled_only=True)
                except Exception as _ge_et:  # noqa: BLE001
                    log.debug(f"settings: provider list_engine_tags failed, fallback: {_ge_et}")
                    rt["agent_tags"] = _rtl.list_engine_tags(rt["id"], enabled_only=True)
                continue
            # engine_capabilities 归运行域：具名查询（任意拓扑）
            rt["agent_tags"] = [r[0] for r in
                                _rt.read_all_any("engine_cap_tags_sorted", (rt["id"],))]
        # B5: worker 版本落后标记（设备级，取首个引擎的 worker_version）
        _dev_wv = runtimes[0].get("worker_version", "") if runtimes else ""
        _dev_wv_stale = False
        if _dev_wv:
            try:
                from src.kernel.worker_package import worker_code_version, normalize_worker_version
                _dev_wv_stale = normalize_worker_version(worker_code_version()) != normalize_worker_version(_dev_wv)
            except Exception:
                pass
        devices.append({"credential_id": cid, "name": cred.get("name", ""), "device_name": cred.get("device_name", ""),
                        "credential_status": cred.get("status", "active"), "last_seen_at": cred.get("last_seen_at"),
                        "worker_ip": cred.get("last_ip", ""), "scope": cred.get("scope", "global"),
                        "owner": cred.get("owner_name", ""),
                        "cred_type": cred.get("cred_type", "worker"), "platform": cred.get("platform", ""),
                        "provider_id": cred.get("provider_id", 0) or 0,
                        "platform_enabled": cred.get("platform_enabled", None),
                        "repo_key": from_repo_key(cred.get("repo_key", "")),
                        "health": _device_health(cred.get("status", "active"), cred.get("last_seen_at"), now),
                        "worker_version": _dev_wv,
                        "worker_version_stale": _dev_wv_stale,
                        "runtimes": runtimes})

    # 本机直连引擎（credential_id=0）：关联到 runtime_credentials 中已有的主控机设备
    _local_hostname = ""
    try:
        _local_hostname = engine_host()
    except Exception:
        pass
    _device_by_name = {d.get("device_name", ""): d for d in devices}
    from src.domains.runtime.engine.instances import engine_mounts_to_credential as _mounts_to

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
        from src.domains.runtime.engine.instances import to_display as _td
        rt["name"] = _td(rt["name"])
        target_device = next((d for d in devices
                              if _mounts_to(0, rt.get("host_name"), d.get("device_name", ""))), None)
        if target_device:
            # Issue #473 范围筛选：本机引擎挂到已有主控机凭据设备时补充归属
            if not target_device.get("local_owner_id"):
                target_device["local_owner_id"] = rt_owner
            target_device["runtimes"].append({
                "id": rt["id"], "name": rt["name"], "version": rt.get("version", ""),
                "healthy": rt.get("healthy", False), "installed": rt.get("installed", False),
                "instance_name": rt.get("instance_name", rt.get("name", "")),
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

@router.get("/runtime-devices", operation_id="settings_runtime_devices", summary="List runtime devices (paginated)", description="Lists devices grouped by credential_id with hosted engines. Supports pagination via page/page_size and filtering via scope/type/q. total reflects the filtered count so the footer pager stays consistent with the displayed list (Issue #245). Issue #277: repo param filters repo-scoped devices to only the current repo. Issue #473: range param narrows the viewing scope - empty/all (default), repo (repo scope only), user (current user's user-scope + own local engines); invalid range values are rejected with 422.")
def get_runtime_devices(user: dict = Depends(require_org_member), page: int = 1, page_size: int = 20,
                        scope: str = "", type: str = "", q: str = "", repo: str = "",
                        range: RTRange = "", sandbox_type: str = ""):
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
    from src.domains.mgmt.user import db as user_db
    from src.kernel.session_manager import db as _db, db_lock as _dl
    import time as _time

    uid = str(user.get("id") or "")
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
    from src.domains.mgmt.user.org_db import is_org_personal
    _org_id = user.get("org_id", "")
    _is_personal_org = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = "" if _is_personal_org else _org_id
    if _filter_org_id:
        # Issue #371：非个人组织严格隔离分支为 repo scope 增加豁免（与 #338 个人组织分支同口径），
        # repo scope 设备对仓库成员可见，可见性由 _visible_credential 的 repo_key in user_repos 收敛。
        _local_runtimes = [rt for rt in _local_runtimes if (rt.get("org_id") or "") == _filter_org_id
                           or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
        cred_map = {k: v for k, v in cred_map.items()
                    if (v.get("org_id") or "") == _filter_org_id
                    or v.get("scope") == "repo"}
    elif _is_personal_org:
        # 个人组织：只保留 org_id=''（全局）和 org_id=自身 的凭据/引擎；
        # Issue #338：个人归属 worker 设备（scope=user 且 owner=当前用户）不受
        # 凭据创建时绑定的 org_id 隔离（与 dashboard 个人组织视图语义一致）
        # Issue #430：repo scope 设备豁免（与 #371 非个人组织分支同口径）——
        # 仓库级设备对仓库成员可见，可见性由 _visible_credential 的 repo_key in
        # user_repos 判定收敛，不越权。
        _local_runtimes = [rt for rt in _local_runtimes if (rt.get("org_id") or "") == ""
                           or (rt.get("org_id") or "") == _org_id
                           or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
        cred_map = {k: v for k, v in cred_map.items()
                    if (v.get("org_id") or "") in ("", _org_id)
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

    # [沙箱迁移 i7] 平台筛选：sandbox_type=平台名只留该平台沙箱 worker；'non_sandbox' 归类所有非沙箱设备
    _avail_sandbox_types = list(set(
        (d.get("platform") or "") for d in devices if (d.get("cred_type") or "worker") == "sandbox" and d.get("platform")
    ))
    _has_non_sandbox = any((d.get("cred_type") or "worker") != "sandbox" for d in devices)
    if sandbox_type:
        if sandbox_type == "non_sandbox":
            devices = [d for d in devices if (d.get("cred_type") or "worker") != "sandbox"]
        else:
            devices = [d for d in devices if (d.get("cred_type") or "worker") == "sandbox" and d.get("platform") == sandbox_type]

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
                "health_stats": health_stats,
                "avail_sandbox_types": _avail_sandbox_types, "has_non_sandbox": _has_non_sandbox}
    return {"devices": devices, "total": total,
            "avail_scopes": _avail_scopes, "avail_types": _avail_types,
            "health_stats": health_stats,
            "avail_sandbox_types": _avail_sandbox_types, "has_non_sandbox": _has_non_sandbox}

def _device_engine_types(d: dict) -> list:
    """从设备的引擎列表派生引擎类型标签（与前端 _availTypes 的 includes 判定一致）。"""
    _KNOWN = [("hermes", "hermes"), ("opencode", "opencode"),
              ("codearts", "codearts"), ("codex", "codex"),
              ("atomcode", "atomcode")]
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
    from src.domains.mgmt.user import db as user_db
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
    from src.domains.runtime.engine.health_scorer import reset_health
    data = reset_health(rid)
    if data is None:
        log.warning(f"health-reset failed for runtime={rid} operator={user.get('username') or user.get('id')}")
        raise HTTPException(status_code=503, detail={"code": 503, "message": "重置健康分失败，请稍后重试"})
    log.info(f"health-reset ok runtime={rid} operator={user.get('username') or user.get('id')} "
             f"health_reset_at={data.get('updated_at')} health_score={data.get('health_score')}")
    return {"runtime_id": rid, "health_reset_at": data.get("health_reset_at"),
            "health_score": data.get("health_score")}


@router.post("/agent-runtimes/{rid}/readd", operation_id="settings_agent_runtime_readd", summary="Re-add runtime engine to its scenes", description="Issue #2133 front-end one-click fallback for engine auto-recovery: rebuilds scene node bindings from the detach audit log, reconciles task bindings (terminalizes cancelled tasks, releases dangling dispatched bindings, converges busy slots) and clears spawn_failed degraded/alarm state. Idempotent; visibility reuses the engine list filter (invisible ids return 404).")
def readd_agent_runtime(rid: int, user: dict = Depends(require_org_member), repo: str = ""):
    """引擎一键加回（Issue #2133 前端兜底）。

    与心跳侧自动加回复用同一实现 ``recovery.recover_engine``：
    重建场景节点绑定 + 任务绑定对账 + 审计留痕；幂等可重复点击。
    """
    result = get_agent_runtimes(user=user, page=1, page_size=0, scope="", repo=repo)
    rt = None
    for item in result.get("runtimes", []):
        if item.get("id") == rid:
            rt = item
            break
    if rt is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "引擎不存在或不可见"})
    # 鉴权口径与 health-reset（上方端点）一致：全局引擎不开放成员级手动操作，
    # 仅仓库级/个人级 worker 设备的引擎可一键加回（I-c，QA 第2轮）。
    if (rt.get("scope") or "global") not in ("user", "repo"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅仓库级/个人级 worker 设备的引擎支持一键加回，全局引擎不开放"})
    from src.domains.runtime.engine.recovery import recover_engine
    out = recover_engine(rid, trigger="manual", force=True)
    if not out.get("recovered"):
        log.warning(f"engine readd failed runtime={rid} operator={user.get('username') or user.get('id')} "
                    f"reason={out.get('reason', '')}")
        raise HTTPException(status_code=503, detail={"code": 503, "message": f"引擎加回失败: {out.get('reason', '')}"})
    log.info(f"engine readd ok runtime={rid} operator={user.get('username') or user.get('id')} "
             f"bindings={len(out.get('bindings') or [])} "
             f"terminalized={len(out.get('terminalized') or [])} "
             f"released={len(out.get('released') or [])}")
    return {"runtime_id": rid, **out}


@router.post("/agent-runtimes/refresh", operation_id="settings_agent_runtimes_refresh", summary="Refresh agent runtimes", description="Manually refreshes executors: discovers local engines, child-process probe, and persists to DB.")
def refresh_agent_runtimes(user: dict = Depends(require_org_member)):
    """手动刷新：发现本地引擎 + 子进程检测 + 写入 DB（探测统一走 local_probe，方案 C）

    P0 修复：save_agent_runtimes 可能因 MySQL Lock wait timeout (1205) 抛异常，
    这里接住返回 500 JSON 而非让异常冒泡导致 uvicorn 进程崩溃（→ 502）。
    """
    from src.domains.runtime.engine.registry import discover_runtimes
    from src.domains.runtime.engine.local_probe import probe_local_engines
    from src.domains.runtime.engine.agent import _scan_local_skills
    from src.domains.mgmt.user import db as user_db
    from src.kernel.config import log
    _hostname = engine_host()
    try:
        discovered = discover_runtimes()
        results = probe_local_engines(discovered)

        # 扫描本机 local_skills 详情并注入
        from src.domains.runtime.engine.agent import _scan_local_skills_with_detail
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

_SCOPE_ALIAS = {"user": "user", "个人": "user", "repo": "repo", "仓库": "repo", "global": "global", "全局": "global"}

class RuntimeCredBody(BaseModel):
    name: str
    scope: str = "user"       # user(个人)/repo(仓库)/global(全局)，默认个人
    repo_key: str = ""        # scope=repo 时必填（目标仓库全名，如 owner/repo）
    cred_type: str = "worker" # worker(普通设备)/sandbox(沙箱 worker)——[沙箱迁移] i7 移植
    platform: str = ""        # cred_type=sandbox 时必填（沙箱平台，如 cubesandbox/devbox）
    # [endpoint 直接入库] 平台连接信息——生成时直接写入 Provider（endpoint/api_key 加密/证书 PEM），
    # 一键命令不再携带 --platform-* 参数；worker 上报降级为模板补报，不覆盖这些字段
    platform_endpoint: str = ""
    platform_api_key: str = ""
    platform_ssl_cert_pem: str = ""

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
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.user.org_db import is_org_personal
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "凭据名称不能为空"})
    scope = _normalize_scope(body.scope)
    # [沙箱迁移] 凭据类型校验：sandbox 类型必须指定已注册的沙箱平台（后端注册表校验，不硬编码）
    cred_type = (body.cred_type or "worker").strip().lower()
    platform = (body.platform or "").strip().lower()
    if cred_type not in ("worker", "sandbox"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"无效的凭据类型: {cred_type}（仅支持 worker/sandbox）"})
    if cred_type == "sandbox":
        from src.domains.runtime.agents.sandbox.backends import get_cloud_registry
        _cloud = get_cloud_registry()
        if not platform or platform not in _cloud:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"沙箱凭据必须指定有效平台（当前支持: {', '.join(sorted(_cloud))}）"})
    else:
        platform = ""
    owner = user.get("username") or ""
    repo_key = ""
    if scope in ("repo", "user"):
        raw_key = (body.repo_key or "").strip()
        if scope == "repo" and not raw_key:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库范围凭据必须指定 repo_key"})
        if raw_key:
            repo_key = _resolve_repo_key(raw_key)
    # 解析当前组织 org_id
    _org_id = user.get("org_id", "")
    _is_personal = is_org_personal(_org_id) if _org_id else True
    _cred_org_id = "" if _is_personal else _org_id
    token = "rta_" + secrets.token_urlsafe(32)
    # P3-5f-3：runtime_credentials 在 runtime 域；重名判定由原语以返回值上报
    # （若让唯一约束异常冒泡，意图会被判为可重试失败而无限重放）
    from src.kernel.domain_read import runtime_lib as _rt_lib_cc
    from src.kernel import cross_write as _xw_cc
    _cc_out: dict = {}
    _cc_done = False
    _rt_cc = _rt_lib_cc(foreign_only=True)
    if _rt_cc is not None:
        _rt_cc_ok = _rt_cc
        _cc_hash = hashlib.sha256(token.encode()).hexdigest()
        _cc_owner_id = repo_key if scope == "repo" else ""

        # 同步直连，**不进幂等意图队列**：token 明文只在本次响应里出现一次，
        # 若落成可重放意图，排水重放时会造出无主凭据（孤儿 token，无法对账）。
        try:
            from src.api.sandbox_uid import new_provider_uid as _npuid_sb
            _cc_out.update(_rt_cc_ok.insert_runtime_credential(
                _cc_hash, name, scope, _cc_owner_id, owner, repo_key, _cred_org_id,
                cred_type=getattr(body, "cred_type", "worker") or "worker",
                platform=getattr(body, "platform", "") or "",
                credential_uid=_npuid_sb()))
            _cc_done = True
        except Exception as _e_cc:
            log.warning(f"create_runtime_credential via lib failed, fallback to local SQL: {_e_cc}")
            _cc_done = False
    if _cc_done:
        if _cc_out.get("action") == "duplicate":
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"凭据名称已存在（全局唯一）: {name}"})
        cred_id = int(_cc_out.get("id") or 0)
    else:
        # runtime_credentials 归运行域：插入原语返回 {action, id}；唯一键冲突以 duplicate 返回（不抛）
        try:
            from src.api.sandbox_uid import new_provider_uid as _npuid_sb2
            _cc_res = _rt.insert_runtime_credential(
                hashlib.sha256(token.encode()).hexdigest(), name, scope,
                repo_key if scope == "repo" else "", owner, repo_key, _cred_org_id,
                cred_type=getattr(body, "cred_type", "worker") or "worker",
                platform=getattr(body, "platform", "") or "",
                credential_uid=_npuid_sb2())
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"生成失败: {e}"})
        if _cc_res.get("action") == "duplicate":
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"凭据名称已存在（全局唯一）: {name}"})
        cred_id = int(_cc_res.get("id") or 0)
    # [endpoint 直接入库] 沙箱凭据：同时在云沙箱 Provider 侧登记一条（幂等，按
    # runtime_credential_id 一凭据一 Provider）——生成时即写入平台连接信息（i7 移植）
    if cred_type == "sandbox":
        try:
            _sb_scope = "system" if scope == "global" else "user"
            _sb_uid = 0 if scope == "global" else (user.get("id") or 0)
            _sb_repo = repo_key if scope == "repo" else ""
            _sb_name = f"{platform}-{name}"
            from src.utils.crypto import encrypt as _enc_sb
            _sb_endpoint = (body.platform_endpoint or "").strip().rstrip("/")
            _sb_key_enc = _enc_sb(body.platform_api_key.strip()) if (body.platform_api_key or "").strip() else ""
            _sb_extra = {"ssl_cert": body.platform_ssl_cert_pem} if body.platform_ssl_cert_pem else {}
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT id FROM sandbox_provider_config WHERE runtime_credential_id=? AND runtime_credential_id>0",
                    (cred_id,))
                _row = c.fetchone()
                if _row:
                    _sb_pid = _row[0]
                    c.execute(
                        "UPDATE sandbox_provider_config SET endpoint=?, api_key_enc=?, extra_config=? WHERE id=?",
                        (_sb_endpoint, _sb_key_enc,
                         json.dumps(_sb_extra, ensure_ascii=False), _sb_pid))
                else:
                    from src.api.sandbox_uid import new_provider_uid as _npuid
                    c.execute(
                        "INSERT INTO sandbox_provider_config (platform, endpoint, api_key_enc, extra_config, "
                        "enabled, max_sandboxes, updated_by, runtime_credential_id, provider_uid) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (platform, _sb_endpoint, _sb_key_enc,
                         json.dumps(_sb_extra, ensure_ascii=False), 1, 10, owner, cred_id,
                         _npuid()))
                    _sb_pid = c.lastrowid
                db.commit()
            log.info(f"sandbox credential created, provider registered: cred={cred_id} platform={platform} "
                     f"provider_id={_sb_pid} endpoint={_sb_endpoint or '(empty)'}")
        except Exception as _sb_e:
            log.warning(f"sandbox credential provider registration failed (non-blocking): {_sb_e}")

    # 预构建 worker 安装包（异步）：凭据生成后即可一键安装，避免首次下载才打包
    # （首次构建含依赖 wheels 下载，耗时数秒至数分钟，放后台不阻塞响应）
    try:
        from src.kernel.worker_package import ensure_package_ready
        ensure_package_ready()
    except Exception as _e:
        log.warning(f"预构建 worker 安装包失败: {_e}")
    return {"token": token, "id": cred_id, "name": name, "scope": scope, "owner": owner,
            "cred_type": cred_type, "platform": platform, "message": "token 仅展示一次，请立即保存"}

@router.put("/runtime-credentials/{cid}", operation_id="settings_runtime_credential_rename", summary="Rename runtime credential", description="Renames a credential (worker name); globally unique, duplicates rejected. owner/scope unchanged.")
def rename_runtime_credential(cid: str, body: RuntimeCredBody, user: dict = Depends(require_no_session_token)):
    """改名凭据（worker 名）：全局唯一，重名拒绝。owner/scope 不可改。"""
    from src.kernel.session_manager import db, db_lock
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "凭据名称不能为空"})
    with db_lock:
        c = db.cursor()
        try:
            # P3-5f-3：runtime_credentials 在 runtime 域（改名仅动 active 行）
            from src.kernel.domain_read import runtime_lib as _rt_lib_rn
            from src.kernel import cross_write as _xw_rn
            _rn_n: dict = {}
            _rn_done = False
            _rt_rn = _rt_lib_rn(foreign_only=True)
            if _rt_rn is not None:
                _rt_rn_ok = _rt_rn

                def _apply_rn() -> None:
                    _rn_n["n"] = _rt_rn_ok.rename_runtime_credential(str(cid), name)

                _rn_done = _xw_rn.submit_if_lib(
                    _rt_rn_ok, "runtime", "runtime_credential_rename", f"rc-rename:{cid}:{name}",
                    {"cid": str(cid), "name": name}, _apply_rn, log)
            if _rn_done:
                _rn_rc = int(_rn_n.get("n") or 0)
                if _rn_rc == 0:
                    row = _rt.read_one_any("runtime_credential_exists", (cid,))
                    if not row:
                        raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
                    raise HTTPException(status_code=400, detail={"code": 400, "message": "仅 active 凭据可改名"})
            else:
                # runtime_credentials 归运行域：改名写原语返回受影响行（任意拓扑）
                if _rt.rename_runtime_credential(str(cid), name) == 0:
                    row = _rt.read_one_any("runtime_credential_exists", (cid,))
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
def change_credential_scope(cid: str, body: CredScopeBody, user: dict = Depends(require_org_member)):
    """切换凭据 scope（仓库/个人）。仅 owner 可操作，仅 active 凭据可改。不允许设为 global。"""
    from src.kernel.session_manager import db, db_lock
    scope = _normalize_scope(body.scope)
    if scope == "global":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "不允许设为全局，仅可切换仓库/个人"})
    raw_key = (body.repo_key or "").strip()
    if scope == "repo" and not raw_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库 scope 需填写 repo_key"})
    repo_key = _resolve_repo_key(raw_key) if raw_key else ""
    # Issue #371：切换 scope 时同步 org_id，消除「scope 已改但 org_id 仍是旧归属」的脏数据。
    # - repo scope：org_id 对齐到仓库所属组织（managed_repos.org_id 权威单一来源），无组织归属时置 ''；
    #   这样非个人组织视图按 org_id 过滤时 repo scope 引擎不会被错误滤除。
    # - user scope：org_id 对齐到当前操作者所在组织（用户在该组织下操作，设备应在该组织下可见）；
    #   个人组织操作时 org_id=个人组织 ID，非个人组织操作时 org_id=该组织 ID。
    _new_org_id = ""
    if scope == "repo" and repo_key:
        try:
            from src.domains.mgmt.user.org_db import get_org_id_by_repo_key
            _new_org_id = get_org_id_by_repo_key(repo_key)
        except Exception as _e:
            log.warning(f"change_credential_scope: resolve org_id for repo_key={repo_key} failed: {_e}")
            _new_org_id = ""
    elif scope == "user":
        _new_org_id = user.get("org_id") or ""
    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_ff9c0b_f
        _ru_lib_ff9c0b = _ru_lib_ff9c0b_f(foreign_only=True)
        if _ru_lib_ff9c0b is not None:
            row = _ru_lib_ff9c0b.read_one("runtime_credentials_ff9c0b", (cid,))
        else:
            # runtime_credentials 归运行域：具名查询（任意拓扑）
            row = _rt.read_one_any("runtime_credential_owner_status", (cid,))
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
        # P3-5f-2：凭据行 + 引擎行 + 重名合并全在 runtime 域 → 一个组合原语（同步直连，切 scope 要立刻生效）
        from src.kernel.domain_read import runtime_lib as _rt_lib_csc
        _rt_csc = _rt_lib_csc(foreign_only=True)
        if _rt_csc is not None:
            _csc = _rt_csc.change_credential_scope(str(cid), scope, repo_key or "",
                                                   _new_owner_id, _new_org_id)
            log.info(f"Credential scope changed via lib: cid={cid} scope={scope} "
                     f"repo_key={repo_key} org_id={_new_org_id} engines={_csc.get('engines')} "
                     f"merged={_csc.get('merged')}")
            return {"scope": scope, "repo_key": repo_key}
        # 凭据行 + 引擎行 + 同凭据重名合并是一个运行域组合原语（一次事务）；
        # 单库拓扑回落进程库，三库拓扑走 owner 连接 —— 与 lib 分支同一实现。
        _csc = _rt.change_credential_scope(str(cid), scope, repo_key or "",
                                           _new_owner_id, _new_org_id)
        log.info(f"Credential scope changed: cid={cid} scope={scope} repo_key={repo_key} "
                 f"engines={_csc.get('engines')} merged={_csc.get('merged')}")
    log.info(f"Credential scope changed: cid={cid} scope={scope} repo_key={repo_key} org_id={_new_org_id}")
    return {"scope": scope, "repo_key": repo_key}

@router.get("/runtime-credentials", operation_id="settings_runtime_credentials", summary="List runtime credentials", description="Lists runtime credentials without token_hash/plaintext.")
def list_runtime_credentials(user: dict = Depends(require_org_member)):
    """凭据列表（不含 token_hash / 明文），按组织过滤。

    非个人组织只显示 org_id 匹配的凭据（不含 global）；
    个人组织显示全部（兼容现有行为）。
    """
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.user.org_db import is_org_personal
    org_id = user.get("org_id", "")
    _is_personal = is_org_personal(org_id) if org_id else True
    _rc_lib = _creds_via_lib()
    if _rc_lib is not None:
        return {"credentials": [
            {**row, "last_seen_at": normalize_event_time(row["last_seen_at"]),
             "created_at": normalize_event_time(row["created_at"]),
             "repo_key": from_repo_key(row["repo_key"] or "")}
            for row in _rc_lib.list_runtime_credentials(org_id or "", _is_personal)
        ]}
    with db_lock:
        c = db.cursor()
        if org_id and not _is_personal:
            # 非个人组织：严格隔离，只显示 org_id 匹配的凭据（具名查询，任意拓扑）
            _rc_rows = _rt.read_all_any("runtime_credentials_by_org", (org_id,))
        else:
            # 个人组织或未指定：显示全部（兼容现有行为）
            _rc_rows = _rt.read_all_any("runtime_credentials_list")
        return {"credentials": [
            {"id": r[0], "name": r[1], "device_name": r[2] or "", "scope": r[3],
             "owner_id": r[4], "owner": r[5] or "", "status": r[6], "last_seen_at": normalize_event_time(r[7]), "created_at": normalize_event_time(r[8]),
             "repo_key": from_repo_key(r[9] or "")}
            for r in _rc_rows
        ]}

def _cleanup_scene_engine_runtime_ids(rid_list: list):
    """清理场景配置中引用了即将被删除的 runtime_id 的 engine_runtime_ids（Issue #250）。

    扫描 devops_pipeline_scene 所有活跃场景的 nodes_json，
    从每个 node 的 engine_runtime_ids 中移除在 rid_list 中的 ID，回写 DB。

    引擎绑定真实性同步：实现已抽至 engine_select.cleanup_scene_engine_bindings（与
    离线超阈值解绑共用），本函数保留为薄包装（调用点不变，行为等价）。
    """
    from src.domains.runtime.engine.engine_select import cleanup_scene_engine_bindings
    cleanup_scene_engine_bindings(rid_list)


def _sb_revoke_cleanup(cid: int) -> dict:
    """[沙箱凭据] 吊销级联：清理该凭据关联的沙箱 Provider（纯 DB，v3 收缩版）。

    [沙箱迁移 i7 漏移植恢复 2026-11-11] —— i7 settings.py:2060 段，此前整段丢失
    （§清单高危点位二次丢失）。2026-10-30 用户裁定（spec §8.1 终版）：不主动 kill
    平台侧沙箱——沙箱有平台超时回收，主动杀使吊销依赖平台可达性，全部收缩掉。
    运行中沙箱由吊销端点前置弹窗确认（409 + confirm=true），用户知情后自行决定。

    级联（全本地 DB，无平台调用）：
    1. 删 engine_template_binding + sandbox_templates（该 Provider 的）
    2. 删 sandbox_provider_config 行
    3. cloud_sandboxes 保留（用户裁定：系统识别到的运行沙箱记录是用户去平台
       手动清理的对账依据）——仅 state 置 orphaned、provider 清零防悬空 JOIN
    4. 孤儿 Provider 兜底（悬空 runtime_credential_id 的行，历史产物残留）
    """
    result = {"deleted_templates": 0, "provider_deleted": False, "orphan_providers": 0}
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id FROM sandbox_provider_config WHERE runtime_credential_id=? AND runtime_credential_id>0",
                (cid,))
            prov = c.fetchone()
            # [吊销文案分流] 关联 Provider 存在 = 沙箱凭据（普通 worker 凭据无 Provider 关联）
            # ——吊销端点据此决定 toast 是否带"机器侧旧 worker 实例将自动退出"
            result["cred_type"] = "sandbox" if prov else "worker"
            if prov:
                provider_id = prov[0]
                c.execute("SELECT COUNT(*) FROM sandbox_templates WHERE provider_id=?", (provider_id,))
                result["deleted_templates"] = c.fetchone()[0] or 0
                c.execute("DELETE FROM engine_template_binding WHERE provider_id=?", (provider_id,))
                c.execute("DELETE FROM sandbox_templates WHERE provider_id=?", (provider_id,))
                c.execute("DELETE FROM sandbox_provider_config WHERE id=?", (provider_id,))
                # 沙箱记录保留：state 置 orphaned（凭据已删无人再更新），provider 清零防悬空
                try:
                    c.execute("UPDATE cloud_sandboxes SET state='orphaned', provider_id=0 WHERE provider_id=?",
                              (provider_id,))
                except Exception as _se:
                    log.warning(f"revoke-cleanup mark sandboxes orphaned failed: {_se}")
                result["provider_deleted"] = True
            # 孤儿 Provider 兜底：runtime_credential_id 悬空的行整体清理
            c.execute(
                "DELETE FROM sandbox_provider_config WHERE runtime_credential_id>0 "
                "AND runtime_credential_id NOT IN (SELECT id FROM runtime_credentials)")
            result["orphan_providers"] = c.rowcount if c.rowcount and c.rowcount > 0 else 0
            db.commit()
        log.info(f"revoke-cleanup: cred={cid} provider={prov[0] if prov else 0} "
                 f"templates={result['deleted_templates']} orphans={result['orphan_providers']}")
    except Exception as e:
        log.warning(f"revoke-cleanup for cred {cid} failed (non-blocking): {e}")
    return result


@router.post("/runtime-credentials/{cid}/revoke", operation_id="settings_runtime_credential_revoke", summary="Revoke runtime credential", description="Revokes and deletes a worker credential: removes the credential row and cascades its agent_runtimes via the unified explicit cleanup chain (pool/capabilities/session/binding/scene references cleaned with a reconciliation report); runtime_session bindings are reset. For sandbox credentials (spec 8.1 v3): returns 409 with active_sandboxes when running sandbox records exist unless confirm=true (platform sandboxes are NOT killed — they time out on the platform or are cleaned manually per preserved cloud_sandboxes records); provider/templates deleted in DB, cloud_sandboxes rows preserved as orphaned.")
def revoke_runtime_credential(cid: int, confirm: bool = False, user: dict = Depends(require_no_session_token)):
    """吊销并删除 worker 记录：删 runtime_credentials 行 + 级联删其 agent_runtimes 行。

    2026-08-17 用户指示「吊销=删除」：删除后 token 即刻失效（_auth_credential
    查不到 → 401）、凭据名释放可复用（全局唯一键不再被 revoked 行占用）。
    级联清理（Issue #646 统一显式清理链 + 对账报告，RESTRICT FK 兜底防漏）：
    - agent_runtimes：该凭据注册的引擎行一并删除（删除前先显式清理引用）
    - agent_type_runtime_pool / engine_capabilities：引用行显式删除
    - devops_agent_session.runtime_id：置 0（dashboard 红字「无 engine」，如实反映）
    - devops_agent_runtime_binding：绑定行删除
    - devops_pipeline_scene.nodes_json：清理 engine_runtime_ids 悬空引用（Issue #250）
    - 历史 agent_tasks：按快照语义保留 uid+名称，恢复走 /api/events/{seq}/redispatch
      （pick_engine 重新绑定后 requeue，见 engine-selection-claim-pool-2026-08.md）。
    """
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_3cd287_f
        _ru_lib_3cd287 = _ru_lib_3cd287_f(foreign_only=True)
        if _ru_lib_3cd287 is not None:
            _row = _ru_lib_3cd287.read_one("runtime_credentials_3cd287", (cid,))
        else:
            # runtime_credentials 归运行域：具名查询（任意拓扑）
            _row = _rt.read_one_any("runtime_credential_owner_name", (cid,))
        if not _row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
        # [沙箱迁移 i7] cred_type 归运行域：具名查询取凭据形态（沙箱 409 门禁/吊销文案用）
        _cred_type = "worker"
        try:
            _ct_row = _rt.read_one_any("runtime_credential_type_by_id", (cid,))
            _cred_type = ((_ct_row[0] if _ct_row else "") or "worker").lower()
        except Exception:
            _cred_type = "worker"
        # 前置门禁（仅沙箱凭据，spec §8.1 v3）：running 沙箱记录存在 → 409 弹窗
        if _cred_type == "sandbox" and not confirm:
            try:
                c.execute(
                    "SELECT COUNT(*) FROM cloud_sandboxes WHERE provider_id IN "
                    "(SELECT id FROM sandbox_provider_config WHERE runtime_credential_id=?) "
                    "AND state IN ('creating','running')",
                    (cid,))
                _active = c.fetchone()[0] or 0
                if _active > 0:
                    raise HTTPException(status_code=409, detail={
                        "code": 409, "active_sandboxes": _active,
                        "message": f"当前有 {_active} 个运行中的沙箱"})
            except HTTPException:
                raise
            except Exception:
                pass
        # 扫雷⑨-H2：仅凭据 owner 可吊销（对齐同文件 scope 端点的 owner 校验范式）
        _owner = _row[0] or ""
        if _owner != (user.get("username") or ""):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅凭据 owner 可吊销"})
        # P3-5f-2：吊销是安全动作，必须**立刻**生效（token 不能有窗口期）→ 同步直连组合原语，
        # 不排队等排水。runtime 半区（会话摘绑 + 能力 + 引擎行 + 凭据行）一次事务完成；
        # mgmt 半区（场景 nodes_json 悬空引用）由本函数收尾提交。
        from src.kernel.domain_read import runtime_lib as _rt_lib_purge
        _rt_purge = _rt_lib_purge(foreign_only=True)
        if _rt_purge is not None:
            _purged = _rt_purge.purge_runtime_credential(str(cid))
            _purged_rids = [int(x) for x in (_purged.get("rids") or [])]
            if _purged_rids:
                from src.domains.runtime.engine.engine_select import cleanup_scene_engine_bindings as _csb_purge
                # commit=False：修掉"在调用方事务中途提前提交"的旧缺陷
                _csb_purge(_purged_rids, commit=False)
            db.commit()
            log.info(f"revoke {cid} via lib: engines={_purged.get('engines')} "
                     f"sessions={_purged.get('sessions')} caps={_purged.get('caps')}")
            return {"message": f"凭据 {cid} 已吊销并删除"}
        # 运行域半区（会话摘绑 + 能力 + 引擎行 + 凭据行）是 owner 域的组合原语，一次事务：
        # 单库拓扑回落进程库，三库拓扑走 owner 连接，与 lib 分支同一实现。
        _purged = _rt.purge_runtime_credential(str(cid))
        _purged_rids = [int(x) for x in (_purged.get("rids") or [])]
        if _purged_rids:
            # mgmt 半区：清理场景 nodes_json 里对被删 runtime 的悬空引用（Issue #250）
            _cleanup_scene_engine_runtime_ids(_purged_rids)
        log.info(f"revoke {cid}: engines={_purged.get('engines')} "
                 f"sessions={_purged.get('sessions')} caps={_purged.get('caps')}")
    # 沙箱链级联（纯 DB：模板/绑定/Provider 删除 + 沙箱记录置 orphaned + 孤儿兜底）
    _sb_cleanup = _sb_revoke_cleanup(cid)
    return {"message": f"凭据 {cid} 已吊销并删除"}

class ExecModeBody(BaseModel):
    mode: str

@router.get("/execution-mode", operation_id="settings_execution_mode_get", summary="Get execution mode", description="Returns the current execution mode.")
def get_execution_mode_api(user: dict = Depends(require_no_session_token)):
    from src.domains.runtime.engine.service import get_execution_mode
    return {"mode": get_execution_mode()}

@router.get("/pipeline-mode", operation_id="settings_pipeline_mode_get", summary="Get pipeline mode", description="Returns the current pipeline mode.")
def get_pipeline_mode_api(user: dict = Depends(require_no_session_token)):
    from src.domains.runtime.pipeline.event_pipeline import is_plugin_mode
    return {"mode": "plugin" if is_plugin_mode() else "legacy"}

@router.put("/pipeline-mode", operation_id="settings_pipeline_mode_set", summary="Set pipeline mode", description="Sets the pipeline mode.")
def set_pipeline_mode_api(body: dict, user: dict = Depends(require_no_session_token)):
    from src.domains.mgmt.user.db import set_meta_value
    mode = (body.get("mode") or "").strip().lower()
    if mode not in ("plugin", "legacy"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "mode 必须为 plugin 或 legacy"})
    set_meta_value("pipeline_mode", mode)
    return {"message": f"管线模式已切换为 {'插件模式' if mode == 'plugin' else '传统模式'}", "mode": mode}

@router.put("/execution-mode", operation_id="settings_execution_mode_set", summary="Set execution mode", description="Sets the execution mode.")
def set_execution_mode_api(body: ExecModeBody, user: dict = Depends(require_no_session_token)):
    from src.domains.runtime.engine.service import set_execution_mode
    if body.mode not in ("auto", "direct", "queue_remote"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "mode 必须为 auto/direct/queue_remote（queue_local 已废除）"})
    set_execution_mode(body.mode)
    return {"message": f"执行模式已切换为 {body.mode}", "mode": body.mode}

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
    from src.kernel.domain_read import runtime_lib as _runtime_lib_sync
    _rtl_sync = _runtime_lib_sync()
    if _rtl_sync is not None:
        tags = _rtl_sync.list_engine_tags(rid, enabled_only=True)
    else:
        # engine_capabilities / agent_runtimes 均归运行域：具名查询 + 写原语（任意拓扑）
        tags = [r[0] for r in _rt.read_all_any("engine_cap_tags_sorted", (rid,))]
    # P3-5f-2：agent_tags 写在 runtime 域（同域进程或 lib 不可用时回落调用方 cursor）
    from src.kernel.domain_read import runtime_lib as _rt_lib_tags
    from src.kernel import cross_write as _xw_tags
    _rt_tags = _rt_lib_tags(foreign_only=True)
    if _rt_tags is not None:
        _rt_tags_ok = _rt_tags
        if _xw_tags.submit_if_lib(
                _rt_tags_ok, "runtime", "agent_tags_set", f"agent-tags:{rid}",
                {"runtime_id": int(rid), "tags": list(tags)},
                lambda: _rt_tags_ok.set_agent_tags(int(rid), list(tags)), log):
            return
    _rt.set_agent_tags(int(rid), list(tags))

@router.get("/agent-runtimes/{rid}/tags", operation_id="settings_engine_tags", summary="List engine tags", description="Lists an engine's tags.")
def get_engine_tags(rid: int, user: dict = Depends(require_no_session_token)):
    """获取引擎的标签列表"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            from src.kernel.domain_read import runtime_lib as _runtime_lib_et
            _rtl_et = _runtime_lib_et()
            if _rtl_et is not None:
                rows = [(r[0], r[1], r[2], r[3], None, None)
                        for r in _rtl_et.list_engine_capabilities(rid)]
            else:
                from src.kernel.domain_read import runtime_lib as _ru_lib_440c87_f
                _ru_lib_440c87 = _ru_lib_440c87_f(foreign_only=True)
                if _ru_lib_440c87 is not None:
                    rows = _ru_lib_440c87.read_all("engine_capabilities_440c87", (rid,))
                else:
                    # engine_capabilities 归运行域：具名查询（任意拓扑）
                    rows = _rt.read_all_any("engine_caps_by_runtime", (rid,))
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
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    tag = (body.tag or "").strip().lower()
    if tag not in ("vod", "issue", "coding", "qa", "sre", "chat"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"无效标签: {tag}，可选: vod/issue/coding/qa/sre/chat"})
    with db_lock:
        c = db.cursor()
        try:
            from src.kernel.domain_read import runtime_lib as _rt_lib_ex2
            _ex2 = _rt_lib_ex2(foreign_only=True)
            _ex_hit = (_ex2.read_one("runtime_exists", (rid,)) is not None) if _ex2 is not None else None
            if _ex_hit is None:
                # agent_runtimes 归运行域：具名查询（任意拓扑）
                _ex_hit = _rt.read_one_any("runtime_exists_any", (rid,)) is not None
            if not _ex_hit:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "引擎不存在"})
            from src.kernel.domain_read import runtime_lib as _rt_ins
            _rti = _rt_ins()
            if _rti is not None:
                from src.kernel import cross_write as _xw_ins
                _xw_ins.submit("runtime", "engine_cap_upsert", f"cap-upsert:{rid}:{tag}",
                               {"rid": rid, "tag": tag, "priority": body.priority},
                               lambda: _rti.upsert_engine_capability(rid, tag, priority=body.priority))
            else:
                # engine_capabilities 归运行域：可移植两段式 upsert（原 ON DUPLICATE 只在 MySQL 合法）
                _rt.upsert_engine_capability(rid, tag, priority=body.priority)
            _sync_agent_tags(rid, c)
            db.commit()
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})
    log.info(f"Engine tag added: runtime={rid} tag={tag} priority={body.priority}")
    return {"message": f"标签 {tag} 已添加", "tag": tag}

@router.put("/agent-runtimes/{rid}/tags/{tag}", operation_id="settings_engine_tag_update", summary="Update engine tag", description="Updates an engine tag configuration.")
def update_engine_tag(rid: int, tag: str, body: EngineTagUpdateBody,
                            user: dict = Depends(require_no_session_token)):
    """修改引擎标签配置"""
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    tag = tag.strip().lower()
    with db_lock:
        c = db.cursor()
        try:
            from src.kernel.domain_read import runtime_lib as _rt_upd
            _rtu = _rt_upd()
            if _rtu is not None:
                from src.kernel import cross_write as _xw_upd
                _updated = {"n": 0}

                def _apply_upd():
                    _updated["n"] = _rtu.update_engine_capability(rid, tag, body.priority, body.enabled)

                _xw_upd.submit("runtime", "engine_cap_update", f"cap-upd:{rid}:{tag}",
                               {"rid": rid, "tag": tag, "priority": body.priority,
                                "enabled": body.enabled}, _apply_upd)
                _rowcount_upd = _updated["n"]
            else:
                # engine_capabilities 归运行域：写原语返回受影响行（任意拓扑）
                _rowcount_upd = _rt.update_engine_capability(rid, tag, body.priority, body.enabled)
            if _rowcount_upd == 0:
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
    from src.kernel.session_manager import db, db_lock
    tag = tag.strip().lower()
    with db_lock:
        c = db.cursor()
        try:
            from src.kernel.domain_read import runtime_lib as _rt_del
            _rtd = _rt_del()
            if _rtd is not None:
                from src.kernel import cross_write as _xw
                _deleted = {"n": 0}

                def _apply_del():
                    _deleted["n"] = _rtd.delete_engine_capability(rid, tag)

                _xw.submit("runtime", "engine_cap_delete",
                           f"cap-del:{rid}:{tag}", {"rid": rid, "tag": tag}, _apply_del)
                _rowcount_del = _deleted["n"]
            else:
                # engine_capabilities 归运行域：写原语返回受影响行（任意拓扑）
                _rowcount_del = _rt.delete_engine_capability(rid, tag)
            if _rowcount_del == 0:
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
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            # 兼容旧表（无 block_changes）
            cols = [r[1] for r in c.execute("PRAGMA table_info(event_action_filter)").fetchall()]
            if "block_changes" in cols:
                c.execute("SELECT id, event_type, action, allow, actor, require_changes, block_changes, handle_side_effect, "
                          "priority, enabled FROM event_action_filter ORDER BY event_type, action")
                rows = c.fetchall()
            else:
                # event_action_filter 归本域（mgmt）：直接本域查询，不再经 runtime lib
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
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT 1 FROM event_action_filter WHERE event_type=? AND action=?", (body.event_type, body.action))
            if c.fetchone():
                raise HTTPException(status_code=409, detail={"code": 409, "message": "该事件/动作规则已存在"})
            # P3-5h：event_action_filter 在 runtime 域
            from src.kernel.domain_read import mgmt_lib as _rt_lib_ar
            from src.kernel import cross_write as _xw_ar
            _ar_done = False
            _ar_fields = {
                "event_type": body.event_type, "action": body.action, "allow": body.allow,
                "actor": body.actor, "require_changes": body.require_changes,
                "block_changes": body.block_changes,
                "handle_side_effect": body.handle_side_effect,
                "priority": body.priority, "enabled": body.enabled,
            }
            _rt_ar = _rt_lib_ar(foreign_only=True)
            if _rt_ar is not None:
                _rt_ar_ok = _rt_ar
                _ar_done = _xw_ar.submit_if_lib(
                    _rt_ar_ok, "mgmt", "event_action_filter_add",
                    f"eaf-add:{body.event_type}:{body.action}",
                    {**_ar_fields, "created_at": now},
                    lambda: _rt_ar_ok.add_event_action_filter(_ar_fields, now), log)
            if not _ar_done:
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
    from src.kernel.session_manager import db, db_lock
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
    # P3-5h：event_action_filter 在 runtime 域（写走 lib 时仍要 rowcount 判 404）
    from src.kernel.domain_read import mgmt_lib as _rt_lib_ur
    from src.kernel import cross_write as _xw_ur
    _ur_fields = {f: getattr(body, f) for f in
                  ("allow", "actor", "require_changes", "block_changes",
                   "handle_side_effect", "priority", "enabled")
                  if getattr(body, f) is not None}
    _ur_n: dict = {}
    _ur_done = False
    _rt_ur = _rt_lib_ur(foreign_only=True)
    if _rt_ur is not None:
        _rt_ur_ok = _rt_ur

        def _apply_ur() -> None:
            _ur_n["n"] = _rt_ur_ok.update_event_action_filter(int(rule_id), _ur_fields, now)

        _ur_done = _xw_ur.submit_if_lib(
            _rt_ur_ok, "mgmt", "event_action_filter_update",
            f"eaf-upd:{rule_id}", {"rule_id": int(rule_id), "fields": _ur_fields,
                                   "updated_at": now}, _apply_ur, log)
    if _ur_done:
        if int(_ur_n.get("n") or 0) == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "规则不存在"})
    else:
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

    try:
        from src.api import event_rule
        event_rule.load_event_action_rule(force=True)
    except Exception:
        pass
    return {"message": "规则已更新"}

@router.delete("/event-action-filters/{rule_id}", operation_id="settings_event_action_rule_delete", summary="Delete event action filter rule", description="Deletes an event action filter rule (no rules = allow all).")
def delete_event_action_rule(rule_id: int, user: dict = Depends(require_no_session_token)):
    """删除事件 action 过滤规则（删除后无规则=放行）。"""
    from src.kernel.session_manager import db, db_lock
    # P3-5h：event_action_filter 在 runtime 域（写走 lib 时仍要 rowcount 判 404）
    from src.kernel.domain_read import mgmt_lib as _rt_lib_dr
    from src.kernel import cross_write as _xw_dr
    _dr_n: dict = {}
    _dr_done = False
    _rt_dr = _rt_lib_dr(foreign_only=True)
    if _rt_dr is not None:
        _rt_dr_ok = _rt_dr

        def _apply_dr() -> None:
            _dr_n["n"] = _rt_dr_ok.delete_event_action_filter(int(rule_id))

        _dr_done = _xw_dr.submit_if_lib(
            _rt_dr_ok, "mgmt", "event_action_filter_delete", f"eaf-del:{rule_id}",
            {"rule_id": int(rule_id)}, _apply_dr, log)
    if _dr_done:
        if int(_dr_n.get("n") or 0) == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "规则不存在"})
    else:
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
    from src.kernel.config import ADMIN_CONSOLE_URL, ADMIN_BETA_APPLY_API_KEY
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

def _cascade_pat_clear(user: dict, provider: str) -> dict:
    """D105k：清除 PAT 的正确级联（四象限矩阵，2026-11-09 用户裁定）。

    - 有 OAuth 绑定（has_oauth_token）：只清 PAT 凭据（供给 ② OAuth 自动接管，
      ei OAuth token 保留=回落能力）
    - 无 OAuth：PAT 即该平台唯一供给 → 平台级解绑级联：删 ei 归属行（PAT 行
      非登录凭据）+ revoke 凭据 + D42 user_repos 缓存清理（供给没了列表不残留）

    返回 {oauth_kept: bool}。
    """
    from src.domains.mgmt.auth.grape_account_db import (
        get_user_identities, delete_external_identity, revoke_provider_credentials)
    from src.domains.platform import hooks as _pl_hooks
    ident = next((i for i in get_user_identities(user["id"]) if i["provider"] == provider), None)
    has_oauth = bool(ident and ident.get("has_oauth_token"))
    _check_login_method_before_unbind(user["id"], provider)
    revoke_provider_credentials(user["id"], provider)
    if not has_oauth:
        delete_external_identity(user["id"], provider)
        # D42：user_repos 缓存清理（仅平台级解绑；OAuth 共存时供给②接管，列表保留）
        try:
            from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
            with _sm_dl:
                _c = _sm_db.cursor()
                _pfx = f"{provider}:"
                _c.execute(
                    "DELETE FROM user_repos WHERE user_id=? AND substr(repo_key, 1, ?) = ?",
                    (user["id"], len(_pfx), _pfx))
                _sm_db.commit()
        except Exception as _e:
            log.warning(f"_cascade_pat_clear: user_repos cleanup failed (user={user['id']}, "
                        f"provider={provider}): {_e}")
    # D109：清 PAT 是"凭据来源消失"事件，必须让关系表同步（hook 内部现查其它来源：
    # 有 OAuth 保有 → 关系行保留；无 OAuth → revoke）。此路径不经过 delete_credential，
    # 漏掉这一步就会出现「PAT 清空、无 OAuth，但用户仍在项目页而进不了引导页」。
    try:
        _pl_hooks.on_pat_deleted(str(user["id"]), str(provider))
    except Exception as _e:
        log.warning(f"_cascade_pat_clear: platform-link hook skipped: {_e}")
    return {"oauth_kept": has_oauth}

def _pr_web_path_of(provider: str) -> str:
    """provider 模块 PR_WEB_PATH 能力位（前端链接权威）。"""
    try:
        from src.domains.runtime.vcs.providers import _MODULES
        return getattr(_MODULES.get(provider), "PR_WEB_PATH", "pull") or "pull"
    except Exception:
        return "pull"


@router.get("/vcs/platforms", operation_id="settings_vcs_platforms",
            summary="List visible vcs platforms (builtin + configured)")
def list_vcs_platforms_public():
    # D109 读路径：平台目录以对象模型为权威（grape_t_repo_platforms + caps），
    # 平台卡只是导入源（migrated 前/表未就绪时兜底）——见 kernel.platform_object.registry。
    from src.domains.platform.registry import platform_descriptors
    # 平台可见性唯一口径（2026-11-15 方案甲）：平台行 `enabled`。
    # 判据单一实现见 src/domains/platform/visibility.py（/repos/member 403 也用同一份）。
    # meta.vcs_platforms_hidden 已退役（迁移 202611170021 把名单写成 enabled=0 并删键）。
    from src.domains.platform.visibility import is_platform_visible
    out = []
    for row in platform_descriptors():
        provider = str(row.get("provider") or "")
        if not provider or not is_platform_visible(provider):
            continue
        out.append({
            "provider": provider,
            "name": row.get("name") or provider,
            "host": row.get("host") or "",
            "dialect": row.get("dialect") or "",
            "pat_create_url": row.get("pat_create_url") or "",
            "brand_color": row.get("brand_color") or "",  # 空=前端兜底
            # 前端 PR/MR 链接路径段（provider 模块能力位，避免前端按平台名/方言猜）
            "pr_web_path": _pr_web_path_of(provider),
        })
    return {"platforms": out}

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
    from src.kernel.session_manager import db, db_lock
    out: dict[int, list[dict]] = {}
    try:
        # obs_llm_usage 归 insight：聚合走该域原语（任意拓扑；只取模型名字段）
        from src.domains.insight import public as _insight_public  # noqa: PLC0415
        # 走 provider（GSB，mgmt → insight）；失败回落 insight 域原语
        try:
            from src.kernel.gsb.provider import get_service as _gs
            _rows = _gs("insight").aggregate_llm_usage(ids)
        except Exception as _ge:  # noqa: BLE001
            from src.kernel.config import log as _lg
            _lg.debug(f"settings: provider aggregate_llm_usage failed, fallback: {_ge}")
            _rows = _insight_public.aggregate_llm_usage(ids)
        for eng_id, m_key, m_raw, cnt, last_used in _rows:
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
    from src.domains.runtime.engine.engine_model import detect_engine_model
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

# D43/S63：存量 PAT 专用字段平台集合（S52：这些平台走专用字段路径，数据非分支）
_LEGACY_PAT_FIELD_PROVIDERS = {"gitcode", "github"}

_RECENT_MODELS_TOP_N = 3
# =============================================================================
# ===== [沙箱迁移] 沙箱模板 / Provider 管理 API（自旧分支 settings.py 移植）=====
# =============================================================================


# ===== 沙箱模板管理（admin only） =====

class SandboxTemplateBody(BaseModel):
    """沙箱模板创建/编辑 body。v3：加 provider_id，engines 废弃写空。

    engines 前端现为三态对象 {et: 'preinstalled'|'install'}（绑定走 bind-engine API），
    此字段接收但忽略（str|dict|list 皆可，防 422）。
    """
    name: str
    provider: str = ""
    template_id: str
    provider_id: int = 0
    alias: str = ""
    description: str = ""
    engines: str | dict | list = "[]"
    preinstalled: str = "{}"
    disk_size_mb: int = 0
    enabled: int = 1


def _require_admin(user: dict):
    """[沙箱迁移] 校验 admin 权限（适配 main：admin_accounts 而非 user_accounts.is_admin）。"""
    from src.domains.mgmt.routes.sandbox_admin import _require_admin as _sa_admin
    _sa_admin(user)



def _sb_cred_report_owner(cred_id: int, user) -> bool:
    """[沙箱凭据] 判定当前用户是否为「沙箱凭据上报 Provider」的管理者（i7 移植）：

    该凭据的 owner_name 与当前用户一致（或 admin）。
    凭据上报的 Provider 即使 scope=system（凭据选全局）也应可操作模板
    （绑定引擎/编辑/启停/删除）——权限单一条件：凭据 owner（系统类别已删除）。
    """
    if (user.get("is_admin") or 0):
        return True
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT owner_name FROM runtime_credentials WHERE id=?", (cred_id,))
            row = c.fetchone()
        return bool(row and (row[0] or "") == (user.get("username") or ""))
    except Exception:
        return False


@router.get("/sandbox-templates", operation_id="settings_sandbox_templates_list", summary="List sandbox templates")
def list_sandbox_templates(request: Request, provider: str = "", provider_id: int = 0, q: str = "", page: int = 1, page_size: int = 20):
    """列出沙箱模板。支持 provider 筛选、搜索、分页。
    
    认证：rta_ runtime 凭据（cloud_worker）或 session token / API key（前端用户）
    授权：rta_ 凭据任意 scope（global/user/repo——按自己 Provider 过滤模板）；
    前端用户正常认证即可
    
    cloud_worker 拉取时 JOIN sandbox_provider_config.enabled——跳过凭据已禁用的 provider 的模板。
    这样 cloud_worker 不会注册无法创建沙箱的模板（provider 凭据被禁用 = 无 api_key 可用）。
    前端用户查询时返回全部模板（管理界面需展示所有模板，包括禁用凭据的 provider 的模板）。
    
    搜索（q）：按 name / description 模糊匹配。
    分页：page/page_size，page_size=0 时不分页（兼容 cloud_worker 旧逻辑）。
    """
    from src.api.deps import get_current_user_or_api_key, _extract_api_token
    from src.kernel.auth_sdk.auth_token import SESSION_TOKEN_PREFIX, API_KEY_PREFIX
    from src.kernel.session_manager import db, db_lock
    import hashlib
    
    token = _extract_api_token(request) if request else ""
    if not token:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "Not logged in"})
    
    # rta_ runtime 凭据——任意 scope 可读（user/repo 只见自己 Provider 的模板）
    _is_cloud_worker = False
    if token.startswith("rta_"):
        _sha = hashlib.sha256(token.encode()).hexdigest()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, scope FROM runtime_credentials WHERE token_hash=? AND status='active'", (_sha,))
            row = c.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail={"code": 401, "message": "Invalid runtime credential"})
        # [scope 打通 2026-10-30 用户裁定] user/repo 沙箱凭据同权拉模板——不再 403 硬拦。
        # 原 global-only 是历史设计(当时只有全局池 cloud_worker); 现在 user/repo 凭据
        # 有自己的 Provider(生成凭据时登记), 走下方 _rt_cred_id 过滤天然只见自己的模板。
        # BOLA: 引擎/任务链路(claim/heartbeat/BOLA 校验)本就 scope 感知, 无需此处拦截。
        _is_cloud_worker = True
        _rt_cred_id = row[0]  # [沙箱凭据] cloud_worker 的凭据 id——模板按 Provider 来源过滤
    else:
        # session token / API key / JWT —— 原有流程（user 用于后续 admin 校验，GET 不需要）
        get_current_user_or_api_key(request)
        _rt_cred_id = 0  # 非凭据链路，不过滤
    # 搜索条件（cloud_worker 和前端用户共用）
    q = (q or "").strip()
    search_conditions = []
    search_params = []
    if q:
        search_conditions.append("(name LIKE ? OR description LIKE ?)")
        search_kw = f"%{q}%"
        search_params.extend([search_kw, search_kw])
    # v3：provider_id 筛选（优先于 provider 字符串）
    if provider_id:
        search_conditions.append("provider_id=?")
        search_params.append(provider_id)
    search_where = (" AND " + " AND ".join(search_conditions)) if search_conditions else ""
    
    use_paging = page_size and page_size > 0
    total = 0
    
    with db_lock:
        c = db.cursor()
        if _is_cloud_worker:
            # cloud_worker：JOIN sandbox_provider_config——跳过凭据已禁用的 provider 的模板
            # + 跳过模板自身 enabled=0 的行（禁用模板的引擎不应注册；provider_id 关联按 v3 语义，
            #  旧 t.provider 字符串 JOIN 会跨实例误匹配同平台所有 Provider）
            if provider_id:
                # [删 provider 列] 字符串筛选改 provider_id(?provider_id 参数既有)
                base_where = "WHERE t.provider_id=? AND (pc.enabled IS NULL OR pc.enabled=1) AND t.enabled=1"
                base_params = [provider_id]
            elif _is_cloud_worker and _rt_cred_id:
                # [沙箱凭据] cloud_worker 只展开自己的 Provider（runtime_credential_id=自己）
                # + 无凭据来源的 Provider（cred_id=0，key_file 时代老链路如 cube）——
                # 否则同平台多 Provider 时每个 worker 都注册所有引擎（test-cube 误挂 cube 引擎根因）
                # [删系统类别] cred_id=0 分支删除——Provider 只能由凭据生成（无凭据 Provider 不存在）
                base_where = ("WHERE (pc.enabled IS NULL OR pc.enabled=1) AND t.enabled=1 "
                              "AND pc.runtime_credential_id=?")
                base_params = [_rt_cred_id]
            else:
                base_where = "WHERE (pc.enabled IS NULL OR pc.enabled=1) AND t.enabled=1"
                base_params = []
            full_where = base_where + search_where
            full_params = base_params + search_params
            
            if use_paging:
                c.execute(f"SELECT COUNT(*) FROM sandbox_templates t LEFT JOIN sandbox_provider_config pc ON t.provider_id = pc.id {full_where}", tuple(full_params))
                total = c.fetchone()[0]
                offset = (page - 1) * page_size
                c.execute(
                    f"SELECT t.* FROM sandbox_templates t "
                    f"LEFT JOIN sandbox_provider_config pc ON t.provider_id = pc.id "
                    f"{full_where} ORDER BY t.id LIMIT ? OFFSET ?",
                    tuple(full_params + [int(page_size), int(offset)]))
            else:
                c.execute(
                    f"SELECT t.* FROM sandbox_templates t "
                    f"LEFT JOIN sandbox_provider_config pc ON t.provider_id = pc.id "
                    f"{full_where} ORDER BY t.id",
                    tuple(full_params))
        else:
            # 前端用户：返回全部模板（管理界面需展示所有模板）
            if provider:
                base_where = "WHERE provider=?"
                base_params = [provider]
            else:
                base_where = ""
                base_params = []
            full_where = base_where + search_where if base_where else (search_where.replace(" AND ", "WHERE ", 1) if search_where else "")
            full_params = base_params + search_params
            
            if use_paging:
                c.execute(f"SELECT COUNT(*) FROM sandbox_templates {full_where}", tuple(full_params))
                total = c.fetchone()[0]
                offset = (page - 1) * page_size
                c.execute(
                    f"SELECT * FROM sandbox_templates {full_where} ORDER BY id LIMIT ? OFFSET ?",
                    tuple(full_params + [int(page_size), int(offset)]))
            else:
                c.execute(
                    f"SELECT * FROM sandbox_templates {full_where} ORDER BY id",
                    tuple(full_params))
        cols = [d[0] for d in c.description]
        rows = [dict(zip(cols, r)) for r in c.fetchall()]
    # v3：补 platform/provider_name/scope（从 sandbox_provider_config JOIN 取）
    if rows:
        _provider_ids = set(r.get("provider_id", 0) for r in rows if r.get("provider_id"))
        if _provider_ids:
            with db_lock:
                c = db.cursor()
                _placeholders = ",".join("?" * len(_provider_ids))
                # [删 name 列] provider_name=凭据名（rc.name JOIN）——引擎名 provider 段
                # 跟随凭据名，凭据改名 → 60s 内引擎名换新（rename-cleanup 兜底清旧行）
                c.execute(
                    f"SELECT spc.id, rc.name, spc.platform, rc.scope FROM sandbox_provider_config spc "
                    f"LEFT JOIN runtime_credentials rc ON rc.id=spc.runtime_credential_id WHERE spc.id IN ({_placeholders})",
                    tuple(_provider_ids))
                _pc_map = {row[0]: {"provider_name": row[1] or "", "platform": row[2], "scope": row[3]} for row in c.fetchall()}
            for r in rows:
                _pc = _pc_map.get(r.get("provider_id", 0), {})
                r["platform"] = _pc.get("platform", "")
                r["provider_name"] = _pc.get("provider_name", "")
                r["scope"] = _pc.get("scope", "system")
        # v3.1：engines 从 engine_template_binding 取（模板行的 engines 列已废弃恒 '[]'）——
        # 前端 Engines 列与 cloud_worker 引擎注册都消费这里
        if rows:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT provider_id, engine_type, template_id FROM engine_template_binding")
                _bind_rows = c.fetchall()
            _tpl_ids = set(r.get("id", 0) for r in rows)
            _bind_map = {}
            for _pid, _et, _tid in _bind_rows:
                if _tid in _tpl_ids:
                    _bind_map.setdefault(_tid, []).append(_et)
            for r in rows:
                _engines = _bind_map.get(r.get("id", 0), [])
                r["engines"] = json.dumps(_engines) if _engines else "[]"
    # [i5 契约 #2] template_uid/provider_uid 出口——旧库缺列降级 ''（迁移前启动的响应不塌）
    for r in rows:
        if "template_uid" not in r:
            r["template_uid"] = ""
        if "provider_uid" not in r:
            r["provider_uid"] = ""
    
    if use_paging:
        return {"templates": rows, "total": total, "page": page, "page_size": page_size}
    return {"templates": rows}


@router.post("/sandbox-templates", operation_id="settings_sandbox_template_create", summary="Create sandbox template")
def create_sandbox_template(body: SandboxTemplateBody, user: dict = Depends(require_no_session_token)):
    """v3：创建沙箱模板。权限按 Provider scope 校验（非 admin only）。写 provider_id + provider。"""
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # v3：校验 Provider 权限
        # [d1e242cba 对账漏改修复 2026-10-30] 同 PUT 端点: 2 列 SELECT 访问 prow[2]
        c.execute(
            "SELECT platform, runtime_credential_id, "
            "(SELECT rc.name FROM runtime_credentials rc WHERE rc.id=spc.runtime_credential_id) "
            "FROM sandbox_provider_config spc WHERE spc.id=?", (body.provider_id,))
        prow = c.fetchone()
        if not prow:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "Provider 不存在"})
        # [删系统类别 2026-10-30] 权限单一条件：凭据 owner（cred_id NOT NULL——无凭据 Provider 已不存在）
        if not (prow[1] and _sb_cred_report_owner(prow[1], user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
        _provider_name = prow[2] or body.provider or ""
        c.execute(
            # [删 provider 列] INSERT 去掉该列
            "INSERT INTO sandbox_templates (name, provider_id, template_id, alias, description, "
            "engines, preinstalled, disk_size_mb, enabled, created_at, updated_at, created_by, updated_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            # 新建模板默认 enabled=1（用户显式创建即生效；启用/禁用走操作栏 toggle）
            (body.name, _provider_name, body.provider_id, body.template_id, body.alias, body.description,
             "[]", body.preinstalled, body.disk_size_mb, 1,
             _now, _now, user.get("username", ""), user.get("username", "")))
        db.commit()
        tpl_id = c.lastrowid
    # 异步校验（不阻塞创建）
    try:
        _check_template_sync(tpl_id, _provider_name, body.template_id)
    except Exception:
        pass
    return {"status": "created", "id": tpl_id}


@router.put("/sandbox-templates/{tpl_id}", operation_id="settings_sandbox_template_update", summary="Update sandbox template")
def update_sandbox_template(tpl_id: int, body: SandboxTemplateBody, user: dict = Depends(require_no_session_token)):
    """v3：更新沙箱模板。权限按 Provider scope 校验。"""
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # v3：校验 Provider 权限
        c.execute("SELECT provider_id, template_id FROM sandbox_templates WHERE id=?", (tpl_id,))
        tpl_row = c.fetchone()
        if not tpl_row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "模板不存在"})
        # template_id 不可变：与库内不一致时拒绝（防止 PUT 改绑到其他平台模板）
        _cur_tid = tpl_row[1] or ""
        if (body.template_id or "") != _cur_tid:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"template_id 不可修改（库内: {_cur_tid}，请求: {body.template_id}）。"
                           f"如需更换请删除后重新登记"})
        # [d1e242cba 对账漏改修复 2026-10-30] SELECT 2 列却访问 prow[2] → tuple index
        # out of range(绑定引擎保存 PUT 500 根因, 模板 58/62 实证); name 列已删,
        # provider 名改关联子查询取凭据名
        c.execute(
            "SELECT platform, runtime_credential_id, "
            "(SELECT rc.name FROM runtime_credentials rc WHERE rc.id=spc.runtime_credential_id) "
            "FROM sandbox_provider_config spc WHERE spc.id=?", (tpl_row[0] or body.provider_id,))
        prow = c.fetchone()
        # [删系统类别] 权限单一条件：凭据 owner
        if prow and not (prow[1] and _sb_cred_report_owner(prow[1], user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
        _provider_name = (prow[2] if prow else "") or body.provider or ""
        # enabled 不随编辑更新（走操作栏 toggle；编辑表单已无该项）
        c.execute(
            # [删 provider 列] provider 列已删——UPDATE 去掉
            "UPDATE sandbox_templates SET name=?, template_id=?, alias=?, description=?, "
            "preinstalled=?, disk_size_mb=?, updated_at=?, updated_by=? WHERE id=?",
            (body.name, body.template_id, body.alias, body.description,
             body.preinstalled, body.disk_size_mb,
             _now, user.get("username", ""), tpl_id))
        db.commit()
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "模板不存在"})
    try:
        _check_template_sync(tpl_id, _provider_name, body.template_id)
    except Exception:
        pass
    return {"status": "updated"}


@router.delete("/sandbox-templates/{tpl_id}", operation_id="settings_sandbox_template_delete", summary="Delete sandbox template")
def delete_sandbox_template(tpl_id: int, user: dict = Depends(require_no_session_token)):
    """v3：删除沙箱模板。权限按 Provider scope 校验。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT provider_id FROM sandbox_templates WHERE id=?", (tpl_id,))
        tpl_row = c.fetchone()
        if not tpl_row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "模板不存在"})
        c.execute("SELECT runtime_credential_id FROM sandbox_provider_config WHERE id=?", (tpl_row[0],))
        prow = c.fetchone()
        # [删系统类别] 权限单一条件：凭据 owner
        if prow and not (prow[0] and _sb_cred_report_owner(prow[0], user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
        c.execute("DELETE FROM engine_template_binding WHERE template_id=?", (tpl_id,))
        c.execute("DELETE FROM sandbox_templates WHERE id=?", (tpl_id,))
        db.commit()
    return {"status": "deleted"}


@router.get("/sandbox-providers", operation_id="settings_sandbox_providers",
            summary="List sandbox providers",
            description="Lists system + current user's sandbox providers. api_key is masked. Supports search and pagination.")
def list_sandbox_providers(q: str = "", page: int = 1, page_size: int = 20,
                           user: dict = Depends(require_no_session_token)):
    """v3 §3.1.1：列出 Provider（系统 + 当前用户）。api_key 脱敏。支持搜索和分页。"""
    from src.kernel.session_manager import db, db_lock
    q = (q or "").strip()
    search_where = ""
    search_params = []
    if q:
        search_where = " AND (rc.name LIKE ? OR spc.endpoint LIKE ? OR spc.platform LIKE ?)"
        search_params = [f"%{q}%", f"%{q}%", f"%{q}%"]

    use_paging = page_size and page_size > 0
    with db_lock:
        c = db.cursor()
        # [范围由凭据承担] 可见性修复(2026-10-30): owner_id 形态错配——user 凭据的
        # owner 在 owner_name(用户名字符串), repo 凭据 owner_id=repo_key 字符串, 与
        # user.id(数字) 永不匹配 → 非 global 凭据全部不可见(443 实测 count=0)。
        # 正确语义(与模板列表同款): global 可见 / owner_name=当前用户 /
        # repo 凭据按 repo_key 归属用户(owner_name 匹配即可, 页面按当前仓库过滤展示)
        base_where = "WHERE (rc.scope='global' OR rc.owner_name=?)"
        base_params = [user.get("username", "")]
        full_where = base_where + search_where
        full_params = base_params + search_params

        if use_paging:
            c.execute(f"SELECT COUNT(*) FROM sandbox_provider_config spc LEFT JOIN runtime_credentials rc ON rc.id=spc.runtime_credential_id {full_where}", tuple(full_params))
            total = c.fetchone()[0]
            offset = (page - 1) * page_size
            c.execute(
                f"SELECT spc.id, rc.name AS name, spc.platform, spc.endpoint, spc.api_key_enc, spc.extra_config, spc.enabled, "
                f"rc.scope, rc.owner_id, rc.repo_key, spc.max_sandboxes, spc.updated_at, spc.runtime_credential_id, rc.name "
                f"FROM sandbox_provider_config spc LEFT JOIN runtime_credentials rc ON rc.id=spc.runtime_credential_id "
                f"{full_where} ORDER BY spc.id LIMIT ? OFFSET ?",
                tuple(full_params + [int(page_size), int(offset)]))
        else:
            c.execute(
                f"SELECT spc.id, rc.name AS name, spc.platform, spc.endpoint, spc.api_key_enc, spc.extra_config, spc.enabled, "
                f"rc.scope, rc.owner_id, rc.repo_key, spc.max_sandboxes, spc.updated_at, spc.runtime_credential_id, rc.name "
                f"FROM sandbox_provider_config spc LEFT JOIN runtime_credentials rc ON rc.id=spc.runtime_credential_id "
                f"{full_where} ORDER BY spc.id",
                tuple(full_params))
            total = 0
        rows = c.fetchall()
    result = []
    for r in rows:
        extra = json.loads(r[5]) if r[5] else {}
        result.append({
            "id": r[0], "name": r[1], "platform": r[2], "endpoint": r[3],
            "api_key": "***" if r[4] else "", "extra_config": extra,
            "enabled": r[6], "scope": r[7], "user_id": r[8],
            "repo_key": r[9], "max_sandboxes": r[10] or 10,
            "updated_at": str(r[11]) if r[11] else "",
            "runtime_credential_id": r[12] or 0,
            "credential_name": r[13] or "",
        })
    return {"providers": result, "total": total, "page": page, "page_size": page_size}


@router.post("/sandbox-providers", operation_id="settings_sandbox_provider_create",
             summary="Create a sandbox provider")
def create_sandbox_provider(body: dict, user: dict = Depends(require_no_session_token)):
    """v3 §3.1.2：新增 Provider。不生成 credential（v3 生命周期独立）。"""
    name = (body.get("name") or "").strip()
    platform = _validate_platform((body.get("platform") or "").strip())
    endpoint = (body.get("endpoint") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "name 不能为空"})
    if not endpoint:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "endpoint 不能为空"})

    api_key_plain = body.get("api_key") or ""
    extra = body.get("extra_config") or {}
    extra = _validate_extra_config(platform, extra)
    extra_json = json.dumps(extra, ensure_ascii=False)
    enabled = 1 if body.get("enabled", True) else 0
    repo_key = (body.get("repo_key") or "").strip()
    max_sandboxes = int(body.get("max_sandboxes", 10))
    if max_sandboxes < 1 or max_sandboxes > 20:
        max_sandboxes = 10
    # 级别三选（scope_level）：user=用户级 / repo=仓库级 / global=全局
    # 全局走 scope='system'（仅 admin 可建）；仓库级 scope='user'+repo_key；用户级 scope='user'
    _scope_level = (body.get("scope_level") or "user").strip().lower()
    if _scope_level not in ("user", "repo", "global"):
        _scope_level = "user"
    if _scope_level == "repo" and not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库级 Provider 需要 repo_key"})
    if _scope_level == "global":
        try:
            _require_admin(user)
        except HTTPException:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅管理员可创建全局 Provider"})

    from src.utils.crypto import encrypt
    from src.kernel.session_manager import db, db_lock
    api_key_enc = encrypt(api_key_plain) if api_key_plain else ""
    _uid = user.get("id", 0)
    _scope = "system" if _scope_level == "global" else "user"

    with db_lock:
        c = db.cursor()
        # [删 name 列] 唯一性由一凭据一 Provider 的 UPSERT 语义保证（创建入口=生成凭据）；手工端点的同名校验随列删除
        # 校验数量上限
        # [删系统类别 2026-10-30] 手工创建整体废弃——Provider 只能由「生成沙箱凭据」诞生
        # （cred_id NOT NULL 约束下手工创建无法满足关联；WebUI 入口已删，API 同步关停）
        raise HTTPException(status_code=410, detail={"code": 410, "message": "Provider 只能通过生成沙箱凭据创建（手工创建已废弃）"})


@router.put("/sandbox-providers/{provider_id}", operation_id="settings_sandbox_provider_update",
             summary="Update a sandbox provider")
def update_sandbox_provider(provider_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """v3 §3.1.3：更新 Provider。级别规则：全局(system)可降级为用户/仓库级；用户/仓库级不可升为全局。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT api_key_enc, runtime_credential_id FROM sandbox_provider_config WHERE id=?", (provider_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "Provider 不存在"})
        _old_key, _sb_cred = row[0], (row[1] or 0)
        # 级别变更规则（与生成凭据功能同款语义）：
        # - 当前全局(system) → 可降级 user/repo（仅 admin 操作）
        # - 当前用户级/仓库级(user) → 不可升为全局
        # [删系统类别 2026-10-30] 权限单一条件：凭据 owner；级别(scope_level)参数废弃——
        # 范围在凭据上改（重新生成凭据），平台编辑只管接入配置（endpoint/key/extra/上限/启停）
        if not (_sb_cred and _sb_cred_report_owner(_sb_cred, user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})

        endpoint = (body.get("endpoint") or "").strip()
        if not endpoint:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "endpoint 不能为空"})

        # platform 不可改，从 DB 取
        c.execute("SELECT platform, extra_config FROM sandbox_provider_config WHERE id=?", (provider_id,))
        _prow = c.fetchone()
        platform = _prow[0]
        # extra_config 语义（与 api_key 同款）：
        # - 缺省（None）或空对象 {} → 保留 DB 旧值。前端编辑表单从平台 schema 动态组装
        #   extra_config，schema 未定义的字段（如 proxy_node_ip）不在表单里——空对象是
        #   常态，不能据此清空（曾两次清空 proxy_node_ip → 云沙箱 DNS 失败）
        # - 非空 → 覆盖 + 校验
        _body_extra = body.get("extra_config")
        if _body_extra:
            extra = _validate_extra_config(platform, _body_extra)
        else:
            try:
                extra = json.loads(_prow[1] or "{}")
            except Exception:
                extra = {}
            if not extra:
                extra = {}
        extra_json = json.dumps(extra, ensure_ascii=False)

        from src.utils.crypto import encrypt
        api_key_plain = body.get("api_key")
        if api_key_plain is not None:
            api_key_enc = encrypt(api_key_plain) if api_key_plain else ""
        else:
            api_key_enc = _old_key

        # [范围由凭据承担] scope_level/repo_key 参数废弃——级别在凭据上改

        enabled_val = body.get("enabled")
        max_sandboxes = int(body.get("max_sandboxes", 10))
        if max_sandboxes < 1 or max_sandboxes > 20:
            max_sandboxes = 10

        if enabled_val is not None:
            c.execute(
                f"UPDATE sandbox_provider_config SET endpoint=?, api_key_enc=?, extra_config=?, enabled=?, max_sandboxes=?, updated_by=? WHERE id=?",
                [endpoint, api_key_enc, extra_json, 1 if enabled_val else 0, max_sandboxes, user["username"], provider_id])
        else:
            c.execute(
                f"UPDATE sandbox_provider_config SET endpoint=?, api_key_enc=?, extra_config=?, max_sandboxes=?, updated_by=? WHERE id=?",
                [endpoint, api_key_enc, extra_json, max_sandboxes, user["username"], provider_id])
        db.commit()
    log.info(f"provider updated: user={user['username']} id={provider_id}")
    return {"message": "Provider 已更新"}


@router.put("/sandbox-providers/{provider_id}/toggle", operation_id="settings_sandbox_provider_toggle",
             summary="Toggle a sandbox provider")
def toggle_sandbox_provider(provider_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """v3 §3.1.5：启停 Provider。

    [沙箱凭据口径对齐 64a02adde] 沙箱凭据生成的 Provider（global 凭据 → scope=system），
    其 owner 判定走 _sb_cred_report_owner（凭据 owner_name=当前用户或 admin）——
    [删系统类别 2026-10-30] 权限单一条件：凭据 owner——系统预置类别已删除。
    """
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT runtime_credential_id FROM sandbox_provider_config WHERE id=?", (provider_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "Provider 不存在"})
        # [删系统类别] 权限单一条件：凭据 owner
        _sb_cred = row[0] or 0
        if not (_sb_cred and _sb_cred_report_owner(_sb_cred, user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
        c.execute("UPDATE sandbox_provider_config SET enabled=?, updated_by=? WHERE id=?",
                  (1 if body.get("enabled") else 0, user["username"], provider_id))
        db.commit()
    return {"message": "Provider 已更新"}


@router.put("/sandbox-templates/{tpl_id}/toggle", operation_id="settings_sandbox_template_toggle",
            summary="Toggle a sandbox template")
def toggle_sandbox_template(tpl_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """启停模板。enabled=0 的模板不参与 cloud_worker 引擎展开（引擎不注册）。

    与 Provider toggle 同款权限语义（system scope 拒改 / owner 校验）。
    """
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    with db_lock:
        c = db.cursor()
        c.execute(
            # [db9d2fb7f 对账漏改修复 2026-10-30] pc.scope/pc.user_id 列已删——SELECT 只留
            # 需要的两列, row 索引相应改(tpl_row[0]=provider_id, tpl_row[1]=cred_id)
            "SELECT t.provider_id, pc.runtime_credential_id FROM sandbox_templates t "
            "LEFT JOIN sandbox_provider_config pc ON pc.id = t.provider_id WHERE t.id=?",
            (tpl_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "模板不存在"})
        # [删系统类别] 权限单一条件：凭据 owner
        if row[1] and not _sb_cred_report_owner(row[1], user):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
        if not row[1]:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无凭据 Provider（已废弃类别）"})
        c.execute("UPDATE sandbox_templates SET enabled=?, updated_at=?, updated_by=? WHERE id=?",
                  (1 if body.get("enabled") else 0,
                   datetime.now(timezone.utc).isoformat(), user["username"], tpl_id))
        db.commit()
    return {"message": "模板已更新"}


@router.post("/sandbox-providers/{provider_id}/health", operation_id="settings_sandbox_provider_health",
              summary="Health check a sandbox provider")
def health_sandbox_provider(provider_id: int, user: dict = Depends(require_no_session_token)):
    """v3 §3.1.6：健康检查。通过 adapter.health_check。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # [d1e242cba 对账漏改修复 2026-10-30] 原 SELECT 含 scope/user_id/name 列(已删/
        # 换), 访问 row[7] 越界(7 列数据) + scope 权限判断用 credential_id 值 → 500。
        # 修: 7 列明确命名, 权限改凭据 owner 判定(同 bind-engine/_sb_cred_report_owner)。
        c.execute("SELECT runtime_credential_id, "
                  "(SELECT rc.name FROM runtime_credentials rc WHERE rc.id = spc.runtime_credential_id) AS name, "
                  "platform, endpoint, api_key_enc, extra_config, max_sandboxes "
                  "FROM sandbox_provider_config spc WHERE spc.id=?", (provider_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "Provider 不存在"})
        if not (row[0] and _sb_cred_report_owner(row[0], user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})

    prov = {"runtime_credential_id": row[0], "name": row[1], "platform": row[2],
            "endpoint": row[3], "api_key_enc": row[4], "extra_config": row[5],
            "max_sandboxes": row[6] or 10}
    _platform = prov["platform"]
    from src.utils.crypto import decrypt
    _api_key = decrypt(prov["api_key_enc"]) if prov["api_key_enc"] else ""

    from src.domains.runtime.agents.sandbox.backends import get_adapter
    adapter = get_adapter(_platform)
    if not adapter:
        return {"healthy": False, "reason": f"no adapter for platform '{_platform}'"}

    _cfg = adapter.normalize_db_config({
        "platform": _platform, "name": prov["name"], "endpoint": prov["endpoint"],
        "api_key": _api_key, "extra_config": prov["extra_config"],
        "max_sandboxes": prov["max_sandboxes"],
    })
    _healthy, _reason = adapter.health_check(_cfg)
    return {"healthy": _healthy, "reason": _reason}


@router.post("/sandbox-templates/{tpl_id}/bind-engine", operation_id="settings_bind_engine",
             summary="Bind engine to template")
def bind_engine(tpl_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """v3 §3.2.5：绑定引擎到模板。UNIQUE(provider_id, engine_type) DB 级保证。幂等。"""
    engine_type = (body.get("engine_type") or "").strip()
    if not engine_type:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "engine_type 不能为空"})
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT provider_id FROM sandbox_templates WHERE id=?", (tpl_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "模板不存在"})
        provider_id = row[0]
        # 权限校验：查 Provider scope
        c.execute("SELECT runtime_credential_id FROM sandbox_provider_config WHERE id=?", (provider_id,))
        prow = c.fetchone()
        # [删系统类别] 权限单一条件：凭据 owner
        if not (prow and prow[0] and _sb_cred_report_owner(prow[0], user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
        # [多模板共用引擎] 同 Provider 下同一引擎可绑多个模板（四段式引擎名区分）——
        # R5 一引擎一模板的 409 冲突检测废除；UNIQUE(provider_id,engine_type,template_id)
        # 保证同模板重复绑定幂等
        c.execute("INSERT OR IGNORE INTO engine_template_binding (provider_id, engine_type, template_id, created_at, updated_at) VALUES (?,?,?,?,?)",
                  (provider_id, engine_type, tpl_id, _now, _now))
        db.commit()
    return {"message": "引擎已绑定"}


@router.delete("/sandbox-templates/{tpl_id}/bind-engine/{engine_type}", operation_id="settings_unbind_engine",
                summary="Unbind engine from template")
def unbind_engine(tpl_id: int, engine_type: str, user: dict = Depends(require_no_session_token)):
    """v3 §3.2.5：解绑引擎。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT provider_id FROM sandbox_templates WHERE id=?", (tpl_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "模板不存在"})
        provider_id = row[0]
        c.execute("SELECT runtime_credential_id FROM sandbox_provider_config WHERE id=?", (provider_id,))
        prow = c.fetchone()
        # [删系统类别] 权限单一条件：凭据 owner
        if not (prow and prow[0] and _sb_cred_report_owner(prow[0], user)):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
        # [多模板共用引擎] 精确解绑该 (引擎,模板)——不误删同引擎在其他模板上的绑定
        c.execute("DELETE FROM engine_template_binding WHERE provider_id=? AND engine_type=? AND template_id=?", (provider_id, engine_type, tpl_id))
        db.commit()
    return {"message": "引擎已解绑"}

@router.post("/sandbox-providers/{provider_id}/scan-templates", operation_id="settings_sandbox_provider_scan_templates",
             summary="Scan platform templates and import new ones")
def scan_sandbox_templates(provider_id: int, user: dict = Depends(require_no_session_token)):
    """扫描 Provider 平台侧模板，自动导入未登记的有效模板。

    解耦设计：扫描能力由 adapter 的 list_templates(config) 提供
    （返回统一格式 [{template_id,name,status,description}]，不支持返回 None），
    本端点不感知任何具体平台 API。有效性 = status 为 ready/ok。
    已按 template_id 去重——DB 中已有的不重复导入。
    """
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT rc.name AS name, spc.platform, spc.runtime_credential_id FROM sandbox_provider_config spc LEFT JOIN runtime_credentials rc ON rc.id=spc.runtime_credential_id WHERE spc.id=?",
                  (provider_id,))
        prow = c.fetchone()
    if not prow:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Provider 不存在"})
    # [删系统类别] 权限单一条件：凭据 owner（扫描导入新增行归凭据 owner）
    if not (prow[2] and _sb_cred_report_owner(prow[2], user)):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})

    # 取 adapter 与配置（复用 health 的配置读取路径）
    from src.domains.runtime.agents.sandbox.backends import get_adapter
    adapter = get_adapter(prow[1])
    if adapter is None:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"平台 {prow[1]} 无 adapter"})
    _cfg = _provider_config_from_db(prow[1], provider_id) or {}
    if not _cfg.get("api_url") and not _cfg.get("endpoint"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Provider 未配置 endpoint"})

    # 能力探测：adapter 未实现 list_templates
    if not hasattr(adapter, "list_templates"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"平台 {prow[3]} 不支持模板扫描"})
    try:
        scanned = adapter.list_templates(_cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"扫描失败: {e}"})
    if scanned is None:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"平台 {prow[3]} 不支持模板扫描"})

    valid = [t for t in scanned
             if t.get("template_id") and str(t.get("status", "")).lower() in ("ready", "ok")]
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    existing, added = [], []
    with db_lock:
        c = db.cursor()
        for t in valid:
            tid = t["template_id"]
            c.execute("SELECT id, name FROM sandbox_templates WHERE provider_id=? AND template_id=?",
                      (provider_id, tid))
            row = c.fetchone()
            if row:
                existing.append({"template_id": tid, "name": row[1] or t.get("name", "")})
                continue
            _name = t.get("name") or tid
            c.execute(
                "INSERT INTO sandbox_templates (name, provider_id, template_id, alias, "
                "description, preinstalled, disk_size_mb, enabled, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                # [删 provider 列 2026-10-30] provider 列已删——INSERT 同步去列
                # 扫描导入默认 enabled=0：需用户在操作栏显式启用（防止误扫入直接生效）
                (_name, provider_id, tid, "", t.get("description", ""), "{}", 0, 0, _now, _now))
            added.append({"template_id": tid, "name": _name})
        db.commit()
    return {
        "scanned": len(scanned),
        "valid": len(valid),
        "existing": len(existing),
        "added": len(added),
        "added_templates": added,
        "existing_templates": existing,
    }


@router.post("/sandbox-templates/{tpl_id}/check", operation_id="settings_sandbox_template_check", summary="Check template validity")
def check_sandbox_template(tpl_id: int, user: dict = Depends(require_no_session_token)):
    """v3：手动触发模板校验。权限按 Provider scope 校验。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # [provider 列语义修复 2026-10-30] templates.provider 列语义混用(历史存平台名,
        # d1e242cba 后写入路径存凭据名)——registry 键是 platform, 统一经 provider_id
        # JOIN spc.platform 取(唯一可靠键)。用户报告: s1 模板检查报
        # 'provider s1 not in registry' 即此混用。
        c.execute(
            "SELECT t.template_id, t.provider_id, spc.platform, spc.runtime_credential_id "
            "FROM sandbox_templates t LEFT JOIN sandbox_provider_config spc ON spc.id = t.provider_id "
            "WHERE t.id=?", (tpl_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "模板不存在"})
    if not (row[3] and _sb_cred_report_owner(row[3], user)):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限"})
    result = _do_check_template(row[2], row[0], provider_id=row[1])
    # 更新校验结果
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE sandbox_templates SET last_check_at=?, last_check_status=?, last_check_error=? WHERE id=?",
                  (_now, result["status"], result["error"], tpl_id))
        db.commit()
    return {"status": result["status"], "error": result["error"]}


def _check_template_sync(tpl_id: int, provider: str, template_id: str):
    """[provider 列语义修复 2026-10-30] 内部统一按 provider_id JOIN spc.platform
    取 registry 键——传入 provider 参数仅保留签名兼容(其值可能是凭据名/平台名
    混用, 不再使用)。"""
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT spc.platform, t.provider_id FROM sandbox_templates t "
                "LEFT JOIN sandbox_provider_config spc ON spc.id = t.provider_id WHERE t.id=?",
                (tpl_id,))
            _row = c.fetchone()
        if not _row:
            return
        _do_check_template(_row[0], template_id, provider_id=_row[1])
    except Exception:
        pass


def _provider_config_from_db(platform: str, provider_id: int | None = None) -> dict | None:
    """从 sandbox_provider_config 取配置，经 adapter 归一化。

    health 端点同款逻辑；无记录/无 adapter 返回 None。
    优先按 provider_id 精确取（模板校验场景），无 id 时回退 system scope 行。
    """
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # [d1e242cba 对账漏改修复] SELECT 引用 rc.name 但 FROM 无 JOIN——补关联子查询
        if provider_id:
            c.execute("SELECT (SELECT rc.name FROM runtime_credentials rc WHERE rc.id = spc.runtime_credential_id) AS name, spc.platform, spc.endpoint, spc.api_key_enc, "
                      "extra_config, max_sandboxes "
                      "FROM sandbox_provider_config spc WHERE spc.id=?", (provider_id,))
        else:
            c.execute("SELECT (SELECT rc.name FROM runtime_credentials rc WHERE rc.id = spc.runtime_credential_id) AS name, spc.platform, spc.endpoint, spc.api_key_enc, "
                      "extra_config, max_sandboxes "
                      "FROM sandbox_provider_config spc WHERE spc.platform=?", (platform,))
        row = c.fetchone()
    if not row:
        return None
    from src.utils.crypto import decrypt
    _api_key = decrypt(row[3]) if row[3] else ""
    from src.domains.runtime.agents.sandbox.backends import get_adapter
    adapter = get_adapter(row[1])
    if not adapter:
        return None
    return adapter.normalize_db_config({
        "platform": row[1], "name": row[0], "endpoint": row[2],
        "api_key": _api_key, "extra_config": row[4],
        "max_sandboxes": row[5],
    })


def _do_check_template(provider: str, template_id: str, provider_id: int | None = None) -> dict:
    """校验模板在平台侧是否可用。返回 {status, error}。"""
    from src.domains.runtime.agents.sandbox.backends import default_registry
    registry = default_registry()
    factory = registry.get(provider)
    if not factory:
        return {"status": "platform_unreachable", "error": f"provider {provider} not in registry"}
    # 配置从 DB 取（config 文件导入后即删除，config=None 会落到空配置）
    _cfg = _provider_config_from_db(provider, provider_id)
    try:
        provider_instance = factory(audit=None, credential=None, seccomp=None,
                                    alert_engine=None, config=_cfg)
        # create_sandbox_for_check 无 Provider 实现（历史死代码）——
        # 改为真实生命周期：create_sandbox 拿实例 → _do_create 真实建箱
        # → kill()（_do_connect + _do_kill）销毁。
        # 任一环节异常 = 平台/模板不可用，走下方错误分支。
        sandbox = provider_instance.create_sandbox(template_id=template_id)
        if hasattr(sandbox, "_do_create"):
            sandbox._do_create()  # 云沙箱：真实建箱（CubeSandbox/DevBox）
        if sandbox:
            for _attempt in range(3):
                try:
                    sandbox.kill()
                    break
                except Exception as _ke:
                    if _attempt < 2:
                        import time as _time
                        _time.sleep(1)
                    else:
                        import logging as _log
                        _log.getLogger("devops-bot").warning(f"_do_check_template: kill failed 3x for template {template_id}: {_ke}")
            return {"status": "ok", "error": ""}
        return {"status": "not_found", "error": "create returned None"}
    except Exception as e:
        err = str(e)
        if "not found" in err.lower() or "404" in err.lower():
            return {"status": "not_found", "error": err}
        if "no more resource" in err.lower():
            return {"status": "ok", "error": ""}
        return {"status": "error", "error": err}


# ====== Sandbox Provider 配置管理（设计文档 §1.3 + §1.5）======


def _validate_extra_config(platform: str, extra: dict) -> dict:
    """通过 adapter 校验 extra_config——必填+类型+白名单。
    不合法时 adapter 抛 ValueError，此处捕获转 HTTPException 400。"""
    from src.domains.runtime.agents.sandbox.backends import get_adapter
    adapter = get_adapter(platform)
    if adapter:
        try:
            return adapter.validate_extra_config(extra or {})
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": 400, "message": str(e)})
    return extra or {}


@router.get("/sandbox-provider-configs", operation_id="settings_provider_configs",
            summary="List sandbox provider configs",
            description="Lists all sandbox provider configs. api_key is masked (***).")
def list_provider_configs(user: dict = Depends(require_no_session_token)):
    """列出所有 provider 配置（api_key 脱敏）。

    GET 不需要 admin 权限——登录用户即可查看（api_key 已脱敏）。
    """
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # [provider 列已删 2026-11-06 修复] 列表 SQL 仍引用已物理删除的 provider 列
        # （500 no such column: provider，E2E 校准实证）——改按 platform 展示 +
        # LEFT JOIN 凭据名做展示名 + runtime_credential_id 供前端深链
        c.execute(
            "SELECT p.id, p.platform, p.endpoint, p.api_key_enc, p.extra_config, "
            "p.enabled, p.updated_at, p.runtime_credential_id, rc.name, p.provider_uid "
            "FROM sandbox_provider_config p "
            "LEFT JOIN runtime_credentials rc ON rc.id = p.runtime_credential_id "
            "ORDER BY p.id"
        )
        rows = c.fetchall()
    result = []
    for r in rows:
        extra = json.loads(r[4]) if r[4] else {}
        result.append({
            "id": r[0],
            "provider": r[1],
            "name": r[8] or "",
            "endpoint": r[2],
            "api_key": "***" if r[3] else "",
            "extra_config": extra,
            "enabled": r[5],
            "updated_at": str(r[6]) if r[6] else "",
            "runtime_credential_id": r[7] or 0,
            "provider_uid": r[9] or "",
        })
    return {"configs": result}


@router.put("/sandbox-provider-configs/{provider}", operation_id="settings_provider_config_update",
            summary="Update sandbox provider config",
            description="Updates a sandbox provider config. api_key is sent in plaintext and encrypted server-side. Sending api_key=null preserves the existing DB value.")
def update_provider_config(provider: str, body: dict, user: dict = Depends(require_no_session_token)):
    """更新 provider 配置。api_key 明文传入，服务端加密存储。

    - api_key 传 None（未传字段）→ 保留 DB 旧值（不覆盖）
    - api_key 传非空字符串 → 加密覆盖
    - api_key 传空字符串 → 清除（置空）
    """
    _require_admin(user)
    from src.domains.runtime.agents.sandbox.backends import get_cloud_platforms
    if provider not in get_cloud_platforms():
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"platform 必须为 {get_cloud_platforms()}"})

    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "endpoint 不能为空"})

    extra = body.get("extra_config") or {}
    extra = _validate_extra_config(provider, extra)
    extra_json = json.dumps(extra, ensure_ascii=False)

    from src.utils.crypto import encrypt
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, api_key_enc FROM sandbox_provider_config WHERE provider=?", (provider,))
        row = c.fetchone()
        # enabled 处理：传了值 → 覆盖；没传 → 保留 DB 旧值（UPDATE）/ 默认 1（INSERT）
        enabled_val = body.get("enabled")
        if row:
            # api_key 处理：传了新值 → 加密覆盖；没传（None）→ 保留 DB 旧值
            api_key_plain = body.get("api_key")
            if api_key_plain is not None:
                api_key_enc = encrypt(api_key_plain) if api_key_plain else ""
            else:
                api_key_enc = row[1]  # 保留旧值
            if enabled_val is not None:
                c.execute(
                    "UPDATE sandbox_provider_config SET endpoint=?, api_key_enc=?, extra_config=?, enabled=?, updated_by=? WHERE id=?",
                    (endpoint, api_key_enc, extra_json, 1 if enabled_val else 0, user["username"], row[0]),
                )
            else:
                c.execute(
                    "UPDATE sandbox_provider_config SET endpoint=?, api_key_enc=?, extra_config=?, updated_by=? WHERE id=?",
                    (endpoint, api_key_enc, extra_json, user["username"], row[0]),
                )
        else:
            # 新增行
            api_key_plain = body.get("api_key") or ""
            api_key_enc = encrypt(api_key_plain) if api_key_plain else ""
            c.execute(
                "INSERT INTO sandbox_provider_config (endpoint, api_key_enc, extra_config, enabled, updated_by) "
                "VALUES (?,?,?,?,?)",
                (endpoint, api_key_enc, extra_json, 1 if enabled_val is None or enabled_val else 0, user["username"]),
            )
        db.commit()
    log.info(f"provider config updated: user={user['username']} provider={provider}")
    return {"provider": provider, "message": "配置已更新"}


# ===== v3：Provider 插拔设计 API（cloud-sandbox-provider-pluggable-design.md §3）=====

def _validate_platform(platform: str) -> str:
    """从 adapter registry 动态获取支持的平台列表。"""
    from src.domains.runtime.agents.sandbox.backends import get_cloud_platforms
    supported = get_cloud_platforms()
    if platform not in supported:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"platform 必须为 {supported}"})
    return platform


@router.get("/sandbox-platforms", operation_id="settings_sandbox_platforms",
            summary="List supported sandbox platforms",
            description="Returns supported platform names and their extra_config fields. Driven by backend registry + Pydantic models, no hardcoding.")
def list_sandbox_platforms(user: dict = Depends(require_no_session_token)):
    """返回云沙箱平台列表 + 每个平台的 ui_schema。本地沙箱不在此列表中。
    按 adapter.priority 排序（小的优先），前端默认选第一个。"""
    from src.domains.runtime.agents.sandbox.backends import get_all_adapters
    cloud_adapters = sorted(
        [a for a in get_all_adapters() if getattr(a, "is_cloud", True)],
        key=lambda a: getattr(a, "priority", 100),
    )
    platforms = []
    for adapter in cloud_adapters:
        platforms.append({
            "platform": adapter.platform,
            "display_name": getattr(adapter, "display_name", adapter.platform),
            "priority": getattr(adapter, "priority", 100),
            "ui_schema": adapter.ui_schema().to_dict(),
            # [USP] manifest 能力声明透传——前端按 capabilities 渲染按钮
            # （无 list_templates 不显示"扫描"），替代点击后 400
            "manifest": getattr(adapter, "MANIFEST", None),
        })
    return {"platforms": platforms}


@router.get("/sandbox-engines", operation_id="settings_sandbox_engines",
            summary="List supported sandbox engines",
            description="Returns engine types supported by cloud sandbox orchestration (driven by ENGINE_REGISTRY, no hardcoding).")
def list_sandbox_engines(user: dict = Depends(require_no_session_token)):
    """返回云沙箱支持的引擎列表——数据源 ENGINE_REGISTRY（engine_strategies.py）。

    与 /sandbox-platforms 对称；新增引擎只需在 ENGINE_REGISTRY 加条目，
    本端点与前端零改动。

    installable: Installer.install_cmd 非空 = 建箱时可自动安装（hermes/opencode）；
    False = 无安装脚本，只能预装进镜像（codearts 模式）。
    """
    from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY
    engines = []
    for et, plugin in sorted(ENGINE_REGISTRY.items()):
        engines.append({
            "engine_type": et,
            "display_name": et,
            "installable": bool(getattr(plugin.installer, "install_cmd", "")),
        })
    return {"engines": engines}


# ===== [沙箱迁移] 用户 LLM 凭据管理（feat/devbox-sandbox-integration 迁移）=====
# user_llm_credentials 表的写入入口——沙箱 agent 引擎的模型凭据配置
class LLMCredentialBody(BaseModel):
    """用户 LLM API key（加密存储）。"""
    provider: str = "custom"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    engine_type: str = "common"  # 'common'=通用兜底, 'hermes'/'opencode'=引擎专属


def _require_repo_llm_admin(repo_key: str, user: dict):
    """[repo-llm-credential] 仓库 LLM 凭据编辑权限——口径复用仓库 Bot Token（FR-20.5）：
    托管人(host_user_id) / user_repos.is_admin / 全局 admin。"""
    from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
    if user.get("role", "") == "admin":
        return
    # [U3 修复 2026-11-13] managed_repos.repo_key 带平台前缀（gitcode:owner/repo），
    # 调用方传的是 URL 形态（owner/repo）——此前直查恒 miss → 仓库托管人自己也
    # 403（E2E 实证账号是 host 仍被拒）。统一走 _resolve_repo_key 解析实际键。
    repo_key = _resolve_repo_key(repo_key)
    try:
        with _sm_dl:
            _c = _sm_db.cursor()
            _c.execute("SELECT host_user_id FROM managed_repos WHERE repo_key=? AND host_user_id=?",
                       (repo_key, user["id"]))
            if _c.fetchone():
                return
            _c.execute("SELECT is_admin FROM user_repos WHERE user_id=? AND repo_key=? AND is_admin=1",
                       (user["id"], repo_key))
            if _c.fetchone():
                return
    except Exception:
        pass
    raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限：仅仓库托管人/管理员可编辑 LLM 凭据"})


class RepoLLMCredentialBody(BaseModel):
    """[repo-llm-credential] 仓库级 LLM 凭据（仓库资产，加密存储）。api_key 留空=保留旧值。"""
    repo_key: str
    engine_type: str = "common"
    provider: str = "custom"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    enabled: bool = True


@router.get("/llm-credentials/repo", operation_id="settings_repo_llm_credentials_list",
            summary="List repo LLM credentials")
def list_repo_llm_credentials(repo_key: str = "", user: dict = Depends(require_no_session_token)):
    """列出仓库 LLM 凭据（不回明文——api_key 只报 has_key 布尔）。读权限=仓库可见即可。"""
    from src.kernel.session_manager import db, db_lock
    if not repo_key:
        return {"credentials": []}
    with db_lock:
        c = db.cursor()
        rows = c.execute("SELECT id, engine_type, provider, api_key_enc, base_url, model, enabled, updated_by, updated_at "
                         "FROM repo_llm_credentials WHERE repo_key=? ORDER BY id", (repo_key,)).fetchall()
    return {"credentials": [
        {"id": r[0], "engine_type": r[1], "provider": r[2], "has_key": bool(r[3]),
         "base_url": r[4], "model": r[5], "enabled": bool(r[6]), "updated_by": r[7], "updated_at": r[8]}
        for r in rows]}


@router.post("/llm-credentials/repo", operation_id="settings_repo_llm_credential_upsert",
             summary="Create or update repo LLM credential")
def upsert_repo_llm_credential(body: RepoLLMCredentialBody, user: dict = Depends(require_no_session_token)):
    """新增/更新（repo_key+engine_type UPSERT）。api_key 留空=保留旧值。仅仓库管理员。"""
    from src.utils.crypto import encrypt
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone
    repo_key = (body.repo_key or "").strip()
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repo_key 不能为空"})
    _require_repo_llm_admin(repo_key, user)
    from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY
    if body.engine_type not in {"common", *ENGINE_REGISTRY.keys()}:
        raise HTTPException(400, detail={"code": 400, "message": f"invalid engine_type: {body.engine_type}"})
    VALID_PROVIDERS = {"openai", "azure", "deepseek", "zhipu", "moonshot", "qwen", "custom"}
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, detail={"code": 400, "message": f"unsupported provider: {body.provider}"})
    _now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id, api_key_enc FROM repo_llm_credentials WHERE repo_key=? AND engine_type=?",
                        (repo_key, body.engine_type)).fetchone()
        # 留空不改：api_key 非空才 encrypt 覆盖；空则沿用旧值（首建必须提供）
        if body.api_key.strip():
            _enc = encrypt(body.api_key.strip())
        elif row and row[1]:
            _enc = row[1]
        else:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "首次配置必须提供 API key"})
        if row:
            c.execute("UPDATE repo_llm_credentials SET provider=?, api_key_enc=?, base_url=?, model=?, "
                      "enabled=?, updated_by=?, updated_at=? WHERE id=?",
                      (body.provider, _enc, body.base_url, body.model, 1 if body.enabled else 0,
                       user["username"], _now, row[0]))
            rid = row[0]
        else:
            c.execute("INSERT INTO repo_llm_credentials (repo_key, engine_type, provider, api_key_enc, "
                      "base_url, model, enabled, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (repo_key, body.engine_type, body.provider, _enc, body.base_url, body.model,
                       1 if body.enabled else 0, user["username"], _now))
            rid = c.lastrowid
        db.commit()
    return {"id": rid, "message": "仓库 LLM 凭据已保存"}


@router.put("/llm-credentials/repo/{cred_id}/toggle", operation_id="settings_repo_llm_credential_toggle")
def toggle_repo_llm_credential(cred_id: int, body: dict, user: dict = Depends(require_no_session_token)):
    """启停。仅仓库管理员。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT repo_key FROM repo_llm_credentials WHERE id=?", (cred_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
        _require_repo_llm_admin(row[0], user)
        c.execute("UPDATE repo_llm_credentials SET enabled=? WHERE id=?",
                  (1 if body.get("enabled") else 0, cred_id))
        db.commit()
    return {"message": "已更新"}


@router.delete("/llm-credentials/repo/{cred_id}", operation_id="settings_repo_llm_credential_delete")
def delete_repo_llm_credential(cred_id: int, user: dict = Depends(require_no_session_token)):
    """删除。仅仓库管理员。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT repo_key FROM repo_llm_credentials WHERE id=?", (cred_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "凭据不存在"})
        _require_repo_llm_admin(row[0], user)
        c.execute("DELETE FROM repo_llm_credentials WHERE id=?", (cred_id,))
        db.commit()
    return {"message": "已删除"}


@router.post("/llm-credentials", operation_id="settings_llm_credential_create", summary="Create LLM credential", description="Save LLM API key (encrypted). One credential per engine_type per user.")
def create_llm_credential(body: LLMCredentialBody, user: dict = Depends(require_no_session_token)):
    """创建/更新 LLM 凭据（加密存储到 user_llm_credentials 表）。"""
    from src.utils.crypto import encrypt
    from src.kernel.session_manager import db, db_lock
    from datetime import datetime, timezone

    if not body.api_key.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API key 不能为空"})

    # 白名单校验 engine_type（惰性导入防循环依赖）
    from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY
    if body.engine_type not in {"common", *ENGINE_REGISTRY.keys()}:
        raise HTTPException(400, detail={"code": 400, "message": f"invalid engine_type: {body.engine_type}"})

    # provider 白名单——防止用户填不认识的 provider（如 anthropic），让错误在配置阶段暴露
    VALID_PROVIDERS = {"openai", "azure", "deepseek", "zhipu", "moonshot", "qwen", "custom"}
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(400, detail={"code": 400, "message": f"unsupported provider: {body.provider}, must be one of: {', '.join(sorted(VALID_PROVIDERS))}"})

    _now = datetime.now(timezone.utc).isoformat()
    _enc = encrypt(body.api_key.strip())

    try:
        with db_lock:
            c = db.cursor()
            # UPSERT：同 user + engine_type 覆盖
            c.execute("SELECT id FROM user_llm_credentials WHERE user_id=? AND engine_type=?", (user["id"], body.engine_type))
            existing = c.fetchone()
            if existing:
                c.execute(
                    "UPDATE user_llm_credentials SET provider=?, api_key_enc=?, base_url=?, model=?, updated_at=? WHERE id=?",
                    (body.provider, _enc, body.base_url, body.model, _now, existing[0])
                )
            else:
                c.execute(
                    "INSERT INTO user_llm_credentials (user_id, provider, api_key_enc, base_url, model, enabled, engine_type, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (user["id"], body.provider, _enc, body.base_url, body.model, body.engine_type, _now, _now)
                )
            db.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"保存 LLM 凭据失败: {e}"})

    log.info(f"LLM credential saved: user={user['username']} provider={body.provider} engine_type={body.engine_type}")
    return {"status": "ok", "message": "LLM 凭据已保存（加密存储）"}


@router.get("/llm-credentials", operation_id="settings_llm_credentials", summary="List LLM credentials", description="List user's LLM credentials without plaintext keys.")
def list_llm_credentials(user: dict = Depends(require_no_session_token)):
    """列出用户的 LLM 凭据（不含明文 key）。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, provider, base_url, model, enabled, engine_type, created_at, updated_at "
            "FROM user_llm_credentials WHERE user_id=? ORDER BY id",
            (user["id"],)
        )
        rows = c.fetchall()
    return {
        "credentials": [
            {
                "id": r[0], "provider": r[1], "base_url": r[2] or "",
                "model": r[3] or "", "enabled": r[4], "engine_type": r[5] or "common",
                "created_at": r[6] or "", "updated_at": r[7] or "",
            }
            for r in rows
        ]
    }


@router.get("/llm-engine-types", operation_id="settings_llm_engine_types", summary="List available engine types for LLM credentials")
def list_llm_engine_types(user: dict = Depends(require_no_session_token)):
    """返回引擎类型列表（common + ENGINE_REGISTRY keys）。"""
    from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY
    types = [{"value": "common", "label": "通用（兜底）"}]
    for engine in ENGINE_REGISTRY.keys():
        types.append({"value": engine, "label": engine})
    return {"engine_types": types}


@router.delete("/llm-credentials/{cred_id}", operation_id="settings_llm_credential_delete", summary="Delete LLM credential")
def delete_llm_credential(cred_id: int, user: dict = Depends(require_no_session_token)):
    """删除 LLM 凭据。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_llm_credentials WHERE id=? AND user_id=?", (cred_id, user["id"]))
        db.commit()
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "LLM 凭据不存在"})
    return {"status": "deleted"}


