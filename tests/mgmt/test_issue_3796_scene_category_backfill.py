"""Issue #3796（PR #1982 审查 Required）：内容未变时分类目标变更仍需落库。

背景：`scene_sync` 解析出的 `cat_l1_id/cat_l2_id/category_id` 经 `_persist_scene_row`
写入，而 `upsert_scene_content*` 在 `content_hash` 与库内一致时直接 `skipped` 不执行
UPDATE → 线上存量旧 slug 场景（scene.json 未改、hash 相同）分类永不停更，
Spec 验收第 2 条「同步一轮后旧 slug 场景 cat_l1/cat_l2 落 1xxx」不成立。

锁住的契约：
- hash 一致但分类目标与库内不同 → **一次窄 UPDATE**（只动分类列）并返回
  `action="backfilled"` + `backfilled=[列名]`；内容列（version/description 等）不动；
- 分类目标与库内一致 → 仍返回 `"skipped"`（幂等层不因回填逻辑变成每轮全量写）；
- 未命中语义不变：分类列缺席 / `category_id=0` → 保留库内旧值，绝不写 0 覆盖；
- `cat_l1_id` 回填沿用 `sync_category_target` 口径（一级变更时重置旧二级）。
"""
import uuid

import pytest

from src.kernel.session_manager import db, db_lock


def _insert_scene(name: str, *, content_hash: str, cat_l1: int = 0, cat_l2: int = 0,
                  category_id: int = 0, version: str = "0.9.0", description: str = "old") -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_market (name, display_name, version, description, source, scope, "
            "cat_l1_id, cat_l2_id, category_id, content_hash, is_active) "
            "VALUES (?,?,?,?, 'official', 'official', ?,?,?,?, 1)",
            (name, name, version, description, cat_l1, cat_l2, category_id, content_hash))
        db.commit()
        sid = c.lastrowid
    return sid


def _row(sid: int):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT version, description, cat_l1_id, cat_l2_id, category_id, content_hash "
                  "FROM scene_market WHERE id=?", (sid,))
        return c.fetchone()


def _delete(sid: int) -> None:
    with db_lock:
        db.execute("DELETE FROM scene_market WHERE id=?", (sid,))
        db.commit()


@pytest.fixture
def scene_name():
    name = "issue3796-backfill-" + uuid.uuid4().hex[:8]
    created: list[int] = []
    yield name, created
    for sid in created:
        _delete(sid)


def _created(scene_name):
    _, created = scene_name
    return created


def test_upsert_any_backfills_category_when_hash_unchanged(scene_name):
    """hash 相同 + 分类目标变更 → 执行窄 UPDATE，内容列原样保留。"""
    from src.domains.mgmt import public

    name, created = scene_name
    sid = _insert_scene(name, content_hash="hash-same", cat_l1=1002, cat_l2=100201,
                        category_id=9)
    created.append(sid)

    res = public.upsert_scene_content_any(name, {
        "content_hash": "hash-same", "version": "1.0.0", "description": "sync",
        "category_id": 5, "cat_l1_id": 1001, "cat_l2_id": 100109,
    })

    assert res["action"] == "backfilled"
    assert sorted(res["backfilled"]) == ["cat_l1_id", "cat_l2_id", "category_id"]
    # 只动分类列：version/description 保持库内旧值
    assert _row(sid) == ("0.9.0", "old", 1001, 100109, 5, "hash-same")


def test_upsert_any_skips_when_category_target_unchanged(scene_name):
    """分类目标与库内一致 → 仍 skipped（不让回填逻辑变成每轮全量 UPDATE）。"""
    from src.domains.mgmt import public

    name, created = scene_name
    sid = _insert_scene(name, content_hash="hash-same", cat_l1=1001, cat_l2=100109,
                        category_id=5)
    created.append(sid)

    res = public.upsert_scene_content_any(name, {
        "content_hash": "hash-same", "version": "1.0.0",
        "category_id": 5, "cat_l1_id": 1001, "cat_l2_id": 100109,
    })

    assert res["action"] == "skipped"
    assert _row(sid) == ("0.9.0", "old", 1001, 100109, 5, "hash-same")


def test_upsert_any_keeps_old_category_on_unresolved_slug(scene_name):
    """未命中（分类列缺席 / category_id=0）→ 保留库内旧值，不写 0 覆盖。"""
    from src.domains.mgmt import public

    name, created = scene_name
    sid = _insert_scene(name, content_hash="hash-same", cat_l1=1001, cat_l2=100109,
                        category_id=5)
    created.append(sid)

    # 未命中路径：只带 category_id=0，cat_l1/cat_l2 缺席
    res = public.upsert_scene_content_any(name, {
        "content_hash": "hash-same", "version": "1.0.0", "category_id": 0,
    })

    assert res["action"] == "skipped"
    assert _row(sid)[2:5] == (1001, 100109, 5)


def test_upsert_any_resets_child_when_l1_changes_on_backfill(scene_name):
    """回填沿用 sync_category_target：一级变更且来源未给二级 → 旧二级重置为 0。"""
    from src.domains.mgmt import public

    name, created = scene_name
    sid = _insert_scene(name, content_hash="hash-same", cat_l1=1010, cat_l2=101002)
    created.append(sid)

    res = public.upsert_scene_content_any(name, {
        "content_hash": "hash-same", "version": "1.0.0", "cat_l1_id": 1001, "cat_l2_id": 0,
    })

    assert res["action"] == "backfilled"
    assert sorted(res["backfilled"]) == ["cat_l1_id", "cat_l2_id"]
    assert _row(sid)[2:4] == (1001, 0)


class _FakeCursor:
    """MySQL/lib 路径（%s 占位符）的假游标：只记录 SQL，不连库。"""

    rowcount = 0  # 与原实现 UPDATE 后取 rowcount 的口径一致

    def __init__(self, row):
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params or ())))
        return self

    def fetchone(self):
        return self.row


def _fake_mgmt_cursor(row):
    from unittest.mock import MagicMock, patch

    from src.domains.mgmt import public

    cur = _FakeCursor(row)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return patch.object(public.db_registry, "cursor", return_value=ctx), cur


def test_upsert_content_lib_path_backfills_only_category_columns():
    """三库拓扑（lib/MySQL 路径）同样在 skipped 层比对分类目标，只发分类列 UPDATE。"""
    from src.domains.mgmt import public

    patcher, cur = _fake_mgmt_cursor(row=(42, "hash-same", "", 1010, 101002, 3))
    with patcher:
        res = public.upsert_scene_content("lib-scene", {
            "content_hash": "hash-same", "version": "1.0.0", "description": "sync",
            "category_id": 3, "cat_l1_id": 1001, "cat_l2_id": 100109,
        })

    assert res["action"] == "backfilled"
    assert res["id"] == 42
    assert sorted(res["backfilled"]) == ["cat_l1_id", "cat_l2_id"]
    updates = [(sql, params) for sql, params in cur.calls if sql.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "cat_l1_id=%s" in sql and "cat_l2_id=%s" in sql
    assert "version=%s" not in sql and "description=%s" not in sql and "content_hash=%s" not in sql
    assert params == (1001, 100109, 42)


def test_upsert_content_lib_path_skips_when_category_unchanged():
    """lib 路径分类一致时保持原幂等语义（不产生任何 UPDATE）。"""
    from src.domains.mgmt import public

    patcher, cur = _fake_mgmt_cursor(row=(42, "hash-same", "", 1001, 100109, 3))
    with patcher:
        res = public.upsert_scene_content("lib-scene", {
            "content_hash": "hash-same", "version": "1.0.0",
            "category_id": 3, "cat_l1_id": 1001, "cat_l2_id": 100109,
        })

    assert res["action"] == "skipped"
    assert not [sql for sql, _ in cur.calls if sql.lstrip().upper().startswith("UPDATE")]