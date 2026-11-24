"""Issue #3580：自建 GitLab 多实例接入（P1 卡校验 / P2 传输安全 / P3 事件队列 / P4 实例发现）。

RED→GREEN：卡名/host/URL/CA、argv 无 token、inbox 幂等与租约、GitLab 实例分页与
Maintainer 判定、web_url 实例隔离。
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from unittest.mock import patch

import pytest

from src.domains.runtime.vcs.platform_card import (
    PlatformCardError, build_platform_card, invalidate_platform_card_cache,
)
from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _clean_cards_and_inbox():
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        try:
            db.cursor().execute("DELETE FROM grape_t_webhook_inbox")
        except Exception:
            pass
        db.commit()
    invalidate_platform_card_cache()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.cursor().execute("DELETE FROM grape_t_webhook_inbox")
        db.commit()
    invalidate_platform_card_cache()


def _instance_raw(**over):
    raw = {
        "name": "GitLab Corp", "dialect": "gitlab", "host": "gitlab.corp.example.com",
        "webhook_path": "/webhook/gitlab-corp", "event_header": "X-Gitlab-Event",
        "api_base": "https://gitlab.corp.example.com/api/v4",
        "api_auth": "private_token", "webhook_verify": "token_compare",
        "clone_auth": "oauth2", "webhook_dialect": "gitlab_v4",
        "download_base": "https://gitlab.corp.example.com",
    }
    raw.update(over)
    return raw


def _register_card(provider: str, raw: dict) -> None:
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({provider: raw}),))
        db.commit()
    invalidate_platform_card_cache()


# ---------------- P1：卡名 / host / URL / CA ----------------

def test_instance_card_one_card_per_instance():
    card = build_platform_card("gitlab-corp", _instance_raw())
    assert card.provider == "gitlab-corp"
    assert card.host == "gitlab.corp.example.com"
    assert card.webhook_path == "/webhook/gitlab-corp"
    assert card.dialect == "gitlab"
    assert card.clone_auth == "oauth2"
    # host 允许合法端口
    hc = build_platform_card("gitlab-corp2", _instance_raw(host="gitlab.corp:8443"))
    assert hc.host == "gitlab.corp:8443"


@pytest.mark.parametrize("name", ["gitlab@corp", "git lab", "", "a" * 65, "gitlab/corp"])
def test_provider_name_rejected(name):
    with pytest.raises(PlatformCardError):
        build_platform_card(name, _instance_raw())


def test_provider_name_hyphen_allowed():
    card = build_platform_card("gitlab-corp", _instance_raw())
    assert card.provider == "gitlab-corp"


@pytest.mark.parametrize("host", [
    "https://gitlab.corp", "gitlab.corp/path", "user@gitlab.corp", "gitlab..corp",
    "gitlab corp", "",
])
def test_host_rejected(host):
    with pytest.raises(PlatformCardError):
        build_platform_card("gitlab-corp", _instance_raw(host=host))


@pytest.mark.parametrize("field,value", [
    ("api_base", "http://gitlab.corp/api/v4"),
    ("api_base", "https://user:pass@gitlab.corp/api/v4"),
    ("api_base", "https://gitlab.corp/api/v4?x=1"),
    ("api_base", "https://gitlab.corp/api/v4#frag"),
    ("api_base", "https://gitlab.corp/api/../v4"),
    ("download_base", "ftp://gitlab.corp"),
    ("download_base", "https://gitlab.corp/?token=1"),
])
def test_instance_url_rejected(field, value):
    with pytest.raises(PlatformCardError):
        build_platform_card("gitlab-corp", _instance_raw(**{field: value}))


def test_ca_bundle_absolute_and_existing():
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(b"-----BEGIN CERTIFICATE-----\n")
        ca = f.name
    card = build_platform_card("gitlab-corp", _instance_raw(ca_bundle=ca))
    assert card.ca_bundle == ca


@pytest.mark.parametrize("ca", ["relative/ca.pem", "/no/such/dir/ca.pem", "/tmp/\x00bad.pem"])
def test_ca_bundle_rejected(ca):
    with pytest.raises(PlatformCardError):
        build_platform_card("gitlab-corp", _instance_raw(ca_bundle=ca))


# ---------------- P2：token 不进 argv / CA + redirect 注入 ----------------

def test_run_git_token_not_in_argv_and_env_injected():
    from src.domains.runtime.engine import repogit

    ca = tempfile.NamedTemporaryFile(suffix=".pem", delete=False).name
    _register_card("gitlab-corp", _instance_raw(ca_bundle=ca))
    grabbed = {}

    def _fake_run(cmd, env=None, **kw):
        grabbed["cmd"] = cmd
        grabbed["env"] = env
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    with patch.object(repogit.subprocess, "run", side_effect=_fake_run):
        r = repogit._run_git(["--version"], token="fake-token-3580",
                             repo_key="gitlab-corp:group/sub/repo")
    assert r.returncode == 0
    cmd = grabbed["cmd"]
    assert "fake-token-3580" not in " ".join(cmd)
    assert "-c" not in cmd
    env = grabbed["env"]
    assert int(env["GIT_CONFIG_COUNT"]) == 3
    vals = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(3)}
    assert vals["http.sslCAInfo"] == ca
    assert vals["http.followRedirects"] == "false"
    auth = vals["http.extraHeader"]
    assert auth.startswith("Authorization: Basic ")
    assert base64.b64decode(auth.split(" ", 2)[2]).decode() == "oauth2:fake-token-3580"


def test_run_git_gitcode_scheme_unchanged():
    from src.domains.runtime.engine import repogit
    grabbed = {}

    def _fake_run(cmd, env=None, **kw):
        grabbed["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    with patch.object(repogit.subprocess, "run", side_effect=_fake_run):
        repogit._run_git(["fetch"], token="tok", repo_key="gitcode:o/r")
    env = grabbed["env"]
    vals = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))}
    auth = vals["http.extraHeader"]
    assert base64.b64decode(auth.split(" ", 2)[2]).decode() == "x-access-token:tok"


# ---------------- P3：事件持久队列幂等 / 租约 / 退避 ----------------

def _enqueue(did="d-1", repo="gitlab-corp:o/r"):
    from src.domains.runtime.webhook_inbox import enqueue_webhook
    return enqueue_webhook(repo_key=repo, provider="gitlab-corp",
                           event_header="Merge Request Hook",
                           body=b'{"object_kind":"merge_request"}',
                           headers={"X-Gitlab-Event-UUID": did}, delivery_id=did)


def test_inbox_enqueue_idempotent():
    a = _enqueue()
    b = _enqueue()
    assert a["duplicate"] is False
    assert b["duplicate"] is True
    assert a["id"] == b["id"]


def test_inbox_delivery_id_fallback_sha256():
    from src.domains.runtime.webhook_inbox import delivery_id_from
    d1 = delivery_id_from({}, b"same")
    d2 = delivery_id_from({}, b"same")
    d3 = delivery_id_from({}, b"other")
    assert d1 == d2 and d1.startswith("sha256:") and d1 != d3


def test_inbox_claim_lease_and_finish_consumed():
    from src.domains.runtime.webhook_inbox import claim_next, finish_claim, inbox_stats
    item = _enqueue("d-2")
    got = claim_next("w1")
    assert got and got["id"] == item["id"]
    assert claim_next("w2") is None  # 租约未过期，不可重复消费
    assert finish_claim(got["id"], "w1", ok=True)["status"] == "consumed"
    assert inbox_stats().get("consumed") == 1


def test_inbox_failure_backoff_then_replay():
    from src.domains.runtime.webhook_inbox import claim_next, finish_claim
    item = _enqueue("d-3")
    got = claim_next("w1")
    res = finish_claim(got["id"], "w1", ok=False, error="boom", base_backoff=0)
    assert res["status"] == "failed" and res["attempts"] == 1
    again = claim_next("w2")
    assert again and again["id"] == item["id"]
    out = finish_claim(again["id"], "w2", ok=False, error="boom2",
                       base_backoff=0)
    assert out["attempts"] == 2


def test_inbox_reclaims_expired_processing_after_crash():
    """进程 kill 后租约过期：processing 行必须可被重新 claim（R10 可重放）。"""
    from src.domains.runtime.webhook_inbox import claim_next, finish_claim
    item = _enqueue("d-crash")
    got = claim_next("dead-worker")
    assert got and got["id"] == item["id"]
    with db_lock:
        db.cursor().execute(
            "UPDATE grape_t_webhook_inbox SET lease_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", item["id"]))
        db.commit()
    again = claim_next("new-worker")
    assert again and again["id"] == item["id"]
    assert finish_claim(again["id"], "new-worker", ok=True)["status"] == "consumed"


# ---------------- web_url 实例隔离 ----------------

def test_web_url_instance_mismatch():
    from src.domains.runtime.routes.webhook import _webhook_instance_mismatch
    _register_card("gitlab-corp", _instance_raw())
    ok = {"project": {"web_url": "https://gitlab.corp.example.com/group/repo"}}
    bad = {"project": {"web_url": "https://gitlab.com/group/repo"}}
    assert _webhook_instance_mismatch("gitlab-corp", ok) is False
    assert _webhook_instance_mismatch("gitlab-corp", bad) is True
    # url 缺失不误杀（旧形态兼容）
    assert _webhook_instance_mismatch("gitlab-corp", {}) is False


# ---------------- P4：GitLab 实例发现分页 + Maintainer ----------------

def test_gitlab_instance_pagination_and_maintainer():
    from src.domains.mgmt.vcs import GitLabProvider
    card = build_platform_card("gitlab-corp", _instance_raw())
    prov = GitLabProvider(card)
    pages = {}

    def _fake_http(method, path, token, data=None, form=False):
        page = int(path.split("page=")[-1])
        pages[page] = pages.get(page, 0) + 1
        if page == 1:
            return [{"path_with_namespace": f"g/r{i}",
                     "permissions": {"project_access": {"access_level": 40}}}
                    for i in range(100)]
        if page == 2:
            return [{"path_with_namespace": "g/last",
                     "permissions": {"group_access": {"access_level": 30},
                                     "project_access": {"access_level": 0}}}]
        return []

    with patch.object(GitLabProvider, "_http", side_effect=_fake_http):
        repos = prov.fetch_repos_with_role("fake-token-3580")
    assert pages == {1: 1, 2: 1}
    assert len(repos) == 101
    assert next(r for r in repos if r["full_name"] == "g/r0")["is_admin"] is True
    assert next(r for r in repos if r["full_name"] == "g/last")["is_admin"] is False


# ---------------- R11：PAT 连接只落 user_credentials(kind=pat) ----------------

def test_connection_route_pat_only_no_login_identity():
    import uuid
    from unittest.mock import MagicMock
    from src.domains.mgmt.routes import repos as repos_routes
    from src.domains.mgmt.user import db as user_db

    _register_card("gitlab-corp", _instance_raw())
    uid = str(uuid.uuid4())
    with db_lock:
        db.cursor().execute(
            "INSERT INTO grape_accounts (id, username, email, password_hash, nickname, status, token_version) "
            "VALUES (?,?,?,?,?,1,0)",
            (uid, "test_3580_conn_" + uid[:6], None, "x", "conn-test"))
        db.commit()
    fake_provider = MagicMock()
    fake_provider.verify_token.return_value = {"id": 7, "username": "alice"}
    try:
        with patch("src.domains.mgmt.vcs.create_provider", return_value=fake_provider):
            out = repos_routes.set_repo_connection(
                "gitlab-corp", repos_routes.RepoConnectionBody(token="fake-token-3580"),
                {"id": uid})
        assert out["configured"] is True and out["source"] == "pat"
        cfg = user_db.get_credential(uid, "gitlab-corp", kind="pat")
        assert cfg["token"] == "fake-token-3580" and cfg["login"] == "alice"
        # R11：PAT 不是登录身份，不写 external_identities
        with db_lock:
            row = db.cursor().execute(
                "SELECT COUNT(*) FROM external_identities WHERE grape_user_id=? AND provider=?",
                (uid, "gitlab-corp")).fetchone()
        assert row[0] == 0
        got = repos_routes.get_repo_connection("gitlab-corp", {"id": uid})
        assert got["configured"] is True and "token" not in got
        # DELETE 清除
        repos_routes.delete_repo_connection("gitlab-corp", {"id": uid})
        assert repos_routes.get_repo_connection("gitlab-corp", {"id": uid})["configured"] is False
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
            db.cursor().execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
            db.commit()


# ---------------- R12：登录渠道无 gitlab ----------------

def test_oauth_channels_have_no_gitlab():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "configs", "oauth-providers.json")
    data = json.load(open(path, encoding="utf-8"))
    assert "gitlab" not in data