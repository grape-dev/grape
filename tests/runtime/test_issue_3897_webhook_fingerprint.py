"""Issue #3897 C 组（B）：webhook 验签拒绝必须带可定位上下文（token 指纹，无明文）。

现场：`grape-event` 反复 `Webhook rejected: token mismatch for repo=grape-dev/grape`
（30 分钟抽样 30/200），nginx 侧 401/403 高企。原日志只有 repo，运维无法判断是
「平台端 Secret 与 Grape 端配置不同」还是「请求根本没带 token」——只能两边猜。

契约：
  1. 拒绝日志带 `expected=<指纹> actual=<指纹>`（sha256 前缀，不可逆）与来源 IP；
  2. 任何情况下不得输出明文 token；
  3. 校验语义不变（403 拒绝、不调度流水线）。
"""
import json

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.runtime.routes import webhook as W

_REPO = "grape-dev/grape"
_EXPECTED = "expected-secret-3897"
_ACTUAL = "actual-wrong-3897"


def test_token_fingerprint_is_irreversible_and_stable():
    fp = W._token_fingerprint(_EXPECTED)
    assert fp.startswith("sha256:")
    assert len(fp) == len("sha256:") + 12
    assert _EXPECTED not in fp
    assert fp == W._token_fingerprint(_EXPECTED)
    assert W._token_fingerprint("") == "absent"


def test_gitcode_token_mismatch_logs_fingerprints(monkeypatch, caplog):
    monkeypatch.setattr(W, "_expected_webhook_token", lambda repo, provider: _EXPECTED)
    monkeypatch.setattr(W, "run_pipeline", lambda *a, **k: pytest.fail("must not dispatch"))
    caplog.set_level("WARNING")

    body = json.dumps({"project": {"path_with_namespace": _REPO}}).encode()
    r = TestClient(app).post("/webhook/gitcode", content=body,
                             headers={"X-GitCode-Token": _ACTUAL})

    assert r.status_code == 403
    text = caplog.text
    assert "token mismatch" in text
    assert W._token_fingerprint(_EXPECTED) in text
    assert W._token_fingerprint(_ACTUAL) in text
    assert _EXPECTED not in text and _ACTUAL not in text


def test_gitcode_missing_token_logs_absent(monkeypatch, caplog):
    monkeypatch.setattr(W, "_expected_webhook_token", lambda repo, provider: _EXPECTED)
    monkeypatch.setattr(W, "run_pipeline", lambda *a, **k: pytest.fail("must not dispatch"))
    caplog.set_level("WARNING")

    body = json.dumps({"project": {"path_with_namespace": _REPO}}).encode()
    r = TestClient(app).post("/webhook/gitcode", content=body)

    assert r.status_code == 403
    assert "actual=absent" in caplog.text