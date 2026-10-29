"""Issue #470: DevStage 配置收敛 + 管理台读写失败测试（红）。

覆盖：
1. devstage.py 模板 ID 单一来源（两处重复写死消除）+ flavor/cores/memory/arch
   从统一 getter 取（env GRAPE_DEVSTAGE_* 覆盖语义保留）
2. 管理台配置中心（/api/admin/config GET/PUT）读写运行参数字段，
   保存后回显、生效值可见
"""
import os
import sys

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from src.config import _write_runtime_config_for_test, RUNTIME_CONFIG_PATH, invalidate_runtime_config_cache  # noqa: E402

DEFAULT_TPL = "4ef59a2b4d3d4909aacd80f6007c00b2"
DEFAULT_FLAVOR = "23ebdfe3b2d34cc4b34a1aaf03d27c54"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("RUNTIME_TASK_TIMEOUT", "GRAPE_DEVSTAGE_FLAVOR_ID",
              "GRAPE_DEVSTAGE_CORES", "GRAPE_DEVSTAGE_MEMORY", "GRAPE_DEVSTAGE_ARCH"):
        monkeypatch.delenv(k, raising=False)
    from src.user.db import set_meta_value
    for k in ("runtime_task_timeout", "devstage_template_id", "devstage_flavor_id",
              "devstage_cores", "devstage_memory", "devstage_arch"):
        set_meta_value(k, "")
    if os.path.exists(RUNTIME_CONFIG_PATH):
        os.remove(RUNTIME_CONFIG_PATH)
    invalidate_runtime_config_cache()
    yield
    if os.path.exists(RUNTIME_CONFIG_PATH):
        os.remove(RUNTIME_CONFIG_PATH)
    # 清理本用例文件写入的 meta，避免污染其他用例
    from src.user.db import set_meta_value
    for k in ("runtime_task_timeout", "devstage_template_id", "devstage_flavor_id",
              "devstage_cores", "devstage_memory", "devstage_arch"):
        set_meta_value(k, "")
    invalidate_runtime_config_cache()


class TestDevstageConvergence:
    """模板 ID / 规格收敛到单一来源（src/config getter），默认行为不变。"""

    def test_template_id_single_source(self):
        from src.api.routes import devstage as ds
        assert ds.GRAPE_AI_DEVSPACE_TEMPLATE() == DEFAULT_TPL

    def test_template_id_from_config(self):
        _write_runtime_config_for_test({"devstage": {"template_id": "tpl-42"}})
        from src.api.routes import devstage as ds
        assert ds.GRAPE_AI_DEVSPACE_TEMPLATE() == "tpl-42"

    def test_create_env_uses_unified_flavor(self):
        """create_environment 不再各自读 env/写死默认——经统一 getter 取规格。

        seam：_create_env_call_args(body) 返回传给 _create_env 的 kwargs（纯组装函数）。
        """
        from src.api.routes import devstage as ds
        args = ds._create_env_call_args({"name": "env-1"})
        assert args["template_id"] == DEFAULT_TPL
        assert args["flavor_id"] == DEFAULT_FLAVOR
        assert args["cores"] == 3
        assert args["memory"] == 6
        assert args["arch"] == "ARM"

    def test_create_env_flavor_from_config(self):
        _write_runtime_config_for_test({"devstage": {
            "flavor_id": "fl-9", "cores": 8, "memory": 32, "arch": "x86"}})
        from src.api.routes import devstage as ds
        args = ds._create_env_call_args({"name": "env-2"})
        assert args["flavor_id"] == "fl-9"
        assert args["cores"] == 8
        assert args["memory"] == 32
        assert args["arch"] == "x86"

    def test_create_env_env_vars_still_override(self, monkeypatch):
        """既有 GRAPE_DEVSTAGE_* env 覆盖语义保留（云侧规格下线时运维兜底）。"""
        _write_runtime_config_for_test({"devstage": {"cores": 8, "memory": 32}})
        monkeypatch.setenv("GRAPE_DEVSTAGE_CORES", "2")
        monkeypatch.setenv("GRAPE_DEVSTAGE_MEMORY", "4")
        from src.api.routes import devstage as ds
        args = ds._create_env_call_args({"name": "env-3"})
        assert args["cores"] == 2
        assert args["memory"] == 4


_MOCK_ADMIN = {"id": 1, "username": "admin", "token_type": "admin"}


@pytest.fixture
def client():
    """TestClient with mocked admin auth — real SQLite DB, no JWT."""
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.deps import require_admin
    app.dependency_overrides[require_admin] = lambda: _MOCK_ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestAdminRuntimeConfig:
    """管理台配置中心：运行参数可查看、可修改、保存后生效值正确。"""

    def test_get_returns_defaults(self, client):
        r = client.get("/api/admin/config")
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["runtime_task_timeout"] == 3600
        assert cfg["runtime_task_timeout_effective"] == 3600
        assert cfg["devstage_template_id"] == DEFAULT_TPL
        assert cfg["devstage_flavor_id"] == DEFAULT_FLAVOR
        assert cfg["devstage_cores"] == 3
        assert cfg["devstage_memory"] == 6
        assert cfg["devstage_arch"] == "ARM"

    def test_put_updates_and_echoes(self, client):
        r = client.put("/api/admin/config", json={
            "runtime_task_timeout": 7200,
            "devstage_template_id": "tpl-admin",
            "devstage_flavor_id": "fl-admin",
            "devstage_cores": 4,
            "devstage_memory": 8,
            "devstage_arch": "x86",
        })
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["runtime_task_timeout"] == 7200
        assert cfg["runtime_task_timeout_effective"] == 7200
        assert cfg["devstage_template_id"] == "tpl-admin"
        assert cfg["devstage_cores"] == 4

    def test_effective_after_save(self, client):
        """保存后统一 getter 立即读到新值（缓存失效链路）。"""
        client.put("/api/admin/config", json={"runtime_task_timeout": "5400"})
        from src.config import get_runtime_task_timeout
        assert get_runtime_task_timeout() == 5400

    def test_invalid_timeout_rejected(self, client):
        """非法超时值（非正数）被校验拒绝（pydantic gt 约束 422），不落库。"""
        r = client.put("/api/admin/config", json={"runtime_task_timeout": -5})
        assert r.status_code == 422
        # 未落库：getter 仍默认
        from src.config import get_runtime_task_timeout
        assert get_runtime_task_timeout() == 3600

    def test_partial_put_keeps_other_fields(self, client):
        """增量语义：只改超时不影响 DevStage 字段（沿用「只保存非空字段」约定）。"""
        client.put("/api/admin/config", json={"devstage_cores": 4})
        r2 = client.put("/api/admin/config", json={"runtime_task_timeout": 1800})
        cfg = r2.json()["config"]
        assert cfg["devstage_cores"] == 4
        assert cfg["runtime_task_timeout"] == 1800

    def test_get_with_dirty_meta_falls_back(self, client):
        """QA Minor2 脏数据防御：meta 非数字脏数据（绕过 API 写库）不导致 GET 500，回退默认值。"""
        from src.user.db import set_meta_value
        set_meta_value("runtime_task_timeout", "abc")
        set_meta_value("devstage_cores", "x-large")
        set_meta_value("devstage_memory", "6GB")
        r = client.get("/api/admin/config")
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["runtime_task_timeout"] == 3600
        assert cfg["devstage_cores"] == 3
        assert cfg["devstage_memory"] == 6
