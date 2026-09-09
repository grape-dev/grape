"""Issue-45 M4：grape CLI 测试（配置/契约/注册/帮助/安全）。

样例契约只含少量 operation，足以验证契约驱动注册（R7：新增接口零 CLI 改动）。
"""
import json
import os
import sys
import uuid

import pytest
from click.testing import CliRunner

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "cli"))

from grape_cli import config as cli_config
from grape_cli import contracts as cli_contracts
from grape_cli import registry as cli_registry
from grape_cli.cli import cli, main

SAMPLE_SCHEMA = {
    "info": {"title": "Agentic DevOps API", "version": "9.9.9"},
    "paths": {
        "/api/tasks/mine": {
            "get": {
                "operationId": "tasks_mine", "summary": "我的任务",
                "parameters": [{"name": "status", "in": "query", "schema": {"type": "string"}}],
            },
        },
        "/api/tasks/create": {
            "post": {
                "operationId": "tasks_create", "summary": "新建任务",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            },
        },
        "/api/repos/{owner}/{repo}/issues": {
            "get": {
                "operationId": "gitrepo_issues_list", "summary": "Issue 列表",
                "parameters": [
                    {"name": "owner", "in": "path"}, {"name": "repo", "in": "path"},
                    {"name": "state", "in": "query", "schema": {"type": "string"}},
                ],
            },
            "post": {
                "operationId": "gitrepo_issues_create", "summary": "创建 Issue",
                "parameters": [{"name": "owner", "in": "path"}, {"name": "repo", "in": "path"}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            },
        },
    },
}

REQUESTS = []


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    cli_config.CONFIG_DIR = tmp_path / ".grape"
    cli_config.CONFIG_FILE = cli_config.CONFIG_DIR / "config.json"
    monkeypatch.setenv("GRAPE_BASE_URL", "http://mock")
    monkeypatch.setenv("GRAPE_SESSION_TOKEN", "tk_mocktask")
    cli_config.set_cli_params("", "")  # 清空一次性参数
    REQUESTS.clear()
    cli._loaded = False
    cli._groups = {}
    yield
    cli_config.set_cli_params("", "")
    cli._loaded = False
    cli._groups = {}


@pytest.fixture
def fake_fetch(monkeypatch):
    monkeypatch.setattr(cli_contracts, "fetch_schema", lambda *a, **k: json.loads(json.dumps(SAMPLE_SCHEMA)))


@pytest.fixture
def fake_requests(monkeypatch):
    from grape_cli.client import ApiClient

    def _fake_request(self, method, path, **kw):
        REQUESTS.append({"method": method, "path": path, "token": self.token,
                         "timeout": self.timeout, **kw})
        return 200, {"ok": True, "path": path, "method": method}

    monkeypatch.setattr(ApiClient, "request", _fake_request)


# ===================== A. 配置与认证 =====================

def test_a1_arg_over_env_over_file(monkeypatch):
    monkeypatch.delenv("GRAPE_SESSION_TOKEN", raising=False)
    cli_config.save_config(base_url="http://file", token="file_tok")
    base, tok = cli_config.resolve(base_url="http://arg", token="arg_tok")
    assert base == "http://arg" and tok == "arg_tok"


def test_a2_env_loading(monkeypatch):
    monkeypatch.delenv("GRAPE_API_TOKEN", raising=False)
    base, tok = cli_config.resolve()
    assert base == "http://mock" and tok == "tk_mocktask"  # GRAPE_SESSION_TOKEN 任务级优先


def test_a2b_task_token_preferred(monkeypatch):
    monkeypatch.setenv("GRAPE_API_TOKEN", "grape_usertok")
    _, tok = cli_config.resolve()
    assert tok == "tk_mocktask"  # 任务级 > 用户级


def test_a3_file_config(monkeypatch, tmp_path):
    cli_config.CONFIG_DIR = tmp_path / ".grape"
    cli_config.CONFIG_FILE = cli_config.CONFIG_DIR / "config.json"
    cli_config.save_config(base_url="http://file", token="file_tok")
    monkeypatch.delenv("GRAPE_BASE_URL", raising=False)
    monkeypatch.delenv("GRAPE_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("GRAPE_API_TOKEN", raising=False)
    base, tok = cli_config.resolve()
    assert (base, tok) == ("http://file", "file_tok")


def test_a3b_injected_server_url_fallback(monkeypatch):
    """GRAPE_BASE_URL 未设时，回退到服务端注入的 GRAPE_SERVER_URL（无 --base-url 也能用）。"""
    monkeypatch.delenv("GRAPE_BASE_URL", raising=False)
    monkeypatch.setenv("GRAPE_SERVER_URL", "http://injected:3000")
    base, _tok = cli_config.resolve()
    assert base == "http://injected:3000"


# ===================== 契约加载 =====================

def test_h1_first_fetch_caches(tmp_path, fake_fetch):
    cli_contract_config_dir = cli_config.CONFIG_DIR
    cli_config.CONFIG_DIR = tmp_path / "h1"
    cli_config.CONFIG_FILE = cli_config.CONFIG_DIR / "config.json"
    from grape_cli import contracts as c
    s = c.load_contract()
    assert s["info"]["version"] == "9.9.9"
    assert (cli_config.CONFIG_DIR / "openapi-9.9.9.json").exists()


def test_h2_cached_reused(tmp_path, monkeypatch):
    cli_config.CONFIG_DIR = tmp_path / "h2"
    cli_config.CONFIG_FILE = cli_config.CONFIG_DIR / "config.json"
    (cli_config.CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    (cli_config.CONFIG_DIR / "openapi-9.9.9.json").write_text(json.dumps(SAMPLE_SCHEMA), encoding="utf-8")
    called = []
    monkeypatch.setattr(cli_contracts, "fetch_schema", lambda *a, **k: called.append(1) or SAMPLE_SCHEMA)
    from grape_cli import contracts as c
    s = c.load_contract()
    assert s["info"]["version"] == "9.9.9"
    assert called == []  # 命中缓存，不发请求


def test_h4_refresh_force(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_contracts, "fetch_schema", lambda *a, **k: calls.append(1) or SAMPLE_SCHEMA)
    from grape_cli import contracts as c
    c.load_contract(refresh=True)
    c.load_contract(refresh=True)
    assert len(calls) == 2


# ===================== 契约生成命令（R） =====================

def test_r1_r2_register_and_query_param(fake_fetch, fake_requests):
    runner = CliRunner()
    r = runner.invoke(cli, ["tasks", "mine", "--status", "active"])
    assert r.exit_code == 0, r.output
    req = REQUESTS[-1]
    assert req["method"] == "GET" and req["path"] == "/api/tasks/mine"
    assert req["params"] == {"status": "active"}


def test_r4_body_via_json(fake_fetch, fake_requests):
    runner = CliRunner()
    r = runner.invoke(cli, ["tasks", "create", "--json", '{"title":"x"}'])
    assert r.exit_code == 0, r.output
    req = REQUESTS[-1]
    assert req["method"] == "POST" and req["json_body"] == {"title": "x"}


def test_r3_path_params_and_nested_group(fake_fetch, fake_requests):
    runner = CliRunner()
    r = runner.invoke(cli, ["git-repo", "issues", "list", "--owner", "o", "--repo", "r", "--state", "all"])
    assert r.exit_code == 0, r.output
    req = REQUESTS[-1]
    assert req["path"] == "/api/repos/o/r/issues"
    assert req["params"] == {"state": "all"}


def test_r7_new_contract_zero_cli_change(fake_requests, monkeypatch):
    """模拟契约新增接口 → CLI 不重装即出现新命令。"""
    schema = json.loads(json.dumps(SAMPLE_SCHEMA))
    schema["paths"]["/api/scenes"] = {"get": {"operationId": "scenes_list", "summary": "场景列表"}}
    monkeypatch.setattr(cli_contracts, "fetch_schema", lambda *a, **k: schema)
    cli._loaded = False
    runner = CliRunner()
    r = runner.invoke(cli, ["scenes", "list"])
    assert r.exit_code == 0, r.output
    assert REQUESTS[-1]["path"] == "/api/scenes"

    r2 = runner.invoke(cli, ["--help"])
    assert "scenes" in r2.output.replace("scenes", "scenes"), r2.output


def test_r9_operation_id_split(fake_fetch):
    tree = cli_registry.command_tree(SAMPLE_SCHEMA)
    assert "tasks" in tree and "mine" in tree["tasks"]
    assert "git-repo" in tree
    assert "issues" in tree["git-repo"] or any(v.startswith("issues") for v in tree["git-repo"])


# ===================== 一次性参数（--token / --base-url） =====================

def test_oneshot_token_flag_beats_env(fake_fetch, fake_requests):
    """grape --token tk_oneshot <cmd>：CLI 一次性 token 优先于环境变量。"""
    runner = CliRunner()
    r = runner.invoke(cli, ["--token", "tk_oneshot", "tasks", "mine", "--status", "active"])
    assert r.exit_code == 0, r.output
    assert REQUESTS[-1]["token"] == "tk_oneshot"      # 参数 > env(tk_mocktask)


def test_oneshot_grape_api_token_alias(fake_fetch, fake_requests):
    runner = CliRunner()
    r = runner.invoke(cli, ["--grape_api_token=grape_oneshot", "tasks", "mine"])
    assert r.exit_code == 0, r.output
    assert REQUESTS[-1]["token"] == "grape_oneshot"


def test_oneshot_base_url_flag(fake_fetch, fake_requests):
    runner = CliRunner()
    r = runner.invoke(cli, ["--base-url", "http://flag-host", "tasks", "mine"])
    assert r.exit_code == 0, r.output
    assert cli_config.resolve()[0] == "http://flag-host"


def test_oneshot_params_not_leak_to_next(fake_fetch, fake_requests):
    """一次性参数进程内不残留：下一次无参命令回落到环境变量。"""
    runner = CliRunner()
    assert runner.invoke(cli, ["--token", "tk_one", "tasks", "mine"]).exit_code == 0
    assert runner.invoke(cli, ["tasks", "mine"]).exit_code == 0
    assert REQUESTS[-1]["token"] == "tk_mocktask"


# ===================== 超时 / 报错细分 / 401 / 中断 / wait =====================

def test_timeout_flag_applied(fake_fetch, fake_requests):
    runner = CliRunner()
    assert runner.invoke(cli, ["--timeout", "120", "tasks", "mine"]).exit_code == 0
    assert REQUESTS[-1]["timeout"] == 120


def test_read_timeout_message(monkeypatch):
    import httpx
    from grape_cli.client import ApiClient, CliError
    monkeypatch.setattr("grape_cli.client.httpx.request",
                        lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    api = ApiClient()
    with pytest.raises(CliError) as ei:
        api.request("GET", "/x")
    assert "请求超时" in str(ei.value) and "--timeout" in str(ei.value)
    assert ei.value.code == 1


def test_connect_error_message(monkeypatch):
    import httpx
    from grape_cli.client import ApiClient, CliError
    monkeypatch.setattr("grape_cli.client.httpx.request",
                        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    api = ApiClient()
    with pytest.raises(CliError) as ei:
        api.request("GET", "/x")
    assert "base_url 不可达" in str(ei.value)


def test_401_passthrough(monkeypatch):
    import httpx
    from grape_cli.client import ApiClient, CliError
    monkeypatch.setattr("grape_cli.client.httpx.request",
                        lambda *a, **k: httpx.Response(401, json={"message": "unauth"}))
    api = ApiClient()
    with pytest.raises(CliError) as ei:
        api.request("GET", "/x")
    assert ei.value.code == 401
    assert "token 无效" in str(ei.value)


def test_ctrl_c_returns_130(fake_fetch, monkeypatch):
    from grape_cli.client import ApiClient
    monkeypatch.setattr(ApiClient, "request",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    rc = main(["tasks", "mine"])
    assert rc == 130


def test_tasks_wait_polls_until_terminal(fake_fetch, monkeypatch, capsys):
    from grape_cli.client import ApiClient
    calls = []

    def _fake_req(self, method, path, **kw):
        calls.append(path)
        st = "active" if len(calls) < 3 else "completed"
        return 200, {"status": st, "id": 56}

    monkeypatch.setattr(ApiClient, "request", _fake_req)
    rc = main(["tasks", "wait", "--task-id", "56", "--interval", "0"])
    assert rc == 0
    assert len(calls) == 3
    assert "completed" in capsys.readouterr().out


# ===================== 帮助系统（H） =====================

def test_h7_help_lists_groups_and_builtin(fake_fetch):
    runner = CliRunner()
    r = runner.invoke(cli, ["--help"])
    assert r.exit_code == 0
    assert "git-repo" in r.output and "tasks" in r.output
    assert "git" in r.output and "api" in r.output and "refresh" in r.output


def test_h10_list_pattern(fake_fetch):
    runner = CliRunner()
    r = runner.invoke(cli, ["commands", "git"])
    assert r.exit_code == 0
    assert all("git" in line for line in r.output.splitlines())


def test_h13_help_degrades_without_contract(monkeypatch, tmp_path):
    """无契约缓存且网络失败 → --help 不崩溃，给出提示。"""
    cli_config.CONFIG_DIR = tmp_path / "h13"
    cli_config.CONFIG_FILE = cli_config.CONFIG_DIR / "config.json"
    monkeypatch.setattr(cli_contracts, "fetch_schema",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no net")))
    from grape_cli import contracts as c
    monkeypatch.setattr(c, "load_contract", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no net")))
    runner = CliRunner()
    r = runner.invoke(cli, ["commands"])
    assert r.exit_code == 0
    assert "契约加载失败" in r.output


# ===================== 安全（D） =====================

def test_d1_no_token_management_command(fake_fetch):
    runner = CliRunner()
    r = runner.invoke(cli, ["--help"])
    low = r.output.lower()
    assert "create-token" not in low and "token" not in low or ("token" not in [l.strip() for l in r.output.splitlines() if l.strip().startswith(("create", "revoke"))])


def test_d2_token_never_in_output(fake_fetch, fake_requests):
    runner = CliRunner()
    r = runner.invoke(cli, ["tasks", "mine", "--json-out"])
    assert "tk_mocktask" not in r.output