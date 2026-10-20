"""Security hardening tests: V1 (JWT default-secret forgery) + V2 (webhook unsigned bypass).

TDD（先红后绿）：这些用例先于修复代码编写，修复前 V1 伪造 token 可越过校验、
V2 无凭据 webhook 被放行——修复后全部收紧为 fail-closed。
"""
import os
import json
import time
import hmac
import hashlib

import pytest

from fastapi.testclient import TestClient

from src.session_manager import db, db_lock
from src.api import jwt_utils
from src.api.main import app

# 用于 V2 的托管 GitHub 仓库（孤立测试数据，不与真实仓库冲突）
_REPO = "secharden/repo"
_REPO_KEY = f"github:{_REPO}"
_WEBHOOK_SECRET = "secharden-webhook-token-0000001"


def _install_managed_gh_repo():
    with db_lock:
        db.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, github_webhook_token) "
            "VALUES (?,?,?,?,?)",
            (_REPO_KEY, "secharden", "repo", _REPO, _WEBHOOK_SECRET))
        db.commit()


def _gh_push_body() -> bytes:
    return json.dumps({"repository": {"full_name": _REPO}}).encode("utf-8")


def _gh_delete_body() -> bytes:
    return json.dumps({"action": "deleted", "repository": {"full_name": _REPO}}).encode("utf-8")


def _hmac_signature(body: bytes) -> str:
    sig = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# ============================================================================
# V1：JWT 默认密钥
# ============================================================================

class TestJwtSecretHardening:

    def test_no_known_default_fallback(self):
        """修复后不允许存在可预测的默认密钥常量。"""
        assert jwt_utils._DEFAULT_SECRET == ""

    def test_signing_key_unpredictable_when_unset(self):
        """未配置 JWT_SECRET 时，签发密钥应是非空、>=32 字节的进程内随机值。"""
        secret, kid = jwt_utils._get_signing_secret()
        assert secret
        assert len(secret) >= 32
        assert kid in ("", None)
        assert secret != "<known_default_secret>"

    def test_known_default_in_blacklist(self):
        """历史默认字符串即使被显式配置也必须拒绝（fail-closed）。"""
        assert jwt_utils.validate_jwt_secret() is True  # 测试环境（dev）应放行
        assert len(jwt_utils._INSECURE_SECRETS) > 0

    def test_weak_secret_rejected(self, monkeypatch):
        """生产环境显式配置弱密钥（<32 字节）必须拒绝；开发随机兜底态放行。"""
        monkeypatch.setattr(jwt_utils, "JWT_SECRET", "short")
        monkeypatch.setattr(jwt_utils, "_SIGNING_SECRET", "")
        monkeypatch.setattr(jwt_utils, "_SIGNING_KID", "")
        monkeypatch.setattr(jwt_utils, "is_production", lambda: True)
        assert jwt_utils.validate_jwt_secret() is False
        # 开发模式弱密钥不直接作为签名密钥，进程内随机兜底保护仍在 → 放行
        monkeypatch.setattr(jwt_utils, "is_production", lambda: False)
        assert jwt_utils.validate_jwt_secret() is True

    def test_production_without_key_refuses_startup(self, monkeypatch):
        import src.auth.security_config as sc
        monkeypatch.setattr(sc, "_config", {"env": "production"})
        monkeypatch.setattr(jwt_utils, "JWT_SECRET", "")
        monkeypatch.setattr(jwt_utils, "_SIGNING_SECRET", "")
        monkeypatch.setattr(jwt_utils, "_SIGNING_KID", "")
        assert jwt_utils.validate_jwt_secret() is False

    def test_dev_ephemeral_secret_sign_and_verify(self):
        tok = jwt_utils.create_access_token(1, "sec_harden", token_version=0)
        payload = jwt_utils.verify_access_token(tok)
        assert payload is not None
        assert payload["sub"] == "1"

    def test_forged_historical_default_token_rejected(self):
        """用历史默认密钥签发的 token 必须被拒绝（服务端已不再使用默认密钥）。"""
        from jose import jwt as jose_jwt
        now = int(time.time())
        forged = jose_jwt.encode(
            {"sub": "1", "username": "sec_harden", "type": "access", "ver": 0,
             "iat": now, "exp": now + 600},
            "grape-dev-secret-change-in-production", algorithm="HS256")
        assert jwt_utils.verify_access_token(forged) is None

    def test_pool_config_allows_empty_jwt_secret(self, monkeypatch):
        secret = "p" * 32
        monkeypatch.setattr(jwt_utils, "_SECRET_KEYS", {"k1": secret})
        monkeypatch.setattr(jwt_utils, "_SIGNING_KID", "k1")
        monkeypatch.setattr(jwt_utils, "_SIGNING_SECRET", secret)
        monkeypatch.setattr(jwt_utils, "JWT_SECRET", "")
        assert jwt_utils.validate_jwt_secret() is True
        tok = jwt_utils.create_access_token(1, "sec_pool", token_version=0)
        assert jwt_utils.verify_access_token(tok) is not None

    def test_docker_compose_requires_jwt_secret(self):
        """docker-compose 必须要求 JWT_SECRET，缺失即启动失败。"""
        path = os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        assert "JWT_SECRET" in text


class TestVerificationHashKey:

    def test_hash_code_uses_effective_secret(self):
        from src.auth import verification as v
        hashed = v._hash_code("000000")
        assert isinstance(hashed, str) and len(hashed) == 64


# ============================================================================
# V2：GitHub webhook 鉴权强校验
# ============================================================================

class TestGithubWebhookAuth:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _install_managed_gh_repo()
        self.called = []
        monkeypatch.setattr("src.api.routes.webhook.run_pipeline",
                            lambda *a, **k: self.called.append((a, k)))
        monkeypatch.setattr("src.api.routes.webhook._handle_repo_deleted",
                            lambda *a, **k: {"status": "repo_deleted", "message": "ok"})
        yield

    def _client(self):
        return TestClient(app)

    def test_missing_signature_and_token_rejected(self):
        r = self._client().post("/webhook/github", content=_gh_push_body(),
                                headers={"X-GitHub-Event": "push"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text[:200]}"
        assert self.called == []

    def test_wrong_signature_rejected(self):
        r = self._client().post("/webhook/github", content=_gh_push_body(),
                                headers={"X-GitHub-Event": "push",
                                         "X-Hub-Signature-256": "sha256=" + "f" * 64})
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:200]}"
        assert self.called == []

    def test_valid_hmac_accepted(self):
        r = self._client().post("/webhook/github", content=_gh_push_body(),
                                headers={"X-GitHub-Event": "push",
                                         "X-Hub-Signature-256": _hmac_signature(_gh_push_body())})
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
        assert self.called, "valid HMAC should schedule pipeline"

    def test_valid_token_header_accepted(self):
        r = self._client().post("/webhook/github", content=_gh_push_body(),
                                headers={"X-GitHub-Event": "push",
                                         "X-GitHub-Token": _WEBHOOK_SECRET})
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
        assert self.called

    def test_wrong_token_header_rejected(self):
        r = self._client().post("/webhook/github", content=_gh_push_body(),
                                headers={"X-GitHub-Event": "push",
                                         "X-GitHub-Token": "wrong-token"})
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:200]}"
        assert self.called == []

    def test_gitcode_route_gh_event_also_rejected(self):
        """GitHub 事件误入 /webhook/gitcode 复用同一强校验：缺凭据 → 401。"""
        r = self._client().post("/webhook/gitcode", content=_gh_push_body(),
                                headers={"X-GitHub-Event": "push"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text[:200]}"
        assert self.called == []

    def test_repo_delete_without_credentials_rejected(self):
        """伪造仓库删除事件但无凭据 → 401，且不触发级联清理。"""
        r = self._client().post("/webhook/github", content=_gh_delete_body(),
                                headers={"X-GitHub-Event": "repository"})
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text[:200]}"
        assert self.called == []

    def test_repo_delete_with_valid_signature_allowed(self):
        r = self._client().post("/webhook/github", content=_gh_delete_body(),
                                headers={"X-GitHub-Event": "repository",
                                         "X-Hub-Signature-256": _hmac_signature(_gh_delete_body())})
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"

    def test_unmanaged_repo_still_404(self):
        r = self._client().post("/webhook/github", content=json.dumps(
            {"repository": {"full_name": "ghost/not-managed"}}).encode(),
            headers={"X-GitHub-Event": "push"})
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"
        assert self.called == []