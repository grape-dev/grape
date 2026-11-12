"""Issue #865：引擎模型展示（配置模型 + 最近实际使用模型）。

覆盖：
- detect_engine_model 三引擎本机配置解析（tmp HOME 隔离）+ 失败降级（返回空串）
- /api/runtime/register 带 model 上报：落 model_name/model_source；空 model 不覆盖既有值
- _recent_models_by_runtime：批量聚合无 N+1（execute 调用计数 = 1）、top3 排序、空 ids
- i18n zh/en key 一致（runtimes.label.model*）
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

import shutil as _shutil


def _zstd_cli_missing() -> bool:
    """N1（QA #1230）：zstd fixture 依赖系统 zstd CLI，仅装 zstandard 包的环境无它。"""
    return _shutil.which("zstd") is None


from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine.engine_model import detect_engine_model

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
ZH_PATH = os.path.join(ROOT, "src", "api", "static", "locales", "zh", "runtimes.json")
EN_PATH = os.path.join(ROOT, "src", "api", "static", "locales", "en", "runtimes.json")


def _make_token(name="test865-worker"):
    import hashlib
    import secrets
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), name, "global"))
        db.commit()
    return token


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%'")
        db.cursor().execute("DELETE FROM obs_llm_usage WHERE engine_id IN "
                            "(SELECT id FROM agent_runtimes WHERE name LIKE 'test_%')")
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'test865-%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM obs_llm_usage WHERE engine_id IN "
                            "(SELECT id FROM agent_runtimes WHERE name LIKE 'test_%')")
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%'")
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'test865-%'")
        db.commit()


@pytest.fixture
def client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


class TestDetectEngineModel:
    def _stub_expanduser(self, monkeypatch, home):
        """把三引擎配置路径指到 tmp home（隔离真实本机配置）。"""
        import src.domains.runtime.engine.engine_model as em
        monkeypatch.setattr(em.os.path, "expanduser", lambda p: {
            "~/.hermes/config.yaml": str(home / ".hermes/config.yaml"),
            "~/.config/opencode/opencode.json": str(home / ".config/opencode/opencode.json"),
            "~/.codeartsdoer/codearts_cli.json": str(home / ".codeartsdoer/codearts_cli.json"),
        }.get(p, p))

    def test_hermes_yaml(self, tmp_path, monkeypatch):
        home = tmp_path / "h"
        home.mkdir()
        her = home / ".hermes"
        her.mkdir()
        (her / "config.yaml").write_text(
            "model:\n  default: glm-5.3\n  provider: custom\n  base_url: https://x/v1\n  api_key: ${K}\n",
            encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(her))
        monkeypatch.delenv("CODEARTS_MODEL", raising=False)
        self._stub_expanduser(monkeypatch, home)
        assert detect_engine_model("hermes") == "glm-5.3"

    def test_opencode_json(self, tmp_path, monkeypatch):
        home = tmp_path / "h"
        (home / ".config/opencode").mkdir(parents=True)
        (home / ".config/opencode/opencode.json").write_text(
            json.dumps({"provider": {"deepseek": {"options": {"apiKey": "sk-secret"}}},
                        "model": "deepseek/glm-5.3"}), encoding="utf-8")
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("CODEARTS_MODEL", raising=False)
        self._stub_expanduser(monkeypatch, home)
        assert detect_engine_model("opencode") == "deepseek/glm-5.3"
        # 解析结果不含任何敏感值
        assert "sk-" not in detect_engine_model("opencode")

    def test_codearts_env_priority_and_json(self, tmp_path, monkeypatch):
        home = tmp_path / "h"
        (home / ".codeartsdoer").mkdir(parents=True)
        (home / ".codeartsdoer/codearts_cli.json").write_text(
            json.dumps({"model": "opengw/glm-5.2"}), encoding="utf-8")
        self._stub_expanduser(monkeypatch, home)
        monkeypatch.setenv("CODEARTS_MODEL", "modelarts/env-model")
        assert detect_engine_model("codearts") == "modelarts/env-model"
        monkeypatch.delenv("CODEARTS_MODEL")
        assert detect_engine_model("codearts") == "opengw/glm-5.2"

    def test_missing_files_and_unknown_degrade_to_empty(self, tmp_path, monkeypatch):
        home = tmp_path / "empty"
        home.mkdir()
        self._stub_expanduser(monkeypatch, home)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("CODEARTS_MODEL", raising=False)
        for name in ("hermes", "opencode", "codearts"):
            assert detect_engine_model(name) == "", f"{name} 缺文件应降级为空串"
        assert detect_engine_model("codex") == ""
        assert detect_engine_model("") == ""

    def test_hermes_no_yaml_dependency(self, tmp_path, monkeypatch):
        """无 pyyaml 时正则兜底仍可解析 default 模型。"""
        home = tmp_path / "h"
        her = home / ".hermes"
        her.mkdir(parents=True)
        (her / "config.yaml").write_text(
            "database:\n  journal_mode: wal\nmodel:\n  default: deepseek-v4-flash\n  provider: custom\n",
            encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(her))
        import builtins
        real_import = builtins.__import__

        def _no_yaml(name, *a, **kw):
            if name == "yaml":
                raise ImportError("no yaml in worker env")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", _no_yaml)
        try:
            assert detect_engine_model("hermes") == "deepseek-v4-flash"
        finally:
            monkeypatch.undo()
            monkeypatch.setenv("HERMES_HOME", str(her))


class TestAishellDshModel:
    """aishell-dsh 的 acp profile（patch 列表）解析（Issue #865 覆盖缺口）。"""

    _PROFILE = (
        "- id: unrelated-patch\n"
        "  config:\n"
        "    model: should-not-be-used\n"
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: internal-gw\n"
        "    model: glm-5.3\n"
        "    apiKeyEnv: INTERNAL_KEY_ENV\n"
        "    baseUrl: https://internal.example/v1\n"
    )

    def _write_profile(self, tmp_path, data):
        home = tmp_path / "aishell"
        acp = home / "profiles" / "acp"
        acp.mkdir(parents=True)
        path = acp / "default.patch.yml"
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return home

    def _block_yaml(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _no_yaml(name, *a, **kw):
            if name == "yaml":
                raise ImportError("no yaml in worker env")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_yaml)

    def test_block_yaml_parses_agent_default_model(self, tmp_path, monkeypatch):
        from src.domains.runtime.engine.engine_model import _aishell_dsh_model

        home = self._write_profile(tmp_path, self._PROFILE)
        monkeypatch.setenv("AISHELL_HOME", str(home))
        got = _aishell_dsh_model()
        assert got == "glm-5.3"
        # 安全约束：apiKeyEnv / baseUrl 绝不进入返回值
        assert "INTERNAL_KEY" not in got and "internal.example" not in got

    def test_no_pyyaml_regex_fallback(self, tmp_path, monkeypatch):
        from src.domains.runtime.engine.engine_model import _aishell_dsh_model

        home = self._write_profile(tmp_path, self._PROFILE)
        monkeypatch.setenv("AISHELL_HOME", str(home))
        self._block_yaml(monkeypatch)
        assert _aishell_dsh_model() == "glm-5.3"

    def test_gbk_encoded_profile(self, tmp_path, monkeypatch):
        from src.domains.runtime.engine.engine_model import _aishell_dsh_model

        text = "# 中文注释：默认模型配置\n" + self._PROFILE
        home = self._write_profile(tmp_path, text.encode("gbk"))
        monkeypatch.setenv("AISHELL_HOME", str(home))
        assert _aishell_dsh_model() == "glm-5.3"

    def test_crlf_profile_regex_fallback(self, tmp_path, monkeypatch):
        from src.domains.runtime.engine.engine_model import _aishell_dsh_model

        home = self._write_profile(tmp_path, self._PROFILE.replace("\n", "\r\n"))
        monkeypatch.setenv("AISHELL_HOME", str(home))
        self._block_yaml(monkeypatch)
        assert _aishell_dsh_model() == "glm-5.3"

    def test_missing_profile_degrades_to_empty(self, tmp_path, monkeypatch):
        from src.domains.runtime.engine.engine_model import _aishell_dsh_model

        home = tmp_path / "empty"
        home.mkdir()
        monkeypatch.setenv("AISHELL_HOME", str(home))
        assert _aishell_dsh_model() == ""


class TestRegisterWithModel:
    def test_register_persists_model(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        body = {"device_name": "ci-865", "runtimes": [
            {"name": "test_opencode", "version": "v1", "healthy": True, "model": "deepseek/glm-5.3"}]}
        r = client.post("/api/runtime/register", json=body, headers=headers)
        assert r.status_code == 200
        rid = r.json()["runtimes"][0]["id"]
        with db_lock:
            row = db.cursor().execute(
                "SELECT model_name, model_source, model_reported_at FROM agent_runtimes WHERE id=?",
                (rid,)).fetchone()
        assert row[0] == "deepseek/glm-5.3"
        assert row[1] == "worker_report"
        assert row[2]

    def test_empty_model_does_not_overwrite(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        body = {"device_name": "ci-865b", "runtimes": [
            {"name": "test_hermes", "healthy": True, "model": "glm-5.3"}]}
        r = client.post("/api/runtime/register", json=body, headers=headers)
        rid = r.json()["runtimes"][0]["id"]
        # 未升级 worker：注册不带 model → 不覆盖既有值
        body2 = {"device_name": "ci-865b", "runtimes": [{"name": "test_hermes", "healthy": True}]}
        r2 = client.post("/api/runtime/register", json=body2, headers=headers)
        assert r2.status_code == 200
        with db_lock:
            row = db.cursor().execute(
                "SELECT model_name FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        assert row[0] == "glm-5.3"

    def test_update_combines_role_tags_and_model(self, client):
        """role_tags 与 model 同时上报（UPDATE 分支）时参数不得错位（#865 基线缺陷）。"""
        token = _make_token("test865-combo")
        headers = {"Authorization": f"Bearer {token}"}
        first = {"device_name": "ci-865-combo", "cli_version": "worker-1.0",
                 "runtimes": [{"name": "test_aishell_dsh", "version": "v1", "healthy": True}]}
        r1 = client.post("/api/runtime/register", json=first, headers=headers)
        assert r1.status_code == 200, r1.text
        rid = r1.json()["runtimes"][0]["id"]
        second = {"device_name": "ci-865-combo", "cli_version": "worker-2.0",
                  "runtimes": [{"name": "test_aishell_dsh", "version": "v2", "healthy": True,
                                "role_tags": ["CODING", " Vod ", ""], "model": "glm-5.3"}]}
        r2 = client.post("/api/runtime/register", json=second, headers=headers)
        assert r2.status_code == 200, r2.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT role_tags, model_name, model_source, worker_version, version "
                "FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        assert json.loads(row[0]) == ["coding", "vod"]
        assert row[1] == "glm-5.3"
        assert row[2] == "worker_report"
        assert row[3] == "worker-2.0"
        assert row[4] == "v2"


class TestAishellDshCapabilityGuard:
    """CLI 同名防误识别（2026-11-10）：非 dsh 版 aishell 不得注册为 aishell-dsh。"""

    def test_capable_empty_path(self):
        from src.domains.runtime.agents import aishell_acp
        assert aishell_acp.aishell_dsh_capable("") is False

    def test_capable_true_on_acp_help(self, monkeypatch):
        import subprocess as sp
        from src.domains.runtime.agents import aishell_acp
        aishell_acp.aishell_dsh_capable._cache.clear()
        class _R:
            returncode = 0
            stdout = "Serve automation clients over Agent Client Protocol stdio."
            stderr = ""
        monkeypatch.setattr(sp, "run", lambda *a, **k: _R())
        assert aishell_acp.aishell_dsh_capable("/fake/aishell-true") is True

    def test_capable_false_on_missing_acp(self, monkeypatch):
        import subprocess as sp
        from src.domains.runtime.agents import aishell_acp
        aishell_acp.aishell_dsh_capable._cache.clear()
        class _R:
            returncode = 1
            stdout = "unknown command"
            stderr = ""
        monkeypatch.setattr(sp, "run", lambda *a, **k: _R())
        assert aishell_acp.aishell_dsh_capable("/fake/aishell-neg") is False

    def test_capable_false_on_timeout_or_error(self, monkeypatch):
        import subprocess as sp
        from src.domains.runtime.agents import aishell_acp
        aishell_acp.aishell_dsh_capable._cache.clear()
        def _boom(*a, **k):
            raise sp.TimeoutExpired(cmd="aishell", timeout=20)
        monkeypatch.setattr(sp, "run", _boom)
        assert aishell_acp.aishell_dsh_capable("/fake/aishell-tmo") is False

    def test_discover_drops_fake_cli(self, monkeypatch):
        """同名假 CLI：find_cli 命中但无 ACP 能力 → installed=False 且不携带 cli_path。"""
        import src.domains.runtime.engine.os_adapter as osa
        import src.domains.runtime.agents.executor as ex
        monkeypatch.setattr(osa, "find_cli", lambda cli: "/fake/aishell" if cli == "aishell" else "")
        from src.domains.runtime.agents import aishell_acp
        monkeypatch.setattr(aishell_acp, "aishell_dsh_capable", lambda p: False)
        found = {e["name"]: e for e in ex.discover_backends()}
        assert found["aishell-dsh"]["installed"] is False
        assert found["aishell-dsh"]["cli_path"] == ""

    def test_discover_keeps_real_cli(self, monkeypatch):
        import src.domains.runtime.engine.os_adapter as osa
        import src.domains.runtime.agents.executor as ex
        monkeypatch.setattr(osa, "find_cli", lambda cli: "/real/aishell" if cli == "aishell" else "")
        from src.domains.runtime.agents import aishell_acp
        monkeypatch.setattr(aishell_acp, "aishell_dsh_capable", lambda p: True)
        found = {e["name"]: e for e in ex.discover_backends()}
        assert found["aishell-dsh"]["installed"] is True
        assert found["aishell-dsh"]["cli_path"] == "/real/aishell"

    def test_register_skips_not_installed(self, client):
        """服务端守卫：installed=False 的引擎上报不建行（假行点亮路径掐死）。"""
        token = _make_token("test865-capguard")
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"device_name": "ci-865-capguard", "cli_version": "worker-1.0",
                   "runtimes": [{"name": "test_fake_aishell", "version": "v1",
                                 "healthy": True, "installed": False},
                                {"name": "test_real_engine_cap", "version": "v1",
                                 "healthy": True, "installed": True}]}
        r = client.post("/api/runtime/register", json=payload, headers=headers)
        assert r.status_code == 200, r.text
        names = {rt["name"]: rt["id"] for rt in r.json()["runtimes"]}
        assert "test_real_engine_cap" in names
        assert "test_fake_aishell" not in names
        with db_lock:
            rows = db.cursor().execute(
                "SELECT name FROM agent_runtimes WHERE credential_id="
                "(SELECT id FROM runtime_credentials WHERE device_name='ci-865-capguard')"
            ).fetchall()
        assert [r[0] for r in rows] == ["test_real_engine_cap"]


class TestRecentModelsAggregation:
    def _seed(self):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_runtimes (credential_id, name, scope, installed, status) "
                      "VALUES (0, 'test_agg_engine', 'global', 1, 'online')")
            rid = c.lastrowid
            rows = []
            for i, (mkey, mraw, cnt) in enumerate([
                    ("glm-5.3", "glm-5.3", 5),
                    ("deepseek-v4-flash", "deepseek-v4-flash", 9),
                    ("glm-5.2", "glm-5.2", 3),
                    ("old-model", "old-model", 1)]):
                for j in range(cnt):
                    rows.append((rid, mkey, mraw, f"2026-11-0{i + 1}T10:00:{j:02d}+00:00"))
            c.executemany(
                "INSERT INTO obs_llm_usage (dedup_key, task_id, engine_id, model_key, model_raw, created_at) "
                "VALUES (?,?,?,?,?,?)",
                [(f"test865:{rid}:{i}", f"task865-{rid}-{i}", eng, mk, mr, ts)
                 for i, (eng, mk, mr, ts) in enumerate(rows)])
            db.commit()
        return rid

    def test_batch_group_by_no_n_plus_1(self):
        from src.domains.mgmt.routes.settings import _recent_models_by_runtime, _RECENT_MODELS_TOP_N
        import src.kernel.session_manager as sm
        rid = self._seed()
        calls = {"n": 0}
        real_db = sm.db

        class _CountingCursor:
            def __init__(self, cur):
                self._cur = cur

            def execute(self, *a, **kw):
                calls["n"] += 1
                return self._cur.execute(*a, **kw)

            def __getattr__(self, name):
                return getattr(self._cur, name)

        class _CountingDB:
            def cursor(self):
                return _CountingCursor(real_db.cursor())

            def __getattr__(self, name):
                return getattr(real_db, name)

        monkey_db = _CountingDB()
        sm.db = monkey_db
        try:
            out = _recent_models_by_runtime([rid])
        finally:
            sm.db = real_db
        assert calls["n"] == 1, f"聚合应单条 SQL（实际 {calls['n']} 条）= 无 N+1"
        top = out[rid]
        assert len(top) == _RECENT_MODELS_TOP_N
        assert top[0]["model"] == "deepseek-v4-flash" and top[0]["count"] == 9
        assert top[1]["model"] == "glm-5.3"
        assert all("api_key" not in json.dumps(t) for t in top)

    def test_empty_ids_returns_empty_without_query(self):
        from src.domains.mgmt.routes.settings import _recent_models_by_runtime
        assert _recent_models_by_runtime([]) == {}
        assert _recent_models_by_runtime(None) == {}


class TestI18nKeys:
    def test_zh_en_have_model_keys(self):
        zh = json.load(open(ZH_PATH, encoding="utf-8"))["label"]
        en = json.load(open(EN_PATH, encoding="utf-8"))["label"]
        for k in ("model", "model_configured", "model_last_used", "recent_models",
                  "recent_models_empty", "model_used_count"):
            assert k in zh and k in en, f"i18n key 缺失: {k}"
            assert zh[k] and en[k]
        assert "{{count}}" in zh["model_used_count"] and "{{count}}" in en["model_used_count"]


class TestAishellDshUsageUpdate:
    """二期 #5：usage_update（{used,size} 上下文占用）消费为 usage 事件。"""

    def test_usage_update_emits_usage_message(self):
        from src.domains.runtime.agents.aishell_acp import _AishellACPClient
        got = []
        cli = _AishellACPClient.__new__(_AishellACPClient)
        cli.on_message = lambda t, c: got.append((t, c))
        cli._handle_notification({"method": "session/update",
                                  "params": {"update": {"sessionUpdate": "usage_update",
                                                        "used": 12000, "size": 100000}}})
        assert got and got[0][0] == "usage"
        assert "12000" in got[0][1].replace(",", "")
        assert "100000" in got[0][1].replace(",", "")
        assert "12%" in got[0][1]

    def test_usage_update_malformed_silent(self):
        from src.domains.runtime.agents.aishell_acp import _AishellACPClient
        got = []
        cli = _AishellACPClient.__new__(_AishellACPClient)
        cli.on_message = lambda t, c: got.append((t, c))
        cli._handle_notification({"method": "session/update",
                                  "params": {"update": {"sessionUpdate": "usage_update"}}})
        assert got == []  # 缺字段静默（fail-open，不炸不误报）


@pytest.mark.skipif(_zstd_cli_missing(), reason="zstd CLI not installed (zstandard-pkg-only env)")
class TestAishellSessionUsageParse:
    """二期 #1：session.v4.jsonl.zstd token 解析轨（zstd CLI 级，fail-open）。"""

    def test_parse_real_shaped_fixture(self, tmp_path):
        import json as _json
        import subprocess as _sp
        from src.domains.runtime.engine.usage_collect import parse_aishell_session_usage
        home = tmp_path / "sessions"
        d = home / "--tmp-fake-task--" / "sid-1"
        d.mkdir(parents=True)
        lines = [
            {"type": "system/message", "data": {"x": 1}},
            {"type": "assistant/message", "data": {"message": {"source": {"replayState": {
                "response": {"usage": {"inputTokens": 100, "outputTokens": 20,
                                       "totalTokens": 120, "cacheReadTokens": 30}}}}}}},
            {"type": "assistant/message", "data": {"message": {"source": {"replayState": {
                "response": {"usage": {"inputTokens": 50, "outputTokens": 10,
                                       "totalTokens": 60, "cacheReadTokens": 0}}}}}}},
            "{'type': 'broken-line",  # 非 JSON 漂移行跳过
        ]
        raw = "\n".join(_json.dumps(l, ensure_ascii=False) if isinstance(l, dict) else l for l in lines)
        f = d / "session.v4.jsonl.zstd"
        p = _sp.run(["zstd", "-o", str(f)], input=raw.encode(), capture_output=True)
        assert p.returncode == 0
        recs = parse_aishell_session_usage("/tmp/fake/task", "sid-1", sessions_root=str(home))
        assert [(r["input"], r["output"], r["total"], r["cache_read"]) for r in recs] == \
               [(100, 20, 120, 30), (50, 10, 60, 0)]

    def test_missing_session_file_empty(self):
        from src.domains.runtime.engine.usage_collect import parse_aishell_session_usage
        assert parse_aishell_session_usage("/nonexistent/cwd", "no-such-sid") == []

    def test_collector_aishell_dsh_returns_records(self, monkeypatch, tmp_path):
        """新会话：start 时无既有记录（水位 0）→ finish 全量。"""
        from src.domains.runtime.engine import usage_collect as uc
        calls = []
        def _fake(c, s):
            calls.append(1)
            # start 探水位时（第 1 次）会话为空；finish 解析时（第 2 次）有记录
            return [{"input": 1, "output": 2}] if len(calls) > 1 else []
        monkeypatch.setattr(uc, "parse_aishell_session_usage", _fake)
        col = uc.UsageCollector("aishell-dsh", cwd="/tmp/x", session_id="s1")
        col.start()
        assert col.finish() == [{"input": 1, "output": 2}]

    def test_collector_resume_watermark_incremental(self, monkeypatch):
        """M1（QA #1230）：resume 复用 session_id——start 水位截断历史轮次，只计新增。"""
        from src.domains.runtime.engine import usage_collect as uc
        monkeypatch.setattr(uc, "parse_aishell_session_usage",
                            lambda c, s: [{"input": 1, "output": 2}, {"input": 3, "output": 4}])
        col = uc.UsageCollector("aishell-dsh", cwd="/tmp/x", session_id="s1")
        col.start()  # 水位 = 2（既有历史）
        assert col.finish() == []  # 无新增轮次 → 不重复计账
        # resume 后新增一轮的形态：start 已有 2 条，finish 时文件新增 1 条
        def _grown(c, s):
            return [{"input": 1, "output": 2}, {"input": 3, "output": 4}, {"input": 5, "output": 6}]
        monkeypatch.setattr(uc, "parse_aishell_session_usage", _grown)
        assert col.finish() == [{"input": 5, "output": 6}]


class TestSessionCloseOrderingGuard:
    """QA #1230 I-3 / I-A：session/close 必须先于 proc.stdin 关闭发出（C2 回归防线）。

    close 块后置曾是双层死代码（请求写不进已关 stdin + request 失败以返回值
    报告，try/except 永不触发）。静态结构断言：close 调用行号 < 收尾 stdin.close
    行号，防未来重构再次后置。
    """

    def test_close_issued_before_stdin_close(self):
        import re as _re
        src_path = os.path.join(os.path.dirname(__file__), "..", "..",
                                "src", "domains", "runtime", "agents", "aishell_acp.py")
        with open(src_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        def _is_code(l):
            t = l.strip()
            return bool(t) and not t.startswith("#")
        close_lines = [i for i, l in enumerate(lines) if 'client.request("session/close"' in l]
        stdin_close_lines = [i for i, l in enumerate(lines) if "proc.stdin.close()" in l and _is_code(l)]
        assert close_lines, "session/close request not found in aishell_acp.py"
        assert stdin_close_lines, "proc.stdin.close() not found in aishell_acp.py"
        first_close = min(close_lines)
        first_stdin_close = min(stdin_close_lines)
        assert first_close < first_stdin_close, (
            "session/close (line %d) must be issued BEFORE proc.stdin.close() (line %d) "
            "- regression of QA #1230 Critical-2 dead-code ordering"
            % (first_close + 1, first_stdin_close + 1))


class TestAishellDirKeyMapping:
    """QA #1230 R1：cwd → sessions 目录键映射（纯字符串，无需 Windows 主机）。"""

    def test_posix_and_windows_dir_key(self):
        from src.domains.runtime.engine.usage_collect import _aishell_sessions_dir_key
        assert _aishell_sessions_dir_key("/tmp") == "--tmp--"
        assert _aishell_sessions_dir_key("/home/dev/project") == "--home-dev-project--"
        # Windows 形态：':\\' 折叠为单个 '-'（QA 实测引擎目录形态）
        assert _aishell_sessions_dir_key("C:\\Users\\dev\\AppData\\Local\\Temp\\task-1") == \
            "--C-Users-dev-AppData-Local-Temp-task-1--"
        # 混合分隔符/首尾分隔符
        assert _aishell_sessions_dir_key("/tmp/") == "--tmp--"

    def test_dir_key_no_colon_residue(self):
        from src.domains.runtime.engine.usage_collect import _aishell_sessions_dir_key
        k = _aishell_sessions_dir_key("C:/mix/sep")
        assert ":" not in k and "\\" not in k and "/" not in k


class TestEngineAwareSessionGateInstanceName:
    """QA #1230 M3：实例名 backend（aishell-dsh-<device>-<cid>）也要走 resume 门。"""

    def test_instance_name_keeps_uuid_session(self):
        from src.domains.runtime.routes.chat import _engine_aware_session_gate
        # 全局引擎名
        sid, ok = _engine_aware_session_gate({"backend": "aishell-dsh"}, "abc-uuid-123")
        assert sid == "abc-uuid-123" and ok is True
        # 个人/远端实例名（前端把 instance_name 回填为 backend）
        sid, ok = _engine_aware_session_gate({"backend": "aishell-dsh-worker-laptop-0003"}, "abc-uuid-123")
        assert sid == "abc-uuid-123" and ok is True
        # 其他引擎实例名不受影响（ses_ 门）
        sid, ok = _engine_aware_session_gate({"backend": "opencode-xyz"}, "ses_abc")
        assert sid == "ses_abc" and ok is True
        sid, ok = _engine_aware_session_gate({"backend": "opencode-xyz"}, "abc-uuid-123")
        assert sid == "" and ok is False
