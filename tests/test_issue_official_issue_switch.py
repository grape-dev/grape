"""official-repo-config-unify-design.md §3.1/§3.2/§七 验收测试（TDD 先红后绿）。

复用 test_feedback_issue_converter 的共享 test_sessions.db 模式（不建内存库，避免
交叉污染既有模块级共享 DB）。覆盖：
- C4 硬编码常量 _OFFICIAL_SKILLS_REPO
- C1/C8/C9/C10 _resolve_target 三分支
- C11/C12/C13 GRAPE_FEEDBACK_OFFICIAL_ISSUE 开关
"""
import os
from unittest import mock

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from src.session_manager import db, db_lock
import src.api.feedback_issue_converter as conv


def _row(skill_scope="repo", feedback_kind="", skill_name="sk", repo_key="gitcode:o/r"):
    return {
        "id": 1, "repo_key": repo_key, "task_id": "t_x", "agent_type": "vod",
        "skill_name": skill_name, "skill_scope": skill_scope, "title": "t",
        "description": "d", "fail_summary": "", "event_seq": 9,
        "issue_id": None, "issue_number": "", "feedback_kind": feedback_kind,
    }


# ---------- C4：硬编码常量 ----------

def test_c4_official_repo_const():
    assert conv._OFFICIAL_SKILLS_REPO.endswith("/grape-skills")
    owner, repo = conv._OFFICIAL_SKILLS_REPO.split("/", 1)
    assert owner and repo


# ---------- C1：official 技能 → 官方技能仓 ----------

def test_c1_official_skill_to_official_repo():
    with mock.patch.object(conv, "_official_skills_target",
                           return_value=("gitcode", "org", "grape-skills", "GB")):
        prov, owner, repo, token = conv._resolve_target(_row(skill_scope="official"))
    assert (prov, owner, repo, token) == ("gitcode", "org", "grape-skills", "GB")


# ---------- C9/C10：CLI / platform → 官方技能仓 ----------

def test_c9_c10_cli_platform_to_official():
    for kind in ("cli", "platform"):
        with mock.patch.object(conv, "_official_skills_target",
                               return_value=("gitcode", "org", "grape-skills", "GB")):
            prov, owner, repo, _ = conv._resolve_target(
                _row(skill_scope="", feedback_kind=kind))
        assert (prov, owner, repo) == ("gitcode", "org", "grape-skills")


# ---------- C8：repo 技能 → 所属仓库 ----------

def test_c8_repo_skill_to_own_repo():
    with mock.patch.object(conv, "_repo_target", return_value=("gitcode", "org", "repo")), \
         mock.patch.object(conv, "_repo_bot_token", return_value="RB"):
        prov, owner, repo, token = conv._resolve_target(_row(skill_scope="repo"))
    assert (prov, owner, repo, token) == ("gitcode", "org", "repo", "RB")


# ---------- C13：开关默认允许 ----------

def test_c13_switch_default_enabled(monkeypatch):
    monkeypatch.delenv("GRAPE_FEEDBACK_OFFICIAL_ISSUE", raising=False)
    assert conv._feedback_official_issue_enabled() is True


# ---------- C11：开关关闭 → official/CLI/platform skipped ----------

def test_c11_switch_off_official_skipped(monkeypatch):
    with mock.patch.object(conv, "_feedback_official_issue_enabled", return_value=False), \
         mock.patch.object(conv, "_official_skills_target",
                           return_value=("gitcode", "org", "grape-skills", "GB")):
        res = conv._resolve_target(_row(skill_scope="official"))
    assert res == ("", "", "", "")


def test_c11_switch_off_cli_platform_skipped(monkeypatch):
    for kind in ("cli", "platform"):
        with mock.patch.object(conv, "_feedback_official_issue_enabled", return_value=False), \
             mock.patch.object(conv, "_official_skills_target",
                               return_value=("gitcode", "org", "grape-skills", "GB")):
            res = conv._resolve_target(_row(skill_scope="", feedback_kind=kind))
        assert res == ("", "", "", "")


# ---------- C12：开关关闭不影响 repo 技能（仍所属仓） ----------

def test_c12_switch_off_repo_still_own_repo(monkeypatch):
    with mock.patch.object(conv, "_feedback_official_issue_enabled", return_value=False), \
         mock.patch.object(conv, "_repo_target", return_value=("gitcode", "org", "repo")), \
         mock.patch.object(conv, "_repo_bot_token", return_value="RB"):
        res = conv._resolve_target(_row(skill_scope="repo"))
    assert res == ("gitcode", "org", "repo", "RB")