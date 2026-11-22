"""RepoBotToken legacy 列回落观测：counter 指标 + 首打/采样降噪（Issue #3740）。

背景（Issue #3740）：`RepoBotToken._fetch()` 的历史列回落分支命中后无条件
``log.warning("RepoBotToken: historical column fallback hit (rc miss) …")``，
生产实测 8,872 条/h（占同期全部 WARNING 46%），把真实告警淹没；同一请求内还会
连打 3-4 条同仓库同列名的记录（无进程内缓存）。

本模块只做两件事，均与解析逻辑解耦：

1. **counter 指标** ``grape_repo_bot_legacy_fallback_total{provider,repo}``：按
   **实际 legacy 列存储读**全量计数。日志可以采样，计数不能——「日志少」不等于
   「信息丢」，回落规模、回落仓库分布始终可查。
2. **日志降噪**：每个 ``(repo_key, field)`` 首次回落必打 WARNING（新仓库异常
   第一时间可见），其后按 1/1000 采样打 WARNING，未命中采样降 DEBUG（保留定位
   能力）。日志文本保持原样，便于按原字符串 grep 定位；但采样会让「持续偶发回落」
   出现零告警，故**日志不是下线判据**——下线判据 = counter 窗口增量 == 0 **且**
   ``repo_bot_hit_legacy_column`` 审计零命中。

分层约束：kernel 不能 import domains（check-arch R1 HARD），Prometheus 的
CollectorRegistry 由 insight 域持有。这里沿用 ``src/kernel/org_metrics.py`` /
``src/kernel/gsb/metrics.py`` 的「可安装收集器 + 无副作用 observe」口径：
``install(registry)`` 之前 observe 是 no-op（worker 包未装 prometheus 时零开销）。
"""
from __future__ import annotations

from typing import Any

#: 采样比：首次必打之外，每 N 次真实回落打一条 WARNING（1/1000）。
LEGACY_FALLBACK_LOG_SAMPLE_RATE = 1000

#: 采样状态上限（按 (repo_key, field) 计）；超限整表清空——回落仓库数量级远小于此，
#: 清空只可能让个别仓库多打一条首打日志，不影响计数口径。
LOG_STATE_MAX_KEYS = 4096

_COUNTERS: list = []
_LOG_SEEN: dict = {}


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


def should_warn_legacy_fallback(repo_key: str, field: str) -> bool:
    """该 (repo_key, field) 的本次回落是否打 WARNING（否则降 DEBUG）。

    口径：第 1 次必打；其后 ``n % LEGACY_FALLBACK_LOG_SAMPLE_RATE == 0`` 采样。
    计数按真实存储读推进（缓存命中不经过本函数），故采样比与缓存解耦。
    """
    key = (str(repo_key or ""), str(field or ""))
    seen = _LOG_SEEN.get(key, 0) + 1
    if key not in _LOG_SEEN and len(_LOG_SEEN) >= LOG_STATE_MAX_KEYS:
        _LOG_SEEN.clear()
    _LOG_SEEN[key] = seen
    return seen == 1 or seen % LEGACY_FALLBACK_LOG_SAMPLE_RATE == 0


def reset_log_state() -> None:
    """清空采样状态（用例隔离 / 长驻进程手工复位）。"""
    _LOG_SEEN.clear()