"""D105：数据库主键 ID 生成——UUIDv7 优先（RFC 9562，时间有序利于索引局部性与
ORDER BY id 近似时序），旧解释器/缺库降级 uuid4（格式同为 36 字符标准 UUID，
VARCHAR(36)/TEXT PK 兼容，存量值不受影响）。

范围纪律（业界口径"v7 用于数据库主键，v4 用于其他"）：仅账号/仓库/凭据域的
表主键生成走本助手；session token、trace id、临时文件名等非 DB 标识符
保持 uuid4（不可预测性优先，v7 内嵌时间戳反而泄露信息）。
"""
import uuid

__all__ = ["uuid7_str"]


def uuid7_str() -> str:
    """返回 36 字符标准形式 UUID，v7 优先、v4 兜底。"""
    u7 = getattr(uuid, "uuid7", None)
    if u7 is not None:
        return str(u7())
    try:
        from uuid6 import uuid7 as _uuid7
    except ImportError:
        return str(uuid.uuid4())
    return str(_uuid7())
