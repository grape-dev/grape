"""Issue #2761：场景/技能统一分类体系 + 标签展示。

覆盖：
- category_dict 种子（10 个一级域 + 二级）与树接口 scope 过滤；
- 标签解析/去重/manual 覆盖 auto/≤3 上限与 400 点名超限标签；
- scene_market 分类与标签回读（列表 + 详情）；
- skill 分类与标签回读（详情/列表）；
- 同步层 slug 命中即写、未命中保留旧值。

直接调用路由函数（绕过鉴权依赖），聚焦业务契约本身。
"""
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEVOPS_DB_PATH", str(ROOT / "data" / "test_sessions.db"))

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.kernel.tags import MAX_TAGS, encode_tags, parse_tags, prepare_tags  # noqa: E402

USER = {"id": "2761", "username": "test2761", "org_id": "", "auth_type": "jwt"}


@pytest.fixture(scope="module")
def cat_ids():
    from src.domains.mgmt.user.db import list_category_dict

    l1 = next(c for c in list_category_dict("scene") if c["parent_id"] == 0)
    l2 = next(c for c in list_category_dict("scene") if c["parent_id"] == l1["id"])
    return {"l1": l1["id"], "l2": l2["id"], "l1_name": l1["name"], "l2_name": l2["name"]}


# ===== 分类字典 =====

def test_category_dict_seeded():
    from src.domains.mgmt.user.db import list_category_dict, list_category_tree

    rows = list_category_dict()
    l1 = [r for r in rows if r["level"] == 1]
    assert len(l1) == 10, f"一级域应为 10 个，实得 {len(l1)}"
    tree = list_category_tree()
    assert len(tree) == 10
    assert all(t["children"] for t in tree), "每个一级域都应有二级子类"


def test_category_tree_api():
    from src.domains.mgmt.routes.categories import category_tree

    data = category_tree(scope="scene", user=USER)
    assert data["scope"] == "scene"
    assert len(data["categories"]) == 10
    assert data["categories"][0]["children"]
    assert len(category_tree(scope="skill", user=USER)["categories"]) == 10
    with pytest.raises(HTTPException) as exc:
        category_tree(scope="bad", user=USER)
    assert exc.value.status_code == 400


def test_legacy_category_endpoints_keep_flat_list():
    from src.domains.mgmt.routes.scenes import list_scene_categories
    from src.domains.mgmt.routes.skills import list_categories

    scene = list_scene_categories(user=USER)
    assert scene["categories"] and scene["tree"]
    skill = list_categories(user=USER)
    assert skill["categories"] and skill["tree"]


# ===== 标签规则 =====

def test_legacy_comma_tags_parse_as_manual():
    tags = parse_tags("iac,terraform")
    assert tags == [{"name": "iac", "source": "manual"},
                    {"name": "terraform", "source": "manual"}]
    assert parse_tags("") == []
    assert parse_tags('[{"name":"AI","source":"auto"}]')[0]["source"] == "auto"


def test_tag_limit_and_dedupe_manual_wins():
    kept, overflow = prepare_tags(["AI", "ai", "测试", "部署", "安全"])
    assert [t["name"] for t in kept] == ["AI", "测试", "部署"]
    assert overflow == ["安全"]
    assert MAX_TAGS == 3

    merged = parse_tags([{"name": "AI", "source": "auto"},
                         {"name": "ai", "source": "manual"}])
    assert merged == [{"name": "AI", "source": "manual"}]


def test_tag_suggest_endpoints():
    from src.domains.mgmt.routes.scenes import suggest_scene_tags
    from src.domains.mgmt.routes.skills import suggest_skill_tags

    r1 = suggest_scene_tags(name="自动化部署", description="", user=USER)
    assert len(r1["tags"]) <= MAX_TAGS
    r2 = suggest_skill_tags(name="AI 测试", description="", user=USER)
    assert len(r2["tags"]) <= MAX_TAGS


def test_scene_tags_over_limit_rejected():
    from src.domains.mgmt.routes.scenes import CreateMarketSceneRequest, create_market_scene

    name = "issue2761-overflow-" + uuid.uuid4().hex[:6]
    body = CreateMarketSceneRequest(
        name=name, scope="repo", nodes=[], tags=["a", "b", "c", "d"])
    with pytest.raises(HTTPException) as exc:
        create_market_scene(body=body, user=USER, repo="testowner/testrepo")
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert "d" in detail["message"]
    assert detail["overflow"] == ["d"]


# ===== 场景分类/标签回读 =====

def test_scene_market_category_and_tags_roundtrip(cat_ids):
    from src.domains.mgmt.routes.scenes import get_scene_market_detail, list_scenes_market
    from src.domains.mgmt.user.db import create_scene_market

    name = "issue2761-scene-" + uuid.uuid4().hex[:6]
    mid = create_scene_market(
        name=name, display_name=name, scope="repo",
        repo_key="gitcode:testowner/testrepo", category_id=1,
        cat_l1_id=cat_ids["l1"], cat_l2_id=cat_ids["l2"],
        tags=encode_tags(["部署", "自动化"]),
    )
    try:
        listing = list_scenes_market(
            user=USER, cat_l1_id=cat_ids["l1"], cat_l2_id=cat_ids["l2"],
            page=1, page_size=20)
        hit = next(s for s in listing["scenes"] if s["id"] == mid)
        assert hit["cat_l1_id"] == cat_ids["l1"]
        assert hit["cat_l2_id"] == cat_ids["l2"]
        assert hit["category"]["l2"]["id"] == cat_ids["l2"]
        assert hit["category"]["l2"]["name"] == cat_ids["l2_name"]
        assert [t["name"] for t in hit["tags"]] == ["部署", "自动化"]

        detail = get_scene_market_detail(mid, user=USER)
        assert detail["category"]["l1"]["id"] == cat_ids["l1"]
        assert len(detail["tags"]) == 2
    finally:
        with db_lock:
            db.execute("DELETE FROM scene_market WHERE id=?", (mid,))
            db.commit()


def test_scene_market_update_category_and_tags(cat_ids):
    from src.domains.mgmt.user.db import (
        create_scene_market, get_scene_market_by_id, update_scene_market,
    )

    name = "issue2761-upd-" + uuid.uuid4().hex[:6]
    mid = create_scene_market(
        name=name, display_name=name, scope="repo",
        repo_key="gitcode:testowner/testrepo", category_id=1,
        cat_l1_id=0, cat_l2_id=0, tags="",
    )
    try:
        assert update_scene_market(
            market_id=mid, cat_l1_id=cat_ids["l1"], cat_l2_id=cat_ids["l2"],
            tags=encode_tags([{"name": "测试", "source": "manual"}]),
        )
        row = get_scene_market_by_id(mid)
        assert row["cat_l1_id"] == cat_ids["l1"]
        assert row["cat_l2_id"] == cat_ids["l2"]
        assert [t["name"] for t in row["tags"]] == ["测试"]
    finally:
        with db_lock:
            db.execute("DELETE FROM scene_market WHERE id=?", (mid,))
            db.commit()


def test_legacy_scene_tags_backfill_idempotent():
    from src.domains.mgmt.user.db import backfill_scene_market_legacy_tags

    name = "issue2761-legacy-" + uuid.uuid4().hex[:6]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_market (name, display_name, source, scope, tags, is_active) "
            "VALUES (?,?, 'official', 'official', 'iac,terraform', 1)",
            (name, name))
        db.commit()
        sid = c.lastrowid
    try:
        assert backfill_scene_market_legacy_tags() >= 1
        with db_lock:
            c = db.cursor()
            c.execute("SELECT tags FROM scene_market WHERE id=?", (sid,))
            raw = c.fetchone()[0]
        assert [t["name"] for t in parse_tags(raw)] == ["iac", "terraform"]
        assert all(t["source"] == "manual" for t in parse_tags(raw))
        backfill_scene_market_legacy_tags()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT tags FROM scene_market WHERE id=?", (sid,))
            assert c.fetchone()[0] == raw
    finally:
        with db_lock:
            db.execute("DELETE FROM scene_market WHERE id=?", (sid,))
            db.commit()


# ===== 技能分类/标签回读 =====

def test_skill_detail_returns_category_and_tags(cat_ids):
    from src.domains.mgmt.routes.skills import get_skill_detail_by_id, list_skills

    skill_name = "issue2761-skill-" + uuid.uuid4().hex[:6]
    tags_json = encode_tags([{"name": "AI", "source": "auto"},
                             {"name": "代码评审", "source": "manual"}])
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "scope, owner_id, is_active, category_id, cat_l1_id, cat_l2_id, tags) "
            "VALUES (?,?,?,?,?, 'official', '', 1, 0, ?, ?, ?)",
            (skill_name, skill_name, "test", "coding", skill_name,
             cat_ids["l1"], cat_ids["l2"], tags_json))
        db.commit()
        sid = c.lastrowid
    try:
        body = get_skill_detail_by_id(str(sid), user=USER, repo="")
        assert body["cat_l1_id"] == cat_ids["l1"]
        assert body["cat_l2_id"] == cat_ids["l2"]
        assert body["category"]["l1"]["id"] == cat_ids["l1"]
        assert [t["name"] for t in body["tags"]] == ["AI", "代码评审"]

        listing = list_skills(user=USER, scope="all", page_size=0,
                              cat_l1_id=cat_ids["l1"])
        hit = next(s for s in listing["skills"] if s["id"] == sid)
        assert hit["category"]["l2"]["id"] == cat_ids["l2"]
        assert len(hit["tags"]) <= MAX_TAGS
    finally:
        with db_lock:
            db.execute("DELETE FROM skill WHERE id=?", (sid,))
            db.commit()


# ===== 同步层映射 =====

def test_scene_sync_resolver_hit_and_miss():
    from src.domains.runtime.storage import scene_sync

    scene_sync._category_dict_cache.clear()
    scene_sync._category_dict_cache.update({
        "it-rd": {"id": 1, "level": 1, "parent_id": 0},
        "it-rd-ai-app": {"id": 2, "level": 2, "parent_id": 1},
    })
    try:
        assert scene_sync._resolve_scene_category({"category_l2": "it-rd-ai-app"}) == (1, 2)
        assert scene_sync._resolve_scene_category({"category": "it-rd"}) == (1, 0)
        assert scene_sync._resolve_scene_category({"category": "unknown-slug"}) == (0, 0)
    finally:
        scene_sync._category_dict_cache.clear()


def test_skill_sync_resolver_hit_and_miss():
    from src.domains.runtime.storage import skill_sync

    mapping = {
        "it-rd": {"id": 1, "level": 1, "parent_id": 0},
        "it-rd-ai-app": {"id": 2, "level": 2, "parent_id": 1},
    }
    assert skill_sync._resolve_skill_category({"category_l2": "it-rd-ai-app"}, mapping) == (1, 2)
    assert skill_sync._resolve_skill_category({"category": "it-rd"}, mapping) == (1, 0)
    assert skill_sync._resolve_skill_category({"category": "missing"}, mapping) == (0, 0)


# ===== QA 复审补充：写侧与同步层 =====

def _upload_file(data: bytes = b"not-a-zip"):
    import io

    from fastapi import UploadFile

    return UploadFile(filename="skill.zip", file=io.BytesIO(data))


def test_skill_upload_tags_over_limit_rejected():
    import asyncio

    from src.domains.mgmt.routes.skills import upload_skill

    with pytest.raises(HTTPException) as exc:
        asyncio.run(upload_skill(
            file=_upload_file(), user=USER, scope="user", repo="",
            agent_type="coding", category_id=0, cat_l1_id=0, cat_l2_id=0,
            tags="a,b,c,d"))
    assert exc.value.status_code == 400
    assert exc.value.detail["overflow"] == ["d"]


def test_skill_update_tags_over_limit_rejected():
    import asyncio

    from src.domains.mgmt.routes.skills import update_skill_by_id

    skill_name = "issue2761-upd-limit-" + uuid.uuid4().hex[:6]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "scope, owner_id, is_active) VALUES (?,?,?,?,?, 'user', ?, 1)",
            (skill_name, skill_name, "test", "coding", skill_name, str(USER["id"])))
        db.commit()
        sid = c.lastrowid
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(update_skill_by_id(
                sid, file=_upload_file(), user=USER, agent_type="", category_id="",
                cat_l1_id=0, cat_l2_id=0, tags="a,b,c,d"))
        assert exc.value.status_code == 400
        assert exc.value.detail["overflow"] == ["d"]
    finally:
        with db_lock:
            db.execute("DELETE FROM skill WHERE id=?", (sid,))
            db.commit()


def test_skill_upload_tags_only_keeps_existing_category(cat_ids):
    """仅带 tags 上传不得以 0 覆盖既有分类（QA 硬违规 1 的读侧等价断言）。"""
    from src.domains.mgmt.routes.skills import _skill_category_payload

    skill_name = "issue2761-upload-keep-" + uuid.uuid4().hex[:6]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "scope, owner_id, is_active, cat_l1_id, cat_l2_id) "
            "VALUES (?,?,?,?,?, 'user', ?, 1, ?, ?)",
            (skill_name, skill_name, "test", "coding", skill_name, str(USER["id"]),
             cat_ids["l1"], cat_ids["l2"]))
        db.commit()
        sid = c.lastrowid
    try:
        # 模拟 upload 的合并口径：仅 tags 提供时回填 cur_l1/cur_l2
        from src.domains.runtime import public as rt
        cur = rt.read_one_any("skill_meta_row", (sid,))
        target_l1 = 0 or int(cur[5] or 0)
        target_l2 = 0 or int(cur[6] or 0)
        rt.update_skill_category_tags(sid, target_l1, target_l2,
                                      encode_tags([{"name": "仅标签", "source": "manual"}]))
        after = rt.read_one_any("skill_meta_row", (sid,))
        assert after[5] == cat_ids["l1"]
        assert after[6] == cat_ids["l2"]
        assert [t["name"] for t in parse_tags(after[7])] == ["仅标签"]
        assert _skill_category_payload(after[5], after[6])["l2"]["id"] == cat_ids["l2"]
    finally:
        with db_lock:
            db.execute("DELETE FROM skill WHERE id=?", (sid,))
            db.commit()


def test_skill_sync_keeps_manual_tags_and_resets_child_on_l1_change(cat_ids):
    from src.domains.mgmt.user.db import list_category_dict
    from src.domains.runtime.storage import skill_sync

    rows = list_category_dict("skill")
    l1s = [r for r in rows if r["level"] == 1]
    other_l1 = next(r for r in l1s if r["id"] != cat_ids["l1"])
    skill_name = "issue2761-sync-" + uuid.uuid4().hex[:6]
    manual_tags = encode_tags([{"name": "人工标签", "source": "manual"}])
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "scope, owner_id, is_active, cat_l1_id, cat_l2_id, tags) "
            "VALUES (?,?,?,?,?, 'official', '', 1, ?, ?, ?)",
            (skill_name, skill_name, "test", "coding", skill_name,
             cat_ids["l1"], cat_ids["l2"], manual_tags))
        db.commit()
        sid = c.lastrowid
    try:
        # 一级命中、二级未命中 → 旧二级必须被重置（不得出现跨父级脏数据）
        skill_sync._sync_skills_to_db({
            skill_name: {"name": skill_name, "category_l1": other_l1["slug"]}})
        with db_lock:
            c = db.cursor()
            c.execute("SELECT cat_l1_id, cat_l2_id, tags FROM skill WHERE id=?", (sid,))
            l1, l2, tags = c.fetchone()
        assert l1 == other_l1["id"]
        assert l2 == 0
        assert [t["name"] for t in parse_tags(tags)] == ["人工标签"]

        # 同步携带 auto 标签：manual 保留，auto 追加
        skill_sync._sync_skills_to_db({
            skill_name: {"name": skill_name, "tags": ["自动标签"]}})
        with db_lock:
            c = db.cursor()
            c.execute("SELECT cat_l1_id, cat_l2_id, tags FROM skill WHERE id=?", (sid,))
            l1, l2, tags = c.fetchone()
        parsed = parse_tags(tags)
        assert [t["name"] for t in parsed] == ["人工标签", "自动标签"]
        assert parsed[0]["source"] == "manual"
        assert parsed[1]["source"] == "auto"
        assert (l1, l2) == (other_l1["id"], 0)
    finally:
        with db_lock:
            db.execute("DELETE FROM skill WHERE id=?", (sid,))
            db.commit()


def test_skill_sync_miss_does_not_overwrite_db(cat_ids):
    from src.domains.runtime.storage import skill_sync

    skill_name = "issue2761-sync-miss-" + uuid.uuid4().hex[:6]
    tags = encode_tags([{"name": "保留", "source": "manual"}])
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "scope, owner_id, is_active, cat_l1_id, cat_l2_id, tags) "
            "VALUES (?,?,?,?,?, 'official', '', 1, ?, ?, ?)",
            (skill_name, skill_name, "test", "coding", skill_name,
             cat_ids["l1"], cat_ids["l2"], tags))
        db.commit()
        sid = c.lastrowid
    try:
        skill_sync._sync_skills_to_db({
            skill_name: {"name": skill_name,
                         "category_l1": "missing-l1", "category_l2": "missing-l2"}})
        with db_lock:
            c = db.cursor()
            c.execute("SELECT cat_l1_id, cat_l2_id, tags FROM skill WHERE id=?", (sid,))
            l1, l2, raw = c.fetchone()
        assert (l1, l2) == (cat_ids["l1"], cat_ids["l2"])
        assert [t["name"] for t in parse_tags(raw)] == ["保留"]
    finally:
        with db_lock:
            db.execute("DELETE FROM skill WHERE id=?", (sid,))
            db.commit()


def test_scene_category_l2_must_belong_to_l1(cat_ids):
    from src.domains.mgmt.routes.scenes import CreateMarketSceneRequest, create_market_scene
    from src.domains.mgmt.user.db import list_category_dict

    other_l1 = next(r for r in list_category_dict("scene")
                    if r["level"] == 1 and r["id"] != cat_ids["l1"])
    name = "issue2761-cat-mismatch-" + uuid.uuid4().hex[:6]
    body = CreateMarketSceneRequest(
        name=name, scope="repo", nodes=[],
        cat_l1_id=other_l1["id"], cat_l2_id=cat_ids["l2"])
    with pytest.raises(HTTPException) as exc:
        create_market_scene(body=body, user=USER, repo="testowner/testrepo")
    assert exc.value.status_code == 400
    assert "不属于" in exc.value.detail["message"]

    # 不存在的分类同样拒绝
    body = CreateMarketSceneRequest(name=name, scope="repo", nodes=[], cat_l2_id=99999999)
    with pytest.raises(HTTPException) as exc:
        create_market_scene(body=body, user=USER, repo="testowner/testrepo")
    assert exc.value.status_code == 400


def test_scene_update_l1_only_resets_l2_and_clear_category(cat_ids):
    from src.domains.mgmt.user.db import (
        create_scene_market, get_scene_market_by_id, update_scene_market,
    )

    name = "issue2761-clear-" + uuid.uuid4().hex[:6]
    mid = create_scene_market(
        name=name, display_name=name, scope="repo",
        repo_key="gitcode:testowner/testrepo", category_id=1,
        cat_l1_id=cat_ids["l1"], cat_l2_id=cat_ids["l2"])
    try:
        # 仅改一级：旧二级必须重置，避免跨父级不一致
        assert update_scene_market(market_id=mid, cat_l1_id=cat_ids["l1"])
        row = get_scene_market_by_id(mid)
        assert row["cat_l1_id"] == cat_ids["l1"]
        assert row["cat_l2_id"] == 0

        # 显式清空：一/二级都归零
        assert update_scene_market(market_id=mid, clear_category=True)
        row = get_scene_market_by_id(mid)
        assert row["cat_l1_id"] == 0
        assert row["cat_l2_id"] == 0
    finally:
        with db_lock:
            db.execute("DELETE FROM scene_market WHERE id=?", (mid,))
            db.commit()


def test_tag_suggest_no_generic_padding():
    from src.domains.mgmt.routes.scenes import suggest_scene_tags
    from src.domains.mgmt.routes.skills import suggest_skill_tags

    assert suggest_scene_tags(name="zzz-unknown", description="", user=USER)["tags"] == []
    assert suggest_skill_tags(name="zzz-unknown", description="", user=USER)["tags"] == []
    assert "AI" in suggest_skill_tags(name="AI 助手", description="", user=USER)["tags"]


def test_merge_tags_keeps_manual_and_replaces_auto():
    from src.kernel.tags import merge_tags

    existing = encode_tags([{"name": "人工", "source": "manual"},
                            {"name": "旧自动", "source": "auto"}])
    merged = merge_tags(existing, encode_tags([{"name": "人工", "source": "auto"},
                                               {"name": "新自动", "source": "auto"}]))
    assert merged == [{"name": "人工", "source": "manual"},
                      {"name": "新自动", "source": "auto"}]


def test_scene_sync_upsert_preserves_manual_tags():
    """同步写场景内容只补 auto 标签，不得抹掉人工标签（QA 硬违规 2）。"""
    from src.domains.mgmt import public

    name = "issue2761-sync-scene-" + uuid.uuid4().hex[:6]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_market (name, display_name, source, scope, tags, "
            "content_hash, is_active) VALUES (?,?, 'official', 'official', ?, '', 1)",
            (name, name, encode_tags([{"name": "人工场景", "source": "manual"}])))
        db.commit()
        sid = c.lastrowid
    try:
        res = public.upsert_scene_content_any(name, {
            "version": "1.0.1", "description": "sync", "content_hash": "h-2761",
            "tags": encode_tags([{"name": "自动场景", "source": "auto"}]),
        })
        assert res["action"] == "updated"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT tags FROM scene_market WHERE id=?", (sid,))
            raw = c.fetchone()[0]
        parsed = parse_tags(raw)
        assert [t["name"] for t in parsed] == ["人工场景", "自动场景"]
        assert parsed[0]["source"] == "manual"
    finally:
        with db_lock:
            db.execute("DELETE FROM scene_market WHERE id=?", (sid,))
            db.commit()


# ===== 技能写路径（upload / update）成功落库与分类校验 =====

def _valid_skill_zip(skill_name: str) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\nversion: 1.0.0\ndescription: test skill\n---\n\n## 场景\n测试技能")
    return buf.getvalue()


class _FakeSkillStorage:
    def __init__(self):
        self.objects = {}

    def backend_name(self):
        return "fake"

    def save_bytes(self, key, data):
        self.objects[key] = data
        return key

    def read_bytes(self, key):
        return self.objects.get(key)


def _patch_storage(monkeypatch):
    fake = _FakeSkillStorage()
    monkeypatch.setattr("src.domains.runtime.storage.get_storage", lambda: fake)
    return fake


def _skill_children(cat_ids):
    from src.domains.mgmt.user.db import list_category_dict

    rows = list_category_dict("skill")
    other_l1 = next(r for r in rows if r["level"] == 1 and r["id"] != cat_ids["l1"])
    other_l2 = next(r for r in rows if r["level"] == 2 and r["parent_id"] == other_l1["id"])
    return other_l1, other_l2


def _insert_user_skill(skill_name, l1=0, l2=0, tags=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "scope, owner_id, is_active, cat_l1_id, cat_l2_id, tags) "
            "VALUES (?,?,?,?,?, 'user', ?, 1, ?, ?, ?)",
            (skill_name, skill_name, "test", "coding", skill_name, str(USER["id"]), l1, l2, tags))
        db.commit()
        return c.lastrowid


def _read_skill(sid):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT cat_l1_id, cat_l2_id, tags FROM skill WHERE id=?", (sid,))
        return c.fetchone()


def _delete_skill(sid):
    with db_lock:
        db.execute("DELETE FROM skill WHERE id=?", (sid,))
        db.commit()


def test_skill_upload_rejects_invalid_category_level_and_parent(cat_ids):
    import asyncio

    from src.domains.mgmt.routes.skills import upload_skill

    other_l1, other_l2 = _skill_children(cat_ids)
    # 一级位置传二级 id：层级错误必须拒绝
    with pytest.raises(HTTPException) as exc:
        asyncio.run(upload_skill(
            file=_upload_file(b"x"), user=USER, scope="user", repo="",
            agent_type="coding", category_id=0, cat_l1_id=cat_ids["l2"],
            cat_l2_id=0, tags="", clear_category=False))
    assert exc.value.status_code == 400
    # A 一级 + B 一级下的二级：归属错误必须拒绝
    with pytest.raises(HTTPException) as exc:
        asyncio.run(upload_skill(
            file=_upload_file(b"x"), user=USER, scope="user", repo="",
            agent_type="coding", category_id=0, cat_l1_id=cat_ids["l1"],
            cat_l2_id=other_l2["id"], tags="", clear_category=False))
    assert exc.value.status_code == 400
    assert "不属于" in exc.value.detail["message"]


def test_skill_upload_success_keeps_category_when_only_tags(monkeypatch, cat_ids):
    import asyncio

    from src.domains.mgmt.routes.skills import upload_skill

    _patch_storage(monkeypatch)
    name = "issue2761-up-ok-" + uuid.uuid4().hex[:6]
    sid = _insert_user_skill(name, cat_ids["l1"], cat_ids["l2"])
    try:
        asyncio.run(upload_skill(
            file=_upload_file(_valid_skill_zip(name)), user=USER, scope="user", repo="",
            agent_type="coding", category_id=0, cat_l1_id=0, cat_l2_id=0,
            tags="仅标签", clear_category=False))
        l1, l2, raw_tags = _read_skill(sid)
        assert (l1, l2) == (cat_ids["l1"], cat_ids["l2"])
        assert [t["name"] for t in parse_tags(raw_tags)] == ["仅标签"]
    finally:
        _delete_skill(sid)


def test_skill_upload_success_resets_child_on_l1_change_and_clear(monkeypatch, cat_ids):
    import asyncio

    from src.domains.mgmt.routes.skills import upload_skill

    _patch_storage(monkeypatch)
    other_l1, _ = _skill_children(cat_ids)
    name = "issue2761-up-reset-" + uuid.uuid4().hex[:6]
    sid = _insert_user_skill(name, cat_ids["l1"], cat_ids["l2"])
    zip_bytes = _valid_skill_zip(name)
    try:
        # 换一级且未给二级：旧二级归零
        asyncio.run(upload_skill(
            file=_upload_file(zip_bytes), user=USER, scope="user", repo="",
            agent_type="coding", category_id=0, cat_l1_id=other_l1["id"],
            cat_l2_id=0, tags="", clear_category=False))
        assert _read_skill(sid)[:2] == (other_l1["id"], 0)

        # 显式清空：一/二级都归零
        asyncio.run(upload_skill(
            file=_upload_file(zip_bytes), user=USER, scope="user", repo="",
            agent_type="coding", category_id=0, cat_l1_id=0, cat_l2_id=0,
            tags="", clear_category=True))
        assert _read_skill(sid)[:2] == (0, 0)
    finally:
        _delete_skill(sid)


def test_skill_update_success_keeps_category_and_resets_child(monkeypatch, cat_ids):
    import asyncio

    from src.domains.mgmt.routes.skills import update_skill_by_id

    _patch_storage(monkeypatch)
    other_l1, _ = _skill_children(cat_ids)
    name = "issue2761-upd-ok-" + uuid.uuid4().hex[:6]
    sid = _insert_user_skill(name, cat_ids["l1"], cat_ids["l2"])
    zip_bytes = _valid_skill_zip(name)
    try:
        # 仅带 tags：分类保留
        asyncio.run(update_skill_by_id(
            sid, file=_upload_file(zip_bytes), user=USER, agent_type="", category_id="",
            cat_l1_id=0, cat_l2_id=0, tags="仅标签", clear_category=False))
        l1, l2, raw_tags = _read_skill(sid)
        assert (l1, l2) == (cat_ids["l1"], cat_ids["l2"])
        assert [t["name"] for t in parse_tags(raw_tags)] == ["仅标签"]

        # 换一级且未给二级：旧二级归零
        asyncio.run(update_skill_by_id(
            sid, file=_upload_file(zip_bytes), user=USER, agent_type="", category_id="",
            cat_l1_id=other_l1["id"], cat_l2_id=0, tags="", clear_category=False))
        assert _read_skill(sid)[:2] == (other_l1["id"], 0)

        # 只给二级：由 parent_id 反推一级
        asyncio.run(update_skill_by_id(
            sid, file=_upload_file(zip_bytes), user=USER, agent_type="", category_id="",
            cat_l1_id=0, cat_l2_id=cat_ids["l2"], tags="", clear_category=False))
        assert _read_skill(sid)[:2] == (cat_ids["l1"], cat_ids["l2"])

        # 显式清空
        asyncio.run(update_skill_by_id(
            sid, file=_upload_file(zip_bytes), user=USER, agent_type="", category_id="",
            cat_l1_id=0, cat_l2_id=0, tags="", clear_category=True))
        assert _read_skill(sid)[:2] == (0, 0)
    finally:
        _delete_skill(sid)


def test_skill_update_success_rejects_cross_parent_category(monkeypatch, cat_ids):
    import asyncio

    from src.domains.mgmt.routes.skills import update_skill_by_id

    _patch_storage(monkeypatch)
    _, other_l2 = _skill_children(cat_ids)
    name = "issue2761-upd-bad-" + uuid.uuid4().hex[:6]
    sid = _insert_user_skill(name, cat_ids["l1"], cat_ids["l2"])
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(update_skill_by_id(
                sid, file=_upload_file(_valid_skill_zip(name)), user=USER,
                agent_type="", category_id="", cat_l1_id=cat_ids["l1"],
                cat_l2_id=other_l2["id"], tags="", clear_category=False))
        assert exc.value.status_code == 400
        assert "不属于" in exc.value.detail["message"]
    finally:
        _delete_skill(sid)


def test_scene_sync_upsert_keeps_child_when_l1_unchanged(cat_ids):
    """同步仅给一级且与库内相同时不得清空既有二级（未命中不覆盖旧值）。"""
    from src.domains.mgmt import public

    name = "issue2761-sync-cat-" + uuid.uuid4().hex[:6]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_market (name, display_name, source, scope, cat_l1_id, cat_l2_id, "
            "content_hash, is_active) VALUES (?,?, 'official', 'official', ?, ?, '', 1)",
            (name, name, cat_ids["l1"], cat_ids["l2"]))
        db.commit()
        sid = c.lastrowid
    try:
        res = public.upsert_scene_content_any(name, {
            "version": "1.0.1", "description": "sync", "content_hash": "h-2761-cat",
            "cat_l1_id": cat_ids["l1"], "cat_l2_id": 0,
        })
        assert res["action"] == "updated"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT cat_l1_id, cat_l2_id FROM scene_market WHERE id=?", (sid,))
            assert c.fetchone() == (cat_ids["l1"], cat_ids["l2"])
    finally:
        with db_lock:
            db.execute("DELETE FROM scene_market WHERE id=?", (sid,))
            db.commit()


def test_skill_sync_keeps_child_when_l1_unchanged(cat_ids):
    """同步仅给一级且与库内相同：既有合法二级不得被清空。"""
    from src.domains.runtime.storage import skill_sync

    skill_name = "issue2761-sync-keep-" + uuid.uuid4().hex[:6]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, "
            "scope, owner_id, is_active, cat_l1_id, cat_l2_id) "
            "VALUES (?,?,?,?,?, 'official', '', 1, ?, ?)",
            (skill_name, skill_name, "test", "coding", skill_name,
             cat_ids["l1"], cat_ids["l2"]))
        db.commit()
        sid = c.lastrowid
    try:
        from src.domains.mgmt.user.db import list_category_dict
        slug = next(r["slug"] for r in list_category_dict("skill")
                    if r["id"] == cat_ids["l1"])
        skill_sync._sync_skills_to_db({skill_name: {"name": skill_name, "category_l1": slug}})
        with db_lock:
            c = db.cursor()
            c.execute("SELECT cat_l1_id, cat_l2_id FROM skill WHERE id=?", (sid,))
            assert c.fetchone() == (cat_ids["l1"], cat_ids["l2"])
    finally:
        _delete_skill(sid)
