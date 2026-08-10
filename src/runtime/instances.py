"""引擎实例名（instance_name）派生与解析。

见 runtime-admin-two-view-design.md §3.2：每个引擎实例（agent_runtimes 行）派生出全局唯一的
可读名 `{engine_type}-{device_suffix}`，作为前端展示与 Agent 绑定下拉的选择标识。
"""
import re


def sanitize_device(device_name: str, max_len: int = 24) -> str:
    """device_name → 合法短名：小写、非字母数字转 '-', 去首尾 '-'、截断。"""
    s = (device_name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len].strip("-")


def instance_name(engine_type: str, device_name: str, credential_id) -> str:
    """派生唯一引擎实例名：`{type}-{device_suffix}`；无 device_name 回退 `{type}-cred{credential_id}`。"""
    et = (engine_type or "engine").strip().lower()
    suffix = sanitize_device(device_name)
    if not suffix:
        suffix = f"cred{credential_id or 0}"
    return f"{et}-{suffix}"


def resolve_instance_id(engine_name: str) -> int | None:
    """按唯一引擎名解析 agent_runtimes.id（逐个匹配派生名）。

    调用方传入的 engine_name 是"当前派生名"；这里对每行重算并精确匹配。
    """
    if not engine_name:
        return None
    target = engine_name.strip().lower()
    with _lock():
        c = _db().cursor()
        c.execute("""
            SELECT ar.id, ar.name, ar.credential_id, rc.device_name
            FROM agent_runtimes ar
            LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
        """)
        for rid, et, cid, dev in c.fetchall():
            if instance_name(et, dev or "", cid) == target:
                return rid
    return None


def scope_of_runtime(rid: int) -> str | None:
    """返回 runtime 行所属 scope（user/repo/global）；credential_id=0 的历史行视为 global。"""
    if not rid:
        return None
    with _lock():
        c = _db().cursor()
        c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (rid,))
        row = c.fetchone()
        if not row:
            return None
        cid = row[0] or 0
        if not cid:
            return "global"
        c.execute("SELECT scope FROM runtime_credentials WHERE id=?", (cid,))
        r2 = c.fetchone()
        return r2[0] if r2 else "global"


def _lock():
    from src.session_manager import db_lock
    return db_lock


def _db():
    from src.session_manager import db
    return db
