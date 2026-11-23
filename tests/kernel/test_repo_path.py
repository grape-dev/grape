"""仓库全路径段级校验（对齐 PR #1854 validate_project，但不误杀 Unicode 组名）。"""
import pytest

from src.kernel.repo_path import RepoPathError, is_valid_repo_path, validate_repo_path


def test_accepts_flat_and_nested_paths():
    assert validate_repo_path("owner/repo") == "owner/repo"
    assert validate_repo_path("team/subgroup/project") == "team/subgroup/project"
    assert validate_repo_path("/owner/repo/") == "owner/repo"      # 首尾斜杠规范化
    assert validate_repo_path("account-a/demo.git") == "account-a/demo.git"
    assert validate_repo_path("小组/子组/项目") == "小组/子组/项目"     # GitLab 合法 Unicode


@pytest.mark.parametrize("bad", [
    "", "   ", "/", "//", "owner//repo", "owner/", "..", "../etc/passwd",
    "owner/../../etc", "owner/..", "./repo", "owner/.", "owner/repo\x00",
    "owner/re po", "owner/re:po", "owner/re*po", "owner\\repo", "a" * 300 + "/b",
])
def test_rejects_unsafe_paths(bad):
    with pytest.raises(RepoPathError):
        validate_repo_path(bad)
    assert is_valid_repo_path(bad) is False


def test_requires_at_least_two_segments():
    with pytest.raises(RepoPathError):
        validate_repo_path("onlyowner")
    assert validate_repo_path("onlyowner", min_segments=1) == "onlyowner"


def test_segment_length_cap():
    with pytest.raises(RepoPathError):
        validate_repo_path("owner/" + "x" * 101)
    assert is_valid_repo_path("owner/" + "x" * 100) is True