"""注册策略判定——白名单优先于开放注册开关。

判定逻辑（优先级从高到低）：
1. 邮箱在邮箱白名单中 → 允许
2. 邮箱域名在域名白名单中 → 允许
3. 两个白名单均为空时，看开放注册开关
4. 白名单非空时，开关不影响结果

注册白名单与现有 login_whitelist（OAuth 白名单）独立运作。
"""
from src.kernel.config import log
from src.kernel.config import _is_login_whitelist_enabled, is_user_allowed
from src.kernel.session_manager import db, db_lock

BETA_APPLY_URL = "/beta-apply"


def _get_whitelist_emails() -> "set[str] | None":
    """获取邮箱白名单。D46 P1-6: 异常时返回 None（fail-closed），区别于空 set。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT value FROM signup_whitelist WHERE type='email'")
            return {row[0].lower() for row in c.fetchall()}
    except Exception as e:
        log.warning(f"Get whitelist emails failed: {e}")
        return None


def _get_whitelist_domains() -> "set[str] | None":
    """获取域名白名单。D46 P1-6: 异常时返回 None（fail-closed），区别于空 set。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT value FROM signup_whitelist WHERE type='domain'")
            return {row[0].lower().lstrip("@") for row in c.fetchall()}
    except Exception as e:
        log.warning(f"Get whitelist domains failed: {e}")
        return None


def _is_allow_signup() -> bool:
    """获取开放注册开关。未配置时默认关闭（P1-3 修复，需求要求默认关闭）。"""
    try:
        from src.domains.mgmt.user.db import get_meta_value
        val = get_meta_value("allow_signup")
        if val is None or val == "":
            return False  # P1-3: 未配置时默认关闭
        return val in ("true", "1", "yes")
    except Exception:
        return False


def check_oauth_signup_allowed(email: str, email_verified: bool = False) -> bool:
    """OAuth 自动建号策略（Issue #3054 P3-6 主路径补齐）。

    与 `/register` 的判定同源，但多一层归属证据要求：**只有 provider 标记为已验证
    的邮箱**才参与邮箱/域名白名单判定（未验证邮箱可被任意构造，不能作为加入域白名单
    的证据）。无邮箱（未下发 / 未验证 / 私有中转域）时无法命中白名单，只能看开放
    注册开关；白名单非空则拒绝。

    查询失败一律 fail-closed（与 check_signup_allowed 一致）。
    """
    e = (email or "").strip().lower()
    if e and email_verified and "@" in e:
        return check_signup_allowed(e)
    email_whitelist = _get_whitelist_emails()
    domain_whitelist = _get_whitelist_domains()
    if email_whitelist is None or domain_whitelist is None:
        return False
    if email_whitelist or domain_whitelist:
        return False  # 白名单非空，但无可用邮箱 → 无从命中
    return _is_allow_signup()


def check_signup_allowed(email: str) -> bool:
    """检查邮箱是否允许注册。

    判定表：
    | 邮箱白名单 | 域名白名单 | 开放注册 | 结果 |
    |-----------|-----------|---------|------|
    | 命中      | —         | 任意    | 允许 |
    | 未命中    | 命中      | 任意    | 允许 |
    | 未命中    | 未命中    | 开启    | 允许 |
    | 未命中    | 未命中    | 关闭    | 拒绝 |
    | 空        | 空        | 开启    | 允许 |
    | 空        | 空        | 关闭    | 拒绝 |
    | 空        | 非空      | 任意    | 仅域名白名单允许 |
    | 非空      | —         | 任意    | 仅邮箱白名单允许 |
    """
    email = email.lower().strip()
    if not email or "@" not in email:
        return False

    domain = email.split("@", 1)[1]

    email_whitelist = _get_whitelist_emails()
    domain_whitelist = _get_whitelist_domains()

    # D46 P1-6: 查询失败时拒绝注册（fail-closed）
    if email_whitelist is None or domain_whitelist is None:
        return False

    # 1. 邮箱白名单命中 → 允许
    if email in email_whitelist:
        return True

    # 2. 域名白名单命中 → 允许
    if domain in domain_whitelist:
        return True

    # 3. 两个白名单均为空 → 看开放注册开关（设计文档第 3 条）
    if not email_whitelist and not domain_whitelist:
        return _is_allow_signup()

    # 4. 白名单非空但未命中 → 拒绝（白名单优先于开关）
    return False


def _signup_whitelist_match(email: str) -> "bool | None":
    """注册白名单命中判定（Issue #4254 内测资格复用）。

    返回 True=命中/无白名单管控；False=白名单非空但未命中；None=查询异常（未知）。
    """
    e = (email or "").strip().lower()
    email_whitelist = _get_whitelist_emails()
    domain_whitelist = _get_whitelist_domains()
    if email_whitelist is None or domain_whitelist is None:
        return None
    if e and "@" in e:
        if e in email_whitelist:
            return True
        if e.split("@", 1)[1] in domain_whitelist:
            return True
    # 两个白名单均为空 = 无白名单管控，不构成内测限制
    if not email_whitelist and not domain_whitelist:
        return True
    return False


def check_beta_eligibility(account: dict, identities: "list[dict] | None" = None) -> dict:
    """当前用户的内测资格判定（Issue #4254）。

    - 仅 `created_by_oauth=0`（平台注册入口自注册）的用户才可能被提示；
    - login_whitelist 启用时按已绑定第三方身份匹配，否则按邮箱/域名匹配 signup_whitelist；
    - 两个白名单均未启用且为空 → 视为无白名单管控，不提示；
    - 查询异常 → 视为已具备资格（不误提示），并记日志；
    - identities=None 表示身份读取异常（未知态），同样视为已具备资格。

    返回 {"is_self_registered", "whitelisted", "apply_url"}。
    """
    created_by_oauth = int((account or {}).get("created_by_oauth") or 0)
    is_self_registered = created_by_oauth == 0
    result = {
        "is_self_registered": is_self_registered,
        "whitelisted": False,
        "apply_url": BETA_APPLY_URL,
    }
    if not is_self_registered:
        # OAuth 注册用户不受内测白名单引导影响
        result["whitelisted"] = True
        return result
    try:
        if _is_login_whitelist_enabled():
            # 身份读取异常（None）= 未知态，不能当作"无绑定"误判未命中
            if identities is None:
                log.warning(
                    "check_beta_eligibility identities unavailable — treat as whitelisted"
                )
                result["whitelisted"] = True
                return result
            matched = False
            for ident in identities:
                provider = (ident or {}).get("provider", "")
                login = (ident or {}).get("external_login", "")
                if provider and login and is_user_allowed(provider, login):
                    matched = True
                    break
            result["whitelisted"] = matched
            return result
        matched = _signup_whitelist_match((account or {}).get("email", ""))
        # None（查询异常）按"已具备资格"处理，避免对用户误提示
        result["whitelisted"] = True if matched is None else matched
        return result
    except Exception as e:
        log.warning(f"check_beta_eligibility failed: {e}")
        result["whitelisted"] = True
        return result
