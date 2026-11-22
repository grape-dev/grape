"""平台卡「表行投影」适配器（D109 / Issue #3580）。

职责边界：跨域读原语（mgmt public）返回的是**表行投影**——NULL 列被统一成 ""；
平台卡 Builder 眼里的 raw 是**声明语义**——键缺席 = 未声明，键存在 = 显式给了值。

两者语义不同，直接照抄会让可选枚举列（branch_create_form / issue_write_form /
pr_head_filter …）以 "" 的形态被当成「显式非法值」，整卡校验失败 → `_build_cards`
回退文件卡 → 表行永远不生效（2026-11-20 实锤：部署实例每次重建都打
"meta 合并卡非法（gitlab: branch_create_form 必须是 form/refs_sha: ''），回退文件卡"）。

本模块就是这两层之间的适配器：**空值列省略**，真实假值（0/False）保留。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = ["row_to_card_raw"]

#: 表行里不属于「卡定义」的列（主键/来源/时间戳/启用开关等）
_NON_CARD_COLUMNS = frozenset({"id", "provider", "source", "config_hash", "created_at", "updated_at", "enabled"})


def row_to_card_raw(
    row: Mapping[str, Any],
    columns: Iterable[str],
    skip: Iterable[str] = (),
) -> dict:
    """表行 → 卡 raw：空值列省略（absent = 未声明），0/False 等有效假值保留。"""
    skipped = _NON_CARD_COLUMNS | frozenset(skip)
    out: dict[str, Any] = {}
    for col in columns:
        if col in skipped:
            continue
        value = row.get(col)
        if value is None or value == "":
            continue
        out[col] = value
    return out