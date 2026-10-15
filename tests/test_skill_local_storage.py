"""技能本地存储兜底（OBS 未启用时单机可用）测试（docs/weekly-06/skill-local-storage-fallback-design.md）。

覆盖：LocalStorage 后端往返、get_skill_storage 选择器、OBS 未启用时 upload/download/detail 本地成功。
"""
import io
from src.auth import grape_account_db
import os
import sys
import zipfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["OBS_ENABLED"] = "false"
os.environ["SKILL_STORAGE"] = "auto"
os.environ["SKILL_STORAGE_DIR"] = os.path.join(ROOT, "data", "test_skill_local_store")

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.db_setup import init_database
from src.api.main import app
from src.config import SKILL_STORAGE_DIR
from src.session_manager import db, db_lock
from src.storage import get_storage, get_skill_storage, reset_skill_storage

init_database()

_client = TestClient(app)
REPO = "gitcode:org/u-local"
SKILL = "my-skill"
_BOT_TOKEN = "bot_local_test"

_SKILL_MD = ("---\nname: my-skill\ndescription: 本地测试技能\n---\n# 本地测试\n"
             "## 场景\n测试\n## 知识\n测试\n## 步骤\n测试\n")


def _session_token(repo_key=REPO, agent_type="sre"):
    return auth_token.issue_session_token(session_key="t_loc", repo_key=repo_key, agent_type=agent_type,
                                          user_id=_UID)


def _user_headers():
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    if not _UID:
        return {"Authorization": f"Bearer {_BOT_TOKEN}"}
    tok = create_access_token(_UID, "skill_local_user", get_token_version(_UID))
    return {"Authorization": f"Bearer {tok}"}


_UID = None


def setup_module():
    global _UID
    from src.user import db as user_db
    _USER = grape_account_db.create_grape_account("skill_local_user", email=None, password="pw123")
    if not _USER:
        _USER = grape_account_db.get_grape_account_by_username("skill_local_user")
    _UID = _USER["id"] if (_USER or {}).get("id") else 0
    if not _UID:
        _UID = 0
    with db_lock:
        db.cursor().execute("INSERT OR REPLACE INTO managed_repos "
                            "(repo_key, repo_owner, repo_name, full_name) VALUES (?, 'org', 'u-local', 'org/u-local')",
                            (REPO,))
        db.commit()
    user_db.mark_user_repo_join(_UID, REPO, True, True)


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{SKILL}/SKILL.md", _SKILL_MD)
    buf.seek(0)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clean():
    reset_skill_storage()
    import shutil
    shutil.rmtree(SKILL_STORAGE_DIR, ignore_errors=True)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name=?", (SKILL,))
        c.execute("DELETE FROM session_api_tokens WHERE session_key LIKE 't_loc%'")
        db.commit()
    yield
    reset_skill_storage()
    shutil.rmtree(SKILL_STORAGE_DIR, ignore_errors=True)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name=?", (SKILL,))
        c.execute("DELETE FROM session_api_tokens WHERE session_key LIKE 't_loc%'")
        db.commit()


class TestLocalStorage:
    def test_roundtrip(self, tmp_path):
        from src.storage.local import LocalStorage
        s = LocalStorage(str(tmp_path))
        key = "grape/prod/skills/repo/org/u-local/my-skill/my-skill.zip"
        assert s.save_bytes(key, b"data") == key
        assert s.exists(key)
        assert s.read_bytes(key) == b"data"
        assert s.list_keys("grape/prod/skills/repo/org/u-local") == [key]
        lp = str(tmp_path / "out.zip")
        assert s.read_file(key, lp)
        assert open(lp, "rb").read() == b"data"
        assert s.copy_object(key, key + ".copy")
        assert s.exists(key + ".copy")
        assert s.delete_object(key)
        assert not s.exists(key)
        assert s.temporary_url(key) == ""

    def test_bad_key_rejected(self, tmp_path):
        from src.storage.local import LocalStorage
        s = LocalStorage(str(tmp_path))
        with pytest.raises(ValueError):
            s.save_bytes("/abs", b"x")
        with pytest.raises(ValueError):
            s.save_bytes("a/../b", b"x")


class TestSkillStorageSelector:
    def test_auto_local_without_obs(self, monkeypatch):
        assert get_storage() is None  # OBS 未启用
        monkeypatch.setattr("src.config.SKILL_STORAGE", "auto")
        reset_skill_storage()
        s = get_skill_storage()
        assert s is not None and s.backend_name() == "local"
        assert s._root == SKILL_STORAGE_DIR

    def test_obs_only_mode_returns_none(self, monkeypatch):
        monkeypatch.setattr("src.config.SKILL_STORAGE", "obs")
        reset_skill_storage()
        assert get_skill_storage() is None


class TestUploadLocalEndToEnd:
    def test_upload_detail_download_without_obs(self):
        tok = _session_token()
        r = _client.post("/api/skills/upload", data={"scope": "repo", "repo": "org/u-local", "agent_type": "sre"},
                         files={"file": ("s.zip", _make_zip(), "application/zip")},
                         headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text

        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT id, scope, owner_id, file_path FROM skill WHERE skill_name=? AND is_active=1",
                            (SKILL,)).fetchone()
        assert row, "DB 应存在该仓库技能"
        assert row[1] == "repo" and row[2] == REPO
        key = row[3]
        assert key, "object_key(file_path) 应有值"
        assert os.path.isfile(os.path.join(SKILL_STORAGE_DIR, *key.split("/"))), "本地对象文件应存在"

        # detail：SKILL.md 内容可读（user ONLY 通道 → 用户 JWT）
        detail = _client.get(f"/api/skills/by-id/{row[0]}", params={"repo": "org/u-local"},
                             headers=_user_headers())
        assert detail.status_code == 200
        assert "本地测试" in detail.json().get("content", "")

        # download：本地字节流（user ONLY 通道 → 用户 JWT）
        dl = _client.get(f"/api/skills/by-id/{row[0]}/download", params={"repo": "org/u-local"},
                         headers=_user_headers())
        assert dl.status_code == 200
        assert dl.headers.get("content-type", "").startswith("application/zip")