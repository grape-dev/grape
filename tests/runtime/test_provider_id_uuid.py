# A12 回归：provider 主键已是 UUIDv4 TEXT（grape_t_sandbox_provider_config.id），
# normalize/构造/上报链不得对 UUID 做 int() 强转。
# 实测根因（2026-11-17 .17 sandbox worker）：provider-configs 每分钟
# `invalid literal for int() with base 10: '31326be2-...'` → 沙箱模板/引擎供给退化。
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.agents.sandbox.backends.api import coerce_provider_id
from src.domains.runtime.agents.sandbox.backends import get_adapter


class TestCoerceProviderId:
    def test_uuid_passthrough(self):
        uid = "31326be2-b1de-4042-b8e9-2cffff6a9f6e"
        assert coerce_provider_id(uid) is uid
        assert coerce_provider_id({"id": 1}.get("x", 0) or 0) == 0

    def test_numeric_string_to_int(self):
        assert coerce_provider_id("4") == 4
        assert coerce_provider_id("04") == 4

    def test_int_passthrough_and_empty(self):
        assert coerce_provider_id(7) == 7
        assert coerce_provider_id(0) == 0
        assert coerce_provider_id("") == 0
        assert coerce_provider_id(None) == 0

    def test_normalize_db_config_uuid_id(self):
        """normalize_db_config 收到 UUID 主键行不炸、provider_id 保留 UUID。"""
        adapter = get_adapter("cubesandbox")
        cfg = adapter.normalize_db_config({
            "id": "31326be2-b1de-4042-b8e9-2cffff6a9f6e",
            "platform": "cubesandbox", "name": "s3",
            "endpoint": "http://192.168.0.17:3000",
            "api_key": "", "extra_config": "{}",
        })
        assert cfg["provider_id"] == "31326be2-b1de-4042-b8e9-2cffff6a9f6e"

    def test_normalize_db_config_legacy_int_id(self):
        """历史数字 id 行仍收敛为 int（旧字典键语义不回退）。"""
        adapter = get_adapter("cubesandbox")
        cfg = adapter.normalize_db_config({
            "id": "4", "platform": "cubesandbox", "name": "legacy",
            "endpoint": "", "api_key": "", "extra_config": "{}",
        })
        assert cfg["provider_id"] == 4
