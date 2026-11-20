"""Issue #3580 复审（对齐 PR #1854）：GitLab 仓库树/文件内容端点。

RED→GREEN 四点：
1. GitLab 方言没有 `git/trees`、`contents` —— 模块钩子把网关照转换成
   `/repository/tree?path=&ref=`、`/repository/files/{整路径编码}?ref=`；
2. 条目归一成网关契约（type=tree|file、sha、size、content=base64）；
3. `/contents/{path}` 先按文件取，上游 404 → 回落目录列表（文件/目录同端点语义）；
4. 非 GitLab 平台零回归（仍走 /git/trees、/contents）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import src.api.git_repo_api as gr
from src.domains.runtime.vcs.providers import gitlab as gl

USER = {"username": "u", "user_id": "1"}


@pytest.fixture()
def fake_upstream(monkeypatch):
    calls: list = []

    def fake_req(provider, method, url, token, body=None):
        calls.append((provider, method, url))
        if provider != "gitlab":
            if "/commits" in url:
                return [{"sha": "treesha"}]
            return {"legacy": True}
        if "/repository/files/" in url:
            if "/repository/files/dir?" in url:
                raise HTTPException(status_code=404, detail={"code": 404})
            return {"file_name": "a.py", "file_path": "src/a.py", "size": 3,
                    "encoding": "base64", "content": "cHJpbnQ=", "last_commit_id": "deadbeef"}
        return [{"id": "id1", "name": "a.py", "type": "blob", "path": "src/a.py",
                 "mode": "100644", "size": 3},
                {"id": "id2", "name": "sub", "type": "tree", "path": "sub", "mode": "040000"}]

    monkeypatch.setattr(gr, "_vcs_req", fake_req)
    monkeypatch.setattr(gr, "_repo_token", lambda owner, repo, user, p: "tok")
    return calls


def test_url_builders_use_gitlab_dialect():
    base = "https://gl.corp/api/v4"
    assert gl.tree_url(base, "grp", "proj", "main") == \
        "https://gl.corp/api/v4/projects/grp%2Fproj/repository/tree?ref=main&per_page=100&page=1"
    assert "recursive=true" in gl.tree_url(base, "grp", "proj", "main", recursive=True)
    assert "/repository/tree?path=src&ref=main" in gl.dir_url(base, "grp", "proj", "src", "main")
    # 文件路径整条编码（含 /），不是逐段拼接
    assert gl.file_url(base, "grp", "proj", "src/a.py", "main") == \
        "https://gl.corp/api/v4/projects/grp%2Fproj/repository/files/src%2Fa.py?ref=main"


def test_normalize_tree_matches_gateway_contract():
    out = gl.normalize_tree([{"id": "x", "name": "src", "type": "tree", "path": "src", "mode": "040000"},
                             {"id": "y", "name": "a.py", "type": "blob", "path": "src/a.py",
                              "mode": "100644", "size": 7}])
    assert out[0] == {"type": "tree", "mode": "040000", "path": "src", "name": "src",
                      "sha": "x", "size": 0}
    assert out[1]["type"] == "file" and out[1]["sha"] == "y" and out[1]["size"] == 7


def test_normalize_file_base64_contract():
    out = gl.normalize_file({"file_name": "a.py", "file_path": "src/a.py", "size": 4,
                             "encoding": "base64", "content": "cHJpbnQ=",
                             "last_commit_id": "abc"}, "src/a.py")
    assert out["type"] == "file" and out["path"] == "src/a.py"
    assert out["content"] == "cHJpbnQ=" and out["encoding"] == "base64" and out["sha"] == "abc"


def test_tree_endpoint_uses_repository_tree(fake_upstream):
    out = gr.get_repo_tree("grp", "proj", ref="main", depth="single", user=USER, p="gitlab")
    assert [e["type"] for e in out] == ["file", "tree"]
    assert "/repository/tree?ref=main" in fake_upstream[-1][2]
    assert "/git/trees/" not in fake_upstream[-1][2]


def test_tree_recursive_allowed_for_gitlab(fake_upstream):
    gr.get_repo_tree("grp", "proj", ref="main", depth="recursive", user=USER, p="gitlab")
    assert "recursive=true" in fake_upstream[-1][2]


def test_root_contents_uses_tree_api(fake_upstream):
    out = gr.list_root_contents("grp", "proj", ref="main", user=USER, p="gitlab")
    assert isinstance(out, list) and out
    assert "/repository/tree?path=&ref=main" in fake_upstream[-1][2]


def test_file_content_and_dir_fallback(fake_upstream):
    f = gr.get_file_content("grp", "proj", path="src/a.py", ref="main", user=USER, p="gitlab")
    assert isinstance(f, dict)
    assert f["type"] == "file" and f["content"] == "cHJpbnQ="
    d = gr.get_file_content("grp", "proj", path="dir", ref="main", user=USER, p="gitlab")
    assert isinstance(d, list) and d and "/repository/tree?path=dir" in fake_upstream[-1][2]


def test_other_platforms_keep_legacy_paths(fake_upstream):
    gr.get_repo_tree("owner", "repo", ref="main", depth="single", user=USER, p="gitcode")
    assert "/git/trees/" in fake_upstream[-1][2]
    gr.get_file_content("owner", "repo", path="a.py", ref="main", user=USER, p="gitcode")
    assert "/contents/a.py" in fake_upstream[-1][2]


def test_default_ref_uses_project_default_branch_for_gitlab(monkeypatch):
    """ref 空时 GitLab 族取项目 default_branch（HEAD/branches[0] 都会踩坑）。"""
    calls = []

    def fake_req(provider, method, url, token, body=None):
        calls.append(url)
        if url.endswith("/api/v4/projects/grp%2Fproj"):
            return {"default_branch": "trunk"}
        return []

    monkeypatch.setattr(gr, "_vcs_req", fake_req)
    monkeypatch.setattr(gr, "_repo_token", lambda owner, repo, user, p: "tok")
    assert gr._default_ref("gitlab", "grp", "proj", USER) == "trunk"

    # 根目录/文件端点：ref 空 → 用默认分支（不得发 ref=HEAD）
    gr.list_root_contents("grp", "proj", ref="", user=USER, p="gitlab")
    assert "ref=trunk" in calls[-1] and "ref=HEAD" not in calls[-1]


def test_empty_ref_file_uses_default_branch(monkeypatch):
    calls = []

    def fake_req(provider, method, url, token, body=None):
        calls.append(url)
        if url.endswith("/api/v4/projects/grp%2Fproj"):
            return {"default_branch": "trunk"}
        return {"file_name": "a.py", "file_path": "a.py", "size": 1,
                "encoding": "base64", "content": "eA==", "last_commit_id": "c0ffee"}

    monkeypatch.setattr(gr, "_vcs_req", fake_req)
    monkeypatch.setattr(gr, "_repo_token", lambda owner, repo, user, p: "tok")
    gr.get_file_content("grp", "proj", path="a.py", ref="", user=USER, p="gitlab")
    assert "ref=trunk" in calls[-1]
