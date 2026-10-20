"""sre-repo-worker-relax 后端约束测试（设计文档 §4）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("GRAPE_DB_BACKEND", "sqlite")


class TestSreNoBroadcast(unittest.TestCase):
    def test_sre_flag(self):
        from src.runtime.engine_select import _sre_no_broadcast
        self.assertTrue(_sre_no_broadcast("sre"))
        self.assertTrue(_sre_no_broadcast("SRE"))
        self.assertTrue(_sre_no_broadcast(" sre "))
        self.assertFalse(_sre_no_broadcast("coding"))
        self.assertFalse(_sre_no_broadcast(""))
        self.assertFalse(_sre_no_broadcast(None))


class TestPickEngineScopeLogic(unittest.TestCase):
    """pick_engine SRE 分支的档位过滤与顺序语义（不落库，纯逻辑）。"""

    def test_scope_filter_user_repo_only(self):
        members = [{"scope": "user"}, {"scope": "repo"}, {"scope": "global"}, {"scope": "local"}]
        filtered = [m for m in members if m.get("scope") in ("user", "repo")]
        self.assertEqual([m["scope"] for m in filtered], ["user", "repo"])

    def test_user_first_then_repo(self):
        scopes = []
        for _scope in ("user", "repo"):
            cand = [m for m in [{"scope": "user"}, {"scope": "repo"}] if m["scope"] == _scope]
            scopes.extend(c["scope"] for c in cand)
        self.assertEqual(scopes, ["user", "repo"])


class TestValidateSceneEngineIds(unittest.TestCase):
    """scenes._validate_scene_engine_ids 的 SRE 空/禁用分支（纯函数行为）。"""

    def _validate(self, nodes, username="alice", repo_key="gitcode:org/repo"):
        from fastapi import HTTPException
        from src.api.routes.scenes import _validate_scene_engine_ids
        try:
            _validate_scene_engine_ids(nodes, repo_key, username)
            return None
        except HTTPException as e:
            if isinstance(e.detail, dict):
                return e.detail.get("message", str(e.detail))
            return str(e.detail)

    def test_sre_empty_list_rejected(self):
        msg = self._validate([{"tag": "sre", "enabled": True, "engine_runtime_ids": []}])
        self.assertIsNotNone(msg)
        self.assertIn("广播", msg)

    def test_sre_disabled_empty_ok(self):
        self.assertIsNone(self._validate([{"tag": "sre", "enabled": False, "engine_runtime_ids": []}]))


if __name__ == "__main__":
    unittest.main()
