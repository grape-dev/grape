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

from src.session_manager import db, db_lock
from src.runtime.engine_model import detect_engine_model

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
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
        import src.runtime.engine_model as em
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
        from src.api.routes.settings import _recent_models_by_runtime, _RECENT_MODELS_TOP_N
        import src.session_manager as sm
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
        from src.api.routes.settings import _recent_models_by_runtime
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
