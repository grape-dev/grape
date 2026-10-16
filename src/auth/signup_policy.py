"""注册策略判定——白名单优先于开放注册开关。

判定逻辑（优先级从高到低）：
1. 邮箱在邮箱白名单中 → 允许
2. 邮箱域名在域名白名单中 → 允许
3. 两个白名单均为空时，看开放注册开关
4. 白名单非空时，开关不影响结果

注册白名单与现有 login_whitelist（OAuth 白名单）独立运作。
"""
from src.config import log
from src.session_manager import db, db_lock


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
        from src.user.db import get_meta_value
        val = get_meta_value("allow_signup")
        if val is None or val == "":
            return False  # P1-3: 未配置时默认关闭
        return val in ("true", "1", "yes")
    except Exception:
        return False


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
