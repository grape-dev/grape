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


class TestLocalHostStatusDecision:
    """方案 X：进入项目纯本地判定（零 API 调用）。"""

    def test_webhook_positive_clears_bad_status(self):
        """webhook_id>0 且 host_status=webhook_missing → 自动清为正常。"""
        from src.user.db import set_host_status, _get_managed_webhook_id, add_managed_repo
        from src.api.routes.dashboard import _check_host_status
        rk = "gitcode:repair/x1"
        add_managed_repo(rk, "repair", "x1", webhook_id=12345)
        set_host_status(rk, "webhook_missing")
        assert _get_managed_webhook_id(rk) == 12345
        status = _check_host_status("repair", "x1", rk)
        assert status == ""
        from src.user.db import get_host_status
        assert get_host_status(rk) == ""

    def test_webhook_zero_marks_missing(self):
        """webhook_id=0 → webhook_missing（本地判定）。"""
        from src.user.db import add_managed_repo, set_host_status
        from src.api.routes.dashboard import _check_host_status
        rk = "gitcode:repair/x2"
        add_managed_repo(rk, "repair", "x2", webhook_id=0)
        set_host_status(rk, "")
        status = _check_host_status("repair", "x2", rk)
        assert status == "webhook_missing"

    def test_bot_missing_preserved(self):
        """host_status=bot_missing 且 webhook_id>0 → 保留 bot_missing（不误清）。"""
        from src.user.db import add_managed_repo, set_host_status
        from src.api.routes.dashboard import _check_host_status
        rk = "gitcode:repair/x3"
        add_managed_repo(rk, "repair", "x3", webhook_id=111)
        set_host_status(rk, "bot_missing")
        status = _check_host_status("repair", "x3", rk)
        assert status == "bot_missing"