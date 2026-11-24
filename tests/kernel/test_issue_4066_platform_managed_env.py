"""Issue #4066：保留前缀语义收窄 —— `_PLATFORM_MANAGED_ENV` 白名单。

背景：`is_reserved_name()` 原对任何 `GRAPE_` 前缀一律判保留，autopilot 任务里
`GRAPE_ANALYSIS_MODE` / `GRAPE_ORG_ID` / `GRAPE_MANAGED_BY` 100% 被执行侧丢弃
（生产 1 小时 7 条 rejected WARNING，配置意图 ≠ 运行实际）。

本用例锁定契约：
1. 白名单三精确名**不**再判保留，`custom_env_error()` 放行；
2. 白名单**只**放这三个名：其它 `GRAPE_*` 仍保留；
3. `BOT_` / `USER_` 凭据命名空间与显式保留集合语义不变；
4. 大写规范校验不因白名单放宽（小写仍报错）。
"""
import pytest

from src.kernel.scene_custom_env import (
    _PLATFORM_MANAGED_ENV,
    custom_env_error,
    is_reserved_name,
)

WHITELIST = {"GRAPE_ANALYSIS_MODE", "GRAPE_ORG_ID", "GRAPE_MANAGED_BY"}

# 真正敏感的 GRAPE_* 仍必须保留（Issue 验收 3）
SENSITIVE_GRAPE = [
    "GRAPE_SESSION_TOKEN",
    "GRAPE_API_TOKEN",
    "GRAPE_WORK_BRANCH",
    "GRAPE_SERVER_URL",
    "GRAPE_SCENE_ID",
    "GRAPE_CLI_MODE",
    "GRAPE_LINKED_REPOS",
    "GRAPE_UNLISTED_FLAG",          # 白名单外的新 GRAPE_ 变量默认仍保留
]


def test_platform_managed_env_is_exact_whitelist():
    """白名单必须是显式三精确名（收窄而非全局放开）。"""
    assert set(_PLATFORM_MANAGED_ENV) == WHITELIST


@pytest.mark.parametrize("name", sorted(WHITELIST))
def test_whitelisted_names_are_not_reserved(name):
    assert is_reserved_name(name) is False
    assert custom_env_error(name) == ""


@pytest.mark.parametrize("name", SENSITIVE_GRAPE)
def test_sensitive_grape_names_stay_reserved(name):
    assert is_reserved_name(name) is True
    assert "保留" in custom_env_error(name)


@pytest.mark.parametrize("name", [
    "BOT_USERNAME", "BOT_GITCODE_TOKEN", "USER_ID", "USER_TOKEN",
    "PATH", "REPO_FULL_NAME", "ACCOUNT", "AGENT_NAME", "RUNTIME_SERVER_URL",
])
def test_credential_namespaces_and_explicit_set_unchanged(name):
    assert is_reserved_name(name) is True


def test_ordinary_custom_name_still_allowed():
    assert is_reserved_name("TEAM_FLAG") is False
    assert custom_env_error("TEAM_FLAG") == ""


def test_lowercase_whitelist_name_still_rejected_by_uppercase_rule():
    """白名单只影响「保留」判定，大写规范照旧（写侧仍 400）。"""
    assert is_reserved_name("grape_analysis_mode") is False
    assert "大写" in custom_env_error("grape_analysis_mode")


def test_empty_name_rejected():
    assert custom_env_error("") == "自定义环境变量名不能为空"