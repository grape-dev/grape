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

__all__ = ["row_to_card_raw", "SEMANTIC_EMPTY_KEYS"]

#: 表行里不属于「卡定义」的列（主键/来源/时间戳/启用开关等）
_NON_CARD_COLUMNS = frozenset({"id", "provider", "source", "config_hash", "created_at", "updated_at", "enabled"})

#: **空串有语义**的列：显式 "" = 该平台没有 managed_repos 物理列（S128/D106 终态，
#: 凭据只落 grape_t_repo_credentials 单表），与「键缺席 = 未声明 → 派生列名」必须
#: 区分。其余列的空串一律按「未配置」省略（可选枚举列不能被 "" 判成非法值）。
#:
#: 这张表是**单一事实点**：契约通道（`card_contract`）与表通道都引用它，避免同一
#: 语义两处实现漂移（S128 终态曾被契约/投影两侧各自丢弃空串 → gitee/gitlab 退役
#: 列在每次冷启动被重新派生建出）。
SEMANTIC_EMPTY_KEYS = frozenset({"token_storage_field", "webhook_token_field"})


def row_to_card_raw(
    row: Mapping[str, Any],
    columns: Iterable[str],
    skip: Iterable[str] = (),
) -> dict:
    """表行 → 卡 raw：空值列省略（absent = 未声明），0/False 等有效假值保留。

    例外：`SEMANTIC_EMPTY_KEYS` 的空串是**声明**（该平台无物理列，S128 终态），
    必须原样保留；丢掉它会让 Builder 回退派生列名，把已退役的列重新建出来。
    """
    skipped = _NON_CARD_COLUMNS | frozenset(skip)
    out: dict[str, Any] = {}
    for col in columns:
        if col in skipped:
            continue
        value = row.get(col)
        if value is None:
            continue
        if value == "" and col not in SEMANTIC_EMPTY_KEYS:
            continue
        out[col] = value
    return out