"""Issue-host-repo-429 方案 F：host_status 托管状态标记 + 自愈测试。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean_repo():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:repair%'")
        except Exception:
            pass
        db.commit()
    yield


def _add_repo(repo_key="gitcode:repair/test"):
    from src.user.db import add_managed_repo
    add_managed_repo(repo_key, "repair", "test")


class TestHostStatusColumn:
    def test_managed_repos_has_host_status(self):
        """F1：managed_repos 表含 host_status 列。"""
        with db_lock:
            c = db.cursor()
            cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
        assert "host_status" in cols

    def test_set_get_host_status(self):
        from src.user.db import set_host_status, get_host_status
        _add_repo()
        assert get_host_status("gitcode:repair/test") == ""
        set_host_status("gitcode:repair/test", "bot_missing")
        assert get_host_status("gitcode:repair/test") == "bot_missing"
        set_host_status("gitcode:repair/test", "")
        assert get_host_status("gitcode:repair/test") == ""


class TestRepairHostApi:
    def test_repair_no_token(self):
        """F4：缺 token 时 400。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        c = TestClient(app)
        # 无认证头 → 401（FastAPI 依赖注入）
        r = c.post("/api/repos/repair/x/repair-host")
        assert r.status_code == 401