"""一致性哈希环（Issue #742 §4.2/A1）——从 feishu_async.py L1148-1171 原样迁移，行为不变。

50 vnodes 粘性归属：环扩缩容只迁移邻近 bot。
依赖铁律：本模块不 import im_engine 之外任何平台/适配器代码（本层只依赖标准库）。
"""
from __future__ import annotations

import hashlib

DEFAULT_VNODES = 50


def build_ring(instances: list, vnodes: int = DEFAULT_VNODES) -> list:
    """一致性哈希环：[(hash, instance)] 升序。实例均匀打点，增删只影响邻近。"""
    ring: list = []
    for inst in instances:
        for _vn in range(vnodes):
            h = int(hashlib.md5(f"{inst}:{_vn}".encode()).hexdigest()[:8], 16)
            ring.append((h, inst))
    ring.sort()
    return ring


def owner(ring: list, key: str) -> str:
    """key 顺时针落到的实例（归属）。ring 空 → ''。"""
    if not ring:
        return ""
    h = int(hashlib.md5(key.encode()).hexdigest()[:8], 16)
    for hv, inst in ring:
        if hv >= h:
            return inst
    return ring[0][1]


def owners(ring: list, keys: list) -> dict:
    return {k: owner(ring, k) for k in keys}


__all__ = ["build_ring", "owner", "owners", "DEFAULT_VNODES"]
