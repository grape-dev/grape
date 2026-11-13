"""仓库域三条 P0 的回归锁（2026-11-12）：探活/托管判定/git 传输 = 卡驱动。

背景（实查）：
- `_probe_stale_repos` 用 "非 github ⇒ gitcode.com" 探活，gitee 托管仓被 gitcode
  探测（trace: 404 + 二次 401），只靠 401 分支侥幸没触发级联删除；
- 托管入口 `_provider_from_hint` 只认两前缀，gitee 仓被兜底误判成 github/gitcode；
- git 传输 `_SUPPORTED_PROVIDERS={"gitcode","github"}`，gitee/gitlab 会话 403。
"""
import os
import os as _os

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC_DIR, "data", "test_sessions.db"))
os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import json
import urllib.error
from unittest.mock import patch

import pytest

from src.domains.mgmt.routes import repos as repos_mod
from src.kernel.session_manager import db, db_lock


# ─────────── 1. 平台卡 URL/鉴权派生 ───────────

def test_platform_api_root_follows_card_dialect():
    assert repos_mod._platform_api_root("gitee", "o", "r") == "https://gitee.com/api/v5/repos/o/r"
    assert repos_mod._platform_api_root("gitcode", "o", "r") == "https://gitcode.com/api/v5/repos/o/r"
    assert repos_mod._platform_api_root("github", "o", "r") == "https://api.github.com/repos/o/r"
    # gitlab：v4 无 /repos 资源段，项目标识走 URL-encoded path
    assert repos_mod._platform_api_root("gitlab", "o", "r") == "https://gitlab.com/api/v4/projects/o%2Fr"


def test_platform_api_get_applies_card_auth():
    seen = {}

    class _Resp:
        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _spy(req, timeout=0):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_spy):
        repos_mod._platform_api_get("gitee", "TOK", "https://gitee.com/api/v5/user")
    assert seen["url"].endswith("?access_token=TOK")          # query_token

    with patch("urllib.request.urlopen", side_effect=_spy):
        repos_mod._platform_api_get("github", "TOK", "https://api.github.com/user")
    assert sent_headers(seen).get("Authorization") == "Bearer TOK"

    with patch("urllib.request.urlopen", side_effect=_spy):
        repos_mod._platform_api_get("gitlab", "TOK", "https://gitlab.com/api/v4/user")
    assert sent_headers(seen).get("Private-token") == "TOK" or sent_headers(seen).get("PRIVATE-TOKEN") == "TOK"


def sent_headers(seen):
    return {k: v for k, v in (seen.get("headers") or {}).items()}


# ─────────── 2. 托管判定：卡目录驱动 ───────────

def test_provider_from_hint_gitee_prefix():
    assert repos_mod._provider_from_hint("gitee:o/r") == "gitee"
    assert repos_mod._provider_from_hint("gitcode:o/r") == "gitcode"
    assert repos_mod._provider_from_hint("github:o/r") == "github"
    assert repos_mod._provider_from_hint("gitlab:o/r") == "gitlab"
    assert repos_mod._provider_from_hint("o/r") == ""          # 无来源线索


def test_hosting_unsupported_platform_is_explicit():
    """托管能力位驱动：已接入平台在列，未接入平台不被兜底（避免 webhook/前缀错配）。"""
    impl = repos_mod._hosting_implemented_providers()
    # 2026-11-13 B 批：gitee 接入（gitee_v5 方言，api_base 卡派生，实弹 host/repair/remove 200）
    assert {"gitcode", "github", "gitee"} <= set(impl)
    # 未声明 HOSTING_FULL_FLOW 的平台必须仍在集合外（host 走 422 显式拒绝）
    assert "gitlab" not in impl


# ─────────── 3. git 传输：平台集合/域名卡驱动 ───────────

def test_transport_providers_and_clone_host():
    provs = repos_mod._transport_providers()
    assert {"gitcode", "gitee", "github", "gitlab"} <= set(provs)
    assert repos_mod._clone_host("gitee") == "gitee.com"
    assert repos_mod._clone_host("gitcode") == "gitcode.com"
    assert repos_mod._clone_host("gitlab") == "gitlab.com"


# ─────────── 4. 探活：按 repo_key provider 打对应平台 ───────────

@pytest.fixture()
def probe_repo():
    uid = "probe-user-1"
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
                  "VALUES (?, 'gitee:probe-owner/probe-repo', 'probe-owner', 'probe-repo', 1, 0)", (uid,))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, host_user_id) "
                  "VALUES ('gitee:probe-owner/probe-repo', 'probe-owner', 'probe-repo', 'probe-owner/probe-repo', 123, ?)", (uid,))
        db.commit()
    yield uid
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_repos WHERE user_id=?", (uid,))
        c.execute("DELETE FROM managed_repos WHERE repo_key='gitee:probe-owner/probe-repo'")
        db.commit()


def test_probe_uses_repo_provider_platform(probe_repo):
    """gitee 托管仓必须打 gitee.com（改造前这里打的是 gitcode.com）。"""
    urls = []

    class _Resp:
        def read(self):
            return json.dumps([{"id": 123}]).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _spy(req, timeout=0):
        urls.append(req.full_url)
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_spy), \
         patch.object(repos_mod, "_probe_repo_token", return_value="TOK"), \
         patch.object(repos_mod.user_db, "get_managed_repos", return_value=[{
             "repo_key": "gitee:probe-owner/probe-repo", "repo_owner": "probe-owner",
             "repo_name": "probe-repo", "webhook_id": 123}]):
        repos_mod._probe_stale_repos(probe_repo)

    assert urls, "探活没有发请求"
    assert all(u.startswith("https://gitee.com/api/v5/repos/probe-owner/probe-repo") for u in urls), urls
    assert all("access_token=TOK" in u for u in urls), urls     # gitee 卡 api_auth=query_token


def test_probe_skips_unknown_provider_without_judgement():
    """未在册平台/无 token → 不做判定（不误删）。"""
    urls = []

    def _spy(req, timeout=0):
        urls.append(req.full_url)
        raise AssertionError("不应发请求")

    with patch("urllib.request.urlopen", side_effect=_spy), \
         patch.object(repos_mod, "_probe_repo_token", return_value=""), \
         patch.object(repos_mod.user_db, "get_managed_repos", return_value=[{
             "repo_key": "gitee:probe-owner/probe-repo", "repo_owner": "probe-owner",
             "repo_name": "probe-repo", "webhook_id": 123}]):
        repos_mod._probe_stale_repos("probe-user-x")
    assert urls == []