"""UC45-UC47（D103）：技能作用域与跨平台同名仓库隔离——纯函数族行为锁。

锚点：src/domains/runtime/storage/__init__.py skill_key（#444 根治：repo 段含 provider）、
src/repo_key.py（repo_key 幂等/默认 gitcode）、skills.py 上传 scope 校验语义。
"""
import os, sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

from src.domains.runtime.storage import skill_key
from src.kernel.repo_key import to_repo_key, from_repo_key


def test_uc45_user_scope_key_has_no_repo_dimension():
    """UC45：个人技能键只含 user_id 段——不同用户天然隔离，键内无仓库维度。"""
    k1 = skill_key("custom", "24", "my-skill")
    k2 = skill_key("custom", "25", "my-skill")
    assert k1 == "skills/custom/24/my-skill/my-skill.zip" or "custom/24/my-skill" in k1
    assert k1 != k2
    assert "gitee" not in k1 and "github" not in k1  # 零仓库/平台维度


def test_uc46_repo_scope_provider_segment_isolation():
    """UC46（#444 根治）：gitee:example-user 与 github:example-user 的仓库技能键不同——
    跨平台同名仓库不共享对象键。"""
    # repo 键两段式 owner/repo（真实仓库恒为 <owner>/<repo> 形态）
    kg = skill_key("repo", "gitee:acme/example-user", "my-skill")
    kh = skill_key("repo", "github:acme/example-user", "my-skill")
    assert kg != kh, "跨平台同名仓库技能键必须隔离（#444）"
    assert "gitee" in kg and "github" in kh


def test_uc46_repo_key_idempotent_and_default():
    """UC46：repo_key 幂等（带前缀原样返回）；裸名默认 gitcode（存量语义）。"""
    assert to_repo_key("gitee:acme/example-user") == "gitee:acme/example-user"
    assert to_repo_key("acme/example-user") == "gitcode:acme/example-user"
    assert from_repo_key("github:acme/example-user") == "acme/example-user"
    assert from_repo_key("example-user") == "example-user"


def test_uc47_skill_key_rejects_bad_scope_and_name():
    """UC47：作用域白名单 + 技能名/用户段白名单——非法输入硬拒。"""
    import pytest
    with pytest.raises(ValueError):
        skill_key("user", "24", "my-skill")          # 旧名 'user' 不在白名单（custom/repo/official）
    with pytest.raises(ValueError):
        skill_key("custom", "abc", "my-skill")       # user_id 段必须纯数字
    with pytest.raises(ValueError):
        skill_key("custom", "24", "../evil")         # 名称白名单防路径逃逸


def test_uc47_upload_scope_validation_semantics():
    """UC47：上传接口作用域校验语义（skills.py 常量级锚点——源码断言锁）。"""
    src = open(os.path.join(_REPO_ROOT, "src/domains/mgmt/routes/skills.py"), encoding="utf-8").read()
    assert 'scope not in ("user", "repo")' in src, "上传 scope 必须显式二选一"
    assert 'scope == "repo" and not repo' in src, "仓库技能必须指定 repo"
    assert "_resolve_repo_key(repo, strict=True)" in src, "#444：写路径歧义硬拒"
    assert "最后写者胜" in src or "最后写者胜" in open(
        os.path.join(_REPO_ROOT, "src/domains/runtime/storage/__init__.py"), encoding="utf-8").read(), "同键覆盖=最后写者胜（设计行为）"
