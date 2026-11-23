"""Issue #3855 技能显示名称独立化：后端校验 / 编辑 / 重新上传保留测试。

覆盖：
- 名称必填、去首尾空格、≤20 Unicode 码点（汉字/emoji）；
- 同 scope 重名（空格/大小写变体）阻断、跨 scope 同名放行、自身排除；
- PATCH 免传包编辑 display_name（官方仅放开名称、其余元信息仍 403）；
- 重新上传/同步保留已录入名称，存量空名回填源名称（不清洗）。
"""
import asyncio
import io
import os
import sqlite3
import tempfile
import threading
import zipfile

import pytest

from src.domains.mgmt.routes import skills as skills_route

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
    is_featured INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    sub_scene TEXT DEFAULT '',
    category_id INTEGER DEFAULT 0,
    cat_l1_id INTEGER DEFAULT 0,
    cat_l2_id INTEGER DEFAULT 0,
    tags TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    intro_override TEXT DEFAULT '',
    download_count INTEGER DEFAULT 0,
    content_hash VARCHAR(256) DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (skill_name, scope, owner_id)
)
"""
_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_skill_version (
    id TEXT PRIMARY KEY,
    skill_id INTEGER,
    version TEXT,
    change_note TEXT DEFAULT '',
    uploaded_at TEXT DEFAULT '',
    uploaded_by TEXT DEFAULT ''
)
"""


@pytest.fixture()
def sdb(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="issue3855-")
    conn = sqlite3.connect(os.path.join(tmp, "s.db"), check_same_thread=False)
    conn.execute(_SKILL_DDL)
    conn.execute(_VERSION_DDL)
    conn.commit()

    class _DB:
        def cursor(self):
            return conn.cursor()

        def commit(self):
            conn.commit()

    db_obj = _DB()
    lock = threading.RLock()
    import src.domains.mgmt.user.db as user_db
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", db_obj)
    monkeypatch.setattr(sm, "db_lock", lock)
    monkeypatch.setattr(user_db, "db", db_obj)
    monkeypatch.setattr(user_db, "db_lock", lock)
    yield {"conn": conn, "tmp": tmp}
    conn.close()


def _make_zip(skill_name: str, content: str = "# v1", version: str = "1.0.0") -> bytes:
    body = (f"---\nname: {skill_name}\ndescription: desc of {skill_name}\n"
            f"version: {version}\n---\n\n{content}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{skill_name}/SKILL.md", body)
    return buf.getvalue()


def _make_zip_v(skill_name: str, version: str, content: str = "# v1") -> bytes:
    return _make_zip(skill_name, content, version)


def _seed(sdb, name="issue3855-skill", scope="user", owner="7", display_name=None,
          description="旧概述", agent_type="coding", version="1.0.0", file_path=""):
    conn = sdb["conn"]
    disp = name if display_name is None else display_name
    cur = conn.execute(
        "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
        "file_path, scope, owner_id, is_active, category_id, cat_l1_id, cat_l2_id, tags, "
        "summary, updated_at) VALUES (?,?,?,?,?,?,?,?,1,0,0,0,'','旧摘要','2020-01-01 00:00:00')",
        (name, disp, description, agent_type, name, file_path, scope, owner))
    sid = cur.lastrowid
    if version:
        conn.execute(
            "INSERT INTO grape_t_skill_version (id, skill_id, version, change_note, uploaded_at, uploaded_by) "
            "VALUES (?,?,?,?,?,?)",
            (f"v-{sid}", sid, version, "初版", "2026-01-01 00:00:00", owner or "7"))
    conn.commit()
    return sid


def _patch(sid, body, user_id="7"):
    return skills_route.patch_skill_meta(skill_id=int(sid), body=body,
                                         user={"id": user_id, "auth_type": "jwt"})


def _display(sdb, sid):
    return sdb["conn"].execute(
        "SELECT display_name FROM skill WHERE id=?", (sid,)).fetchone()[0]


# ===== 必填 / 长度 / Unicode 计数 =====

def test_validate_trims_and_accepts_twenty_chars():
    assert skills_route._validate_skill_display_name(
        "  技能名称  ", scope="user", owner_id="7") == "技能名称"


def test_validate_rejects_empty_or_whitespace():
    for raw in ("", "   ", None):
        with pytest.raises(Exception) as ei:
            skills_route._validate_skill_display_name(raw, scope="user", owner_id="7")
        assert getattr(ei.value, "status_code", 0) == 400


def test_validate_length_boundary_unicode():
    ok = "测" * 20
    assert skills_route._validate_skill_display_name(ok, scope="user", owner_id="7") == ok
    with pytest.raises(Exception) as ei:
        skills_route._validate_skill_display_name("测" * 21, scope="user", owner_id="7")
    assert getattr(ei.value, "status_code", 0) == 400


def test_validate_counts_emoji_as_one():
    ok = "😀" * 20
    assert skills_route._validate_skill_display_name(ok, scope="user", owner_id="7") == ok
    with pytest.raises(Exception) as ei:
        skills_route._validate_skill_display_name("😀" * 21, scope="user", owner_id="7")
    assert getattr(ei.value, "status_code", 0) == 400


# ===== 同 scope 重名 / 跨 scope 同名 =====

def test_duplicate_same_scope_case_and_space_insensitive(sdb):
    _seed(sdb, name="dup-a", display_name="Alpha Team")
    for variant in ("alpha team", "  ALPHA TEAM  ", "Alpha  Team".replace("  ", " ")):
        with pytest.raises(Exception) as ei:
            skills_route._validate_skill_display_name(
                variant, scope="user", owner_id="7")
        assert getattr(ei.value, "status_code", 0) == 409
        assert ei.value.detail["message"] == "名称已存在"


def test_cross_scope_same_name_allowed(sdb):
    _seed(sdb, name="cross-user", scope="user", owner="7", display_name="同名")
    assert skills_route._validate_skill_display_name(
        "同名", scope="repo", owner_id="gitcode:o/r") == "同名"


def test_internal_spaces_are_significant(sdb):
    # 需求口径：仅去首尾空格 + 大小写不敏感，内部空格不折叠。
    _seed(sdb, name="inner-space", display_name="a b")
    assert skills_route._validate_skill_display_name(
        "ab", scope="user", owner_id="7") == "ab"


def test_duplicate_excludes_self(sdb):
    sid = _seed(sdb, name="self-dup", display_name="Self Name")
    assert skills_route._validate_skill_display_name(
        "self name", scope="user", owner_id="7", exclude_id=sid) == "self name"


# ===== PATCH 编辑 =====

def test_patch_updates_display_name_and_returns_it(sdb):
    sid = _seed(sdb, display_name="Old Name")
    r = _patch(sid, {"display_name": "  New Name  "})
    assert r["display_name"] == "New Name"
    assert _display(sdb, sid) == "New Name"


def test_patch_rejects_duplicate_display_name(sdb):
    _seed(sdb, name="dup-1", display_name="Taken")
    sid2 = _seed(sdb, name="dup-2", display_name="Other")
    with pytest.raises(Exception) as ei:
        _patch(sid2, {"display_name": "  taken "})
    assert getattr(ei.value, "status_code", 0) == 409
    assert ei.value.detail["message"] == "名称已存在"


def test_patch_boundary_and_unicode(sdb):
    sid = _seed(sdb, display_name="Old")
    assert _patch(sid, {"display_name": "😀" * 20})["display_name"] == "😀" * 20
    with pytest.raises(Exception) as ei:
        _patch(sid, {"display_name": "😀" * 21})
    assert getattr(ei.value, "status_code", 0) == 400


def test_patch_official_allows_display_name_only(sdb, monkeypatch):
    # 官方改名仅管理员：显式打 is_admin 标记，绕开真实 admin_accounts 依赖。
    monkeypatch.setattr(skills_route, "_is_official_skill_admin", lambda user: True)
    sid = _seed(sdb, name="official-skill", scope="official", owner="",
                display_name="官方旧名", agent_type="official")
    r = _patch(sid, {"display_name": "官方新名"}, user_id="7")
    assert r["display_name"] == "官方新名"
    assert _display(sdb, sid) == "官方新名"

    # 官方其余元信息维持 403
    with pytest.raises(Exception) as ei:
        _patch(sid, {"summary": "官方不能改摘要"}, user_id="7")
    assert getattr(ei.value, "status_code", 0) == 403
    with pytest.raises(Exception) as ei:
        _patch(sid, {"display_name": "另一个名", "summary": "混合"}, user_id="7")
    assert getattr(ei.value, "status_code", 0) == 403


def test_patch_official_requires_admin(sdb, monkeypatch):
    monkeypatch.setattr(skills_route, "_is_official_skill_admin", lambda user: False)
    sid = _seed(sdb, name="official-no-admin", scope="official", owner="",
                display_name="官方旧名", agent_type="official")
    with pytest.raises(Exception) as ei:
        _patch(sid, {"display_name": "越权改名"}, user_id="7")
    assert getattr(ei.value, "status_code", 0) == 403
    assert _display(sdb, sid) == "官方旧名"


def test_official_duplicate_ignores_owner_id_variant(sdb):
    # 官方行 owner_id 历史并存 '' 与 0，重名取数必须 scope-only。
    _seed(sdb, name="official-owner-empty", scope="official", owner="",
          display_name="官方同名")
    _seed(sdb, name="official-owner-zero", scope="official", owner="0",
          display_name="另一个官方名")
    with pytest.raises(Exception) as ei:
        skills_route._validate_skill_display_name(
            "官方同名", scope="official", owner_id="0")
    assert getattr(ei.value, "status_code", 0) == 409


def test_patch_cross_scope_same_name_allowed(sdb):
    _seed(sdb, name="off-same", scope="official", owner="", display_name="同名")
    sid = _seed(sdb, name="user-same", scope="user", owner="7", display_name="Other")
    r = _patch(sid, {"display_name": "同名"})
    assert r["display_name"] == "同名"


# ===== 重新上传 / 同步保留名称 =====

def test_upsert_default_preserves_existing_display_name(sdb):
    from src.domains.mgmt.user.db import upsert_skill_obs
    sid = upsert_skill_obs("user", "7", "preserve-skill", "coding", "d", "key", "7", 0, "h")
    assert sid
    sdb["conn"].execute(
        "UPDATE skill SET display_name='用户命名' WHERE id=?", (sid,))
    sdb["conn"].commit()

    # 未显式传名称（重新上传/同步路径）→ 保留
    assert upsert_skill_obs(
        "user", "7", "preserve-skill", "coding", "d", "key", "7", 0, "h2") == sid
    assert _display(sdb, sid) == "用户命名"

    # 显式传入（新增/编辑录入）→ 覆盖
    assert upsert_skill_obs(
        "user", "7", "preserve-skill", "coding", "d", "key", "7", 0, "h3",
        display_name="  新名称  ") == sid
    assert _display(sdb, sid) == "新名称"


def test_upsert_insert_with_explicit_display_name(sdb):
    from src.domains.mgmt.user.db import upsert_skill_obs
    sid = upsert_skill_obs(
        "user", "7", "named-skill", "coding", "d", "key", "7", 0, "h",
        display_name="录入名称")
    assert sid
    assert _display(sdb, sid) == "录入名称"


def test_upsert_backfills_source_name_for_legacy_empty_row(sdb):
    from src.domains.mgmt.user.db import upsert_skill_obs
    sid = _seed(sdb, name="legacy-empty", display_name="")
    assert upsert_skill_obs(
        "user", "7", "legacy-empty", "coding", "d", "key", "7", 0, "h") == sid
    assert _display(sdb, sid) == "legacy-empty"


class _MemStorage:
    def __init__(self):
        self.objects = {}

    def read_bytes(self, key):
        return self.objects.get(key)

    def save_bytes(self, key, data):
        self.objects[key] = data
        return key

    def backend_name(self):
        return "test"


def test_update_route_preserves_display_name(sdb, monkeypatch):
    sid = _seed(sdb, name="update-skill", display_name="保留名", file_path="obs/keep.zip")
    storage = _MemStorage()
    storage.objects["obs/keep.zip"] = _make_zip("update-skill", "# old")
    import src.domains.runtime.public as rt
    monkeypatch.setattr(rt, "get_skill_storage", lambda: storage)
    monkeypatch.setattr(rt, "zip_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(rt, "mem_cache_invalidate", lambda *a, **k: None)

    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(_make_zip_v("update-skill", "1.1.0", "# v2")), filename="s.zip")
    asyncio.run(skills_route.update_skill_by_id(
        skill_id=sid, file=uf, user={"id": "7", "auth_type": "api_key"},
        agent_type="", category_id="", cat_l1_id=0, cat_l2_id=0,
        tags="", summary="更新摘要", clear_category=False))
    assert _display(sdb, sid) == "保留名"


# ===== 上传入口 =====

def test_upload_requires_non_empty_display_name(sdb, monkeypatch):
    import src.domains.runtime.public as rt
    monkeypatch.setattr(rt, "get_skill_storage", lambda: _MemStorage())
    from fastapi import UploadFile
    for raw in ("", "   "):
        uf = UploadFile(file=io.BytesIO(_make_zip("upload-empty")), filename="s.zip")
        with pytest.raises(Exception) as ei:
            asyncio.run(skills_route.upload_skill(
                file=uf, user={"id": "7", "auth_type": "api_key"}, scope="user", repo="",
                agent_type="coding", category_id=0, cat_l1_id=0, cat_l2_id=0,
                tags="", summary="摘要", display_name=raw))
        assert getattr(ei.value, "status_code", 0) == 400


def test_upload_rejects_duplicate_display_name(sdb, monkeypatch):
    import src.domains.runtime.public as rt
    monkeypatch.setattr(rt, "get_skill_storage", lambda: _MemStorage())
    _seed(sdb, name="existing", display_name="Taken")
    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(_make_zip("upload-dup")), filename="s.zip")
    with pytest.raises(Exception) as ei:
        asyncio.run(skills_route.upload_skill(
            file=uf, user={"id": "7", "auth_type": "api_key"}, scope="user", repo="",
            agent_type="coding", category_id=0, cat_l1_id=0, cat_l2_id=0,
            tags="", summary="摘要", display_name="  taken "))
    assert getattr(ei.value, "status_code", 0) == 409


def test_upload_persists_display_name(sdb, monkeypatch):
    captured = {}

    def _spy_upsert(*args, **kwargs):
        captured.update(kwargs)
        return 99

    import src.domains.mgmt.user.db as user_db
    import src.domains.runtime.public as rt
    storage = _MemStorage()
    monkeypatch.setattr(rt, "get_skill_storage", lambda: storage)
    monkeypatch.setattr(rt, "zip_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(rt, "mem_cache_invalidate", lambda *a, **k: None)
    monkeypatch.setattr(user_db, "upsert_skill_obs", _spy_upsert)

    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(_make_zip("upload-named")), filename="s.zip")
    asyncio.run(skills_route.upload_skill(
        file=uf, user={"id": "7", "auth_type": "api_key"}, scope="user", repo="",
        agent_type="coding", category_id=0, cat_l1_id=0, cat_l2_id=0,
        tags="", summary="摘要", display_name="  上传名称  "))
    assert captured.get("display_name") == "上传名称"


# ===== 官方 30 分钟同步保留已录入名称 =====

def _sync_official(monkeypatch, sdb, data):
    import threading as _threading

    import src.domains.mgmt.public as mgmt_public
    from src.domains.runtime.storage import skill_sync
    from src.kernel import owner_db

    lock = _threading.RLock()
    monkeypatch.setattr(owner_db, "db_pair", lambda domain: (sdb["conn"], lock))
    monkeypatch.setattr(skill_sync, "_resolve_skill_category", lambda info, dm: (0, 0))
    monkeypatch.setattr(mgmt_public, "skill_category_active_map", dict)
    monkeypatch.setattr(mgmt_public, "category_dict_active_map", lambda *a, **k: {})
    return skill_sync._sync_skills_to_db(data)


def test_official_sync_preserves_manual_display_name(sdb, monkeypatch):
    sid = _seed(sdb, name="official-sync", scope="official", owner="",
                display_name="人工官方名", description="旧概述", agent_type="official")
    _sync_official(monkeypatch, sdb, {
        "official-sync": {"name": "源名称", "description": "新概述",
                          "agent_type": "official"}})
    assert _display(sdb, sid) == "人工官方名"


def test_official_sync_backfills_source_name_when_empty(sdb, monkeypatch):
    sid = _seed(sdb, name="official-empty-sync", scope="official", owner="",
                display_name="", description="旧概述", agent_type="official")
    _sync_official(monkeypatch, sdb, {
        "official-empty-sync": {"name": "源名称", "description": "新概述",
                                "agent_type": "official"}})
    assert _display(sdb, sid) == "源名称"


# ===== 存量回退展示（display_name 为空 → 源名称） =====

def test_legacy_empty_display_name_falls_back_to_source_name(sdb):
    sid = _seed(sdb, name="legacy-skill", display_name="")
    row = sdb["conn"].execute(
        "SELECT skill_name, display_name FROM skill WHERE id=?", (sid,)).fetchone()
    assert (row[1] or row[0]) == "legacy-skill"