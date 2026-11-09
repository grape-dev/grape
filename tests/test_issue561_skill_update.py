"""Issue #561：按 skill_id 上传 ZIP 覆盖更新技能（卡片直接更新）测试。

覆盖：
- U1 成功更新：id 不变、content_hash 刷新、场景引用（scene_skill_ref）/点赞（skill_likes）保留、
     存储对象被覆盖、zip 缓存与内存缓存失效（新字节可读）
- U2 404：技能不存在
- U3 403：official 不可更新 / user 非本人 / repo 非托管
- U4 400：ZIP 内目录名与既有技能名不一致 / 坏 ZIP
- U5 409 回滚：upsert 失败时恢复旧对象字节
- U6 repo scope 成功（托管校验通过）

Mock 策略（对齐 test_issue444_skill_cache_sync，不触真实外部依赖）：
- DB：临时 SQLite（skill/scene_skill_ref/skill_likes 表，SQL 本身是测试对象）
- 存储：内存 dict 后端（monkeypatch src.storage.get_skill_storage）
- 缓存：目录隔离 + 清空内存缓存
- 路由直调（async 函数 + UploadFile 构造），权限依赖由 user dict 直接传入
"""
import asyncio
import hashlib
import io
import os
import sqlite3
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
_SCENE_REF_DDL = "CREATE TABLE IF NOT EXISTS scene_skill_ref (id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id INTEGER, skill_id INTEGER)"
_LIKES_DDL = "CREATE TABLE IF NOT EXISTS skill_likes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, skill_id INTEGER)"


class _MemStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.read_count = 0

    def backend_name(self):
        return "mem"

    def save_bytes(self, key, data):
        self.objects[key] = data
        return key

    def read_bytes(self, key):
        self.read_count += 1
        return self.objects.get(key)

    def copy_object(self, src, dst):
        if src not in self.objects:
            return False
        self.objects[dst] = self.objects[src]
        return True

    def delete_object(self, key):
        self.objects.pop(key, None)
        return True


@pytest.fixture()
def wdb(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="issue561-")
    conn = sqlite3.connect(os.path.join(tmp, "w.db"), check_same_thread=False)
    conn.execute(_SKILL_DDL)
    conn.execute(_SCENE_REF_DDL)
    conn.execute(_LIKES_DDL)
    conn.commit()

    class _DB:
        def cursor(self):
            return conn.cursor()

        def commit(self):
            conn.commit()

    db_obj = _DB()
    db_lock = threading.Lock()

    import src.session_manager as sm
    import src.user.db as user_db
    monkeypatch.setattr(sm, "db", db_obj)
    monkeypatch.setattr(sm, "db_lock", db_lock)
    monkeypatch.setattr(user_db, "db", db_obj)
    monkeypatch.setattr(user_db, "db_lock", db_lock)

    # 存储与缓存隔离
    storage = _MemStorage()
    import src.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_skill_storage", lambda: storage)
    monkeypatch.setattr(storage_mod, "ZIP_CACHE_DIR", os.path.join(tmp, "zipcache"))
    monkeypatch.setattr(storage_mod, "ZIP_CACHE_INDEX", os.path.join(tmp, "zipcache", "index.json"))
    monkeypatch.setattr(storage_mod, "CACHE_ROOT", os.path.join(tmp, "cache"))
    storage_mod._mem_cache.clear()

    yield {"db": db_obj, "conn": conn, "storage": storage, "tmp": tmp}
    storage_mod._mem_cache.clear()
    conn.close()


def _make_zip(skill_name: str, content: str) -> bytes:
    body = f"---\nname: {skill_name}\ndescription: desc of {skill_name}\nversion: 1.0.0\n---\n\n{content}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{skill_name}/SKILL.md", body)
    return buf.getvalue()


def _seed_user_skill(wdb, user_id=7, name="my-skill", content="# v1", with_refs=True):
    """预置一条 scope=user 技能 + 场景引用 + 点赞，返回 skill_id。"""
    conn = wdb["conn"]
    zip_bytes = _make_zip(name, content)
    h = hashlib.sha256(zip_bytes).hexdigest()
    from src.storage import skill_key
    key = skill_key("custom", str(user_id), name)
    cur = conn.execute(
        "INSERT INTO skill (skill_name, display_name, description, agent_type, file_path, scope, owner_id, is_active, content_hash) "
        "VALUES (?,?,?,?,?,'user',?,1,?)",
        (name, name, "old desc", "coding", key, str(user_id), h))
    sid = cur.lastrowid
    if with_refs:
        conn.execute("INSERT INTO scene_skill_ref (scene_id, skill_id) VALUES (11, ?)", (sid,))
        conn.execute("INSERT INTO scene_skill_ref (scene_id, skill_id) VALUES (12, ?)", (sid,))
        conn.execute("INSERT INTO skill_likes (user_id, skill_id) VALUES (99, ?)", (sid,))
    conn.commit()
    wdb["storage"].objects[key] = zip_bytes
    return sid, key


def _call_update(wdb, skill_id, zip_bytes, zip_name="s.zip", user_id=7, agent_type="", category_id=""):
    from src.api.routes.skills import update_skill_by_id
    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(zip_bytes), filename=zip_name)
    user = {"id": user_id, "auth_type": "api_key"}
    return asyncio.run(update_skill_by_id(
        skill_id=skill_id, file=uf, user=user, agent_type=agent_type, category_id=category_id))


# ---------------------------------------------------------------------------
# U1 成功更新（核心）
# ---------------------------------------------------------------------------

def test_u1_update_success_keeps_id_refs_likes(wdb):
    sid, key = _seed_user_skill(wdb)
    new_zip = _make_zip("my-skill", "# v2 new content")
    old_bytes = wdb["storage"].objects[key]

    r = _call_update(wdb, sid, new_zip)
    assert r["skill_id"] == sid, "更新后技能 id 不变"
    assert "更新成功" in r["message"]

    conn = wdb["conn"]
    row = conn.execute("SELECT content_hash, scope, owner_id, updated_at, is_active FROM skill WHERE id=?", (sid,)).fetchone()
    stored = wdb["storage"].objects[key]
    assert row[0] == hashlib.sha256(stored).hexdigest(), "content_hash 应刷新为覆盖后（规范化重打包）zip 的 hash"
    assert row[1] == "user" and row[2] == "7" and row[4] == 1
    refs = conn.execute("SELECT COUNT(*) FROM scene_skill_ref WHERE skill_id=?", (sid,)).fetchone()[0]
    likes = conn.execute("SELECT COUNT(*) FROM skill_likes WHERE skill_id=?", (sid,)).fetchone()[0]
    assert refs == 2 and likes == 1, "场景引用/点赞应保留"
    assert wdb["storage"].objects[key] != old_bytes, "存储对象应被覆盖"

    # 两级缓存失效：zip 缓存读回新字节 + 新 hash
    from src.storage import zip_cache_get_with_hash
    data, chash, csid = zip_cache_get_with_hash(key)
    assert data == stored and chash == row[0] and csid == sid


# ---------------------------------------------------------------------------
# U2/U3 权限与存在性
# ---------------------------------------------------------------------------

def test_u2_not_found(wdb):
    with pytest.raises(Exception) as ei:
        _call_update(wdb, 999, _make_zip("my-skill", "# v"))
    assert getattr(ei.value, "status_code", 0) == 404


def test_u3_official_forbidden(wdb):
    conn = wdb["conn"]
    cur = conn.execute("INSERT INTO skill (skill_name, scope, owner_id, is_active) VALUES ('off-1','official','',1)")
    conn.commit()
    with pytest.raises(Exception) as ei:
        _call_update(wdb, cur.lastrowid, _make_zip("off-1", "# v"))
    assert getattr(ei.value, "status_code", 0) == 403


def test_u3_user_not_owner_forbidden(wdb):
    sid, _ = _seed_user_skill(wdb, user_id=7)
    with pytest.raises(Exception) as ei:
        _call_update(wdb, sid, _make_zip("my-skill", "# v"), user_id=8)
    assert getattr(ei.value, "status_code", 0) == 403


def test_u3_repo_not_managed_forbidden(wdb):
    from src.user.db import get_user_repos as _gur
    import src.user.db as user_db
    monkey = pytest.MonkeyPatch()
    monkey.setattr(user_db, "get_user_repos", lambda uid: [])
    try:
        conn = wdb["conn"]
        cur = conn.execute("INSERT INTO skill (skill_name, scope, owner_id, is_active) VALUES ('r-1','repo','gitcode:o/r',1)")
        conn.commit()
        with pytest.raises(Exception) as ei:
            _call_update(wdb, cur.lastrowid, _make_zip("r-1", "# v"))
        assert getattr(ei.value, "status_code", 0) == 403
    finally:
        monkey.undo()


def test_u6_repo_managed_success(wdb):
    import src.user.db as user_db
    monkey = pytest.MonkeyPatch()
    monkey.setattr(user_db, "get_user_repos", lambda uid: [{"repo_key": "gitcode:o/r"}])
    try:
        conn = wdb["conn"]
        from src.storage import skill_key
        key = skill_key("repo", "gitcode:o/r", "r-1")
        zip_bytes = _make_zip("r-1", "# v1")
        cur = conn.execute(
            "INSERT INTO skill (skill_name, agent_type, file_path, scope, owner_id, is_active, content_hash) "
            "VALUES ('r-1','coding',?,'repo','gitcode:o/r',1,?)", (key, hashlib.sha256(zip_bytes).hexdigest()))
        conn.commit()
        wdb["storage"].objects[key] = zip_bytes
        new_zip = _make_zip("r-1", "# v2")
        r = _call_update(wdb, cur.lastrowid, new_zip)
        assert r["skill_id"] == cur.lastrowid
        assert wdb["storage"].objects[key] != zip_bytes, "repo scope 存储对象应被覆盖更新"
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# U4 校验
# ---------------------------------------------------------------------------

def test_u4_name_mismatch_rejected(wdb):
    sid, key = _seed_user_skill(wdb)
    old_bytes = wdb["storage"].objects[key]
    with pytest.raises(Exception) as ei:
        _call_update(wdb, sid, _make_zip("other-name", "# v"))
    assert getattr(ei.value, "status_code", 0) == 400
    assert wdb["storage"].objects[key] == old_bytes, "拒绝时不得改写存储"
    conn = wdb["conn"]
    assert conn.execute("SELECT COUNT(*) FROM skill WHERE skill_name='other-name'").fetchone()[0] == 0, "不得新建行"


def test_u4_bad_zip_rejected(wdb):
    sid, _ = _seed_user_skill(wdb)
    with pytest.raises(Exception) as ei:
        _call_update(wdb, sid, b"not-a-zip")
    assert getattr(ei.value, "status_code", 0) == 400


def test_u4_invalid_agent_type_rejected(wdb):
    sid, _ = _seed_user_skill(wdb)
    with pytest.raises(Exception) as ei:
        _call_update(wdb, sid, _make_zip("my-skill", "# v"), agent_type="bad-type")
    assert getattr(ei.value, "status_code", 0) == 400


# ---------------------------------------------------------------------------
# U5 upsert 失败回滚（409 + 恢复旧对象字节）
# ---------------------------------------------------------------------------

def test_u5_upsert_failure_rollback(wdb, monkeypatch):
    sid, key = _seed_user_skill(wdb)
    old_bytes = wdb["storage"].objects[key]

    import src.user.db as user_db

    def _broken(*a, **kw):
        return None

    monkeypatch.setattr(user_db, "upsert_skill_obs", _broken)
    with pytest.raises(Exception) as ei:
        _call_update(wdb, sid, _make_zip("my-skill", "# v2"))
    assert getattr(ei.value, "status_code", 0) == 409
    assert wdb["storage"].objects[key] == old_bytes, "409 时应回滚恢复旧对象字节"
