"""Issue #3202 技能卡片展示优化：后端数据链路测试。

覆盖：
- 标签写侧规则：技能侧 ≤5 个、每个 ≤12 字符、禁与系统标签（agent 类型名/分类名）重名；
- 上传/更新摘要必填（空 400），≤50 字清洗；
- PATCH /api/skills/{id} 免传包更新 tags/summary/description/agent_type，
  official 403 / 非属主 403 / 非仓库成员 403，刷新 updated_at、版本号不变；
- 列表 API 透出 summary（详情已有）。
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
    tmp = tempfile.mkdtemp(prefix="issue3202-")
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
    # RLock（对齐生产 session_manager.db_lock）：log_audit 持锁内会再进
    # _next_seq/_get_last_hash 重取同一把锁，非重入 Lock 会自死锁。
    lock = threading.RLock()
    import src.kernel.session_manager as sm
    import src.domains.mgmt.user.db as user_db
    monkeypatch.setattr(sm, "db", db_obj)
    monkeypatch.setattr(sm, "db_lock", lock)
    monkeypatch.setattr(user_db, "db", db_obj)
    monkeypatch.setattr(user_db, "db_lock", lock)
    yield {"conn": conn, "tmp": tmp}
    conn.close()


def _make_zip(skill_name: str, content: str = "# v1") -> bytes:
    body = (f"---\nname: {skill_name}\ndescription: desc of {skill_name}\n"
            f"version: 1.0.0\n---\n\n{content}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{skill_name}/SKILL.md", body)
    return buf.getvalue()


def _seed(sdb, name="issue3202-skill", scope="user", owner="7",
          tags="", summary="旧摘要", description="旧概述", agent_type="coding",
          version="1.0.0"):
    conn = sdb["conn"]
    cur = conn.execute(
        "INSERT INTO skill (skill_name, display_name, description, agent_type, scope, "
        "owner_id, is_active, category_id, cat_l1_id, cat_l2_id, tags, summary, updated_at) "
        "VALUES (?,?,?,?,?,?,1,0,0,0,?,?,'2020-01-01 00:00:00')",
        (name, name, description, agent_type, scope, owner, tags, summary))
    sid = cur.lastrowid
    if version:
        conn.execute(
            "INSERT INTO grape_t_skill_version (id, skill_id, version, change_note, uploaded_at, uploaded_by) "
            "VALUES (?,?,?,?,?,?)",
            (f"v-{sid}", sid, version, "初版", "2026-01-01 00:00:00", "7"))
    conn.commit()
    return sid


def _patch(sid, body, user_id="7"):
    return skills_route.patch_skill_meta(skill_id=int(sid), body=body,
                                         user={"id": user_id, "auth_type": "jwt"})


# ===== 标签写侧规则 =====

def test_validate_skill_tags_allows_five_and_rejects_six():
    kept = skills_route._validate_skill_tags("a,b,c,d,e")
    assert [t["name"] for t in kept] == ["a", "b", "c", "d", "e"]
    with pytest.raises(Exception) as ei:
        skills_route._validate_skill_tags("a,b,c,d,e,f")
    assert getattr(ei.value, "status_code", 0) == 400
    assert ei.value.detail["overflow"] == ["f"]


def test_validate_skill_tags_rejects_too_long():
    with pytest.raises(Exception) as ei:
        skills_route._validate_skill_tags("这个标签名字实在是太长超过十二个字了")
    assert getattr(ei.value, "status_code", 0) == 400
    assert ei.value.detail["oversized"]


def test_validate_skill_tags_rejects_system_names():
    with pytest.raises(Exception) as ei:
        skills_route._validate_skill_tags("Coding")
    assert getattr(ei.value, "status_code", 0) == 400
    assert "Coding" in ei.value.detail["message"]


def test_validate_skill_tags_dedupes_case_insensitive():
    kept = skills_route._validate_skill_tags("AI, ai, 测试")
    assert [t["name"] for t in kept] == ["AI", "测试"]


# ===== 上传/更新摘要必填 =====

def _upload(zip_bytes, summary=""):
    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(zip_bytes), filename="s.zip")
    return asyncio.run(skills_route.upload_skill(
        file=uf, user={"id": "7", "auth_type": "api_key"}, scope="user", repo="",
        agent_type="coding", category_id=0, cat_l1_id=0, cat_l2_id=0,
        tags="", summary=summary))


def test_upload_requires_summary(sdb):
    with pytest.raises(Exception) as ei:
        _upload(_make_zip("issue3202-up"))
    assert getattr(ei.value, "status_code", 0) == 400
    assert "摘要" in ei.value.detail["message"]


def test_update_requires_summary(sdb):
    sid = _seed(sdb, name="issue3202-upd")
    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(_make_zip("issue3202-upd", "# v2")), filename="s.zip")
    with pytest.raises(Exception) as ei:
        asyncio.run(skills_route.update_skill_by_id(
            skill_id=sid, file=uf, user={"id": "7", "auth_type": "api_key"},
            agent_type="", category_id="", cat_l1_id=0, cat_l2_id=0,
            tags="", summary="", clear_category=False))
    assert getattr(ei.value, "status_code", 0) == 400
    assert "摘要" in ei.value.detail["message"]


# ===== PATCH 免传包元信息 =====

def test_patch_updates_fields_refreshes_updated_at_keeps_version(sdb):
    sid = _seed(sdb, tags="旧标签")
    old = sdb["conn"].execute(
        "SELECT updated_at, description FROM skill WHERE id=?", (sid,)).fetchone()
    r = _patch(sid, {
        "tags": [{"name": "代码评审"}, {"name": "python"}],
        "summary": "自动审核仓库所有 open PR，通过即合入",
        "description": "新的概述正文",
        "agent_type": "qa",
    })
    assert r["skill_id"] == sid
    assert r["summary"].startswith("自动审核")
    assert [t["name"] for t in r["tags"]] == ["代码评审", "python"]

    row = sdb["conn"].execute(
        "SELECT description, agent_type, tags, summary, updated_at FROM skill WHERE id=?",
        (sid,)).fetchone()
    assert row[0] == "新的概述正文"
    assert row[1] == "qa"
    assert "代码评审" in row[2] and "python" in row[2]
    assert row[3].startswith("自动审核")
    assert row[4] != old[0], "updated_at 应刷新"
    versions = sdb["conn"].execute(
        "SELECT COUNT(*), MAX(version) FROM grape_t_skill_version WHERE skill_id=?", (sid,)).fetchone()
    assert versions == (1, "1.0.0"), "PATCH 不得新增版本行"


def test_patch_summary_truncates_to_120(sdb):
    # polish round 把 _SUMMARY_MAX 从 50 提到 120（卡片摘要双行），截断上限同步。
    sid = _seed(sdb)
    r = _patch(sid, {"summary": "摘" * 200})
    assert len(r["summary"]) <= 120


def test_patch_rejects_empty_summary(sdb):
    sid = _seed(sdb)
    with pytest.raises(Exception) as ei:
        _patch(sid, {"summary": "   "})
    assert getattr(ei.value, "status_code", 0) == 400


def test_patch_rejects_official(sdb):
    sid = _seed(sdb, name="off", scope="official", owner="")
    with pytest.raises(Exception) as ei:
        _patch(sid, {"summary": "官方不能改"})
    assert getattr(ei.value, "status_code", 0) == 403


def test_patch_rejects_non_owner(sdb):
    sid = _seed(sdb)
    with pytest.raises(Exception) as ei:
        _patch(sid, {"summary": "越权"}, user_id="8")
    assert getattr(ei.value, "status_code", 0) == 403


def test_patch_rejects_repo_non_member(sdb, monkeypatch):
    import src.domains.mgmt.user.db as user_db
    monkeypatch.setattr(user_db, "get_user_repos", lambda uid: [])
    sid = _seed(sdb, name="repo-skill", scope="repo", owner="gitcode:o/r")
    with pytest.raises(Exception) as ei:
        _patch(sid, {"summary": "非成员"})
    assert getattr(ei.value, "status_code", 0) == 403


def test_patch_allows_repo_member(sdb, monkeypatch):
    import src.domains.mgmt.user.db as user_db
    monkeypatch.setattr(user_db, "get_user_repos",
                        lambda uid: [{"repo_key": "gitcode:o/r"}])
    sid = _seed(sdb, name="repo-skill-ok", scope="repo", owner="gitcode:o/r")
    r = _patch(sid, {"summary": "成员可改"})
    assert r["summary"] == "成员可改"


def test_patch_rejects_tag_rules(sdb):
    sid = _seed(sdb)
    for tags in (["a", "b", "c", "d", "e", "f"],
                 ["这个标签名字实在是太长超过十二个字了"],
                 ["Coding"]):
        with pytest.raises(Exception) as ei:
            _patch(sid, {"tags": tags})
        assert getattr(ei.value, "status_code", 0) == 400


def test_patch_rejects_invalid_agent_type(sdb):
    sid = _seed(sdb)
    with pytest.raises(Exception) as ei:
        _patch(sid, {"agent_type": "unknown"})
    assert getattr(ei.value, "status_code", 0) == 400


def test_patch_clears_tags(sdb):
    sid = _seed(sdb, tags="旧标签")
    r = _patch(sid, {"tags": []})
    assert r["tags"] == []
    raw = sdb["conn"].execute("SELECT tags FROM skill WHERE id=?", (sid,)).fetchone()[0]
    assert raw == ""
