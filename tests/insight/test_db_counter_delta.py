"""DB 计数器不许把服务端累计值当增量重复累加。

MySQL `SHOW STATUS` 的 `Aborted_connects` / `Threads_created` 是**服务端累计值**。
旧实现每次抓取都 `.inc(value)`，于是 Prometheus 计数器的增速 = 累计值 × 抓取次数：
实测 `Aborted_connects=12` 被报成 48/min，`increase()` 型告警因此长期误报。

这里钉死：同一累计值重复观测只记一次，真增长了才补差额。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from prometheus_client import Counter  # noqa: E402

from src.domains.insight import observability as obs  # noqa: E402


def _fresh_counter(name="test_aborted_total"):
    return Counter(name, "test counter", registry=None)


def _value(counter):
    return counter._value.get()


def test_same_absolute_value_is_counted_once():
    obs._COUNTER_LAST.clear()
    c = _fresh_counter()
    obs._inc_by_delta(c, "Aborted_connects", 12)
    assert _value(c) == 12
    # 再抓 9 次同一累计值：一次都不许再加
    for _ in range(9):
        obs._inc_by_delta(c, "Aborted_connects", 12)
    assert _value(c) == 12


def test_real_growth_counts_only_the_delta():
    obs._COUNTER_LAST.clear()
    c = _fresh_counter("test_aborted_delta_total")
    obs._inc_by_delta(c, "Aborted_connects", 12)
    obs._inc_by_delta(c, "Aborted_connects", 15)
    obs._inc_by_delta(c, "Aborted_connects", 15)
    assert _value(c) == 15


def test_server_restart_resets_baseline():
    """服务端重启后累计值回绕（变小）：当成新的累计值整段补上，而不是算负增量。"""
    obs._COUNTER_LAST.clear()
    c = _fresh_counter("test_aborted_restart_total")
    obs._inc_by_delta(c, "Aborted_connects", 100)
    obs._inc_by_delta(c, "Aborted_connects", 3)
    assert _value(c) == 103
    obs._inc_by_delta(c, "Aborted_connects", 5)
    assert _value(c) == 105
