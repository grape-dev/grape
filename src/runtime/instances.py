"""引擎实例名（instance_name）派生与解析。

见 runtime-admin-two-view-design.md §3.2：每个引擎实例（agent_runtimes 行）派生出全局唯一的
可读名 `{engine_type}-{device_suffix}`，作为前端展示与 Agent 绑定下拉的选择标识。
"""
import re


def sanitize_device(device_name: str, max_len: int = 24) -> str:
    """device_name → 合法短名：小写、非字母数字转 '-', 去首尾 '-'、截断。

    截断用「保头保尾」：保留末尾数字段（机器编号 0002/0003...），否则
    worker-ecs-grape-dev-build-0002..0006 等长 device 截断 24 字符后全变成
    同一前缀，多实例 instance_name 撞车（2026-08-17 事故：新旧行全解析到旧行）。
    """
    s = (device_name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) <= max_len:
        return s
    m = re.search(r"(\d+)$", s)
    tail = m.group(1) if m else s[-3:]
    head = s[: max(1, max_len - len(tail) - 1)]
    return f"{head}-{tail}".strip("-")[:max_len].strip("-")


# 引擎显示名映射（内部标识 → 用户可见名）。
# hwcloud → aishell（2026-10-13）：DB name/路由键/binary 全部保持 hwcloud 不动，
# 仅展示层（instance_name 派生前缀/display_name/前端卡片）用 AIShell。
# 显示名要求 sanitize_device 合法（小写字母数字）；注册时 display_name 用大写 AIShell。
ENGINE_DISPLAY_NAMES = {
    "hwcloud": "aishell",
}


def engine_display_name(engine_type: str) -> str:
    """内部引擎名 → 展示名（小写，instance_name 派生用）；未映射原样返回。"""
    et = (engine_type or "engine").strip().lower()
    return ENGINE_DISPLAY_NAMES.get(et, et)


def to_display(name: str) -> str:
    """API 出口统一显示名转换（大写 AIShell）：内部名 hwcloud → AIShell。

    供 settings/dashboard 等 API 在返回引擎列表前替换 name/display_name 字段，
    前端所有读 rt.name 的位置一次生效。内部路由键不受影响（仅出口转换）。
    """
    et = (name or "").strip().lower()
    dn = ENGINE_DISPLAY_NAMES.get(et, "")
    if not dn:
        return name or ""
    return dn.upper() if dn == "aishell" else dn


def instance_name(engine_type: str, device_name: str, credential_id) -> str:
    """派生唯一引擎实例名：`{display_name}-{device_suffix}`；无 device_name 回退 `{display_name}-cred{credential_id}`。

    展示名映射见 ENGINE_DISPLAY_NAMES（hwcloud 派生 aishell-worker-xxx）。
    """
    et = engine_display_name(engine_type)
    suffix = sanitize_device(device_name)
    if not suffix:
        suffix = f"cred{credential_id or 0}"
    return f"{et}-{suffix}"


def resolve_instance_id(engine_name: str, scope: str | None = None) -> int | None:
    """按唯一引擎名解析 agent_runtimes.id（逐个匹配派生名）。

    调用方传入的 engine_name 是"当前派生名"；这里对每行重算并精确匹配。
    scope 非空时仅在该作用域内匹配（如 Chat 配置只匹配 user 个人引擎，
    避免 device_name 截断同名时误中 global 实例）。

    只认可用引擎（2026-08-17 修复）：本机直连（credential_id=0）或凭据
    status='active' 的行；凭据已吊销（revoked）的行跳过——否则同 instance_name
    撞车时第一个命中的常是历史旧行（实测 resolve('hermes-...')→18、'opencode-...'→17
    均为 revoked 凭据行，用户配置的引擎实际落到已注销的引擎上）。
    """
    if not engine_name:
        return None
    target = engine_name.strip().lower()
    with _lock():
        c = _db().cursor()
        if scope:
            c.execute("""
                SELECT ar.id, ar.name, ar.credential_id, rc.device_name, rc.status
                FROM agent_runtimes ar
                LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
                WHERE ar.scope = ?
                ORDER BY CASE WHEN ar.credential_id = 0 OR rc.status IS NULL OR rc.status = 'active' THEN 0 ELSE 1 END, ar.id
            """, (scope,))
        else:
            c.execute("""
                SELECT ar.id, ar.name, ar.credential_id, rc.device_name, rc.status
                FROM agent_runtimes ar
                LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
                ORDER BY CASE WHEN ar.credential_id = 0 OR rc.status IS NULL OR rc.status = 'active' THEN 0 ELSE 1 END, ar.id
            """)
        for rid, et, cid, dev, cst in c.fetchall():
            # 双名匹配（ENGINE_DISPLAY_NAMES 引入后的兼容）：先试新派生名
            # （aishell-worker-xxx），再试原名派生（hwcloud-worker-xxx）——历史
            # 保存的 Agent 绑定/Chat 配置仍是旧名，改名不断链。
            _new = instance_name(et, dev or "", cid)
            _old = f"{(et or 'engine').strip().lower()}-{sanitize_device(dev or '') or f'cred{cid or 0}'}"
            if _new == target or (_new != _old and _old == target):
                # 可用：本机直连（cred 0）、凭据行不存在（LEFT JOIN 无行，测试/新环境）、凭据 active；
                # 仅凭据存在且 revoked 的旧行排除（2026-08-17 修复）
                if (cid or 0) == 0 or cst is None or cst == "active":
                    return rid
    return None


def runtime_instance_name(runtime_id) -> str:
    """按 runtime_id 反查引擎实例名（`{type}-{device}`，与 UI 同源）。查不到返回空串。

    日志与 events.runtime 用统一实例名（如 `opencode-worker-123249`），而非仅类型名 `opencode`。
    """
    if not runtime_id:
        return ""
    try:
        with _lock():
            c = _db().cursor()
            c.execute(
                "SELECT ar.name, rc.device_name, ar.credential_id "
                "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                "WHERE ar.id=?",
                (runtime_id,),
            )
            row = c.fetchone()
            if row:
                return instance_name(row[0], row[1] or "", row[2] or 0)
    except Exception:
        pass
    return ""


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


def engine_type_of_runtime(rid: int) -> str:
    """按 runtime_id 反查引擎类型名（agent_runtimes.name，如 opencode/hermes）。

    session 引擎守卫用：消费 issue_agent_session.runtime_session_id 前校验
    "指针引擎 == 本次执行引擎"，不一致则弃用指针（开新会话），防止引擎池混跑时
    hermes 拿到 opencode 的会话号 resume 假成功后静默挂死。查不到返回空串（守卫放行）。
    """
    if not rid:
        return ""
    try:
        with _lock():
            c = _db().cursor()
            c.execute("SELECT name FROM agent_runtimes WHERE id=?", (rid,))
            row = c.fetchone()
            return (row[0] or "") if row else ""
    except Exception:
        return ""


def _lock():
    from src.session_manager import db_lock
    return db_lock


def _db():
    from src.session_manager import db
    return db
