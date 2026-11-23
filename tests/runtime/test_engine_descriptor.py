"""P0b B2：EngineDescriptorRegistry——descriptor 目录加载 / 别名查找 / 值域枚举。

根因：引擎元数据散布 6 处（executor.known / instances.py / db._KNOWN /
engine_probe 命令表 / settings 展示名 / 前端颜色表）+ 两套注册表
（_BACKENDS 8 vs ENGINE_REGISTRY 5）靠注释对齐——漏改任何一处即漂移
（db.py:4390 _KNOWN 漏加 dsh/hwcloud/aishell-dsh 曾导致拒建占位行）。
spec B1/B2：每引擎一份 JSON 单源（文件名=canonical），registry 进程内加载。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.domains.runtime.engine.registry  # noqa: E402,F401 先触发（绕开循环导入）
from src.domains.runtime.engine.descriptor import (
    EngineDescriptorRegistry, get_registry,
)

DESC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src", "domains", "runtime", "engine", "descriptors")


class TestRegistryLoad:
    def test_descriptor_dir_exists_and_populated(self):
        reg = EngineDescriptorRegistry(DESC_DIR)
        assert reg.count() >= 9, "现役+ghost 引擎（含 cursor）各须一份 descriptor"
        # spec B1：文件名 = canonical 名
        for canonical in ("opencode", "hermes", "hwcloud", "aishell-dsh", "cursor",
                          "codearts", "codex", "dsh", "atomcode"):
            assert canonical in reg, f"缺 {canonical} descriptor"

    def test_fields_validated(self):
        """六字段必备：canonical/protocol/discover/display/probe/capabilities。"""
        reg = EngineDescriptorRegistry(DESC_DIR)
        for canonical, d in reg.all().items():
            assert d["canonical"] == canonical
            assert d["protocol"] in ("acp", "ndjson", "uhp", "ghost")
            assert "cli" in d["discover"]
            assert "name" in d["display"]
            assert "command" in d["probe"] and "mode" in d["probe"]
            assert isinstance(d["capabilities"], dict)
            assert "sandbox_installable" in d["capabilities"]

    def test_alias_lookup(self):
        """别名查找：aishell 前缀 → hwcloud；canonical 精确命中自身。"""
        reg = EngineDescriptorRegistry(DESC_DIR)
        assert reg.get("aishell")["canonical"] == "hwcloud"
        assert reg.get("hwcloud")["canonical"] == "hwcloud"
        assert reg.get("hermes")["canonical"] == "hermes"
        assert reg.get("no-such-engine") is None

    def test_prefix_map_derived_from_descriptors(self):
        """instances.ENGINE_CANONICAL_PREFIXES 的值域须与 descriptor 派生一致
        （最长前缀语义由 instances 保留，但键值对单源化）。"""
        reg = EngineDescriptorRegistry(DESC_DIR)
        derived = reg.prefix_map()
        from src.domains.runtime.engine.instances import ENGINE_CANONICAL_PREFIXES
        assert derived == ENGINE_CANONICAL_PREFIXES

    def test_singleton_get_registry(self):
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2


class TestConsumerValueDomains:
    """B4 前置：descriptor 派生清单与散布静态值可对账（drift 脚本的数据基础）。"""

    def test_executor_known_matches_descriptors(self):
        reg = EngineDescriptorRegistry(DESC_DIR)
        from src.domains.runtime.agents.executor import discover_backends
        known = {r["name"] for r in discover_backends()}
        assert known == set(reg.all().keys()), "executor known 值域须与 descriptor 一致"

    def test_probe_commands_match_descriptors(self):
        """[B5 合一后修正] 命令表已消——口径改为 descriptor probe 字段与
        分派器注册表的齐备性（两档字段齐备：mode/impl/env_hook/light_args）。"""
        reg = EngineDescriptorRegistry(DESC_DIR)
        from src.domains.runtime.engine import probe_dispatch
        for canonical, d in reg.all().items():
            probe = d["probe"]
            if probe["mode"] == "cli_run":
                assert isinstance(probe["command"], list) and probe["command"], canonical
            if probe["mode"] == "acp_handshake":
                assert probe.get("impl") in probe_dispatch._ACP_IMPLS, \
                    f"{canonical} probe.impl 未注册"
            if probe.get("env_hook"):
                assert probe["env_hook"] in probe_dispatch._ENV_HOOKS, \
                    f"{canonical} probe.env_hook 未注册"
