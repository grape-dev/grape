"""Onboarding 首次托管引导测试：场景技能解析、幂等触发、dev_scene 读写。"""
import os
import sys
import asyncio
from unittest.mock import patch, MagicMock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.user import db as user_db
from src.api import onboarding as ob_mod


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos")
        c.execute("DELETE FROM meta WHERE key LIKE 'onboarded:%'")
        db.commit()


# ===================== dev_scene 读写 =====================

def test_set_get_repo_scene():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.set_repo_scene("gitcode:owner/repo", "cloud-native")
    assert user_db.get_repo_scene("gitcode:owner/repo") == "cloud-native"
    assert user_db.get_repo_scene("gitcode:no/such") == "default"
    _clean()


def test_add_managed_repo_dev_scene_default():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    assert user_db.get_repo_scene("gitcode:owner/repo") == "default"
    _clean()


def test_add_managed_repo_dev_scene_explicit():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1, dev_scene="iac")
    assert user_db.get_repo_scene("gitcode:owner/repo") == "iac"
    _clean()


# ===================== 场景 onboarding 技能解析 =====================

def test_scene_onboarding_skill():
    assert ob_mod._scene_onboarding_skill("default") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("cloud-native") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("iac") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("no-such-scene") == ""


# ===================== 幂等标记 =====================

def test_onboarded_mark_idempotent():
    _clean()
    assert ob_mod._is_onboarded("gitcode:owner/repo") is False
    ob_mod._mark_onboarding_started("gitcode:owner/repo")
    assert ob_mod._is_onboarded("gitcode:owner/repo") is True
    _clean()


# ===================== trigger_onboarding =====================

def test_trigger_skips_when_no_skill():
    _clean()
    # 未托管 → get_repo_scene 返回 default → default 有技能，但未托管无 dev_scene
    # 用 patch 模拟场景无技能
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value=""), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod.asyncio, "create_task") as mock_ct:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
        mock_ct.assert_not_called()
    _clean()


def test_trigger_skips_when_already_onboarded():
    _clean()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=True), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod.asyncio, "create_task") as mock_ct:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
        mock_ct.assert_not_called()
    _clean()


def test_trigger_launches_background_task():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.set_repo_scene("gitcode:owner/repo", "cloud-native")
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding") as mock_skill, \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod.asyncio, "create_task") as mock_ct:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        # 场景解析应按 dev_scene=cloud-native 查
        mock_skill.assert_called_with("cloud-native")
        mock_mark.assert_called_once()
        mock_ct.assert_called_once()
    _clean()


# ===================== _run_onboarding 后台执行 =====================

def test_run_onboarding_consumes_agent():
    _clean()
    async def _fake_consume(*args, **kwargs):
        yield ("text", "分析中...")
        yield ("done", {"text": "已创建 2 个 Issue", "exit_code": 0})
        yield (None, None)

    async def _main():
        with patch("src.runtime.executor.consume_stream_async", side_effect=_fake_consume) as mock_cs:
            await ob_mod._run_onboarding("gitcode:owner/repo", "owner", "repo-onboarding")
            mock_cs.assert_called_once()
            args, kwargs = mock_cs.call_args
            # env_extra 含 REPO_FULL_NAME / REPO_OWNER
            assert kwargs.get("env_extra", {}).get("REPO_OWNER") == "owner"

    asyncio.run(_main())
    _clean()
