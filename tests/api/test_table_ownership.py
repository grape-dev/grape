"""表归属守卫用例：域判定、owner 判定、未知放行、开关、索引表名解析、db_setup 接入点。"""
import pathlib

import pytest

from src.kernel import table_ownership as own

MANIFEST = pathlib.Path(__file__).resolve().parents[2] / "configs" / "table_ownership.json"


@pytest.fixture(autouse=True)
def _reload():
    own.reload_manifest()
    yield
    own.reload_manifest()


def test_manifest_exists_and_covers_domains():
    assert MANIFEST.is_file(), "表归属清单缺失"
    data = own._load_manifest()["tables"]
    assert len(data) >= 100
    for tbl in ("agent_tasks", "skill", "devops_pipeline_scene", "obs_log", "takeover_sessions"):
        assert tbl in data, f"{tbl} 未登记归属"


def test_key_owners():
    assert own.owner_of("agent_tasks") == "runtime"
    assert own.owner_of("skill") == "runtime"
    assert own.owner_of("devops_pipeline_scene") == "mgmt"
    assert own.owner_of("obs_log") == "insight"
    # 多份表的裁决以 docs/weekly-11/table-owner.tsv（P0 冻结）为准
    for t in ("branch_guard_state", "takeover_sessions",
              "grape_t_credential_usage_log", "grape_t_system_credentials"):
        assert own.owner_of(t) == "runtime", f"{t} 应归 runtime"
    # 渠道族：SQL 落在 mgmt public（runtime 仅转发），归属 mgmt（2026-11-15 对齐）
    for t in ("grape_t_channel_lease", "grape_t_channel_installation",
              "grape_t_channel_inbound_dedup", "event_action_filter",
              "event_route_config", "pipeline_instances"):
        assert own.owner_of(t) == "mgmt", f"{t} 应归 mgmt"
    for t in ("grape_t_repo_credentials", "grape_t_merge_gate_log"):
        assert own.owner_of(t) == "insight", f"{t} 应归 insight"
    for t in ("meta", "scene_category"):
        assert own.owner_of(t) == "runtime", f"{t} 应归 runtime"


@pytest.mark.parametrize("raw,expected", [
    ("grape_dev_mgmt", "mgmt"),
    ("grape_dev_runtime", "runtime"),
    ("grape_dev_insight", "insight"),
    ("grape_mgmt_db", "mgmt"),
    ("grape_runtime_db", "runtime"),
    ("mgmt", "mgmt"),
    ("insight", "insight"),
    ("", None),
    ("postgres", None),
])
def test_domain_detection(raw, expected):
    assert own._domain_from(raw) == expected


def test_current_domain_from_env(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_runtime")
    monkeypatch.delenv("GRAPE_PROCESS_ROLE", raising=False)
    assert own.current_domain() == "runtime"

    monkeypatch.delenv("DEVOPS_DB_NAME")
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "insight")
    assert own.current_domain() == "insight"


def test_foreign_detection(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.delenv("GRAPE_DDL_OWNER_GUARD", raising=False)
    assert own.foreign_owner("agent_tasks") == "runtime"
    assert own.foreign_owner("devops_pipeline_scene") is None
    assert own.foreign_owner("a_table_that_does_not_exist") is None  # 未知放行


def test_guard_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.setenv("GRAPE_DDL_OWNER_GUARD", "0")
    assert own.foreign_owner("agent_tasks") is None
    assert own.should_skip("agent_tasks") is False


def test_should_skip_for_other_domain(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_runtime")
    assert own.should_skip("devops_pipeline_scene") is True
    assert own.should_skip("agent_tasks") is False


def test_index_table_parsing():
    from src.api import db_setup

    assert db_setup._index_table("CREATE INDEX IF NOT EXISTS idx_x ON agent_tasks(status)") == "agent_tasks"
    assert db_setup._index_table("CREATE UNIQUE INDEX idx_y ON `issue`(repo_key)") == "issue"
    assert db_setup._index_table("CREATE TABLE t (id int)") == ""


def test_db_setup_guard_hook(monkeypatch):
    from src.api import db_setup

    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.delenv("GRAPE_DDL_OWNER_GUARD", raising=False)
    own.reload_manifest()
    assert db_setup._owned_skip("agent_tasks", "create table") is True
    assert db_setup._owned_skip("devops_pipeline_scene", "create table") is False

    monkeypatch.setenv("GRAPE_DDL_OWNER_GUARD", "0")
    assert db_setup._owned_skip("agent_tasks", "create table") is False

def test_guard_disabled_for_sqlite_backend(monkeypatch):
    """单库（sqlite）模式必须放行：否则测试/单机库会缺表。"""
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    assert own.guard_enabled() is False
    assert own.foreign_owner("agent_tasks") is None
