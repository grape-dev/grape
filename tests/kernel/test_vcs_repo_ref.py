"""仓库身份对齐器（kernel.vcs.repo_ref）——平台响应差异只在 mapper 里。

回归背景（2026-11-20 实锤）：GitLab 建仓响应没有 full_name/owner，路由按那对键取值
得到空串，`to_repo_key("")` 写出空 repo_key，托管记录无法被 webhook 定位，回调一律
404 repository not managed。修复不是给路由加 if，而是把「响应 → 仓库身份」抽成
值对象 + 按卡 dialect 注册的对齐器。
"""
import pytest

from src.kernel.vcs.repo_ref import (
    FlatRepoRefMapper,
    GitLabRepoRefMapper,
    RepoRef,
    RepoRefError,
    mapper_for,
    normalize_repo_payload,
    normalize_repo_ref,
)

GITLAB_RAW = {
    "id": 89213173, "name": "proj", "path": "proj",
    "path_with_namespace": "grp/sub/proj",
    "namespace": {"id": 9, "full_path": "grp/sub", "path": "sub", "kind": "group"},
    "web_url": "https://gitlab.com/grp/sub/proj",
    "default_branch": "main",
}

GITHUB_RAW = {
    "id": 42, "name": "proj", "full_name": "acme/proj",
    "owner": {"login": "acme", "id": 7},
    "html_url": "https://github.com/acme/proj", "default_branch": "main",
}


def test_gitlab_mapper_reads_path_with_namespace_and_namespace_full_path():
    ref = normalize_repo_ref("gitlab", GITLAB_RAW, dialect="gitlab")
    assert isinstance(ref, RepoRef)
    assert (ref.provider, ref.owner, ref.name, ref.full_name) == ("gitlab", "grp/sub", "proj", "grp/sub/proj")
    assert ref.repo_key == "gitlab:grp/sub/proj"      # 子组路径必须完整
    assert ref.default_branch == "main" and ref.native_id == "89213173"
    assert ref.is_complete


def test_flat_mapper_handles_object_owner_and_html_url():
    ref = normalize_repo_ref("github", GITHUB_RAW, dialect="github")
    assert (ref.owner, ref.full_name) == ("acme", "acme/proj")
    assert ref.web_url == "https://github.com/acme/proj"
    assert ref.repo_key == "github:acme/proj"


def test_mapper_registry_is_dialect_driven():
    assert isinstance(mapper_for("gitlab"), GitLabRepoRefMapper)
    assert isinstance(mapper_for("github"), FlatRepoRefMapper)
    assert isinstance(mapper_for(""), FlatRepoRefMapper)     # 未声明方言 → 平铺口径


def test_ref_is_frozen_value_object():
    ref = normalize_repo_ref("gitlab", GITLAB_RAW, dialect="gitlab")
    with pytest.raises(Exception):
        ref.owner = "x"  # type: ignore[misc]


def test_empty_full_name_yields_empty_repo_key_instead_of_lying():
    ref = normalize_repo_ref("gitlab", {"id": 1}, dialect="gitlab")
    assert ref.repo_key == "" and not ref.is_complete


def test_payload_normalization_keeps_native_fields():
    out = normalize_repo_payload("gitlab", GITLAB_RAW, dialect="gitlab")
    assert out["full_name"] == "grp/sub/proj" and out["owner"] == "grp/sub"
    assert out["path_with_namespace"] == "grp/sub/proj"      # 原始字段保留
    assert out["namespace"]["full_path"] == "grp/sub"
    assert out["web_url"].endswith("/grp/sub/proj")


def test_non_object_payload_raises_domain_error():
    with pytest.raises(RepoRefError):
        normalize_repo_ref("gitlab", ["not", "a", "dict"], dialect="gitlab")  # type: ignore[arg-type]