"""D105 之后的 user_id 语义工具：判「已归属/未归属」，禁止数值比较。

背景：grape_accounts.id 自 D105 起是 UUID 字符串（36 字符），user_id 族列同步改为
TEXT / VARCHAR(36)。历史代码大量使用 ``user_id > 0`` 判断"是否归属某个用户"：

* Python 侧：uuid 字符串与 int 比较直接抛
  ``TypeError: '>' not supported between instances of 'str' and 'int'``
  （生产实锤：enqueue_task 失败原因就是这句）。
* SQL 侧：SQLite 的类型排序把 text 排在 integer 之后，``user_id > 0`` 对字符串列恒真；
  MySQL 则把非数字串强转 0，``user_id > 0`` 恒假。两种后端都不是"未归属"的意思。

统一口径：未归属 = ``None`` / ``""`` / ``0`` / ``"0"`` / ``"none"`` / ``"null"``，
其余一律视为已归属。判断请统一走本模块，不要再写数值比较。
"""

from typing import Any

__all__ = ["USER_ID_UNSET", "has_user_id", "norm_uid", "user_id_set_sql", "user_id_unset_sql"]

# 未归属哨兵的字面量集合（小写比较，覆盖 int 0 / str "0" / 空串 / None 字符串化）
USER_ID_UNSET = frozenset({"", "0", "none", "null"})


def has_user_id(value: Any) -> bool:
    """值是否代表「已归属某个用户」。

    兼容三种历史形态：UUID 字符串（当前）、int 主键（旧库）、None / 0 哨兵。
    布尔值 True/False 不视为 id（避免 ``has_user_id(True)`` 被误判成 1）。
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in USER_ID_UNSET


def norm_uid(value: Any) -> int | str:
    """账号 id 归一：纯数字串转 ``int``，其余（D105 后的 UUIDv7 字符串）原样保留。

    历史通知链直接 ``int(user_id)``，对 UUID 抛 ``ValueError`` 后被各层 ``except``
    静默吞掉，表现为「通知没发但调用方以为成功」（Issue #3510）。纯数字仍转 int，
    保证既有以 int 参与 sha1 的 ``out_track_id`` 单号不漂移（跨进程幂等不受影响）。
    """
    text = str(value if value is not None else "").strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        return text


def user_id_set_sql(col: str = "user_id") -> str:
    """SQL 片段：该列表示「已归属」（TEXT / INTEGER 两种物理类型都成立）。"""
    return f"{col} IS NOT NULL AND {col} NOT IN ('', '0')"


def user_id_unset_sql(col: str = "user_id") -> str:
    """SQL 片段：该列表示「未归属/全局」。"""
    return f"({col} IS NULL OR {col} IN ('', '0'))"