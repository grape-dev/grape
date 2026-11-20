"""插件配置继承链 —— 五层 overlay 归并语义（Issue #1082）。

Spec 固化合并规则：
- dict 与 dict：对象级深合并（逐键递归归并，未覆盖键自动继承上层）
- 标量 / 列表 / None / 类型冲突：整值覆盖（后者胜，越具体优先级越高）
- 层顺序固定：组织 → 个人 → 仓库 → 节点 → 任务（低 → 高优先级，传入即按此序）

归并为纯函数、无 IO：运行时现场归并（resolve）每一层 overlay 后调用，
无快照副本，组织层改动下层下一次 resolve 立即生效。
"""
from __future__ import annotations

from typing import Any


def merge_overlays(layers: list[dict]) -> dict:
    """按传入顺序（低优先级在前）归并多层差异覆盖，返回有效配置视图。

    合并语义：
    - 双方均为 dict → 逐键深合并（递归）；
    - 其余（标量/列表/None/dict vs 标量类型冲突）→ 整值覆盖。
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        _deep_merge(merged, layer)
    return merged


def _deep_merge(base: dict, overlay: dict) -> None:
    """把 overlay 深合并进 base（就地修改）。dict 键级递归，其余整值覆盖。"""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
