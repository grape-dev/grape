"""Issue #3976 技能详情页：原地互转 scope / 在线保存 SKILL.md / AI 填写。

覆盖：
- 原地改 scope：保留 skill id、历史版本行、scene_skill_ref，scope/owner/file_path 原地变更；
- 官方技能不提供互转（403）；
- 个人技能保存 SKILL.md 后版本号不递增，zip 内正文被替换；repo/official 只读；
- AI 填写返回规整后的 summary/description/intro/tags；模型输出不可解析时 502。
"""
import asyncio
import io
import os
import sqlite3
import tempfile
import threading
import zipfile

import pytest
from fastapi import HTTPException

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
_FILE_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_skill_file (
    id TEXT PRIMARY KEY,
    skill_id INTEGER,
    path TEXT,
    size INTEGER DEFAULT 0
)
"""
_SCENE_REF_DDL = """
CREATE TABLE IF NOT EXISTS scene_skill_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id INTEGER NOT NULL,
    skill_id INTEGER DEFAULT 0,
    skill_name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, skill_name)
)
"""


@pytest.fixture()
def sdb(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="issue3976-")
    conn = sqlite3.connect(os.path.join(tmp, "s.db"), check_same_thread=False)
    for ddl in (_SKILL_DDL, _VERSION_DDL, _FILE_DDL, _SCENE_REF_DDL):
        conn.execute(ddl)
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


class _MemStorage:
    def __init__(self):
        self.objects = {}

    def read_bytes(self, key):
        return self.objects.get(key)

    def save_bytes(self, key, data):
        self.objects[key] = data
        return key

    def copy_object(self, src, dst):
        if src not in self.objects:
            return False
        self.objects[dst] = self.objects[src]
        return True

    def delete_object(self, key):
        self.objects.pop(key, None)
        return True

    def backend_name(self):
        return "test"


def _make_zip(skill_name: str, body: str = "# v1", version: str = "1.0.0") -> bytes:
    md = (f"---\nname: {skill_name}\ndescription: desc of {skill_name}\n"
          f"version: {version}\n---\n\n{body}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{skill_name}/SKILL.md", md)
        zf.writestr(f"{skill_name}/references/api.md", "api doc")
    return buf.getvalue()


def _read_zip_member(zip_bytes: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        return zf.read(name).decode("utf-8")


def _seed(sdb, name="issue3976-skill", scope="user", owner="7", display_name=None,
          version="1.0.0", file_path="", like_count=0):
    conn = sdb["conn"]
    disp = name if display_name is None else display_name
    cur = conn.execute(
        "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
        "file_path, scope, owner_id, is_active, like_count, category_id, cat_l1_id, cat_l2_id, "
        "tags, summary, content_hash, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,?,0,0,0,'','旧摘要','oldhash','2020-01-01 00:00:00')",
        (name, disp, "旧描述", "coding", name, file_path, scope, owner, like_count))
    sid = cur.lastrowid
    if version:
        conn.execute(
            "INSERT INTO grape_t_skill_version (id, skill_id, version, change_note, uploaded_at, uploaded_by) "
            "VALUES (?,?,?,?,?,?)",
            (f"v-{sid}", sid, version, "初版", "2026-01-01 00:00:00", owner or "7"))
    conn.commit()
    return sid


def _patch_storage(monkeypatch, storage):
    import src.domains.runtime.public as rt
    monkeypatch.setattr(rt, "get_skill_storage", lambda: storage)
    monkeypatch.setattr(rt, "zip_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(rt, "mem_cache_invalidate", lambda *a, **k: None)
    return rt


def _patch_repos(monkeypatch, repo_keys):
    import src.domains.mgmt.user.db as user_db
    monkeypatch.setattr(user_db, "get_user_repos",
                        lambda user_id, org_id="": [{"repo_key": k} for k in repo_keys])


# ===== 原地互转 scope =====

def test_convert_scope_moves_in_place_keeping_id_versions_and_scene_refs(sdb, monkeypatch):
    old_key = "obs/user-7/skill.zip"
    sid = _seed(sdb, name="conv-skill", scope="user", owner="7", file_path=old_key, like_count=3)
    conn = sdb["conn"]
    conn.execute("INSERT INTO scene_skill_ref (scene_id, skill_id, skill_name) VALUES (?,?,?)",
                 (11, sid, "conv-skill"))
    conn.commit()
    storage = _MemStorage()
    storage.objects[old_key] = _make_zip("conv-skill")
    _patch_storage(monkeypatch, storage)
    _patch_repos(monkeypatch, {"gitcode:org/cv-repo"})

    res = skills_route.convert_skill_scope(
        sid, skills_route.ConvertScopeRequest(target_scope="repo", repo="org/cv-repo"),
        user={"id": "7"})

    assert res["scope"] == "repo" and res["skill_id"] == sid
    row = conn.execute("SELECT scope, owner_id, file_path FROM skill WHERE id=?", (sid,)).fetchone()
    assert row[0] == "repo"
    assert row[1] == "gitcode:org/cv-repo"
    assert row[2] != old_key
    assert row[2] in storage.objects and old_key not in storage.objects
    # id/版本行/场景引用/点赞不变
    assert conn.execute("SELECT COUNT(*) FROM grape_t_skill_version WHERE skill_id=?",
                        (sid,)).fetchone()[0] == 1
    assert conn.execute("SELECT skill_id FROM scene_skill_ref").fetchone()[0] == sid
    assert conn.execute("SELECT like_count FROM skill WHERE id=?", (sid,)).fetchone()[0] == 3


def test_convert_scope_official_is_forbidden(sdb, monkeypatch):
    sid = _seed(sdb, name="official-skill", scope="official", owner="")
    with pytest.raises(HTTPException) as ei:
        skills_route.convert_skill_scope(
            sid, skills_route.ConvertScopeRequest(target_scope="user"),
            user={"id": "7"})
    assert ei.value.status_code == 403


def test_convert_scope_rejects_same_scope_and_duplicate(sdb, monkeypatch):
    sid = _seed(sdb, name="dup-skill", scope="user", owner="7", file_path="obs/a.zip")
    _patch_repos(monkeypatch, {"gitcode:org/cv-repo"})
    with pytest.raises(HTTPException) as ei:
        skills_route.convert_skill_scope(
            sid, skills_route.ConvertScopeRequest(target_scope="user"),
            user={"id": "7"})
    assert ei.value.status_code == 400
    _seed(sdb, name="dup-skill", scope="repo", owner="gitcode:org/cv-repo")
    with pytest.raises(HTTPException) as ei2:
        skills_route.convert_skill_scope(
            sid, skills_route.ConvertScopeRequest(target_scope="repo", repo="org/cv-repo"),
            user={"id": "7"})
    assert ei2.value.status_code == 409


# ===== 在线保存 SKILL.md =====

def test_save_skill_md_keeps_version_and_replaces_content(sdb, monkeypatch):
    key = "obs/user-7/md.zip"
    sid = _seed(sdb, name="md-skill", scope="user", owner="7", version="1.0.0", file_path=key)
    storage = _MemStorage()
    storage.objects[key] = _make_zip("md-skill", "# old")
    rt = _patch_storage(monkeypatch, storage)
    monkeypatch.setattr(rt, "skill_md_content", lambda k, n: "# old")

    new_md = "---\nname: md-skill\ndescription: new\nversion: 9.9.9\n---\n\n# brand new\n"
    res = skills_route.save_skill_content(
        sid, skills_route.SkillContentRequest(content=new_md), user={"id": "7"})

    assert res["version"] == "1.0.0"
    assert sdb["conn"].execute(
        "SELECT COUNT(*) FROM grape_t_skill_version WHERE skill_id=?", (sid,)).fetchone()[0] == 1
    assert _read_zip_member(storage.objects[key], "md-skill/SKILL.md") == new_md
    # 其它包内文件保留
    assert _read_zip_member(storage.objects[key], "md-skill/references/api.md") == "api doc"
    # content_hash 已刷新
    assert sdb["conn"].execute(
        "SELECT content_hash FROM skill WHERE id=?", (sid,)).fetchone()[0] != "oldhash"


def test_save_skill_md_rejects_official_and_repo(sdb, monkeypatch):
    official = _seed(sdb, name="off-md", scope="official", owner="", file_path="obs/o.zip")
    repo = _seed(sdb, name="repo-md", scope="repo", owner="gitcode:org/r", file_path="obs/r.zip")
    _patch_repos(monkeypatch, {"gitcode:org/r"})
    for sid in (official, repo):
        with pytest.raises(HTTPException) as ei:
            skills_route.save_skill_content(
                sid, skills_route.SkillContentRequest(content="# x"), user={"id": "7"})
        assert ei.value.status_code == 403


def test_save_skill_md_rejects_empty_content(sdb):
    sid = _seed(sdb, name="empty-md", scope="user", owner="7", file_path="obs/e.zip")
    with pytest.raises(HTTPException) as ei:
        skills_route.save_skill_content(
            sid, skills_route.SkillContentRequest(content="   "), user={"id": "7"})
    assert ei.value.status_code == 400


# ===== AI 填写 =====

def test_ai_fill_normalizes_and_filters_tags(sdb, monkeypatch):
    sid = _seed(sdb, name="ai-skill", scope="user", owner="7", file_path="obs/ai.zip")
    rt = _patch_storage(monkeypatch, _MemStorage())
    monkeypatch.setattr(rt, "skill_md_content", lambda k, n: "# ai skill body")
    monkeypatch.setattr(skills_route, "_platform_llm_chat", lambda prompt, max_tokens=1200: (
        '```json\n{"summary":"一句话摘要","description":"详细描述",'
        '"intro":{"scope":"适用范围","steps":"","products":"","constraints":"","review":""},'
        '"tags":[{"name":"TDD"},{"name":"qa"},"TDD",{"name":"这是一个超长标签名称"}]}\n```'))

    res = asyncio.run(skills_route.ai_fill_skill_meta(sid, user={"id": "7"}))
    assert res["summary"] == "一句话摘要"
    assert res["description"] == "详细描述"
    assert res["intro"]["scope"] == "适用范围"
    names = [t["name"] for t in res["tags"]]
    assert "TDD" in names and "qa" not in names
    assert len(res["tags"]) <= skills_route.SKILL_MAX_TAGS
    assert all(len(n) <= 12 for n in names)


def test_ai_fill_unparseable_output_raises_502(sdb, monkeypatch):
    sid = _seed(sdb, name="ai-bad", scope="user", owner="7", file_path="obs/ai2.zip")
    rt = _patch_storage(monkeypatch, _MemStorage())
    monkeypatch.setattr(rt, "skill_md_content", lambda k, n: "# body")
    monkeypatch.setattr(skills_route, "_platform_llm_chat", lambda prompt, max_tokens=1200: "not json")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(skills_route.ai_fill_skill_meta(sid, user={"id": "7"}))
    assert ei.value.status_code == 502


def test_ai_fill_official_is_forbidden(sdb, monkeypatch):
    sid = _seed(sdb, name="ai-off", scope="official", owner="")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(skills_route.ai_fill_skill_meta(sid, user={"id": "7"}))
    assert ei.value.status_code == 403


def test_convert_scope_request_defaults_and_models():
    req = skills_route.ConvertScopeRequest(target_scope="user")
    assert req.repo == ""
    assert skills_route.SkillContentRequest(content="x").content == "x"

# ===== 补充覆盖（QA 复审：方向/鉴权/边界/回滚/不落库） =====

def test_convert_scope_repo_to_user_moves_back_in_place(sdb, monkeypatch):
    old_key = "obs/repo-org/r/conv.zip"
    sid = _seed(sdb, name="back-skill", scope="repo", owner="gitcode:org/r", file_path=old_key)
    storage = _MemStorage()
    storage.objects[old_key] = _make_zip("back-skill")
    _patch_storage(monkeypatch, storage)
    _patch_repos(monkeypatch, {"gitcode:org/r"})

    res = skills_route.convert_skill_scope(
        sid, skills_route.ConvertScopeRequest(target_scope="user"), user={"id": "7"})

    assert res["scope"] == "user" and res["skill_id"] == sid
    row = sdb["conn"].execute(
        "SELECT scope, owner_id, file_path FROM skill WHERE id=?", (sid,)).fetchone()
    assert row[0] == "user" and row[1] == "7"
    assert row[2] != old_key and row[2] in storage.objects
    assert old_key not in storage.objects


def test_convert_scope_rejects_foreign_skill_and_unmanaged_repo(sdb, monkeypatch):
    _patch_repos(monkeypatch, {"gitcode:org/r"})
    foreign_user = _seed(sdb, name="foreign-user", scope="user", owner="9", file_path="obs/f1.zip")
    with pytest.raises(HTTPException) as ei:
        skills_route.convert_skill_scope(
            foreign_user, skills_route.ConvertScopeRequest(target_scope="repo", repo="org/r"),
            user={"id": "7"})
    assert ei.value.status_code == 403

    foreign_repo = _seed(sdb, name="foreign-repo", scope="repo", owner="gitcode:other/x",
                         file_path="obs/f2.zip")
    with pytest.raises(HTTPException) as ei2:
        skills_route.convert_skill_scope(
            foreign_repo, skills_route.ConvertScopeRequest(target_scope="user"), user={"id": "7"})
    assert ei2.value.status_code == 403

    mine = _seed(sdb, name="mine-user", scope="user", owner="7", file_path="obs/f3.zip")
    with pytest.raises(HTTPException) as ei3:
        skills_route.convert_skill_scope(
            mine, skills_route.ConvertScopeRequest(target_scope="repo", repo="org/unmanaged"),
            user={"id": "7"})
    assert ei3.value.status_code == 403


def test_convert_scope_missing_skill_returns_404(sdb):
    with pytest.raises(HTTPException) as ei:
        skills_route.convert_skill_scope(
            999999, skills_route.ConvertScopeRequest(target_scope="user"), user={"id": "7"})
    assert ei.value.status_code == 404


def test_convert_scope_db_failure_rolls_back_new_object(sdb, monkeypatch):
    old_key = "obs/user-7/rollback.zip"
    sid = _seed(sdb, name="rollback-skill", scope="user", owner="7", file_path=old_key)
    storage = _MemStorage()
    storage.objects[old_key] = _make_zip("rollback-skill")
    rt = _patch_storage(monkeypatch, storage)
    _patch_repos(monkeypatch, {"gitcode:org/r"})

    real_exec = rt.exec_any

    def _boom(name, params=()):
        if name == "skill_scope_move_update":
            raise RuntimeError("db down")
        return real_exec(name, params)

    monkeypatch.setattr(rt, "exec_any", _boom)
    with pytest.raises(HTTPException) as ei:
        skills_route.convert_skill_scope(
            sid, skills_route.ConvertScopeRequest(target_scope="repo", repo="org/r"),
            user={"id": "7"})
    assert ei.value.status_code == 502
    # 归属未改、源对象仍在、目标副本已回滚
    row = sdb["conn"].execute(
        "SELECT scope, owner_id, file_path FROM skill WHERE id=?", (sid,)).fetchone()
    assert tuple(row) == ("user", "7", old_key)
    assert old_key in storage.objects
    assert list(storage.objects) == [old_key]


def test_save_skill_md_rejects_foreign_missing_and_oversize(sdb, monkeypatch):
    foreign = _seed(sdb, name="foreign-md", scope="user", owner="9", file_path="obs/x1.zip")
    with pytest.raises(HTTPException) as ei:
        skills_route.save_skill_content(
            foreign, skills_route.SkillContentRequest(content="# x"), user={"id": "7"})
    assert ei.value.status_code == 403

    with pytest.raises(HTTPException) as ei2:
        skills_route.save_skill_content(
            999999, skills_route.SkillContentRequest(content="# x"), user={"id": "7"})
    assert ei2.value.status_code == 404

    sid = _seed(sdb, name="big-md", scope="user", owner="7", file_path="obs/x2.zip")
    oversize = "x" * (skills_route._SKILL_MD_MAX_BYTES + 1)
    with pytest.raises(HTTPException) as ei3:
        skills_route.save_skill_content(
            sid, skills_route.SkillContentRequest(content=oversize), user={"id": "7"})
    assert ei3.value.status_code == 400


def test_save_skill_md_db_failure_rolls_back_hash_and_bytes(sdb, monkeypatch):
    key = "obs/user-7/hashrollback.zip"
    sid = _seed(sdb, name="hash-rollback", scope="user", owner="7", file_path=key)
    prev_zip = _make_zip("hash-rollback", "# old")
    storage = _MemStorage()
    storage.objects[key] = prev_zip
    rt = _patch_storage(monkeypatch, storage)
    monkeypatch.setattr(rt, "skill_md_content", lambda k, n: "# old")

    def _boom(*_a, **_k):
        raise RuntimeError("file table down")

    monkeypatch.setattr(skills_route, "_replace_skill_files", _boom)
    with pytest.raises(HTTPException) as ei:
        skills_route.save_skill_content(
            sid, skills_route.SkillContentRequest(content="# new"), user={"id": "7"})
    assert ei.value.status_code == 502
    assert storage.objects[key] == prev_zip
    assert sdb["conn"].execute(
        "SELECT content_hash FROM skill WHERE id=?", (sid,)).fetchone()[0] == "oldhash"


def test_ai_fill_empty_body_returns_400(sdb, monkeypatch):
    sid = _seed(sdb, name="ai-empty-body", scope="user", owner="7", file_path="obs/ai3.zip")
    rt = _patch_storage(monkeypatch, _MemStorage())
    monkeypatch.setattr(rt, "skill_md_content", lambda k, n: "   ")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(skills_route.ai_fill_skill_meta(sid, user={"id": "7"}))
    assert ei.value.status_code == 400


def test_ai_fill_all_empty_fields_returns_502(sdb, monkeypatch):
    sid = _seed(sdb, name="ai-all-empty", scope="user", owner="7", file_path="obs/ai4.zip")
    rt = _patch_storage(monkeypatch, _MemStorage())
    monkeypatch.setattr(rt, "skill_md_content", lambda k, n: "# body")
    monkeypatch.setattr(skills_route, "_platform_llm_chat", lambda prompt, max_tokens=1200: (
        '{"summary":"","description":"","intro":{"scope":"","steps":"","products":"",'
        '"constraints":"","review":""},"tags":[]}'))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(skills_route.ai_fill_skill_meta(sid, user={"id": "7"}))
    assert ei.value.status_code == 502


def test_ai_fill_all_five_slots_and_does_not_persist(sdb, monkeypatch):
    sid = _seed(sdb, name="ai-five", scope="user", owner="7", file_path="obs/ai5.zip")
    rt = _patch_storage(monkeypatch, _MemStorage())
    monkeypatch.setattr(rt, "skill_md_content", lambda k, n: "# body")
    monkeypatch.setattr(skills_route, "_platform_llm_chat", lambda prompt, max_tokens=1200: (
        '{"summary":"摘要","description":"描述","intro":{"scope":"适用范围","steps":"步骤",'
        '"products":"产物","constraints":"约束","review":"评审"},"tags":["标签A"]}'))

    before = sdb["conn"].execute(
        "SELECT description, summary, content_hash, updated_at FROM skill WHERE id=?",
        (sid,)).fetchone()
    res = asyncio.run(skills_route.ai_fill_skill_meta(sid, user={"id": "7"}))
    assert set(res["intro"]) == set(skills_route._AI_INTRO_KEYS)
    assert res["intro"]["review"] == "评审"
    after = sdb["conn"].execute(
        "SELECT description, summary, content_hash, updated_at FROM skill WHERE id=?",
        (sid,)).fetchone()
    assert tuple(after) == tuple(before)
