"""B3 点 7：settings 展示名分派 descriptor 化（行为冻结）。

- `display.page_type`：canonical → 页面类型标签（settings 类型过滤口径）。
  hwcloud 特例 = "aishell"（历史展示名，与前端 _availTypes 一致）；其余缺省
  = canonical。消费方：`_engine_type_of`（原两条硬编码特例删除）。
- `display.device_types`：设备视图引擎类型位（原 `_device_engine_types`
  手抄 `_KNOWN` 5 家元组）。
- 非前缀历史名 includes 兜底链 = 顺序敏感 quirk 留代码；其返回标签经
  registry.page_type() 派生（hwcloud 别名名段 → "aishell" 单源）。
"""
import inspect

import pytest


class TestDescriptorPageType:
    def test_hwcloud_page_type_is_legacy_aishell(self):
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        assert reg.page_type("hwcloud") == "aishell"  # 历史展示名特例

    def test_page_type_defaults_canonical(self):
        from src.domains.runtime.engine.descriptor import get_registry
        reg = get_registry()
        for c in reg.all():
            expected = "aishell" if c == "hwcloud" else c
            assert reg.page_type(c) == expected, f"{c} page_type 漂移"

    def test_device_types_flag_matches_legacy_tuple(self):
        """派生集合 == 原 _KNOWN 5 家 + mock-acp（C05 验收引擎, device_types:true）。"""
        from src.domains.runtime.engine.descriptor import get_registry
        got = {c for c, d in get_registry().all().items()
               if d.get("display", {}).get("device_types")}
        assert got == {"hermes", "opencode", "codearts", "codex", "atomcode", "mock-acp"}


class TestEngineTypeOfFrozen:
    """_engine_type_of 行为冻结：历史名/别名/兜底链逐条对照原实现。"""

    @pytest.fixture()
    def f(self):
        from src.domains.mgmt.routes import settings as s
        return s._engine_type_of

    @pytest.mark.parametrize("name,expected", [
        ("hermes-t1", "hermes"),           # canonical 前缀
        ("hwcloud-prod", "aishell"),       # hwcloud → 页面历史名
        ("aishell-1", "aishell"),          # 别名前缀
        ("aishell-dsh-t1", "aishell-dsh"), # 长前缀优先
        ("opencode-dev", "opencode"),
        ("my-hermes-2", "hermes"),        # 非前缀历史名兜底
        ("x-aishell-9", "aishell"),       # 兜底链别名段 → 页面历史名
        ("x-hwcloud-9", "aishell"),       # 兜底链 canonical 段
        ("x-codearts-codex-1", "codearts"),  # 兜底链顺序（codearts 先于 codex）
        ("y-codex-atomcode-1", "codex"),     # 兜底链顺序（codex 先于 atomcode）
        ("x-atomcode-3", "atomcode"),
        ("", ""),                          # 空名
        ("unknown-engine", ""),             # 兜底不中 → 空
    ])
    def test_labels(self, f, name, expected):
        rt = {"instance_name": name, "name": name}
        assert f(rt) == expected, f"_engine_type_of({name!r}) 行为漂移"


class TestSettingsSourceConverged:
    def test_no_hardcoded_hwcloud_page_label(self):
        from src.domains.mgmt.routes import settings
        src = inspect.getsource(settings)
        assert 'if c == "hwcloud"' not in src, "page_type 特例应经 descriptor 派生"
        assert 'if c == "aishell-dsh"' not in src, "page_type 特例应经 descriptor 派生"

    def test_no_hand_copied_known_tuple(self):
        from src.domains.mgmt.routes import settings
        src = inspect.getsource(settings)
        assert '_KNOWN = [("hermes"' not in src, "设备视图清单应经 descriptor 派生"
        assert "page_type" in src, "标签应经 registry.page_type 派生"
