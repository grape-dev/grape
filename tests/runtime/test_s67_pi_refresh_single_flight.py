"""D45（S67）用例：project-info 后台刷新单飞（段错误专项·线程风暴治理）。

现场（2026-11-04 全量 pytest ~43% faulthandler dump）：
- 崩溃线程 Thread-944 在 builder._write_to_obs（DB 写段）SIGSEGV；
- 每线程携带 20s 级网络超时 + 共享 sqlite 连接写，测试期 mock 连接
  换出/关闭的 UAF 窗口随线程数放大；
- 泄漏源头：event_pipeline 每个 webhook 事件裸起
  Thread(maybe_refresh_project_info)，积压 944 存活线程。

修复：_spawn_project_info_refresh 单飞（同 repo_key 在刷新即跳过）
+ 全局并发上限（幂等 TTL 操作，跳过无损失）+ 完成释放槽位。
"""
import os
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db"))

from src.domains.runtime.pipeline.event_pipeline import (
    _spawn_project_info_refresh,
    _PI_REFRESH_GATE,
)


class TestPiRefreshSingleFlight:

    def setup_method(self):
        with _PI_REFRESH_GATE:
            # 清空在飞集合（测试隔离：不杀已起线程，只重置登记表）
            _spawn = sys.modules["src.api.event_pipeline"]
            _spawn._PI_REFRESH_INFLIGHT.clear()

    def test_s67_same_repo_single_flight(self):
        """同 repo_key 二次派发被跳过（只起一个工作线程）。"""
        started = threading.Event()
        release = threading.Event()

        def _block(*a, **k):
            started.set()
            release.wait(timeout=10)

        with patch("src.domains.runtime.project_info.builder.maybe_refresh_project_info", side_effect=_block):
            _spawn_project_info_refresh("gitcode:acme/one", "t")
            assert started.wait(timeout=5)
            # 在飞期间再次派发 → 直接跳过（不新增工作线程）
            with _PI_REFRESH_GATE:
                inflight = set(sys.modules["src.api.event_pipeline"]._PI_REFRESH_INFLIGHT)
            assert inflight == {"gitcode:acme/one"}
            _spawn_project_info_refresh("gitcode:acme/one", "t")
            with _PI_REFRESH_GATE:
                assert sys.modules["src.api.event_pipeline"]._PI_REFRESH_INFLIGHT == inflight
            release.set()

    def test_s67_concurrency_cap(self):
        """全局并发上限：超过 _PI_REFRESH_MAX_CONCURRENT 的派发被跳过。"""
        from src.domains.runtime.pipeline.event_pipeline import _PI_REFRESH_MAX_CONCURRENT
        started = threading.Semaphore(0)
        release = threading.Event()
        entries = {"n": 0}
        lock = threading.Lock()

        def _work(*a, **k):
            with lock:
                entries["n"] += 1
            started.release()
            release.wait(timeout=10)

        with patch("src.domains.runtime.project_info.builder.maybe_refresh_project_info", side_effect=_work):
            for i in range(_PI_REFRESH_MAX_CONCURRENT + 3):
                _spawn_project_info_refresh(f"gitcode:acme/cap{i}", "t")
            # 上限个线程真正进入工作函数
            for _ in range(_PI_REFRESH_MAX_CONCURRENT):
                assert started.acquire(timeout=5)
            time.sleep(0.2)  # 给多余派发留跳过时间
            assert entries["n"] == _PI_REFRESH_MAX_CONCURRENT
            release.set()

    def test_s67_slot_released_after_finish(self):
        """刷新完成后释放槽位：同 repo_key 可再次派发。"""
        done = threading.Event()

        def _quick(*a, **k):
            done.set()

        with patch("src.domains.runtime.project_info.builder.maybe_refresh_project_info", side_effect=_quick):
            _spawn_project_info_refresh("gitcode:acme/rel", "t")
            assert done.wait(timeout=5)
            # 槽位回收有 finally 保证；轮询短暂等待回收
            for _ in range(50):
                with _PI_REFRESH_GATE:
                    if not sys.modules["src.api.event_pipeline"]._PI_REFRESH_INFLIGHT:
                        break
                time.sleep(0.1)
            with _PI_REFRESH_GATE:
                assert sys.modules["src.api.event_pipeline"]._PI_REFRESH_INFLIGHT == set()
            # 再次派发照常放行
            _spawn_project_info_refresh("gitcode:acme/rel", "t")
            assert done.wait(timeout=5)

    def test_s67_exception_releases_slot(self):
        """工作函数抛异常不泄漏槽位（finally 兜底）。"""
        def _boom(*a, **k):
            raise RuntimeError("boom")

        with patch("src.domains.runtime.project_info.builder.maybe_refresh_project_info", side_effect=_boom):
            _spawn_project_info_refresh("gitcode:acme/err", "t")
            for _ in range(50):
                with _PI_REFRESH_GATE:
                    if not sys.modules["src.api.event_pipeline"]._PI_REFRESH_INFLIGHT:
                        break
                time.sleep(0.1)
            with _PI_REFRESH_GATE:
                assert sys.modules["src.api.event_pipeline"]._PI_REFRESH_INFLIGHT == set()
