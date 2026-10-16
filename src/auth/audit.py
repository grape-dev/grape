"""审计日志——append-only + 哈希链。

每条日志记录前一条的哈希值，任何篡改会断链。
使用自增序列号保证链顺序，数据库行锁保证并发安全。
不记录密码、验证码等敏感信息。
"""
import hashlib
import json

from src.config import log
from src.session_manager import db, db_lock


# 事件类型
EVENT_TYPES = frozenset({
    "register",                    # 注册成功
    "login_success",               # 登录成功
    "login_failed",                # 登录失败
    "password_reset",              # 密码重置
    "password_change",             # 密码修改
    "email_change",                # 邮箱变更
    "bind",                        # 第三方绑定
    "unbind",                      # 第三方解绑
    "account_disabled",            # 账号禁用
    "account_restored",            # 账号恢复
    "account_locked",              # 账号锁定
    "account_unlocked",            # 账号解锁
    "account_deleted",             # 账号注销
    "account_anonymized",          # 账号匿名化（冷静期到期自动执行）
    "set_password",                # 设置密码（无密码用户）
    "admin_user_created",          # admin 手动创建用户
    "admin_user_edited",           # admin 编辑用户信息
    "admin_password_reset",        # admin 重置用户密码
    "admin_whitelist_added",       # admin 添加登录白名单
    "admin_whitelist_deleted",     # admin 删除登录白名单
    "admin_whitelist_toggled",     # admin 切换白名单开关
    "admin_login_failed",          # admin 登录失败
    "admin_signup_whitelist_added",   # admin 添加注册白名单
    "admin_signup_whitelist_deleted", # admin 删除注册白名单
    "admin_allow_signup_toggled",     # admin 切换开放注册开关
    "admin_password_changed",        # admin 修改自己的密码
    "admin_config_saved",            # admin 保存平台凭据配置
    "dev_verification_code_used",   # 固定验证码使用（需求8/16）
    "repo_cascade_cleanup",         # 仓库级联清理（Issue #340：解除托管/仓库删除清空关联数据）
})


def _now_str() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _get_last_hash() -> str:
    """获取最后一条审计日志的哈希值。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT curr_hash FROM audit_logs ORDER BY seq DESC LIMIT 1"
            )
            row = c.fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _next_seq() -> int:
    """获取下一个序列号。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT MAX(seq) FROM audit_logs")
            row = c.fetchone()
            return (row[0] + 1) if row and row[0] else 1
    except Exception:
        return 1


def log_audit(
    event_type: str,
    user_id: int = 0,
    operator_id: int = 0,
    ip: str = "",
    detail: "dict | None" = None,
) -> None:
    """记录审计日志。

    参数：
    - event_type: 事件类型（见 EVENT_TYPES）
    - user_id: 被操作的用户 ID
    - operator_id: 操作者 ID（管理员操作时与 user_id 不同）
    - ip: 请求 IP
    - detail: 附加信息（JSON，不可包含密码/验证码等敏感信息）
    """
    if event_type not in EVENT_TYPES:
        log.warning(f"Unknown audit event type: {event_type}")
        return

    try:
        for attempt in range(3):
            with db_lock:
                c = db.cursor()
                seq = _next_seq()
                prev_hash = _get_last_hash()
                created_at = _now_str()

                # 构建哈希内容
                content = json.dumps({
                    "seq": seq,
                    "event_type": event_type,
                    "user_id": user_id,
                    "operator_id": operator_id,
                    "ip": ip,
                    "detail": detail or {},
                    "prev_hash": prev_hash,
                    "created_at": created_at,
                }, sort_keys=True, ensure_ascii=False)
                curr_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

                # 插入（seq UNIQUE 约束——集群并发时冲突重试）
                try:
                    c.execute(
                        "INSERT INTO audit_logs "
                        "(seq, event_type, user_id, operator_id, ip, detail, "
                        "prev_hash, curr_hash, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (seq, event_type, user_id, operator_id, ip,
                         json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                         prev_hash, curr_hash, created_at),
                    )
                    db.commit()
                    return  # 成功，退出重试循环
                except Exception as insert_err:
                    db.rollback()
                    if attempt < 2:
                        # seq 冲突，重试（重新获取 seq + prev_hash）
                        continue
                    raise insert_err  # 最后一次重试仍失败，抛出
    except Exception as e:
        log.warning(f"Audit log failed: {e}")


# ---------------------------------------------------------------------------
# 脱敏工具
# ---------------------------------------------------------------------------

def mask_email(email: str) -> str:
    """邮箱脱敏：alice@example.com → a***@example.com"""
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 1:
        return f"*@{domain}"
    return f"{name[0]}***@{domain}"


def mask_identifier(identifier: str) -> str:
    """标识符脱敏：邮箱用 mask_email，用户名保留首尾字符。"""
    if "@" in identifier:
        return mask_email(identifier)
    if len(identifier) <= 2:
        return identifier
    return f"{identifier[0]}***{identifier[-1]}"
