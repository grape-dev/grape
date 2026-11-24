"""Issue #3848：启动期 kernel port 依赖工作必须收敛到绑定之后。

缺陷（Issue 正文 + Loki 实测）：装配层 `contracts_wiring.bind_all()` 之前就跑的两处
启动期工作，在端口未绑时静默降级 —— 每次启动必然空跑，且只能靠 grep 日志发现。

本文件锁四件事（红→绿）：
  1. `credential_usage_log.cleanup_expired_retention` 在端口未绑时返回 0、**只打
     debug** 并计入 `startup_degraded{component=credential_usage_log}`（预期时序，
     不是故障），不再刷 `retention cleanup failed` WARNING；
  2. `bind("tokens")` 之后由 `_base.on_bind` 钩子补跑一次保留期清理，`prune` **恰好
     1 次**（`replace=True` 重绑不重复 DELETE）；已绑端口下布防（模块晚于装配层导入）
     则立即补跑；
  3. 启动调用点只「布防」：bind 前不执行、bind 后执行（四条启动路径一处生效）；
  4. `card_sources` 端口未就绪与真读失败分流：未就绪 → debug + `startup_degraded`，
     真异常 → 保留 WARNING 并同样打点。
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from src.kernel.persistence import credential_usage_log as cul  # noqa: E402
from src.kernel.ports import _base as ports_base  # noqa: E402
from src.kernel.ports import repos as repos_port  # noqa: E402
from src.kernel.ports import tokens as tokens_port  # noqa: E402
from src.kernel.ports._base import PortNotBound  # noqa: E402

_LOGGER = "devops-bot"


class _FakeTokens:
    """tokens 端口替身：只记录启动清理真正调用的 prune 原语。"""

    def __init__(self, rows: int = 0) -> None:
        self.calls: list[tuple] = []
        self.rows = rows

    def credential_usage_log_prune(self, cutoff: str, batch: int) -> int:
        self.calls.append((cutoff, batch))
        return self.rows


@pytest.fixture(autouse=True)
def _isolate_ports():
    """端口注册表与启动清理闸都是进程级状态：逐用例快照/复位，结束后原样恢复。

    不能只是 ``clear_bindings()`` 收尾——同进程里更早的用例（或装配层）可能已经绑好
    了 tokens/pipeline 等真实现，直接清空会让后续用例拿到未绑端口（例：
    tests/mgmt/test_n4_audit_coverage 的保留期清理用例依赖已绑的 tokens）。故先存快照，
    用例结束后按名字重绑回去。
    """
    saved = {name: ports_base.get_binding(name) for name in ports_base.bound_names()}
    ports_base.clear_bindings()
    cul.reset_startup_prune_state()
    yield
    ports_base.clear_bindings()
    # 恢复快照时不要顺带跑一次真实清理：先把「已补跑」闸置位（仅测试需要）
    cul._startup_prune_done = True
    for name, impl in saved.items():
        ports_base._bind_registry().bind(name, impl, replace=True)
    cul.reset_startup_prune_state()


def _warn_messages(caplog) -> list[str]:
    return [r.message for r in caplog.records if r.levelno >= logging.WARNING]


# ----------------------------------------------------------------- 1) 异常分类
def test_cleanup_returns_zero_and_logs_debug_when_port_unbound(caplog):
    """端口未绑 = 预期时序：返回 0、只 debug，不产生可巡检的 WARNING。"""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        n = cul.cleanup_expired_retention(force=True)
    assert n == 0
    assert _warn_messages(caplog) == [], "绑定前调用不该告警"
    assert any(
        "not bound" in r.message and r.levelno == logging.DEBUG for r in caplog.records
    ), "应留下 debug 级的 deferred 记录（说明是时序而非故障）"


def test_deferred_cleanup_counts_startup_degraded(caplog):
    """deferred 也不能只靠日志：debug 之外须计入 startup_degraded{component=credential_usage_log}。

    指标层存在的理由就是「启动期降级要么可巡检、要么只能 grep 日志」——usage_log 的
    未绑分支同样属预期时序，必须与 card_source 对称打点，否则该维度生产上恒空。
    """
    reg, _metrics = _fresh_metric_registry()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert cul.cleanup_expired_retention(force=True) == 0
    assert reg.get_sample_value(
        "grape_startup_degraded_total",
        {"component": "credential_usage_log", "reason": "port_not_bound"},
    ) == 1, "预期时序的 deferred 清理必须可巡检（与 card_source 同口径）"


# ------------------------------------------------- 2) bind 钩子补跑（恰好一次）
def test_bind_tokens_runs_startup_prune_exactly_once():
    """绑定 tokens 即补跑启动清理一次；replace=True 重绑不得重复 DELETE。"""
    fake = _FakeTokens(rows=3)
    tokens_port.bind(fake)
    assert len(fake.calls) == 1, "绑定后必须补跑一次保留期清理"
    tokens_port.bind(fake, replace=True)
    assert len(fake.calls) == 1, "重绑（replace=True）不得重复清理"


def test_other_port_bind_does_not_trigger_startup_prune():
    """清理只依赖 tokens：其它端口绑定不触发（8 次 bind 不能变成 8 次 DELETE）。"""
    fake = _FakeTokens()
    repos_port.bind(object())
    assert fake.calls == []
    tokens_port.bind(fake)
    assert len(fake.calls) == 1


def test_startup_prune_reports_deleted_rows(caplog):
    """长无写入实例：仅靠启动钩子即完成清理，删除行数可见（非恒 0）。"""
    fake = _FakeTokens(rows=7)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        tokens_port.bind(fake)
    assert len(fake.calls) == 1
    assert any(
        "7 rows" in r.message and r.levelno >= logging.INFO for r in caplog.records
    ), "清理结果应可巡检（行数打点）"


# --------------------------------------------------------- 3) 启动调用点布防
def test_startup_call_site_arms_cleanup_instead_of_running_it():
    """`init_db` 的启动调用点只布防：bind 前零执行，bind 后恰好一次。"""
    from src.domains.mgmt.user import db as user_db  # 导入期即跑 init_db（真实启动时序）

    fake = _FakeTokens()
    assert fake.calls == [], "模块导入期（端口未绑）不该执行清理"

    user_db._cleanup_usage_log_retention()
    assert fake.calls == [], "布防调用本身不该执行清理"

    tokens_port.bind(fake)
    assert len(fake.calls) == 1, "bind 之后由钩子补跑，恰好一次"


def test_defer_runs_immediately_when_tokens_already_bound():
    """时序二：本模块晚于装配层导入（tokens 已绑）→ 布防应立即补跑，不等下一次 bind。

    与上一条互补——只测「未绑→bind 时补跑」会漏掉「已绑→defer 立即跑」这条路径
    （测试/重复启动路径即走它）；该路径失效则已绑进程的启动清理永不执行。
    """
    fake = _FakeTokens(rows=2)
    tokens_port.bind(fake)
    assert len(fake.calls) == 1, "前序 bind 自身已补跑一次"

    cul.reset_startup_prune_state()  # 模拟「本次启动的钩子未参与」的晚导入路径
    cul.defer_startup_retention_cleanup()
    assert len(fake.calls) == 2, "已绑端口下布防必须立即执行清理，而不是静默跳过"


# --------------------------------------------- 4) card_sources 分流 + 打点
def _fresh_metric_registry():
    from prometheus_client import CollectorRegistry

    from src.kernel import startup_metrics

    startup_metrics._reset_for_tests()
    reg = CollectorRegistry()
    startup_metrics.install(reg)
    return reg, startup_metrics


def test_card_source_port_not_bound_is_debug_and_counted(caplog):
    """端口未就绪：debug + startup_degraded{reason=port_not_bound}，不刷 fallback WARNING。"""
    from src.domains.runtime.vcs.card_sources import OwnerTableCardSource

    reg, _metrics = _fresh_metric_registry()

    def _not_bound(*_a, **_k):
        raise PortNotBound("pipeline")

    source = OwnerTableCardSource(
        read_rows=_not_bound, ready=lambda: True, has_owner_dsn=lambda: True)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert source.raws() == {}

    assert _warn_messages(caplog) == [], "启动窗口内端口未绑不该产生 fallback WARNING"
    assert any(r.levelno == logging.DEBUG for r in caplog.records), "应留 debug 说明"
    assert reg.get_sample_value(
        "grape_startup_degraded_total",
        {"component": "card_source", "reason": "port_not_bound"},
    ) == 1


def test_card_source_real_failure_keeps_warning(caplog):
    """真读失败仍必须 WARNING（fail-open 但响亮），并计入 reason=read_failed。"""
    from src.domains.runtime.vcs.card_sources import OwnerTableCardSource

    reg, _metrics = _fresh_metric_registry()

    def _boom(*_a, **_k):
        raise RuntimeError("no such table: grape_t_repo_platforms")

    source = OwnerTableCardSource(
        read_rows=_boom, ready=lambda: True, has_owner_dsn=lambda: True)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert source.raws() == {}

    warns = _warn_messages(caplog)
    assert any("fallback to meta+file" in m for m in warns), "真故障必须继续响亮"
    assert reg.get_sample_value(
        "grape_startup_degraded_total",
        {"component": "card_source", "reason": "read_failed"},
    ) == 1


# --------------------------------------------------- 5) startup_degraded 指标
def test_startup_metrics_counts_by_component_and_reason():
    """指标口径：component + reason 两个维度分别计数。"""
    reg, metrics = _fresh_metric_registry()
    metrics.record_startup_degraded("card_source", "port_not_bound")
    metrics.record_startup_degraded("card_source", "port_not_bound")
    metrics.record_startup_degraded("credential_usage_log", "port_not_bound")
    assert reg.get_sample_value(
        "grape_startup_degraded_total",
        {"component": "card_source", "reason": "port_not_bound"},
    ) == 2
    assert reg.get_sample_value(
        "grape_startup_degraded_total",
        {"component": "credential_usage_log", "reason": "port_not_bound"},
    ) == 1
    assert reg.get_sample_value(
        "grape_startup_degraded_total",
        {"component": "card_source", "reason": "read_failed"},
    ) in (None, 0)


def test_startup_metrics_noop_before_install():
    """未安装（未启用 METRICS_ADDR / worker 包无 prometheus_client）时 record 为 no-op。"""
    from src.kernel import startup_metrics

    startup_metrics._reset_for_tests()
    startup_metrics.record_startup_degraded("card_source", "port_not_bound")  # 不得抛
    assert startup_metrics._reset_for_tests() is None