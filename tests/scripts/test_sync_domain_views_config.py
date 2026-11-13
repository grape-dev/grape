"""跨域视图同步脚本的参数解析/校验单测（QA #1324 #4 要求；无需连库）。"""
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "sync_domain_views.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_domain_views", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_defaults_when_no_env_no_cli():
    owner, targets, tables = mod.resolve_config(env={})
    assert owner == "grape_runtime_db"
    assert targets == ("grape_mgmt_db", "grape_insight_db")
    assert "grape_t_repo_credentials" in tables


def test_env_overrides_defaults_and_filters_blanks():
    env = {
        "GRAPE_OWNER_DB": " grape_dev_runtime ",
        "GRAPE_TARGET_DBS": "grape_dev_mgmt, ,grape_dev_insight,,grape_dev_mgmt",
        "GRAPE_SYNC_TABLES": "grape_t_system_credentials , grape_t_repo_credentials",
    }
    owner, targets, tables = mod.resolve_config(env=env)
    assert owner == "grape_dev_runtime"
    assert targets == ("grape_dev_mgmt", "grape_dev_insight")  # 空白/空项过滤 + 去重保序
    assert tables == ("grape_t_system_credentials", "grape_t_repo_credentials")


def test_cli_beats_env():
    env = {"GRAPE_OWNER_DB": "env_owner", "GRAPE_TARGET_DBS": "env_target"}
    owner, targets, _ = mod.resolve_config("cli_owner", "cli_target", None, env=env)
    assert owner == "cli_owner"
    assert targets == ("cli_target",)


@pytest.mark.parametrize("bad", ["grape`db", "grape-db", "grape db", "", " ", "a" * 65])
def test_invalid_owner_identifier_rejected(bad):
    with pytest.raises(ValueError):
        mod.resolve_config(owner=bad, env={})


@pytest.mark.parametrize("bad", ["tbl`x", "tbl-x"])
def test_invalid_table_identifier_rejected(bad):
    with pytest.raises(ValueError):
        mod.resolve_config(tables=f"agent_tasks,{bad}", env={})


def test_empty_tables_rejected():
    # 显式空值报错；而 "a,,b" 这类空项只是被过滤（不算错误）
    with pytest.raises(ValueError):
        mod.resolve_config(tables="", env={})
    owner, targets, tables = mod.resolve_config(tables="agent_tasks, ,grape_t_repo_credentials", env={})
    assert tables == ("agent_tasks", "grape_t_repo_credentials")


def test_invalid_target_identifier_rejected():
    with pytest.raises(ValueError):
        mod.resolve_config(target="grape_dev_mgmt,evil;drop", env={})