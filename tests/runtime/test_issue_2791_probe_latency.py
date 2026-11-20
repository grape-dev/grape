"""Issue #2791（P1-13）：worker 安装/启动完成后 60s 内完成首次探活。

用户症状（母单 #2747 实测）：安装 worker 后约 **10 分钟**才首次探活 ok——
worker 起来后 `_worker_probe_loop` 首轮先 sleep `_PROBE_INTERVAL`（600s），
而 `_reregister` 拾起「起机后新装的引擎」时**不触发探测**，于是新引擎要等
下一轮 600s 探活才第一次被判 ok（安装了却不可用）。

Spec「首探触发（时延收口）」：
- 安装/启动完成事件后**立即**发起首次探活（不等周期）；
- 失败按**指数退避**重试（不打爆引擎），保证整条
  「安装完成 → 首探 ok」链路落进 60s 窗口。

测试 seam：worker 侧纯调度函数（可注入探测/睡眠），不依赖真实 CLI 与线程，
也不依赖挂钟时间——断言在「第几次尝试、每次间隔、总窗口」这些行为上。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestBackoffPlan:
    """首探窗口的退避计划：立即首探 + 指数退避 + 封顶（不产生风暴）。"""

    def test_first_attempt_is_immediate(self):
        import src.domains.runtime.engine.agent as ag
        plan = ag._probe_first_window_plan(window=60.0, base=4.0, cap=20.0)
        assert plan, "空计划不可能覆盖首探窗口"
        assert plan[0] == 0.0, f"首次尝试必须立即执行（t=0），实际 {plan[0]}"

    def test_gaps_are_exponential_and_capped(self):
        import src.domains.runtime.engine.agent as ag
        plan = ag._probe_first_window_plan(window=60.0, base=4.0, cap=20.0)
        gaps = [round(plan[i] - plan[i - 1], 6) for i in range(1, len(plan))]
        assert len(gaps) >= 2, f"60s 窗口内至少要有多次尝试，实际 {plan}"
        # 指数退避：间隔不递减
        assert all(gaps[i] >= gaps[i - 1] for i in range(1, len(gaps))), f"间隔必须单调不减: {gaps}"
        # 封顶：单次等待不超过 cap（防风暴式高频重试的反面：也防一次等到超窗）
        assert max(gaps) <= 20.0 + 1e-9, f"间隔必须封顶在 cap 内: {gaps}"
        # 首个重试间隔 = base
        assert gaps[0] == pytest.approx(4.0)

    def test_plan_stays_inside_the_window(self):
        import src.domains.runtime.engine.agent as ag
        plan = ag._probe_first_window_plan(window=60.0, base=4.0, cap=20.0)
        assert max(plan) <= 60.0, f"计划不得把尝试排到窗口之外: {plan}"
        # 60s 窗口要留出足够尝试点，才算「首探时延收敛」而不是只补一次
        assert len(plan) >= 3, f"60s 窗口内尝试点过少: {plan}"

    def test_backoff_seconds_grows_then_caps(self):
        import src.domains.runtime.engine.agent as ag
        assert ag._probe_backoff_seconds(1, base=4.0, cap=20.0) == pytest.approx(4.0)
        assert ag._probe_backoff_seconds(2, base=4.0, cap=20.0) == pytest.approx(8.0)
        assert ag._probe_backoff_seconds(3, base=4.0, cap=20.0) == pytest.approx(16.0)
        assert ag._probe_backoff_seconds(4, base=4.0, cap=20.0) == pytest.approx(20.0)
        assert ag._probe_backoff_seconds(9, base=4.0, cap=20.0) == pytest.approx(20.0)


class TestFirstProbeWindow:
    """事件驱动首探：立即探测 → 失败退避重试 → 命中即停。"""

    def _run(self, ag, outcomes, window=60.0):
        calls = {"t": 0.0, "probes": 0, "times": []}
        slept = []

        def _sleep(secs):
            slept.append(secs)
            calls["t"] += secs

        def _probe(eng):
            calls["probes"] += 1
            calls["times"].append(round(calls["t"], 6))
            status = outcomes[min(calls["probes"] - 1, len(outcomes) - 1)]
            return {"status": status, "latency": 0.1, "error": "" if status == "ok" else "not ready"}

        res = ag._probe_engines_first_window(
            [{"name": "codearts-2791", "installed": True, "cli_path": "/bin/true"}],
            reason="install", window=window, probe_fn=_probe, sleep_fn=_sleep,
            now_fn=lambda: calls["t"])
        return res, slept, calls

    def test_ok_on_first_probe_no_retry(self):
        import src.domains.runtime.engine.agent as ag
        res, slept, calls = self._run(ag, ["ok"])
        assert calls["probes"] == 1, "引擎就绪时只探一次（不无谓重试）"
        assert slept == [], "没有必要等待"
        assert res["codearts-2791"]["status"] == "ok"

    def test_retries_with_backoff_until_ok(self):
        import src.domains.runtime.engine.agent as ag
        res, slept, calls = self._run(ag, ["fail", "fail", "ok"])
        assert calls["probes"] == 3, f"失败必须按退避重试，实际探测 {calls['probes']} 次"
        assert slept == [4.0, 8.0], f"退避间隔应为 4s → 8s，实际 {slept}"
        assert calls["times"][-1] <= 60.0, f"首探 ok 必须落在 60s 窗口内，实际 {calls['times']}"
        assert res["codearts-2791"]["status"] == "ok"

    def test_gives_up_inside_window_when_never_ready(self):
        import src.domains.runtime.engine.agent as ag
        res, slept, calls = self._run(ag, ["fail"])
        assert calls["times"][-1] <= 60.0 + 1e-9, f"不得把重试排到窗口之外: {calls['times']}"
        assert sum(slept) <= 60.0 + 1e-9, f"总等待不得超窗: {slept}"
        assert res["codearts-2791"]["status"] == "fail", "始终不就绪则保留失败观测（交给周期探活）"
        assert max(slept) <= 20.0 + 1e-9, f"单次等待必须封顶: {slept}"

    def test_uninstalled_engine_is_skipped_not_retried(self):
        import src.domains.runtime.engine.agent as ag
        probes = []
        res = ag._probe_engines_first_window(
            [{"name": "ghost-2791", "installed": False}],
            reason="install", probe_fn=lambda e: probes.append(e) or {"status": "ok"},
            sleep_fn=lambda s: None)
        assert probes == [], "未安装的引擎不应发起探测"
        assert res["ghost-2791"]["status"] == "skip"


class TestNewEngineDetection:
    """起机后新装引擎：重注册拾起时必须当作「安装完成事件」立即触发首探。"""

    def test_new_engines_detected_by_name(self):
        import src.domains.runtime.engine.agent as ag
        prev = [{"name": "opencode", "installed": True}]
        now = [{"name": "opencode", "installed": True},
               {"name": "codearts", "installed": True},
               {"name": "hermes", "installed": False}]
        new = ag._new_engines(prev, now)
        assert [e["name"] for e in new] == ["codearts"], (
            f"只应把「新出现且已安装」的引擎当成安装完成事件，实际 {new}")

    def test_probe_triggered_after_reregister_when_new_engine_appears(self, monkeypatch):
        import src.domains.runtime.engine.agent as ag
        triggered = []
        monkeypatch.setattr(ag, "_start_first_probe_window",
                            lambda engines, reason="": triggered.append(([e["name"] for e in engines], reason)))
        ag._probe_after_reregister(
            [{"name": "opencode", "installed": True}],
            [{"name": "opencode", "installed": True}, {"name": "codearts", "installed": True}])
        assert triggered == [(["codearts"], "install")], f"新装引擎必须立即触发首探，实际 {triggered}"


    def test_no_probe_when_no_new_engine(self, monkeypatch, caplog):
        import src.domains.runtime.engine.agent as ag
        triggered = []
        monkeypatch.setattr(ag, "_start_first_probe_window",
                            lambda engines, reason="": triggered.append(engines))
        ag._probe_after_reregister(
            [{"name": "opencode", "installed": True}],
            [{"name": "opencode", "installed": True}])
        assert triggered == [], "没有新引擎时不应额外触发首探（避免重复探测）"

    def test_startup_registration_uses_first_probe_window(self, monkeypatch):
        """worker 启动注册完成 → 先即时探测一轮（首个心跳带观测），未 ok 的进首探窗口。"""
        import src.domains.runtime.engine.agent as ag
        probed, windows = [], []

        def _probe_once():
            probed.append(True)
            ag._worker_probe_results["codearts-2791"] = {"status": "fail"}

        monkeypatch.setattr(ag, "_worker_probe_once", _probe_once)
        monkeypatch.setattr(ag, "_start_first_probe_window",
                            lambda engines, reason="": windows.append(([e["name"] for e in engines], reason)))
        not_ok = ag._probe_after_registration(
            [{"name": "codearts-2791", "installed": True},
             {"name": "opencode-2791", "installed": False}])

        assert probed == [True], "注册成功后必须先同步探一轮（首个心跳要带观测）"
        assert [e["name"] for e in not_ok] == ["codearts-2791"]
        assert windows == [(["codearts-2791"], "startup")], (
            f"未 ok 的引擎必须进首探窗口，实际 {windows}")

class TestFastInstallDiscovery:
    """评审 P2：窗口起点必须贴近「安装完成」，而不是检测之后。

    原实现只在重注册（`RUNTIME_REREGISTER_INTERVAL` 默认 300s）里发现新装引擎，
    60s 首探窗口是**检测之后**的子区间 → install→ok 最坏约 360s，不满足
    「安装完成 60s 内探活 ok」。修法：加一档更短的廉价发现（只查 PATH，
    不跑 CLI/握手），命中「新装引擎」立即重注册并进入首探窗口。
    """

    def test_fast_discover_interval_fits_inside_first_probe_window(self):
        """发现间隔必须落在 60s 口径内，否则窗口起点又被推远。"""
        import src.domains.runtime.engine.agent as ag
        assert ag._FAST_DISCOVER_INTERVAL > 0
        assert ag._FAST_DISCOVER_INTERVAL <= ag._PROBE_FIRST_WINDOW, (
            f"快速发现间隔 {ag._FAST_DISCOVER_INTERVAL}s 不得超出首探窗口 {ag._PROBE_FIRST_WINDOW}s")
        assert ag._FAST_DISCOVER_INTERVAL <= 60.0

    def test_install_completion_to_probe_ok_is_within_60s(self):
        """「安装完成 → 首探 ok」上界 = 发现间隔 + 窗口首个尝试点（t=0 立即探）。"""
        import src.domains.runtime.engine.agent as ag
        plan = ag._probe_first_window_plan()
        bound = ag._FAST_DISCOVER_INTERVAL + plan[0]
        assert plan[0] == 0.0, "首探必须在窗口起点立即执行"
        assert bound <= 60.0, f"install→首探上界 {bound}s 超出 60s 口径"

    def test_cheap_scan_does_not_probe_cli(self, monkeypatch):
        """廉价发现只读 PATH：不得走 --version/ACP 握手（否则 15s 量级不可行）。"""
        import src.domains.runtime.engine.agent as ag
        called = []
        monkeypatch.setattr(ag, "_discover_local_runtimes",
                            lambda: called.append("full") or [])
        monkeypatch.setattr("src.domains.runtime.engine.registry.discover_runtimes",
                            lambda: [{"name": "codearts", "installed": True, "cli_path": "/x"}])
        new = ag._fast_install_triggered([{"name": "opencode", "installed": True}])
        assert called == [], "廉价发现不得调用带 CLI 探测的全量发现"
        assert [e["name"] for e in new] == ["codearts"]

    def test_no_trigger_when_no_new_engine(self, monkeypatch):
        import src.domains.runtime.engine.agent as ag
        monkeypatch.setattr("src.domains.runtime.engine.registry.discover_runtimes",
                            lambda: [{"name": "opencode", "installed": True, "cli_path": "/x"}])
        assert ag._fast_install_triggered([{"name": "opencode", "installed": True}]) == []

    def test_scan_failure_does_not_raise_or_trigger(self, monkeypatch):
        import src.domains.runtime.engine.agent as ag

        def _boom():
            raise RuntimeError("scan exploded")

        monkeypatch.setattr("src.domains.runtime.engine.registry.discover_runtimes", _boom)
        assert ag._fast_install_triggered([{"name": "opencode", "installed": True}]) == []


class TestReregisterCycle:
    """两档重注册决策（周期兜底 / 安装完成事件）单点实现。"""

    async def _cycle(self, ag, monkeypatch, *, now, last_rereg, last_scan, installed,
                     scan=None, reregister_ret=None, force=False):
        calls = {"reregister": 0, "probe": [], "probe_fn": 0}

        async def _rereg(client, prev, device_name, cli_version=""):
            calls["reregister"] += 1
            return reregister_ret

        def _after_reregister(prev, new):
            calls["probe_fn"] += 1
            calls["probe"].append(([e["name"] for e in ag._new_engines(prev, new)], "install"))

        monkeypatch.setattr(ag, "_reregister", _rereg)
        monkeypatch.setattr(ag, "_probe_after_reregister", _after_reregister)
        state = await ag._reregister_cycle(
            None, installed, "dev-1", now=now, last_reregister=last_rereg,
            last_fast_scan=last_scan, force=force,
            scan_fn=(lambda: (scan if scan is not None else installed)),
        )
        return state, calls

    async def test_periodic_reregister_still_runs(self, monkeypatch):
        import src.domains.runtime.engine.agent as ag
        eng = [{"name": "opencode", "installed": True}]
        state, calls = await self._cycle(
            ag, monkeypatch, now=1000.0, last_rereg=1000.0 - ag._REREGISTER_INTERVAL,
            last_scan=1000.0, installed=eng, reregister_ret=([1], {1: "opencode"}, eng))
        assert state["did"] is True and state["triggered_by"] == "periodic"
        assert calls["reregister"] == 1
        assert calls["probe_fn"] == 1, "每次成功重注册都必须接首探（新装引擎立即探）"
        assert state["last_reregister"] == 1000.0

    async def test_forced_cycle_runs_without_waiting_for_clocks(self, monkeypatch):
        """心跳连续失败档（force）：不看时钟直接重注册 —— 与周期/安装档同一实现。"""
        import src.domains.runtime.engine.agent as ag
        eng = [{"name": "opencode", "installed": True}]
        state, calls = await self._cycle(
            ag, monkeypatch, now=1.0, last_rereg=1.0, last_scan=1.0, installed=eng,
            reregister_ret=([1], {1: "opencode"}, eng), force=True)
        assert state["did"] is True and state["triggered_by"] == "forced"
        assert calls["reregister"] == 1

    async def test_install_event_triggers_reregister_before_periodic_window(self, monkeypatch):
        """新装引擎在周期（300s）之前就被拾起 —— P2 的修法本身。"""
        import src.domains.runtime.engine.agent as ag
        opencode = {"name": "opencode", "installed": True}
        codearts = {"name": "codearts", "installed": True}
        state, calls = await self._cycle(
            ag, monkeypatch, now=ag._FAST_DISCOVER_INTERVAL,
            last_rereg=0.0, last_scan=0.0, installed=[opencode],
            scan=[opencode, codearts],
            reregister_ret=([1, 2], {1: "opencode", 2: "codearts"}, [opencode, codearts]))
        assert state["did"] is True and state["triggered_by"] == "install"
        assert calls["reregister"] == 1, "安装完成事件必须立即重注册（拾起新引擎 id）"
        assert calls["probe_fn"] == 1, "重注册成功必须触发首探（含新引擎）"
        assert (["codearts"], "install") in calls["probe"], (
            f"重注册后必须立即触发新引擎首探，实际 {calls['probe']}")

    async def test_nothing_due_does_nothing(self, monkeypatch):
        import src.domains.runtime.engine.agent as ag
        eng = [{"name": "opencode", "installed": True}]
        state, calls = await self._cycle(
            ag, monkeypatch, now=5.0, last_rereg=0.0, last_scan=0.0, installed=eng)
        assert state["did"] is False and state["triggered_by"] == ""
        assert calls["reregister"] == 0

    async def test_failed_reregister_still_advances_clock(self, monkeypatch):
        import src.domains.runtime.engine.agent as ag
        state, calls = await self._cycle(
            ag, monkeypatch, now=ag._FAST_DISCOVER_INTERVAL, last_rereg=0.0,
            last_scan=0.0, installed=[{"name": "opencode", "installed": True}],
            scan=[{"name": "codearts", "installed": True}], reregister_ret=None)
        assert calls["reregister"] == 1
        assert state["ok"] is False
        assert state["last_reregister"] == ag._FAST_DISCOVER_INTERVAL, (
            "重注册失败也要推进时钟，否则每轮都重试")
