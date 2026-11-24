"""角色能力行的补齐原语（Issue #3396 后续修复）。

背景：注册链与本机发现链原来用「engine_capabilities 已有任意一行」判定是否补
`all`，而派生行（`engine:` / `scene:` / `scope:`）也算一行 —— 于是「只剩派生行」
的引擎永远补不回角色行。Issue #3396 起空角色行是 fail-closed（A7），这类引擎会
静默变成任何角色都不可调度（基线靠 role_tags 默认映射兜底，不会死）。

本原语把口径集中在一处，只数**角色行**（`all` 或 KNOWN_AGENT_ROLES）：

- 已有角色行 → 不动（UI/接口写入优先）；
- `tags_user_set=1` 且角色行为空 → **不动**（用户显式清空＝故意让该引擎停派）；
- 有合法声明 → 按声明补齐（声明即收窄，A8b）；
- 无声明 / 声明非法 → 补 `all`（与基线默认一致）。
"""
from datetime import datetime, timezone

from src.domains.runtime.engine.engine_select import KNOWN_AGENT_ROLES


def role_rows(c, runtime_id):
    """返回 (全部 enabled 标签, 其中属于角色值域的标签)。用于派发/生效判定。"""
    rows = c.execute(
        "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1",
        (int(runtime_id),)).fetchall()
    tags = [str(r[0] or "").strip().lower() for r in rows if str(r[0] or "").strip()]
    return tags, [t for t in tags if t == "all" or t in KNOWN_AGENT_ROLES]


def has_role_rows(c, runtime_id) -> bool:
    """是否**已存在**任何角色行（不论 enabled）。

    补齐判定必须用它而不是 `role_rows`：`enabled=0` 的角色行是「用户/流程显式停用」，
    代表已配置且被关掉——若按「没有启用行」去补，会撞 (runtime_id, tag) 唯一键
    （实测 1062 Duplicate entry '<rid>-all'），且语义上等于擅自把停用重新打开。
    """
    rows = c.execute(
        "SELECT tag FROM engine_capabilities WHERE runtime_id=?",
        (int(runtime_id),)).fetchall()
    for (t,) in rows:
        _t = str(t or "").strip().lower()
        if _t == "all" or _t in KNOWN_AGENT_ROLES:
            return True
    return False


def ensure_role_caps(c, runtime_id, declared=None, user_set=None, now_iso=None):
    """角色行为空时补齐（声明优先，否则 `all`）。

    declared/user_set 为 None 时从 agent_runtimes 行内读取（存量行迁移面）。
    返回实际写入的标签列表；无需动作返回 []。
    """
    try:
        rid = int(runtime_id or 0)
    except (TypeError, ValueError):
        return []
    if not rid:
        return []
    if has_role_rows(c, rid):
        return []  # 已有角色行（含 enabled=0 的显式停用）→ 不补、不重开
    _tags, _roles = role_rows(c, rid)
    if _roles:  # 双保险（正常不会走到）
        return []
    if declared is None or user_set is None:
        row = c.execute(
            "SELECT ifnull(role_tags,''), ifnull(tags_user_set,0) FROM agent_runtimes WHERE id=?",
            (rid,)).fetchone()
        if not row:
            return []
        if declared is None:
            declared = _parse_declared(row[0])
        if user_set is None:
            user_set = row[1] or 0
    if user_set:
        return []  # UI 手工清空 → 尊重用户意图，不自动补
    _valid = [str(t).strip().lower() for t in (declared or []) if str(t).strip()]
    _valid = [t for t in dict.fromkeys(_valid) if t in KNOWN_AGENT_ROLES]
    _write = _valid or ["all"]
    _now = now_iso or datetime.now(timezone.utc).isoformat()
    for _t in _write:
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
            "VALUES (?,?,1,50,1,?,?)",
            (rid, _t, _now, _now))
    return _write


def _parse_declared(raw) -> list:
    """把 role_tags 列（JSON 数组文本）解析成 list；坏数据回落空。"""
    import json
    try:
        _p = json.loads(raw or "[]")
        return _p if isinstance(_p, list) else []
    except Exception:  # noqa: BLE001 - 坏声明按无声明处理（补 all）
        return []