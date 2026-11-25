"""Issue #3759：上游失败日志窗口去重（`src/kernel/log_dedupe.py`）。

验收口径（Issue #3759）：
- 同窗口同 repo/错误码聚合，**首条保留完整响应**，窗口内仅计数；
- 计数可查（窗口滚动时以 `folded` 汇总回传），不随 issue 数线性放大告警。
"""
import threading

from src.kernel.log_dedupe import DedupeDecision, WindowDeduper


class _Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_first_event_emits_full_and_rest_are_folded():
    clock = _Clock()
    d = WindowDeduper(60, clock=clock)

    first = d.note("gitcode:o/r:404")
    assert isinstance(first, DedupeDecision)
    assert first.emit_full is True
    assert first.folded == 0

    second = d.note("gitcode:o/r:404")
    third = d.note("gitcode:o/r:404")
    assert second.emit_full is False and second.folded == 1
    assert third.emit_full is False and third.folded == 2
    assert d.folded_total("gitcode:o/r:404") == 2


def test_window_rollover_reports_previous_window_count():
    clock = _Clock()
    d = WindowDeduper(60, clock=clock)
    d.note("k")
    d.note("k")
    d.note("k")
    clock.advance(61)
    rolled = d.note("k")
    assert rolled.emit_full is True
    # 上一个窗口折叠了 2 条（3 条里只输出了首条）
    assert rolled.folded == 2
    assert d.folded_total("k") == 0


def test_distinct_keys_do_not_share_windows():
    clock = _Clock()
    d = WindowDeduper(60, clock=clock)
    assert d.note("a").emit_full is True
    assert d.note("b").emit_full is True
    assert d.note("a").emit_full is False
    assert d.note("b").folded == 1


def test_window_boundary_is_inclusive_of_elapsed_window():
    clock = _Clock()
    d = WindowDeduper(60, clock=clock)
    d.note("k")
    clock.advance(59.9)
    assert d.note("k").emit_full is False
    clock.advance(0.2)
    assert d.note("k").emit_full is True


def test_reset_and_active_keys():
    d = WindowDeduper(60)
    d.note("a")
    d.note("b")
    assert d.active_keys() == ["a", "b"]
    d.reset("a")
    assert d.active_keys() == ["b"]
    d.reset()
    assert d.active_keys() == []


def test_thread_safety_single_first_emission():
    """并发 note 同一个 key：只允许一个线程拿到 emit_full（防首条重复刷屏）。"""
    d = WindowDeduper(60)
    results: list = []
    barrier = threading.Barrier(8)

    def _work():
        barrier.wait()
        results.append(d.note("same").emit_full)

    threads = [threading.Thread(target=_work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert d.folded_total("same") == 7


def test_invalid_window_falls_back_to_default(monkeypatch):
    d = WindowDeduper(0)
    assert d.window_seconds == 60.0
    d2 = WindowDeduper("bad")  # type: ignore[arg-type]
    assert d2.window_seconds == 60.0