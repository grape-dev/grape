"""RepoBotToken legacy 列回落观测：counter 全量计数（Issue #3740）。

背景（Issue #3740）：`RepoBotToken._fetch()` 的历史列回落分支命中后无条件
``log.warning("RepoBotToken: historical column fallback hit (rc miss) …")``，
生产实测 8,872 条/h（占同期全部 WARNING 46%），把真实告警淹没；同一请求内还会
连打 3-4 条同仓库同列名的记录（无进程内缓存）。

本模块只做一件事，与解析逻辑解耦：

**counter 指标** ``grape_repo_bot_legacy_fallback_total{provider,repo}``：按
**实际 legacy 列存储读**全量计数。日志可以降噪，计数不能——「日志少」不等于
「信息丢」，回落规模、回落仓库分布始终可查。

日志降噪口径不在本模块：基线 Issue #3897 D 组已把该日志定为「DEBUG 常态 +
每 ``(repo, field)`` 每小时 1 条 WARNING」（``token_resolver._legacy_fallback_log``），
本单沿用同一实现，不另立采样状态——两套状态会让「打不打」出现两处真相。
生命周期判据同样是 counter（窗口增量 == 0）而非日志：日志降频后可能零命中。

分层约束：kernel 不能 import domains（check-arch R1 HARD），Prometheus 的
CollectorRegistry 由 insight 域持有。这里沿用 ``src/kernel/org_metrics.py`` /
``src/kernel/gsb/metrics.py`` 的「可安装收集器 + 无副作用 observe」口径：
``install(registry)`` 之前 observe 是 no-op（worker 包未装 prometheus 时零开销）。
"""
from __future__ import annotations

from typing import Any

_COUNTERS: list = []


def install(registry: Any) -> None:
    """把 counter 注册到给定的 prometheus_client CollectorRegistry（可多次、幂等）。

    同一 registry 重复注册会抛 ValueError（prometheus_client 语义），这里吞掉——
    测试会为每个 registry 单独 install，故计数按 registry 分别持有、observe 全部递增。
    """
    try:
        from prometheus_client import Counter
    except Exception:  # pragma: no cover - worker 包不带 prometheus_client
        return

    try:
        counter = Counter(
            "grape_repo_bot_legacy_fallback_total",
            "RepoBotToken resolutions served from the managed_repos legacy credential "
            "column because grape_t_repo_credentials (cred_type='bot') missed.",
            ["provider", "repo"],
            registry=registry,
        )
    except ValueError:  # pragma: no cover - 同一 registry 重复注册
        return
    _COUNTERS.append(counter)


def observe_legacy_fallback(provider: str, repo: str) -> None:
    """记录一次真实的历史列回落（未安装指标时为 no-op）。

    ``repo`` 用无前缀 ``owner/repo``（provider 已有独立标签），保留 ``repo_key``
    语义信息又不重复前缀。
    """
    if not _COUNTERS:
        return
    for counter in _COUNTERS:
        counter.labels(provider=str(provider or ""), repo=str(repo or "")).inc()


def repo_labels(repo_key: str) -> "tuple[str, str]":
    """repo_key → (provider, owner/repo) 指标标签（解析失败时 provider 空、repo 原值）。"""
    try:
        from src.kernel.repo_key import from_repo_key, parse_repo_key

        provider, _owner, _repo = parse_repo_key(repo_key or "")
        return str(provider or ""), str(from_repo_key(repo_key or ""))
    except Exception:  # pragma: no cover - 标签降级不阻断解析热路径
        return "", str(repo_key or "")
