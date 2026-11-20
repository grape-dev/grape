"""Issue #3580（S7）：实例接入自检脚本 `scripts/probe_gitlab_instance.py`。

锁三件事：
1. `--self-test`（内置假上游）六项全 PASS、退出码 0 —— 证明探针本身逻辑与 URL 形态正确；
2. 缺 token → token 相关项 SKIP（不是 FAIL），退出码仍 0（不能把「没给凭据」判成实例坏）；
3. 上游 401 → FAIL 且退出码 1（探针要能真正判死，不能恒绿）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe_gitlab_instance.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("probe_gitlab_instance", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_self_test_all_checks_pass(probe, capsys):
    rc = probe.main(["gitlab", "--self-test", "--json", "--repo", "grp/sub/proj"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    checks = {c["check"]: c["status"] for c in out["checks"]}
    assert checks["token"] == probe.PASS
    assert checks["repos"] == probe.PASS
    assert checks["tree"] == probe.PASS
    assert checks["file"] == probe.PASS
    assert checks["hook"] == probe.PASS
    assert checks["ca"] == probe.PASS


def test_missing_token_skips_instead_of_failing(probe):
    card = probe._card("gitlab")
    results = {c["check"]: c["status"] for c in
               probe.probe(card, "", "grp", "sub/proj", "README.md", probe.stub_fetch)}
    for name in ("token", "repos", "tree", "file", "hook"):
        assert results[name] == probe.SKIP
    assert results["ca"] == probe.PASS


def test_upstream_401_fails(probe):
    card = probe._card("gitlab")

    def unauthorized(card, path, token, params=None):
        return 401, "unauthorized"

    results = {c["check"]: c["status"] for c in
               probe.probe(card, "bad-token", "grp", "proj", "README.md", unauthorized)}
    assert results["token"] == probe.FAIL
    assert results["repos"] == probe.FAIL


def test_real_fetch_reports_connect_error_as_status_zero(probe, monkeypatch):
    """连接/TLS 失败必须变成 (0, detail) 结论，不能抛栈让脚本崩。"""
    import urllib.error

    import src.kernel.upstream_http as uh

    card = probe._card("gitlab")

    def boom(req, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(uh, "open_with_policy", boom)
    status, detail = probe.real_fetch(card, "/user", "tok")
    assert status == 0
    assert "URLError" in str(detail)


def test_probe_reports_subgroup_owner_in_url(probe):
    """子组仓库：探针打的 URL 必须整路径编码（grp%2Fsub%2Fproj）。"""
    card = probe._card("gitlab")
    seen = []

    def capture(card, path, token, params=None):
        seen.append(path)
        return probe.stub_fetch(card, path, token, params)

    probe.probe(card, "tok", "grp/sub", "proj", "README.md", capture)
    assert any("projects/grp%2Fsub%2Fproj/repository/tree" in p for p in seen), seen