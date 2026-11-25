"""B5 cubesandbox 探测白名单/深检 分派 descriptor 化。

盘点裁定（回执 B5 B类/A类）：
- `_PROBE_WHITELIST` 手抄元组 → descriptor capabilities.sandbox_installable 派生；
  箱内 CLI 名经 `sandbox.probe_cli` 声明（hwcloud 箱内二进制名是别名 aishell，
  散布点证据=原元组第 5 项——不能用 canonical hwcloud，否则改变探测行为）。
- `engine == "hermes"` 箱内 ACP 深检分支 → descriptor `sandbox.acp_deep_check`
  驱动（hwcloud 同为 acp_handshake 但不做箱内深检——mode 不足以分派，须独立位）；
  深检 shell 公式是引擎 quirk，留代码不描述化（红线：不吞并语义）。
"""
import pytest


class TestDescriptorSandboxFields:
    def test_probe_cli_defaults_and_overrides(self):
        """sandbox_installable 引擎必有探测名；hwcloud 箱内名=别名 aishell。"""
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        inst = [c for c, d in reg.all().items()
                if d["capabilities"]["sandbox_installable"]]
        assert sorted(inst) == ["codearts", "codex", "hermes", "hwcloud", "opencode"]
        for c in inst:
            d = reg.get(c)
            cli = d.get("sandbox", {}).get("probe_cli", c)
            assert cli, f"{c} 缺 sandbox.probe_cli"
        assert reg.get("hwcloud")["sandbox"]["probe_cli"] == "aishell"

    def test_acp_deep_check_only_hermes(self):
        """箱内 ACP 深检位：仅 hermes（hwcloud 同 acp_handshake 但无箱内深检）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        for c, d in reg.all().items():
            flag = bool(d.get("sandbox", {}).get("acp_deep_check"))
            assert flag == (c == "hermes"), f"{c} acp_deep_check 漂移"

    def test_sandbox_probe_list_matches_legacy_whitelist(self):
        """派生白名单与原手抄元组同集合（行为冻结口径）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        derived = set(get_registry().sandbox_probe_engines())
        legacy = {"hermes", "opencode", "codearts", "codex", "aishell"}
        assert derived == legacy, f"派生白名单漂移: {derived}"


class TestCubeSandboxDispatch:
    def test_whitelist_derived_not_hand_copied(self):
        """类级白名单来自 registry 派生（源码不得含手抄引擎名元组）。"""
        import inspect
        from src.domains.runtime.agents.sandbox.backends import cubesandbox
        src = inspect.getsource(cubesandbox)
        assert '_PROBE_WHITELIST = (' not in src, "白名单不得再手抄（B5 散点）"
        assert cubesandbox.CubeSandboxAdapter._PROBE_WHITELIST, "派生白名单非空"

    def test_deep_check_dispatch_by_descriptor(self, monkeypatch):
        """箱内深检按 descriptor acp_deep_check 派发：hermes 真、aishell 假。"""
        from src.domains.runtime.agents.sandbox.backends import cubesandbox
        cls = cubesandbox.CubeSandboxAdapter
        assert cls._box_acp_deep_check("hermes") is True
        assert cls._box_acp_deep_check("aishell") is False   # hwcloud 别名归一
        assert cls._box_acp_deep_check("opencode") is False
        # 派发判定不读引擎名字面量（inspect 实证，深检 shell 公式除外）
        import inspect
        src = inspect.getsource(cls)
        deep_src = inspect.getsource(cls._box_acp_deep_check)
        assert '"hermes"' not in deep_src, "深检派发不得按名字分支"
