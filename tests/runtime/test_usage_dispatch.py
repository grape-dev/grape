"""B5 usage_collect 账本分派合一：账本模式值域改 descriptor 驱动。

盘点裁定（回执 B5 A类）：数据源差异属引擎 quirk 不描述化——读账本的
实现函数（_hermes_usage/_hwcloud_usage/_opencode_usage）留在代码；收敛
的是「哪个引擎走哪种账本模式」的分派知识：原 start/finish 里的
`engine == "hermes"` / `in ("hwcloud","aishell")` 名字分支，改为按
descriptor `usage.mode` 派发（alias 经 registry 归一：aishell → hwcloud）。
"""
import pytest


_MODES = ("none", "event_stream", "hermes_cumulative",
          "hwcloud_offset", "opencode_watermark",
    "aishell_dsh_watermark",  # arch 二期解析轨融合 mode
)


class TestDescriptorUsageMode:
    def test_all_descriptors_have_usage_mode(self):
        """齐备性：防「新引擎漏配 usage.mode」半接入（值域内必选）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        for name, d in get_registry().all().items():
            mode = d.get("usage", {}).get("mode")
            assert mode in _MODES, f"{name}: usage.mode 缺失或越值域: {mode!r}"

    def test_mode_matches_legacy_dispatch(self):
        """与原名字分支逐引擎对账（行为冻结期口径）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        want = {
            "aishell-dsh": "aishell_dsh_watermark",  # 二期解析轨已在 arch 基线落地（fused mode）
            "codex": "event_stream",
            "atomcode": "event_stream",
            "hermes": "hermes_cumulative",
            "hwcloud": "hwcloud_offset",
            "opencode": "opencode_watermark",
            "codearts": "none",
            "cursor": "none",
            "dsh": "none",
        }
        for name, mode in want.items():
            assert reg.get(name)["usage"]["mode"] == mode, f"{name} usage.mode 漂移"

    def test_alias_resolves_to_hwcloud_mode(self):
        """aishell 是 hwcloud 的换名：registry 归一后账本模式一致（原
        `in ("hwcloud","aishell")` 分支的语义保留）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        assert reg.get("aishell")["canonical"] == "hwcloud"
        assert reg.get("aishell")["usage"]["mode"] == "hwcloud_offset"


class TestDispatchByNameGone:
    def test_start_finish_have_no_engine_name_branches(self):
        """分派知识已收 descriptor：start/finish 不得残留引擎名相等判定。"""
        import inspect
        from src.domains.runtime.engine import usage_collect
        for fn in (usage_collect.UsageCollector.start, usage_collect.UsageCollector.finish):
            src = inspect.getsource(fn)
            for name in ("hermes", "hwcloud", "aishell", "codex", "atomcode", "opencode"):
                assert f'== "{name}"' not in src, f"{fn.__name__} 残留名字分支: {name}"
                assert f'in ("{name}' not in src, f"{fn.__name__} 残留名字元组判定: {name}"

    def test_finish_dispatches_by_mode(self, monkeypatch):
        """finish 按 usage.mode 派发到既有账本实现（quirk 函数留代码）。"""
        from src.domains.runtime.engine import usage_collect
        called = []

        def _mark(tag):
            def _impl(self, *a, **kw):
                called.append(tag)
                return [{"via": tag}]
            return _impl

        monkeypatch.setattr(usage_collect.UsageCollector, "_hermes_usage", _mark("hermes"))
        monkeypatch.setattr(usage_collect.UsageCollector, "_hwcloud_usage", _mark("hwcloud"))
        monkeypatch.setattr(usage_collect.UsageCollector, "_opencode_usage", _mark("opencode"))

        assert usage_collect.UsageCollector("hermes").finish() == [{"via": "hermes"}]
        assert usage_collect.UsageCollector("hwcloud").finish() == [{"via": "hwcloud"}]
        assert usage_collect.UsageCollector("aishell").finish() == [{"via": "hwcloud"}]
        assert usage_collect.UsageCollector("opencode").finish() == [{"via": "opencode"}]
        # event_stream：走 _fed（finish 直返）
        uc = usage_collect.UsageCollector("codex")
        uc.feed("step_finish", {"usage": {"input": 1}})
        assert uc.finish() == [{"input": 1, "turn": 1}]
        # aishell_dsh_watermark：走二期解析轨（无会话文件时 fail-open 返回空）
        assert usage_collect.UsageCollector("aishell-dsh").finish() == []
        assert usage_collect.UsageCollector("dsh").finish() == []
        assert called == ["hermes", "hwcloud", "hwcloud", "opencode"]

    def test_start_dispatches_baseline_by_mode(self, monkeypatch):
        """start 按 usage.mode 记基线（累计/偏移源）；event_stream/none 无基线。"""
        from src.domains.runtime.engine import usage_collect
        called = []

        monkeypatch.setattr(usage_collect.UsageCollector, "_hermes_snapshot",
                            lambda self: {"snap": 1})
        monkeypatch.setattr(usage_collect.UsageCollector, "_hwcloud_offset",
                            lambda self: 123)
        monkeypatch.setattr(usage_collect.UsageCollector, "_opencode_watermark",
                            lambda self: {"wm": 9})

        uc = usage_collect.UsageCollector("hermes"); uc.start()
        assert uc._baseline == {"hermes": {"snap": 1}}
        uc = usage_collect.UsageCollector("hwcloud"); uc.start()
        assert uc._baseline == {"hwcloud": 123}
        uc = usage_collect.UsageCollector("aishell"); uc.start()
        assert uc._baseline == {"hwcloud": 123}  # alias 归一后同一模式
        uc = usage_collect.UsageCollector("opencode"); uc.start()
        assert uc._baseline == {"opencode": {"wm": 9}}
        uc = usage_collect.UsageCollector("codex"); uc.start()
        assert uc._baseline == {}
        uc = usage_collect.UsageCollector("aishell-dsh"); uc.start()
        assert uc._baseline == {"aishell_dsh_records": 0}  # 二期水位：无会话文件=0
