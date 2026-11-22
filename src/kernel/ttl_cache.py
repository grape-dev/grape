"""进程内 TTL 缓存（通用横切组件，零业务依赖）。

为什么放 kernel：缓存是最典型的横切关注点，任何域都可能需要；把它写成
可组合组件（组合优于在业务函数里内联 if/else），业务侧只表达「取或算」：

    cache = TTLCache("vcs-spaces", ttl_provider=lambda: 1800,
                     key_builder=lambda provider, token: f"{provider}:{fp(token)}")
    value = cache.get_or_compute((provider, token), lambda: collect(provider, token))

设计约束：
- TTL 每次调用时读取（配置改了立即生效；<=0 直接绕过缓存，不残留旧值语义）。
- 值深拷贝进出（缓存持有者与调用方互不影响）。
- 键不含明文凭据时由调用方保证；本组件只负责存取。
- 命中/未命中/清除计数暴露给 stats()，便于运维观测（不做埋点副作用）。
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Hashable


class TTLCache:
    """最小可行的进程内 TTL 缓存（线程安全，多进程各自持有）。"""

    __slots__ = ("name", "_ttl", "_clock", "_store", "_lock", "_hits", "_misses")

    def __init__(self, name: str, ttl_provider: Callable[[], int],
                 clock: Callable[[], float] = time.time) -> None:
        self.name = name
        self._ttl = ttl_provider          # 秒；每次调用读取，支持运行时改配置
        self._clock = clock
        self._store: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    # ── 存取 ────────────────────────────────────────────────────────
    def get(self, key: Hashable) -> Any | None:
        """命中返回深拷贝；未命中/已过期返回 None。"""
        ttl = int(self._ttl())
        if ttl <= 0:
            return None
        now = self._clock()
        with self._lock:
            ent = self._store.get(key)
            if ent is None:
                return None
            ts, value = ent
            if now - ts >= ttl:
                self._store.pop(key, None)
                return None
            self._hits += 1
            return copy.deepcopy(value)

    def set(self, key: Hashable, value: Any) -> None:
        ttl = int(self._ttl())
        if ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            # 顺手清过期项，避免长期运行后字典无界增长
            for k, (ts, _v) in list(self._store.items()):
                if now - ts >= ttl:
                    self._store.pop(k, None)
            self._store[key] = (now, copy.deepcopy(value))

    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        hit = self.get(key)
        if hit is not None:
            return hit
        with self._lock:
            self._misses += 1
        value = compute()
        self.set(key, value)
        return copy.deepcopy(value)

    # ── 运维 ────────────────────────────────────────────────────────
    def invalidate(self, prefix: str = "") -> int:
        """清缓存（prefix 空 = 全清）；返回清除条数。"""
        with self._lock:
            if not prefix:
                n = len(self._store)
                self._store.clear()
                return n
            keys = [k for k in self._store if str(k).startswith(prefix)]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {"name": self.name, "entries": len(self._store),
                    "hits": self._hits, "misses": self._misses,
                    "hit_rate": round(self._hits / total, 4) if total else 0.0,
                    "ttl_seconds": int(self._ttl())}