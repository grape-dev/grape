# -*- coding: utf-8 -*-
"""本机探活深检位单测（F1 恢复，PR #2047 复审条件 2）。

背景：P0 收编时 probe_local_engines 删掉了 hermes 的 acp_probe 注入，但
消费点 local_probe._probe_one:78 原样保留 → 真实 ACP 握手 + 模型调用探针
（Issue #1982 R1 / #2545）不可达，「healthy 但任务 404 Model not found」回归。
修复 = descriptor probe.local_acp_deep_check 位驱动注入（不做引擎名判定）。

本套锁三件事：
① 位为 true 的引擎（hermes）被注入 acp_probe=True；
② 位为 false/缺省的引擎不注入；
③ registry 查询方法的别名/未知名口径（不猜不 fallback）。
"""
from __future__ import annotations

from src.domains.runtime.engine.local_probe import probe_local_engines
from src.domains.runtime.engine.descriptor import get_registry


class TestLocalAcpDeepCheckGate:
    """acp_probe 注入 = descriptor 位，非引擎名。"""

    def test_hermes_gets_acp_probe_flag(self):
        engines = [
            {"name": "hermes", "installed": True, "cli_path": "/bin/true"},
            {"name": "opencode", "installed": True, "cli_path": "/bin/true"},
        ]
        # 只断言注入层：不真跑探活（_probe_one 不进入——提供未安装的第三行即可，
        # 这里直接调注入前置不可能单独调，改用 probe_local_engines 传入
        # installed=False 浅探短路路径，检查输入 dict 的注入结果经返回透传）。
        out = {r["name"]: r for r in probe_local_engines(
            [{"name": n, "installed": True, "cli_path": ""}
             for n in ("hermes", "opencode", "dsh")])}
        # cli_path 为空 → _probe_one 早退原样透传（installed=True 保证注入层生效，
        # 浅探不真跑，断言注入结果经早退路径透传）
        assert out["hermes"].get("acp_probe") is True
        assert "acp_probe" not in out["opencode"]
        assert "acp_probe" not in out["dsh"]

    def test_registry_bit_hermes_only(self):
        reg = get_registry()
        assert reg.probe_local_acp_deep_check("hermes") is True
        for other in ("opencode", "hwcloud", "aishell-dsh", "dsh", "codearts"):
            assert reg.probe_local_acp_deep_check(other) is False, other

    def test_registry_unknown_name_is_false(self):
        assert get_registry().probe_local_acp_deep_check("no-such-engine") is False
        assert get_registry().probe_local_acp_deep_check(None) is False

    def test_descriptor_field_present_on_hermes(self):
        d = get_registry().get("hermes")
        assert d is not None
        assert d.get("probe", {}).get("local_acp_deep_check") is True
