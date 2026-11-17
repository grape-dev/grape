"""P2-6 五维白名单匹配——给 proxy.py 用。

五维：domain + method + path_prefix + scheme + port
全部匹配才允许。

设计：
- normalize_path：unquote 一次 + 拒绝残留 % + .. pop 回溯 + 去尾斜杠
- match_path_prefix：段前缀匹配（/api/v5 匹配 /api/v5/repos 不匹配 /api/v50）
- match_whitelist：五维逐维匹配
- compile_rules：DB row → CompiledRule（path_prefix 在加载时归一化一次）
- 内存缓存 + 主动失效

不给 sandbox NetworkProxy 用——NetworkProxy 走 match_domain（P1 行为）。
"""
from __future__ import annotations

import logging
from urllib.parse import unquote

from .types import WhitelistRule, CompiledRule

log = logging.getLogger("devops-bot")


# ===== path 归一化 =====

def normalize_path(path: str) -> str:
    """URL path 归一化——防穿越。

    unquote 一次 + 拒绝残留 %（防双重编码，fail-closed）。
    .. 回溯(pop) / . 丢弃 / // 合并 / 去尾斜杠。
    """
    if not path:
        return "/"
    path = unquote(path)
    if '%' in path:
        raise ValueError(f"path contains unresolved percent-encoding: {path[:50]}")
    parts: list[str] = []
    for seg in path.split('/'):
        if seg == '..':
            if parts:
                parts.pop()
            continue
        if seg == '.' or not seg:
            continue
        parts.append(seg)
    result = '/' + '/'.join(parts)
    if len(result) > 1 and result.endswith('/'):
        result = result.rstrip('/')
    return result


def match_path_prefix(path: str, prefix: str) -> bool:
    """段前缀匹配。/api/v5 匹配 /api/v5/repos 但不匹配 /api/v50。
    * 匹配所有。不支持中间通配。
    """
    if prefix == '*':
        return True
    norm_path = normalize_path(path)
    norm_prefix = normalize_path(prefix)
    if norm_path == norm_prefix:
        return True
    return norm_path.startswith(norm_prefix + '/')


# ===== domain 匹配（复用 P1 match_domain 语义） =====

def match_domain(host: str, pattern: str) -> bool:
    """白名单域名匹配——支持通配符。DNS 大小写不敏感，统一转小写。"""
    host = host.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        if "." not in suffix or suffix.startswith(".") or suffix.endswith("."):
            return False
        host_base = host.split(":")[0]
        return host_base.endswith("." + suffix)
    host_base = host.split(":")[0]
    pattern_base = pattern.split(":")[0]
    return host_base == pattern_base


# ===== 五维匹配 =====

def match_whitelist(
    host: str, method: str, path: str,
    scheme: str = "https", port: int = 443,
    rules: list[CompiledRule] | None = None,
) -> tuple[bool, str]:
    """五维白名单匹配。

    维度：domain + method + path_prefix + scheme + port
    全部匹配才允许。

    Returns:
        (allowed, reason) — reason 格式："matched:rule_id=12" / "no_match"
    """
    if not rules:
        return False, "no_match"

    host_lower = host.lower()
    method_upper = method.upper()
    scheme_lower = scheme.lower()

    for rule in rules:
        # 1. domain
        if not match_domain(host_lower, rule.domain):
            continue
        # 2. method
        if rule.method != "*" and method_upper != rule.method:
            continue
        # 3. path_prefix
        if not match_path_prefix(path, rule.path_prefix_norm):
            continue
        # 4. scheme
        if rule.scheme != scheme_lower:
            continue
        # 5. port
        if rule.port != 0 and rule.port != port:
            continue
        return True, f"matched:rule_id={rule.id}"

    return False, "no_match"


# ===== 规则编译 + 缓存 =====

def compile_rules(raw_rules: list[WhitelistRule]) -> list[CompiledRule]:
    """编译 DB 行为 CompiledRule。path_prefix 在此时归一化一次。"""
    compiled: list[CompiledRule] = []
    for raw in raw_rules:
        try:
            norm_prefix = normalize_path(raw.path_prefix) if raw.path_prefix != "*" else "*"
            compiled.append(CompiledRule(
                id=raw.id,
                domain=raw.domain.lower(),
                method=raw.method.upper(),
                path_prefix_norm=norm_prefix,
                scheme=raw.scheme.lower(),
                port=raw.port,
            ))
        except ValueError as e:
            log.error(f"skip invalid whitelist rule id={raw.id}: {e}")
    return compiled


class RuleCache:
    """白名单规则缓存。封装 load/compile/invalidate。

    替代模块级 _whitelist_cache dict——消除测试间状态泄漏。
    构造时不依赖 DB——_load_from_db 内部 import（和现有模式一致）。
    """

    def __init__(self):
        self._cache: dict[str, list[CompiledRule]] = {}

    def get(self, backend_name: str) -> list[CompiledRule]:
        """缓存优先，未缓存从 DB 加载 + 编译。"""
        cached = self._cache.get(backend_name)
        if cached is not None:
            return cached
        return self._load_from_db(backend_name)

    def invalidate(self, backend_name: str | None = None) -> None:
        """失效缓存。backend_name=None 清空全部。"""
        if backend_name is None:
            self._cache.clear()
        else:
            self._cache.pop(backend_name, None)

    def _load_from_db(self, backend_name: str) -> list[CompiledRule]:
        """从 DB 加载 + 编译 + 缓存。"""
        raw_rules: list[WhitelistRule] = []
        try:
            from src.kernel.session_manager import db, db_lock
            with db_lock:
                rows = db.execute(
                    "SELECT id, backend_name, domain, method, path_prefix, scheme, port "
                    "FROM grape_t_engine_sandbox_whitelist WHERE backend_name=?",
                    (backend_name,)
                ).fetchall()
            for row in rows:
                raw_rules.append(WhitelistRule(
                    id=row[0], backend_name=row[1], domain=row[2],
                    method=row[3], path_prefix=row[4], scheme=row[5], port=row[6],
                ))
        except Exception as e:
            log.error(f"load whitelist from DB failed: {e}")

        compiled = compile_rules(raw_rules)
        self._cache[backend_name] = compiled
        return compiled


# 全局实例——main.py 构造后注入 app.state。
# 向后兼容：模块级函数保留，内部委托全局 _rule_cache。
_rule_cache = RuleCache()


def get_compiled_rules(backend_name: str) -> list[CompiledRule] | None:
    """从缓存获取编译后的规则。None 表示未缓存。"""
    return _rule_cache._cache.get(backend_name)


def set_compiled_rules(backend_name: str, rules: list[CompiledRule]) -> None:
    """写入缓存。"""
    _rule_cache._cache[backend_name] = rules


def invalidate_cache(backend_name: str | None = None) -> None:
    """失效缓存。"""
    _rule_cache.invalidate(backend_name)


def load_rules_from_db(backend_name: str) -> list[CompiledRule]:
    """从 DB 加载规则并编译 + 缓存。"""
    return _rule_cache._load_from_db(backend_name)


def get_rules(backend_name: str) -> list[CompiledRule]:
    """获取规则——缓存优先，未缓存从 DB 加载。"""
    return _rule_cache.get(backend_name)
