"""2026-11-13 P1-full 实弹回归：issues/{number}/artifacts/clean 接受字符串 issue ident。

背景：gitee 的 issue ident 是 IK* 字符串（如 IKUZJW）——路径参数 int 直接 422
（int_parsing），该端点对 gitee 完全不可用。issues 族其余端点（get/update/close/
comments）均为 int | str，本端点对齐口径。路由层测试（TestClient）确保 FastAPI
路径解析真实接受字符串，非仅函数签名。
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

gra = importlib.import_module("src.api.git_repo_api")


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.deps import get_current_user_or_api_key
    from src.api.main import app

    def _fake_req(provider, method, url, token, body=None):
        # dry_run 探针路径：目录列表全部为空 → paths=[]，零删除
        if method == "GET":
            return []
        return {}

    monkeypatch.setattr(gra, "_vcs_req", _fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "u", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "repo_key": "gitee:o/livefire"}
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_artifacts_clean_accepts_gitee_string_ident(monkeypatch):
    """gitee IK* 字符串 ident 不再 422（原先 int_parsing 路由拒绝）。"""
    for client in _client(monkeypatch):
        r = client.post("/api/git-repo/o/livefire/issues/IKUZJW/artifacts/clean")
        assert r.status_code == 200, r.text
        assert r.json()["dry_run"] is True


def test_artifacts_clean_accepts_numeric_ident(monkeypatch):
    """数字 ident（gitcode/github）行为不变。"""
    for client in _client(monkeypatch):
        r = client.post("/api/git-repo/o/hello/issues/39/artifacts/clean")
        assert r.status_code == 200, r.text
        assert r.json()["dry_run"] is True
