"""Issue #856：技能上传/更新链 UploadFile.filename 路径穿越防护测试。

覆盖（CWE-22）：
- P1 恶意 filename 400：/tmp/evil.env（绝对路径）、../../evil（穿越且无 .zip）、
     ../../evil.zip（穿越 + .zip）、..\\..\\evil.zip（反斜杠归一后穿越）
- P2 磁盘零副作用：恶意请求不产生 tmp_dir 之外的文件（/tmp/evil.env 不存在）
- P3 空/None filename 默认 skill.zip 行为不变（正常 zip 正常校验）
- P4 正常回归：skill.zip / skill-x.zip 上传与更新成功
- P5 chmod realpath 前缀校验：伪造穿越成员名不抛异常（extractall 已拦截）

Mock 策略对齐 test_issue561_skill_update.py（临时 SQLite + 内存存储 + 路由直调）。
"""
import asyncio
import hashlib
import io
import os
import sys
import tempfile
import threading
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SKILL_DDL = """
CREATE TABLE IF NOT EXISTS skill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    agent_type TEXT DEFAULT '',
    skill_path TEXT DEFAULT '',
    file_path VARCHAR(512) DEFAULT '',
    scope TEXT DEFAULT 'official',
    owner_id TEXT DEFAULT '',
    created_by VARCHAR(64) DEFAULT '',
    updated_by VARCHAR(64) DEFAULT '',
    is_active INTEGER DEFAULT 1,
    category_id INTEGER DEFAULT 0,
    content_hash VARCHAR(256) DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (skill_name, scope, owner_id)
)
"""


class _MemStorage:
    backend = "mem"
    def __init__(self):
        self.objects: dict[str, bytes] = {}
    def backend_name(self): return "mem"
    def read_bytes(self, key): return self.objects.get(key)
    def save_bytes(self, key, data): self.objects[key] = bytes(data)
    def delete_object(self, key): self.objects.pop(key, None)
    def exists(self, key): return key in self.objects


class _DB:
    def __init__(self, conn):
        self._conn = conn
    def cursor(self):
        return self._conn.cursor()
    def commit(self):
        self._conn.commit()


@pytest.fixture()
def wdb(tmp_path, monkeypatch):
    conn = sqlite3_conn = __import__("sqlite3").connect(str(tmp_path / "t.db"))
    sqlite3_conn.execute(_SKILL_DDL)
    sqlite3_conn.commit()
    db_obj = _DB(sqlite3_conn)
    db_lock = threading.Lock()
    import src.session_manager as sm
    import src.user.db as user_db
    monkeypatch.setattr(sm, "db", db_obj)
    monkeypatch.setattr(sm, "db_lock", db_lock)
    monkeypatch.setattr(user_db, "db", db_obj)
    monkeypatch.setattr(user_db, "db_lock", db_lock)

    storage = _MemStorage()
    import src.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_skill_storage", lambda: storage)
    monkeypatch.setattr(storage_mod, "ZIP_CACHE_DIR", str(tmp_path / "zipcache"))
    monkeypatch.setattr(storage_mod, "ZIP_CACHE_INDEX", str(tmp_path / "zipcache" / "index.json"))
    monkeypatch.setattr(storage_mod, "CACHE_ROOT", str(tmp_path / "cache"))
    storage_mod._mem_cache.clear()
    yield {"db": db_obj, "conn": sqlite3_conn, "storage": storage, "tmp": str(tmp_path)}
    storage_mod._mem_cache.clear()
    sqlite3_conn.close()


def _make_zip(skill_name: str, content: str = "# v") -> bytes:
    body = f"---\nname: {skill_name}\ndescription: desc\nversion: 1.0.0\n---\n\n{content}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{skill_name}/SKILL.md", body)
    return buf.getvalue()


def _call_upload(wdb, zip_bytes, filename, user_id=7):
    from src.api.routes.skills import upload_skill
    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(zip_bytes), filename=filename)
    user = {"id": user_id, "auth_type": "api_key"}
    return asyncio.run(upload_skill(file=uf, user=user, scope="user", repo="",
                                    agent_type="coding", category_id=0))


def _call_update(wdb, skill_id, zip_bytes, filename, user_id=7):
    from src.api.routes.skills import update_skill_by_id
    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(zip_bytes), filename=filename)
    user = {"id": user_id, "auth_type": "api_key"}
    return asyncio.run(update_skill_by_id(skill_id=skill_id, file=uf, user=user,
                                          agent_type="", category_id=""))


def _seed_skill(wdb, user_id=7, name="my-skill"):
    conn = wdb["conn"]
    zip_bytes = _make_zip(name)
    h = hashlib.sha256(zip_bytes).hexdigest()
    from src.storage import skill_key
    key = skill_key("custom", str(user_id), name)
    cur = conn.execute(
        "INSERT INTO skill (skill_name, display_name, description, agent_type, file_path, scope, owner_id, is_active, content_hash) "
        "VALUES (?,?,?,?,?,'user',?,1,?)",
        (name, name, "old", "coding", key, str(user_id), h))
    conn.commit()
    wdb["storage"].objects[key] = zip_bytes
    return cur.lastrowid


# ---------------------------------------------------------------------------
# P1/P2 恶意 filename → 400 且磁盘零副作用（上传 + 更新两链）
# ---------------------------------------------------------------------------

EVIL_NAMES = ["/tmp/evil.env", "../../evil", "../../evil.zip", "..\\..\\evil.zip",
              "/tmp/evil.zip", "a/../../evil.zip", ".."]


def _assert_no_side_effect():
    for p in ("/tmp/evil.env", "/tmp/evil.zip", "evil", "evil.zip"):
        assert not os.path.exists(os.path.abspath(p)), f"side effect file exists: {p}"
    assert not any("evil" in e for e in os.listdir("/tmp/opencode") if os.path.isdir("/tmp/opencode"))


@pytest.mark.parametrize("bad_name", EVIL_NAMES)
def test_upload_rejects_malicious_filename(wdb, bad_name):
    from fastapi import HTTPException
    payload = _make_zip("my-skill") if bad_name.endswith((".zip", "zip")) or ".zip" in bad_name else _make_zip("my-skill")
    with pytest.raises(HTTPException) as ei:
        _call_upload(wdb, payload, bad_name)
    assert ei.value.status_code == 400
    _assert_no_side_effect()


@pytest.mark.parametrize("bad_name", EVIL_NAMES)
def test_update_rejects_malicious_filename(wdb, bad_name):
    from fastapi import HTTPException
    sid = _seed_skill(wdb)
    with pytest.raises(HTTPException) as ei:
        _call_update(wdb, sid, _make_zip("my-skill"), bad_name)
    assert ei.value.status_code == 400
    _assert_no_side_effect()


# ---------------------------------------------------------------------------
# P3 空/None filename → 默认 skill.zip 行为不变
# ---------------------------------------------------------------------------

def test_upload_none_filename_bad_zip_defaults(wdb):
    # None filename：走默认名 skill.zip 落盘，非 zip 内容 → BadZipFile 400（证明默认名走完整链）
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _call_upload(wdb, b"not-a-zip", None)
    assert ei.value.status_code == 400


def _mk_uploadfile(data, filename):
    from fastapi import UploadFile
    return UploadFile(file=io.BytesIO(data), filename=filename)


def test_upload_none_filename_valid_zip(wdb):
    # None filename + 有效 zip：行为与 skill.zip 一致（成功上传）
    r = _call_upload(wdb, _make_zip("my-skill"), None)
    assert r["message"].startswith("技能 my-skill 上传成功") or "上传成功" in r["message"]


def test_upload_empty_filename_valid_zip(wdb):
    r = _call_upload(wdb, _make_zip("my-skill"), "")
    assert "上传成功" in r["message"]


# ---------------------------------------------------------------------------
# P4 正常回归：skill.zip / skill-x.zip 上传与更新成功
# ---------------------------------------------------------------------------

def test_upload_normal_filename_ok(wdb):
    r = _call_upload(wdb, _make_zip("my-skill"), "skill.zip")
    assert "上传成功" in r["message"]
    r2 = _call_upload(wdb, _make_zip("other-skill"), "skill-x.zip")
    assert "上传成功" in r2["message"]


def test_update_normal_filename_ok(wdb):
    sid = _seed_skill(wdb, name="my-skill")
    r = _call_update(wdb, sid, _make_zip("my-skill", "# v2"), "skill.zip")
    assert "更新成功" in r["message"] or "成功" in r["message"]
    row = wdb["conn"].execute("SELECT is_active FROM skill WHERE id=?", (sid,)).fetchone()
    assert row[0] == 1


# ---------------------------------------------------------------------------
# 单元级：_safe_zip_name 行为
# ---------------------------------------------------------------------------

def test_safe_zip_name_unit():
    from src.api.routes.skills import _safe_zip_name
    assert _safe_zip_name(None) == "skill.zip"
    assert _safe_zip_name("") == "skill.zip"
    assert _safe_zip_name("  ") == "skill.zip"
    assert _safe_zip_name("skill.zip") == "skill.zip"
    assert _safe_zip_name("a.zip") == "a.zip"
    assert _safe_zip_name("A.ZIP") == "A.ZIP"
    assert _safe_zip_name("evil.txt") == ""
    assert _safe_zip_name("/tmp/evil.env") == ""
    assert _safe_zip_name("/tmp/evil.zip") == ""
    assert _safe_zip_name("../../evil") == ""
    assert _safe_zip_name("../../evil.zip") == ""
    assert _safe_zip_name("..\\..\\evil.zip") == ""
    assert _safe_zip_name("a/../../evil.zip") == ""
    assert _safe_zip_name("..") == ""


# ---------------------------------------------------------------------------
# P5 chmod realpath 前缀校验（_restore_zip_permissions 深度防御）
# ---------------------------------------------------------------------------

def test_restore_zip_permissions_skips_escaping_member(tmp_path):
    from src.api.routes.skills import _restore_zip_permissions
    extract_dir = str(tmp_path / "ex")
    os.makedirs(extract_dir)
    victim = str(tmp_path / "victim.txt")
    with open(victim, "w") as f:
        f.write("x")
    st_before = os.stat(victim).st_mode
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("norm.txt", "n")
        zi = zipfile.ZipInfo("../../victim.txt")
        zi.external_attr = 0o777 << 16
        zf.writestr(zi, "evil")
    with zipfile.ZipFile(buf, "r") as zf:
        _restore_zip_permissions(zf, extract_dir)
    assert os.stat(victim).st_mode == st_before  # 穿越成员未 chmod
