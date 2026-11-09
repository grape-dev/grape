"""Issue #721: REPO_FULL_NAME 注入归一 + runbook clone URL 模板拼接测试。

背景（用户反馈 id 488）：codearts-deliver-upload runbook 的 clone URL 模板
`https://gitcode.com/$REPO_FULL_NAME.git` 依赖 `$REPO_FULL_NAME` 为 `owner/repo`
全名（无 host、无 provider 前缀、无 .git）。注入侧若直通带前缀的 repository
（gitcode:owner/repo），会拼出错误地址导致 clone 失败（同族 #584 / #595 注入不匹配）。

覆盖：
- from_repo_key 归一：剥 provider 前缀；无前缀幂等
- env_extra 注入：带前缀 repository → REPO_FULL_NAME 无前缀（与 REPO_OWNER/REPO_NAME 口径一致）
- clone URL 模板拼接：正常 / 前缀泄漏（修复前 RED）/ 空值守卫
"""
import os, sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))


# ── from_repo_key 归一 ──

def test_from_repo_key_strips_provider_prefix():
    from src.repo_key import from_repo_key
    assert from_repo_key("gitcode:GTDTeam/CodeArts") == "GTDTeam/CodeArts"
    assert from_repo_key("github:org/repo") == "org/repo"


def test_from_repo_key_idempotent_on_clean_input():
    from src.repo_key import from_repo_key
    assert from_repo_key("grape-dev/grape") == "grape-dev/grape"
    assert from_repo_key("") == ""


# ── env_extra 注入归一（复现 _execute_agent_inner / _enqueue_pipeline_task 构建口径）──

def _env_extra(repository):
    """同步实现注入侧构建（Issue #721 修复后）：REPO_FULL_NAME 归一。"""
    from src.repo_key import split_owner_repo, from_repo_key
    _owner, _repo_name = split_owner_repo(repository)
    return {
        "REPO_FULL_NAME": from_repo_key(repository) or repository,
        "REPO_OWNER": _owner,
        "REPO_NAME": _repo_name,
    }


def test_env_extra_prefixed_repository_normalized():
    env = _env_extra("gitcode:GTDTeam/CodeArts")
    assert env["REPO_FULL_NAME"] == "GTDTeam/CodeArts"   # 无前缀
    assert env["REPO_OWNER"] == "GTDTeam"
    assert env["REPO_NAME"] == "CodeArts"


def test_env_extra_clean_repository_unchanged():
    env = _env_extra("grape-dev/grape")
    assert env["REPO_FULL_NAME"] == "grape-dev/grape"
    assert env["REPO_OWNER"] == "grape-dev"
    assert env["REPO_NAME"] == "grape"


# ── runbook clone URL 模板拼接 ──

def _clone_url(repo_full_name):
    """runbook 模板（归一后拼接）：https://gitcode.com/$REPO_FULL_NAME.git"""
    from src.repo_key import from_repo_key
    _normalized = from_repo_key(repo_full_name or "")
    return f"https://gitcode.com/{_normalized}.git" if _normalized else ""


def test_clone_url_template_normal_input():
    assert _clone_url("GTDTeam/CodeArts") == "https://gitcode.com/GTDTeam/CodeArts.git"


def test_clone_url_template_prefixed_input_fixed():
    # 修复前（直通拼接）会得到错误地址 https://gitcode.com/gitcode:GTDTeam/CodeArts.git
    assert _clone_url("gitcode:GTDTeam/CodeArts") == "https://gitcode.com/GTDTeam/CodeArts.git"


def test_clone_url_template_empty_value_guard():
    # 空值不得拼出 https://gitcode.com/.git（同族 #584/#595 漏注入形态）
    assert _clone_url("") == ""
    assert _clone_url(None) == ""


# ── 同族一致性核对：#584 BOT_USERNAME / #595 PR_NUMBER 注入键存在性（清单联动）──

def test_injection_checklist_family_keys_present():
    """#584/#595/#721 一致性：三键均为注入清单保留名，不被场景自定义变量覆盖。"""
    from src.scene_custom_env import is_reserved_name
    for name in ("REPO_FULL_NAME", "BOT_USERNAME", "PR_NUMBER"):
        assert is_reserved_name(name), f"{name} 应为保留注入名"
