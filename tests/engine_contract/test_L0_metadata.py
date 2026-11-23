"""B7 段①（L0）：注册表可发现 + schema 校验 + 治理字段——纯文件，CI 恒跑。

新 agent「descriptor 落对了没有」在本段判定：可发现、字段合法、与
executor 注册表/探活表/drift 守卫一致。零环境要求，恒不许 skip。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import src.domains.runtime.engine.registry  # noqa: E402,F401

from src.domains.runtime.engine.descriptor import get_registry, EngineDescriptorRegistry


def test_s1_descriptor_discoverable_and_schema_valid(engine):
    """段①：descriptor 可被 registry 发现且六字段合法。"""
    reg = get_registry()
    d = reg.get(engine)
    assert d is not None, f"{engine} 不可被 registry 发现"
    assert d["canonical"] == engine
    assert d["protocol"] in ("acp", "ndjson", "uhp", "ghost")
    assert d["discover"]["cli"], "discover.cli 必备"
    assert d["display"]["name"], "display.name 必备"
    assert d["probe"]["mode"] in ("cli_run", "acp_handshake", "none")
    assert isinstance(d["capabilities"].get("sandbox_installable"), bool)


def test_s1_registry_schema_validation_rejects_bad_file(tmp_path):
    """schema 校验的负路径：坏 descriptor 必须被拒（不是静默加载）。"""
    bad = tmp_path / "bad-engine.json"
    bad.write_text('{"canonical": "bad-engine", "protocol": "bogus"}', encoding="utf-8")
    from src.domains.runtime.engine.descriptor import DescriptorValidationError
    # [复审 R6] assert → 显式异常（python -O 下 assert 失效）；校验须带文件路径
    with pytest.raises(DescriptorValidationError) as ei:
        EngineDescriptorRegistry(str(tmp_path))
    assert str(bad) in str(ei.value), "校验异常须携带 descriptor 文件路径"


def test_s1_executor_registration_consistent(engine):
    """段①治理面：已注册 _BACKENDS 的引擎须有对应 descriptor（反之 ghost 除外）。

    即 drift 双向守卫：descriptor 加了但 _BACKENDS 没加 → 接入未完成；
    _BACKENDS 有但 descriptor 没有 → 散布回潮。
    """
    from src.domains.runtime.agents import executor
    reg = get_registry()
    d = reg.get(engine)
    if engine in executor._BACKENDS:
        assert d is not None, f"{engine} 已注册 _BACKENDS 但缺 descriptor"
        # ACP 引擎必须暴露 acp_backend() 钩子（P0a dispatch 契约）
        if d["protocol"] == "acp":
            b = executor._BACKENDS[engine]
            assert callable(getattr(b, "acp_backend", None)), \
                f"{engine} protocol=acp 但注册 backend 缺 acp_backend() 钩子"
    else:
        assert d.get("protocol") == "ghost" or engine == "cursor", \
            f"descriptor {engine} 未注册 _BACKENDS 且非 ghost——接入半完成"


def test_s1_drift_guard_green():
    """段①：drift 守卫零漂移（所有散布点与 descriptor 一致）。"""
    from scripts.engine_descriptor_drift import collect_drift
    assert collect_drift() == []
