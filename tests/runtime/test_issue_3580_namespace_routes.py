"""Issue #3580 复审（对齐 PR #1854）：多级命名空间（组/子组/仓）寻址。

RED→GREEN 三点：
1. `parse_repo_key` / `split_owner_repo` 对 GitLab 家族按**最后一段**切分；
   其它平台首个 `/` 切分（存量零回归）。
2. git-repo 网关在既有两段式路由之后追加 `{owner:path}` 变体（斜杠直连 + `/-/` 分隔），
   且 `/-/` 变体先注册（星网址点贪婪，否则 repo 段会被判成 "-"）。
3. 两段式路径仍命中原路由；OpenAPI/CLI 命令面零变化（include_in_schema=False）。
"""
from __future__ import annotations

import json

import pytest

import src.api.git_repo_api as gr
from src.domains.runtime.vcs.platform_card import (
    build_platform_card, invalidate_platform_card_cache,
)
from src.kernel import repo_key as rk
from src.kernel.session_manager import db, db_lock


def _register_gitlab_instance(provider: str = "gitlab-corp") -> None:
    raw = {
        "name": "GitLab Corp", "dialect": "gitlab", "host": "gitlab.corp.example.com",
        "webhook_path": f"/webhook/{provider}", "event_header": "X-Gitlab-Event",
        "api_base": "https://gitlab.corp.example.com/api/v4",
        "api_auth": "private_token", "webhook_verify": "token_compare",
        "clone_auth": "oauth2", "webhook_dialect": "gitlab_v4",
        "download_base": "https://gitlab.corp.example.com",
    }
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({provider: raw}),))
        db.commit()
    invalidate_platform_card_cache()


@pytest.fixture()
def gitlab_instance_card():
    _register_gitlab_instance()
    yield "gitlab-corp"
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


# ---------------- 1. repo_key 切分 ----------------

def test_parse_repo_key_splits_gitlab_namespace_on_last_segment(gitlab_instance_card):
    assert rk.parse_repo_key("gitlab-corp:grp/sub/proj") == ("gitlab-corp", "grp/sub", "proj")
    assert rk.parse_repo_key("gitlab:grp/sub/proj") == ("gitlab", "grp/sub", "proj")
    assert rk.parse_repo_key("gitlab-corp:grp/proj") == ("gitlab-corp", "grp", "proj")


def test_parse_repo_key_other_platforms_keep_first_segment():
    # 非 GitLab 家族：历史语义（首个 / 切分）零回归
    assert rk.parse_repo_key("gitcode:owner/repo/sub") == ("gitcode", "owner", "repo/sub")
    assert rk.parse_repo_key("github:owner/repo") == ("github", "owner", "repo")


def test_split_owner_repo_namespace(gitlab_instance_card):
    assert rk.split_owner_repo("gitlab-corp:grp/sub/proj") == ("grp/sub", "proj")
    assert rk.split_owner_repo("gitcode:owner/repo/sub") == ("owner", "repo/sub")


def test_vcs_url_quotes_full_namespace_path(gitlab_instance_card):
    """owner 带子组时上游 URL 是「整路径 URL 编码」，不是两段拼接。"""
    url = gr._vcs_url("grp/sub", "proj", "/issues", None, "gitlab-corp")
    assert "/projects/grp%2Fsub%2Fproj/issues" in url


# ---------------- 2/3. 路由变体与顺序 ----------------

def _first_match(path: str):
    scope = {"type": "http", "path": path, "method": "GET", "root_path": "",
             "headers": [], "query_string": b""}
    for r in gr.router.routes:
        matched, _ = r.matches(scope)
        if matched.name == "FULL":
            return str(getattr(r, "path", "")), getattr(getattr(r, "endpoint", None), "__name__", "?")
    return ("NO-MATCH", None)


def test_two_segment_routes_unchanged():
    assert _first_match("/api/git-repo/a/b")[0] == "/api/git-repo/{owner}/{repo}"
    assert _first_match("/api/git-repo/a/b/tree")[0] == "/api/git-repo/{owner}/{repo}/tree"


def test_namespace_routes_resolve_three_segments():
    assert _first_match("/api/git-repo/grp/sub/proj") == \
        ("/api/git-repo/{owner:path}/{repo}", "get_repo_info")
    assert _first_match("/api/git-repo/grp/sub/proj/tree")[0] == \
        "/api/git-repo/{owner:path}/{repo}/tree"
    assert _first_match("/api/git-repo/grp/sub/proj/contents/x/y.md")[0] == \
        "/api/git-repo/{owner:path}/{repo}/contents/{path:path}"


def test_namespace_separated_variant_wins_over_plain():
    """`/-/` 形态必须命中分隔变体：否则 repo 段被贪婪判成 '-'（实测坑）。"""
    assert _first_match("/api/git-repo/grp/sub/proj/-/tree")[0] == \
        "/api/git-repo/{owner:path}/{repo}/-/tree"


def test_namespace_routes_not_in_openapi():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(gr.router)
    spec = app.openapi()
    assert not any("owner:path" in path for path in spec["paths"])
    assert len(spec["paths"]) == len({p for p in spec["paths"]})