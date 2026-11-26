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

# 引擎名规范化表（aishell-dsh 接入，2026-11-06）：实例名前缀 → 规范引擎键。
# 解决散落各处的 startswith/in 判定顺序问题（aishell-dsh 必须先于 aishell 命中）：
# 匹配用最长前缀（与书写顺序无关），所有引擎判定点统一改走 canonical_engine()。
# 注意：hwcloud 与 aishell 前缀都映射回 hwcloud 后端（D1：旧名不动，backend 键不变）。
# [P0b B3] 表数据源=engine/descriptors/*.json（prefix_map 派生：canonical+aliases，
# 排除 is_default 与 ghost 引擎）；drift 守卫对账（本处已派生则恒一致）。
def _descriptor_prefix_map() -> dict[str, str]:
    from src.domains.runtime.engine.descriptor import get_registry
    return get_registry().prefix_map()

ENGINE_CANONICAL_PREFIXES = _descriptor_prefix_map()


def canonical_engine(engine_name: str | None) -> str:
    """由引擎实例名推导规范引擎键（前缀最长匹配；未知名字 → opencode）。

    长前缀优先保证 aishell-dsh-xxx 命中 aishell-dsh 而非 aishell；表序无关。
    单测锁死：tests/test_canonical_engine.py。
    """
    name = (engine_name or "").strip().lower()
    best_key = ""
    best_val = ""
    for prefix, canonical in ENGINE_CANONICAL_PREFIXES.items():
        if name.startswith(prefix) and len(prefix) > len(best_key):
            best_key, best_val = prefix, canonical
    return best_val if best_key else "opencode"


def page_type_of_instance(engine_name: str | None) -> str:
    """[B6 尾/C08-A2] 实例名→页面类型单源派生（settings._engine_type_of 迁移至此）。

    canonical→页面标签经 registry.page_type() descriptor 派生（hwcloud 显示
    历史名 aishell，单源）。非前缀历史名 includes 兜底=顺序敏感旧轨判定，
    链留代码（B3 点 7 裁定），全仓只此一份；标签同样经 page_type() 派生。
    """
    n = (engine_name or "").strip().lower()
    if not n:
        return ""
    from src.domains.runtime.engine.descriptor import get_registry
    _pt = get_registry().page_type
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
        return _pt(c)
    # 旧轨兜底：名字中段包含引擎词的历史形态（顺序冻结：aishell-dsh 先于
    # aishell/hwcloud，codearts/codex 先于 atomcode）
    if "aishell-dsh" in n:
        return _pt("aishell-dsh")
    if "aishell" in n or "hwcloud" in n:
        return _pt("hwcloud")
    if "hermes" in n:
        return _pt("hermes")
    if "opencode" in n:
        return _pt("opencode")
    if "codearts" in n:
        return _pt("codearts")
    if "codex" in n:
        return _pt("codex")
    if "atomcode" in n:
        return _pt("atomcode")
    return ""


# usage 为空时值得短重试的引擎（日志/账本异步落盘型）；aishell-dsh 的账本
# 在会话文件（二期解析），一期虽无 usage 也保留重试口径一致。
# [P0b B3] 清单数据源=descriptor usage_retry 位。
def _descriptor_usage_retry() -> set[str]:
    from src.domains.runtime.engine.descriptor import get_registry
    return {c for c, d in get_registry().all().items() if d.get("usage_retry")}

USAGE_RETRY_ENGINES = _descriptor_usage_retry()


def usage_retry_engine(engine_name: str | None) -> bool:
    """该引擎 usage 为空时是否值得短重试一次（统一判定，替代散落的元组成员检查）。"""
    return canonical_engine(engine_name) in USAGE_RETRY_ENGINES


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


def local_engine_device_name(host_name: str | None, local_hostname: str | None = None) -> str:
    """本机直连引擎（credential_id=0）挂载到的设备名 = host_name（无则本机 hostname）。

    设备视图（_build_runtime_devices）与凭据任务聚合
    （GET /api/runtime/credentials/{cid}/tasks）共用此口径，保持单一来源。
    """
    if not local_hostname:
        # Issue #2094：设备身份统一走 engine_host()（GRAPE_HOSTNAME 优先，容器
        # 重建不漂移）；本函数本身就是「本机直连引擎挂载设备名」的单一来源，
        # 设备视图与凭据任务聚合共用，不得绕开 host_identity 直取系统主机名。
        try:
            from src.kernel.host_identity import engine_host
            local_hostname = engine_host()
        except Exception:
            local_hostname = ""
    return (host_name or local_hostname or "").strip()


def engine_mounts_to_credential(credential_id, host_name: str | None,
                                cred_device_name: str | None) -> bool:
    """引擎是否挂载到某凭据的设备（与设备视图挂载逻辑同一口径）。

    - credential_id == 该凭据 id：引擎直接注册在该凭据下；
    - credential_id == 0（本机直连）：host_name（或本机 hostname）等于凭据
      device_name 时挂载到该设备——设备视图据此把本机引擎展示在凭据设备下，
      任务聚合必须同样计入，否则设备「查看任务」错误显示「暂无任务」。
    """
    if (credential_id or 0) != 0:
        return False
    dev = (cred_device_name or "").strip()
    if not dev:
        return False
    return local_engine_device_name(host_name) == dev


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
    # agent_runtimes / runtime_credentials 归运行域：三库独立后同域代码也必须走 owner 连接
    # （进程库在非 runtime 域进程里是别域的库，直查即 1146）
    from src.domains.runtime import public as _rt_inst

    if scope:
        _rows = _rt_inst.read_all_any("runtimes_for_instance_scan_scope", (scope,))
    else:
        _rows = _rt_inst.read_all_any("runtimes_for_instance_scan")
    for rid, et, cid, dev, cst in _rows:
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
        from src.domains.runtime import public as _rt_name

        row = _rt_name.read_one_any("runtime_instance_row", (runtime_id,))
        if row:
            return instance_name(row[0], row[1] or "", row[2] or 0)
    except Exception:
        pass
    return ""


def scope_of_runtime(rid: int) -> str | None:
    """返回 runtime 行所属 scope（user/repo/global）；credential_id=0 的历史行视为 global。"""
    if not rid:
        return None
    from src.domains.runtime import public as _rt_scope

    row = _rt_scope.read_one_any("runtime_credential_id", (rid,))
    if not row:
        return None
    cid = row[0] or 0
    if not cid:
        return "global"
    r2 = _rt_scope.read_one_any("runtime_credential_scope", (cid,))
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
        from src.domains.runtime import public as _rt_type

        row = _rt_type.read_one_any("agent_runtime_name_by_id", (rid,))
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def _lock():
    from src.kernel.session_manager import db_lock
    return db_lock


def _db():
    from src.kernel.session_manager import db
    return db
