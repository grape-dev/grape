"""Issue 卡片 ident 观测指标（Issue #3999）。

背景：``get_issue_stage`` 对 ``autopilot-*`` / ``pr_*`` / Gitee ``IK*`` 等非数字 ident
会走 GSB provider hop。修复前这类 ident 在 ``/internal/issues/card`` 恒定 422（生产
561 次/小时），异常被吞成一条 debug 日志后静默回落 legacy ``*_any`` 直读；失败既无
计数也无 warning。根因已在 #4065（PR #2063）消除，本模块补齐 #3999 验收要求的观测项：

- ``grape_gsb_issue_card_invalid_ident_total{provider}``：ident 无法归一为卡片主键时计数；
- provider / 回退直读失败由 debug/静默提升为 warning（日志侧由 ``session_store`` 打）。

分层约束（与 ``src/kernel/comment_metrics.py`` / ``cascade_converge_metrics.py`` 同口径）：
- kernel 不 import domains（``scripts/check-arch.py`` R1 HARD）；
- ``CollectorRegistry`` 由 insight 域持有，这里只定义「可安装收集器」，由
  ``src/domains/insight/observability/__init__.py`` 启动时 ``install(registry)``；
  未安装时 ``observe_invalid_ident`` 是 no-op（worker 不暴露 /metrics，也不影响主流程）；
- ``provider`` 限固定枚举，避免自建实例名抬高标签基数。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

INVALID_IDENT_METRIC = "grape_gsb_issue_card_invalid_ident_total"

# 与 src.kernel.config 同一 logger（stdlib 单例，同名字即同对象）；指标失败仅降噪留痕。
_log = logging.getLogger("devops-bot")

#: provider 标签固定枚举（自建实例一律归 other，控制基数）
PROVIDERS = ("gitcode", "gitee", "github", "gitlab", "other")

_LOCK = threading.Lock()
#: 已注册的 registry 及其收集器：[{"registry", "invalid_ident"}]
_INSTALLS: list[dict] = []


def install(registry: Any) -> bool:
    """把 ident 观测指标注册进 prometheus_client CollectorRegistry（按 registry 幂等）。

    返回 True 表示本次完成注册；同一 registry 二次调用返回 False（不重复建收集器）。
    注册失败（重名等）只降级为 no-op，不影响 stage 读取主流程。
    """
    with _LOCK:
        if any(inst["registry"] is registry for inst in _INSTALLS):
            return False
        try:
            from prometheus_client import Counter

            _inst = {
                "registry": registry,
                "invalid_ident": Counter(
                    INVALID_IDENT_METRIC,
                    "Issue idents that cannot be normalized to an issue card key, by provider.",
                    ["provider"],
                    registry=registry,
                ),
            }
        except ValueError:  # pragma: no cover - 同一 registry 重复注册
            return False
        _INSTALLS.append(_inst)
        return True


def provider_of(repo_key: str) -> str:
    """从 ``provider:owner/repo`` 取固定枚举 provider（无前缀默认 gitcode）。"""
    raw = str(repo_key or "").partition(":")[0].strip().lower()
    if not raw or raw == str(repo_key or "").strip().lower():
        # 无 ``:`` 前缀 → provider 缺省
        raw = "gitcode"
    return raw if raw in PROVIDERS else "other"


def observe_invalid_ident(repo_key: str) -> str:
    """记录一次「ident 无法归一为卡片主键」，返回归一后的 provider 标签值。"""
    provider = provider_of(repo_key)
    with _LOCK:
        for inst in _INSTALLS:
            try:
                inst["invalid_ident"].labels(provider=provider).inc()
            except Exception as _e:  # pragma: no cover - prometheus 版本差异
                _log.debug(f"issue_card_metrics: counter inc failed: {_e}")
    return provider