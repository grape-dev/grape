
"""canonical_engine 查表单测（aishell-dsh 接入 S1 地基，Q 函数面锁死）。

覆盖设计 spec §10.5 风险表与实施计划 §4.1 用例 Q：
- 四向映射：aishell-dsh 先于 aishell/hwcloud 命中，旧轨逐字不变
- 表序无关：字典乱序不影响结果（最长前缀匹配）
- 三判定点同源：_backend_for_engine / _engine_type_of 与表一致（#14 守卫抽查）
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.runtime.instances import canonical_engine, usage_retry_engine


def test_four_way_mapping():
    assert canonical_engine("aishell-dsh-worker-001") == "aishell-dsh"
    assert canonical_engine("aishell-worker-001") == "hwcloud"
    assert canonical_engine("hwcloud-worker-001") == "hwcloud"
    assert canonical_engine("dsh-worker-001") == "dsh"


def test_other_engines_unchanged():
    assert canonical_engine("hermes-abc") == "hermes"
    assert canonical_engine("codearts-x") == "codearts"
    assert canonical_engine("codex-y") == "codex"
    # 旧轨：cursor 从未在 _backend_for_engine 映射，保持默认 opencode
    assert canonical_engine("cursor-z") == "opencode"
    assert canonical_engine("unknown-engine") == "opencode"
    assert canonical_engine("") == "opencode"
    assert canonical_engine(None) == "opencode"


def test_longest_prefix_order_independent():
    # 表序无关：即便把 aishell 排在 aishell-dsh 前面（打乱 dict 顺序重建），结果不变
    import importlib
    from src.runtime import instances

    saved = dict(instances.ENGINE_CANONICAL_PREFIXES)
    try:
        reordered = {
            "aishell": "hwcloud",       # 故意先短前缀
            "aishell-dsh": "aishell-dsh",  # 长前缀在后
            "hwcloud": "hwcloud",
            "hermes": "hermes",
            "codearts": "codearts",
            "codex": "codex",
            "dsh": "dsh",
        }
        instances.ENGINE_CANONICAL_PREFIXES.clear()
        instances.ENGINE_CANONICAL_PREFIXES.update(reordered)
        assert canonical_engine("aishell-dsh-worker-9") == "aishell-dsh"
        assert canonical_engine("aishell-worker-9") == "hwcloud"
    finally:
        instances.ENGINE_CANONICAL_PREFIXES.clear()
        instances.ENGINE_CANONICAL_PREFIXES.update(saved)


def test_backend_for_engine_same_table():
    from src.runtime.agent import _backend_for_engine

    assert _backend_for_engine("aishell-dsh-worker-1") == "aishell-dsh"
    assert _backend_for_engine("aishell-worker-1") == "hwcloud"
    assert _backend_for_engine("hwcloud-1") == "hwcloud"
    assert _backend_for_engine("hermes-1") == "hermes"
    assert _backend_for_engine("") == "opencode"


def test_engine_type_of_same_table():
    from src.api.routes.settings import _engine_type_of

    assert _engine_type_of({"name": "aishell-dsh-worker-1"}) == "aishell-dsh"
    assert _engine_type_of({"instance_name": "aishell-dsh-node2"}) == "aishell-dsh"
    assert _engine_type_of({"name": "aishell-worker-1"}) == "aishell"
    assert _engine_type_of({"name": "hwcloud-1"}) == "aishell"
    assert _engine_type_of({"name": "hermes-1"}) == "hermes"
    assert _engine_type_of({"name": ""}) == ""


def test_usage_retry_guards():
    assert usage_retry_engine("hermes-1") is True
    assert usage_retry_engine("hwcloud-1") is True
    assert usage_retry_engine("aishell-1") is True       # 旧轨
    assert usage_retry_engine("aishell-dsh-1") is True   # 新引擎纳入（#14 收敛口径）
    assert usage_retry_engine("opencode-1") is False
    assert usage_retry_engine("") is False
