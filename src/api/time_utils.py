"""时间归一化工具：存储层时间统一输出为 UTC ISO（带时区），供 API 序列化使用。

背景（docs/event-time-localization-design.md）：
- MySQL 后端 created_at 默认 `now()` = CST 本地时间（无时区标记，如 '2026-08-17 20:46:19'）
- SQLite 后端 created_at = `datetime('now')` = UTC（无时区标记）
- started_at / completed_at 等 = `datetime.now(timezone.utc).isoformat()`（带 +00:00）

前端 fmtLocalTime 假定输入为 UTC ISO → 无时区字符串必须由后端按 backend 语义归一化，
否则 MySQL 下 CST 被当 UTC 解析，展示错位 +8h（见 docs/event-time-localization-design.md）。
"""
from datetime import datetime, timedelta, timezone

_CST = timezone(timedelta(hours=8), "CST")


def _db_backend() -> str:
    """当前数据库后端：mysql / sqlite（测试环境由 DEVOPS_DB_BACKEND 或 conftest 覆盖）。"""
    try:
        from src.mysql_compat import load_db_backend_config
        return str(load_db_backend_config().get("backend", "sqlite")).lower()
    except Exception:
        return "sqlite"


def normalize_event_time(raw) -> str:
    """把存储层时间字符串统一为 UTC ISO（带 +00:00 偏移）。

    - 空值/非字符串 → 原样返回
    - 带时区（'Z' 或 '±HH:MM'）→ 解析后转 UTC（已是 ISO，原样语义）
    - 无时区 'YYYY-MM-DD HH:MM:SS[.ffffff]' →
        mysql backend：视为 CST(+08:00) → 转 UTC
        sqlite backend：视为 UTC → 直接标注 +00:00
    - 解析失败 → 原样返回（不破坏数据，不抛异常）
    """
    if not raw:
        return raw
    s = str(raw).strip()
    if not s:
        return s
    try:
        # 带时区：Z 结尾或 +HH:MM / -HH:MM 偏移
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        elif len(s) > 10 and ("+" in s[10:] or "-" in s[10:]):
            dt = datetime.fromisoformat(s)
        else:
            # 无时区：按 backend 语义补时区
            dt = datetime.fromisoformat(s.replace(" ", "T"))
            if _db_backend() == "mysql":
                dt = dt.replace(tzinfo=_CST)
            else:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return s
