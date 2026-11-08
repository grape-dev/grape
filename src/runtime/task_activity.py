"""task_activity：task_messages 活跃度判定统一口径（Issue #1495）。

病根：回收/回炉链路原先用「created_at > cutoff 形状的逐字节比较谓词」做活跃度判定——
created_at 是 TEXT 列，该谓词是**逐字节字符串比较**，cutoff 由
Python `datetime.now(timezone.utc).isoformat()` 生成（T 分隔、带微秒、+00:00）。
两类现网存量/异形格式因此被系统性误判为「不活跃」：
  1. 表默认值行（SQLite `datetime('now')` / MySQL `now()` → 'YYYY-MM-DD HH:MM:SS'
     空格分隔）：同日内第 11 字节 ' '(0x20) < 'T'(0x54) → 恒早于 cutoff →
     长命令静默期（terraform apply / 长编译）的活跃任务被误判死任务：
     误杀旧进程 + attempts+1 → 三次后强制 failed。
  2. 无微秒 ISO 行（'...T08:00:00+00:00'）与同秒 cutoff
     （'...T08:00:00.123456+00:00'）比较：第 19 字节 '+'(0x2B) < '.'(0x2E) →
     同秒窗口塌陷为不活跃（与 health_scorer.py 记录的 '...:42+00:00' vs
     '...:42.916425+00:00' 字典序误判同一个坑）。

修法（Spec 拍板）：SQL 仅按 task_id（idx_task_messages_task 索引列）过滤取
created_at，**禁止在 SQL 里对 created_at 施任何函数运算**（4.8M 行表、索引不可
退化）；取回后 Python 侧宽容解析为 epoch，与 `now - window` 做数值比较。解析口径
复用 `src.runtime.health.parse_dt`（与 service._epoch_of / scheduler 判死扫描同源）：
- ISO 带微秒/无微秒、带 +00:00/Z 后缀均可解析；
- 空格分隔 naive 串按双后端语义解析（mysql=CST(+08:00) 墙钟、sqlite=UTC），
  与存储层归一口径（Issue #512）一致；
- 解析失败的行不计入活跃（保守：宁可误回收也不留死任务，延续既有取舍）。

窗口常量单点定义（Claim 兜底 600s / recover_orphans 1200s，数值沿用现状不调整，
Spec 明确「一处定义」指每值单点）；本模块所有函数均为纯内存计算（一次索引查询 +
字符串解析），无新增网络/磁盘 IO，调用方必须持有 db_lock。
"""
import time

# claim 兜底回收活跃窗口（秒）：SRE 执行 terraform apply 等长命令可达 900s 无事件
# 产出；600s 窗口让长命令有足够时间完成（数值沿用 service.py 现状，Spec 不调整）。
CLAIM_ACTIVITY_WINDOW_SECONDS = 600

# recover_orphans 活跃窗口（秒）：兼容 hermes 长命令静默窗口 + 分次等待策略
# （数值沿用 runtime.py RECOVER_ACTIVITY_WINDOW 默认值，Spec 不调整）。
RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS = 1200

# 每任务最多检查的最近消息行数：单任务窗口内消息极少，覆盖静默期前的尾部产出
# 足够；行数上限防极端历史任务（万行消息）在 claim 循环内拖慢回收判定。
_MAX_ROWS_PER_TASK = 50


def _epoch_of(value) -> float | None:
    """库中时间串 → epoch 秒；不可解析返回 None。

    复用 health.parse_dt 的双后端口径（mysql=naive 视为 CST(+08:00)、
    sqlite=naive 视为 UTC），与 service._epoch_of / scheduler 判死扫描同源，
    不引入第三种解析口径。
    """
    if not value:
        return None
    try:
        from src.runtime.health import parse_dt
        _v = parse_dt(value)
        return float(_v) if _v is not None else None
    except Exception:
        return None


def has_recent_activity(cursor, task_id, window_seconds: int,
                        now_epoch: float | None = None) -> bool:
    """该任务最近 window_seconds 秒内是否有活跃（可解析）的 task_messages 产出。

    Args:
        cursor: 调用方在 db_lock 内持有的 DB 游标（不另开连接、不提交）。
        task_id: 任务 id（索引列过滤，SQL 形态对双后端一致）。
        window_seconds: 活跃窗口秒数（调用方取本模块具名常量）。
        now_epoch: 可注入当前 epoch（测试确定性用）；缺省取真实 UTC now。

    Returns:
        True = 窗口内有可解析的活跃产出（跳过回收）；False = 无产出/解析失败/
        查询异常（保守按「不活跃」处理，维持宁可误回收也不留死任务的取舍）。
    """
    try:
        cursor.execute(
            "SELECT created_at FROM task_messages WHERE task_id=? "
            "ORDER BY id DESC LIMIT ?",
            (task_id, _MAX_ROWS_PER_TASK))
        rows = cursor.fetchall()
    except Exception:
        return False  # 查询失败保守跳过检查（保持旧行为：回收）
    _cutoff = (now_epoch if now_epoch is not None else time.time()) - float(window_seconds)
    for (created_at,) in rows or []:
        _e = _epoch_of(created_at)
        if _e is not None and _e >= _cutoff:
            return True
    return False
