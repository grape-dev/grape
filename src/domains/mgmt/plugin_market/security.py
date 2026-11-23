"""插件市场供应链安全服务（B2，Issue #1085）。

D5 双轨信任模型：默认开放发布 + 官方认证。本模块是 B1（#1084）发布/消费链路的
安全校验 seam，供路由层嵌入调用：
- @owner 命名空间防抢注：``ensure_namespace_ownership``（发布前归属校验，越权 403）
- content_hash 完整性：``verify_content_hash``（下载/安装/物化重算指纹比对，篡改 409）
- 认证位：``set_certification``（管理端标记/撤销，操作留痕）

数据模型：B1 ``plugin`` 表增量列（namespace_owner / certification 系列，含存量
迁移回填），不另建插件表——与 B1 市场卡片/列表共用同一插件对象。
"""
import hashlib
import hmac
import re
from datetime import datetime, timezone

# 认证位取值（D5 双轨：未认证 / 官方认证）
CERT_NONE = "none"
CERT_OFFICIAL = "official"

CERT_AUDIT_TABLE = "grape_t_plugin_certification_audit"

# @owner 命名空间插件名：@{owner}/{bare}（两段字符白名单对齐 skills 对象键规范）
_PLUGIN_NAME_NS_RE = re.compile(r"^@([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")


class NamespaceViolationError(PermissionError):
    """@owner 命名空间越权（非命名空间所有者发布，路由层映射 403）。"""


def parse_plugin_name(name: str) -> tuple:
    """解析插件名 → (namespace_owner, bare_name)。

    带 @owner 前缀 → (owner, 去前缀名)；裸名/其他 → ('', strip 后原名)。
    不做合法性裁决（裸名合法性由 B1 对象键规范兜底），只识别命名空间结构。
    """
    name = (name or "").strip()
    m = _PLUGIN_NAME_NS_RE.match(name)
    if m:
        return m.group(1), m.group(2)
    return "", name


def ensure_namespace_ownership(name: str, publisher: str) -> tuple:
    """@owner 命名空间归属校验（B1 发布链路集成点，上传静态校验后调用）。

    带 @owner 命名空间且 namespace_owner != 发布者身份 → NamespaceViolationError
    （冒名发布/抢注他人命名空间，403 拒绝且不入库）；裸名（开放轨）放行。
    归属比较大小写不敏感（平台账号名大小写不敏感唯一，#348 LOWER 唯一索引），
    命中时返回规范发布者账号名与裸名，避免入库大小写旁支。
    返回 (namespace_owner, bare_name)。
    """
    ns_owner, bare = parse_plugin_name(name)
    if ns_owner and ns_owner.casefold() != (publisher or "").strip().casefold():
        raise NamespaceViolationError(
            f"命名空间 @{ns_owner} 不属于发布者 {publisher or ''}，拒绝发布（防抢注）")
    return ((publisher or "").strip() if ns_owner else ""), bare


def certification_projection(certification: str) -> dict:
    """认证位卡片投影（list/detail/管理端返回共用同一实现，避免三处散写）。"""
    cert = certification or CERT_NONE
    return {"certification": cert, "certified": cert == CERT_OFFICIAL}


def compute_content_hash(data: bytes) -> str:
    """包内容指纹：sha256 hex（与 B1 上传入库格式一致，上传与重算共用同一实现）。"""
    return hashlib.sha256(data or b"").hexdigest()


def verify_content_hash(expected: str, data: bytes) -> bool:
    """指纹比对（恒时比较，消费链路统一校验入口：下载/安装/物化取包后调用）。

    expected 为空视为存量未登记指纹（Spec 存量口径：置空并在下次上传补算），放行；
    非空则与重算指纹严格比对，不一致 → False（路由层映射 409 拒绝落出）。
    """
    if not (expected or "").strip():
        return True
    return hmac.compare_digest(expected.strip(), compute_content_hash(data))


def set_certification(plugin_id: int, certified: bool, operator: str) -> dict:
    """认证位标记/撤销（仅管理端调用，路由层挂 require_admin）：状态落库 + 操作留痕。

    D5 双轨：certification = official（标记）/ none（撤销）；撤销清标记留
    revoked_at，操作人每次写入（Spec 风险项：仅管理端可标/撤 + 操作留痕）。
    返回卡片投影字段 {"certification", "certified", "certified_by", "certified_at"}。
    """
    from src.kernel.session_manager import db, db_lock
    if not (operator or "").strip():
        raise ValueError("operator required")
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        if c.execute("SELECT 1 FROM grape_t_plugin WHERE id=?", (plugin_id,)).fetchone() is None:
            raise LookupError(f"plugin not found: {plugin_id}")
        if certified:
            c.execute(
                "UPDATE grape_t_plugin SET certification=?, certified_by=?, certified_at=?, "
                "revoked_at='', updated_at=datetime('now') WHERE id=?",
                (CERT_OFFICIAL, operator.strip(), now, plugin_id))
            action = "certify"
        else:
            # 撤销同步清认证人/时间（详情不残留历史认证人；历史留审计表）
            c.execute(
                "UPDATE grape_t_plugin SET certification=?, certified_by='', certified_at='', "
                "revoked_at=?, updated_at=datetime('now') WHERE id=?",
                (CERT_NONE, now, plugin_id))
            action = "revoke"
        c.execute(
            f"INSERT INTO {CERT_AUDIT_TABLE} (plugin_id, action, operator, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (plugin_id, action, operator.strip(),
             f"certification -> {CERT_OFFICIAL if certified else CERT_NONE}", now))
        db.commit()
    proj = certification_projection(CERT_OFFICIAL if certified else CERT_NONE)
    proj.update({"certified_by": operator.strip() if certified else "",
                 "certified_at": now if certified else ""})
    return proj
