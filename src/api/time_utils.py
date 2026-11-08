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
    """当前数据库后端：mysql / sqlite（测试环境由 DEVOPS_DB_BACKEND 或 conftest 覆盖）。

    与 src.mysql_compat.get_db_backend() 同口径：环境变量 DEVOPS_DB_BACKEND 优先，
    其次配置文件。否则 conftest 强制 sqlite 时本函数仍返回 mysql，
    parse_dt/migrate 会把 SQLite 的 UTC naive 值按 CST 解析（Issue #512 回归，
    test_event_conversation TestLocalRetry 409 根因）。
    """
    try:
        from src.mysql_compat import get_db_backend
        return get_db_backend()
    except Exception:
        return "sqlite"


def db_now_str(offset_seconds: int = 0) -> str:
    """按**存储层时间口径**返回当前时间字符串（'YYYY-MM-DD HH:MM:SS'），offset 秒偏移。

    与 parse_dt / normalize_event_time 是同一口径的正反向：表默认值在 MySQL 下由
    NOW() 写成 CST 本地墙钟、在 SQLite 下由 datetime('now') 写成 UTC，两者都是
    无时区字符串。任何「created_at >= 某个 cutoff」的 SQL 比较必须用本函数生成
    cutoff，否则两侧时区错位（MySQL 下 UTC cutoff 会把 60s 窗口放大到 8h+）。

    反面案例（Issue #1487 衍生）：event_pipeline 的去重②曾用 _utc_now_str(-60)
    （Python UTC 字符串）与 events.created_at（MySQL 本地墙钟）比较，导致 PR 评论
    等 webhook 类事件在同 (repo,issue,agent,event_type) 8 小时内被静默判重丢弃。
    """
    from datetime import datetime as _dt, timezone as _tz
    ts = _dt.now(_tz.utc).timestamp() + offset_seconds
    if _db_backend() == "mysql":
        return _dt.fromtimestamp(ts, _CST).strftime("%Y-%m-%d %H:%M:%S")
    return _dt.fromtimestamp(ts, _tz.utc).strftime("%Y-%m-%d %H:%M:%S")


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
