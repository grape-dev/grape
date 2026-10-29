"""Issue #470: 写死运行配置抽取——配置读取层失败测试（红）。

覆盖 src/config.py 新增的统一配置 getter：
- get_runtime_task_timeout()        超时家族单一来源（默认 3600s）
- get_stuck_threshold_minutes()     stuck 预警派生阈值（默认 22min）
- get_orphan_threshold_minutes()    孤儿回收派生阈值（默认 360min）
- get_devstage_template_id()        DevStage 模板 ID（默认 4ef59a…）
- get_devstage_flavor()             DevStage 规格（默认 3c6g ARM + flavor）

优先级契约（Spec 验收）：env 覆盖 > 管理台值（DB meta）> 配置文件（runtime-config.json）> 默认值。
非法值逐级回退，任何一层解析失败不得抛异常。
"""
import json
import os
import sys

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from src.config import (  # noqa: E402
    RUNTIME_CONFIG_PATH,
    get_runtime_task_timeout,
    get_stuck_threshold_minutes,
    get_orphan_threshold_minutes,
    get_devstage_template_id,
    get_devstage_flavor,
    invalidate_runtime_config_cache,
    _write_runtime_config_for_test,
)

DEFAULT_TIMEOUT = 3600
DEFAULT_TEMPLATE_ID = "4ef59a2b4d3d4909aacd80f6007c00b2"
DEFAULT_FLAVOR_ID = "23ebdfe3b2d34cc4b34a1aaf03d27c54"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例前清空三源（env / meta / 文件）+ 缓存，保证从确定基线开始。"""
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
    monkeypatch.delenv("GRAPE_DEVSTAGE_FLAVOR_ID", raising=False)
    monkeypatch.delenv("GRAPE_DEVSTAGE_CORES", raising=False)
    monkeypatch.delenv("GRAPE_DEVSTAGE_MEMORY", raising=False)
    from src.user.db import set_meta_value
    set_meta_value("runtime_task_timeout", "")
    set_meta_value("devstage_template_id", "")
    set_meta_value("devstage_flavor_id", "")
    set_meta_value("devstage_cores", "")
    set_meta_value("devstage_memory", "")
    set_meta_value("devstage_arch", "")
    if os.path.exists(RUNTIME_CONFIG_PATH):
        os.remove(RUNTIME_CONFIG_PATH)
    invalidate_runtime_config_cache()
    yield
    if os.path.exists(RUNTIME_CONFIG_PATH):
        os.remove(RUNTIME_CONFIG_PATH)
    invalidate_runtime_config_cache()


class TestDefaults:
    """三源全空 → 与抽取前现状完全一致的默认值（验收：默认值兼容）。"""

    def test_task_timeout_default(self):
        assert get_runtime_task_timeout() == DEFAULT_TIMEOUT

    def test_stuck_default(self):
        assert get_stuck_threshold_minutes() == 22

    def test_orphan_default(self):
        assert get_orphan_threshold_minutes() == 360

    def test_devstage_template_default(self):
        assert get_devstage_template_id() == DEFAULT_TEMPLATE_ID

    def test_devstage_flavor_default(self):
        f = get_devstage_flavor()
        assert f == {
            "flavor_id": DEFAULT_FLAVOR_ID, "cores": 3, "memory": 6, "arch": "ARM",
        }


class TestFileSource:
    """配置文件 runtime-config.json 单独生效（验收：改配置文件即可生效）。"""

    def test_timeout_from_file(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 1800})
        assert get_runtime_task_timeout() == 1800

    def test_devstage_from_file(self):
        _write_runtime_config_for_test({"devstage": {
            "template_id": "aaa", "flavor_id": "bbb", "cores": 8, "memory": 16, "arch": "x86",
        }})
        assert get_devstage_template_id() == "aaa"
        assert get_devstage_flavor()["cores"] == 8
        assert get_devstage_flavor()["memory"] == 16
        assert get_devstage_flavor()["arch"] == "x86"

    def test_partial_file_keeps_other_defaults(self):
        """文件只给超时 → DevStage 仍默认（增量语义，不整组覆盖）。"""
        _write_runtime_config_for_test({"runtime_task_timeout": 7200})
        assert get_devstage_template_id() == DEFAULT_TEMPLATE_ID
        assert get_devstage_flavor()["cores"] == 3

    def test_corrupt_file_falls_back(self):
        """损坏 JSON → 全默认，不抛异常（非法值回退）。"""
        with open(RUNTIME_CONFIG_PATH, "w") as f:
            f.write("{not json")
        assert get_runtime_task_timeout() == DEFAULT_TIMEOUT


class TestMetaSource:
    """管理台值（DB meta）单独生效，且 文件 < meta（验收：管理台优先级）。"""

    def test_timeout_from_meta(self):
        from src.user.db import set_meta_value
        set_meta_value("runtime_task_timeout", "5400")
        invalidate_runtime_config_cache()
        assert get_runtime_task_timeout() == 5400

    def test_meta_overrides_file(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 1800})
        from src.user.db import set_meta_value
        set_meta_value("runtime_task_timeout", "5400")
        invalidate_runtime_config_cache()
        assert get_runtime_task_timeout() == 5400

    def test_devstage_from_meta(self):
        from src.user.db import set_meta_value
        set_meta_value("devstage_template_id", "tpl-meta")
        set_meta_value("devstage_cores", "4")
        set_meta_value("devstage_memory", "8")
        invalidate_runtime_config_cache()
        assert get_devstage_template_id() == "tpl-meta"
        assert get_devstage_flavor()["cores"] == 4
        assert get_devstage_flavor()["memory"] == 8


class TestEnvPriority:
    """env 覆盖最高优（验收：env 覆盖保留）——RUNTIME_TASK_TIMEOUT / GRAPE_DEVSTAGE_*。"""

    def test_env_overrides_all(self, monkeypatch):
        _write_runtime_config_for_test({"runtime_task_timeout": 1800})
        from src.user.db import set_meta_value
        set_meta_value("runtime_task_timeout", "5400")
        invalidate_runtime_config_cache()
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "900")
        assert get_runtime_task_timeout() == 900

    def test_devstage_env_overrides(self, monkeypatch):
        _write_runtime_config_for_test({"devstage": {"cores": 8, "memory": 16}})
        monkeypatch.setenv("GRAPE_DEVSTAGE_CORES", "2")
        monkeypatch.setenv("GRAPE_DEVSTAGE_MEMORY", "4")
        assert get_devstage_flavor()["cores"] == 2
        assert get_devstage_flavor()["memory"] == 4


class TestInvalidFallback:
    """非法值逐级回退：非法 meta 回退文件/默认，非法 env 回退 meta/文件/默认。"""

    def test_invalid_meta_falls_back_to_file(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 1800})
        from src.user.db import set_meta_value
        set_meta_value("runtime_task_timeout", "not-a-number")
        invalidate_runtime_config_cache()
        assert get_runtime_task_timeout() == 1800

    def test_invalid_env_falls_back_to_meta(self, monkeypatch):
        from src.user.db import set_meta_value
        set_meta_value("runtime_task_timeout", "5400")
        invalidate_runtime_config_cache()
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "garbage")
        assert get_runtime_task_timeout() == 5400

    def test_invalid_file_value_falls_back_to_default(self):
        _write_runtime_config_for_test({"runtime_task_timeout": -5})
        assert get_runtime_task_timeout() == DEFAULT_TIMEOUT

    def test_nonpositive_env_ignored(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_TASK_TIMEOUT", "0")
        assert get_runtime_task_timeout() == DEFAULT_TIMEOUT


class TestDerivation:
    """派生阈值随超时派生（Spec：派生阈值由统一超时配置派生）。"""

    def test_stuck_scales_with_timeout(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 7200})
        # 7200s → 22/3600 比例 → 44min
        assert get_stuck_threshold_minutes() == 44

    def test_orphan_scales_with_timeout(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 7200})
        # 7200s = 120min × 6 → 720min
        assert get_orphan_threshold_minutes() == 720

    def test_stuck_minimum_one_minute(self):
        _write_runtime_config_for_test({"runtime_task_timeout": 60})
        assert get_stuck_threshold_minutes() >= 1
