"""场景技能档位锁定回归（2026-11-13）：三档同名时 ref 表必须落在用户选中的那一条。

背景：前端下的拉早已按 skill_id 区分（#978），后端节点模型也有 skill_id（#979），
但 `_rebuild_scene_skill_ref` 一直只按名字重解析（official → 任意 repo），
导致「界面选 repoB、落库却是 official」。本测试固化修复后的解析顺序：

  1. 节点带 skill_id 且有效 → 直接用它（权威）
  2. 无 id 时按名字回落：repo_key 命中本仓 repo 档 → official → 任意 repo → user
  3. skill_id 失效 → 回落名字解析并告警（不静默 0）
  4. 名字查不到 → 0（保持既有兜底语义）

跑法：.venv/bin/python -m pytest -q tests/mgmt/test_scene_skill_ref_authoritative.py
"""
import uuid

import pytest

from src.kernel.session_manager import db, db_lock

TABLE = "scene_skill_ref"


def _mk_skill(name, scope, owner="", agent_type="coding", active=1):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, agent_type, skill_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,?)",
            (name, agent_type, f"/skills/{name}", scope, str(owner), int(active)))
        db.commit()
        row = c.execute(
            "SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? "
            "ORDER BY id DESC LIMIT 1", (name, scope, str(owner))).fetchone()
    assert row, f"insert skill failed {name}/{scope}"
    return int(row[0])


def _cleanup(name, scene_ids):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name=?", (name,))
        for sid in scene_ids:
            c.execute(f"DELETE FROM {TABLE} WHERE scene_id=?", (sid,))
        db.commit()


def _refs(scene_id):
    with db_lock:
        rows = db.cursor().execute(
            f"SELECT skill_id, skill_name FROM {TABLE} WHERE scene_id=?", (scene_id,)).fetchall()
    return [(int(r[0] or 0), r[1]) for r in rows]


@pytest.fixture()
def three_scopes():
    """official / repo:A / repo:B / user 四条同名技能 + 独立场景 id。"""
    name = "tid-skill-" + uuid.uuid4().hex[:6]
    repo_a = "gitcode:acme/aa-" + uuid.uuid4().hex[:4]
    repo_b = "gitcode:acme/bb-" + uuid.uuid4().hex[:4]
    ids = {
        "official": _mk_skill(name, "official"),
        "repo_a": _mk_skill(name, "repo", repo_a),
        "repo_b": _mk_skill(name, "repo", repo_b),
        "user": _mk_skill(name, "user", "u-1"),
    }
    scene_ids = [900000 + i for i in (1, 2, 3, 4, 5)]
    yield {"name": name, "ids": ids, "repo_a": repo_a, "repo_b": repo_b, "scenes": scene_ids}
    _cleanup(name, scene_ids)


def _rebuild():
    from src.domains.runtime.storage.scene_sync import _rebuild_scene_skill_ref
    return _rebuild_scene_skill_ref


def test_explicit_skill_id_wins(three_scopes):
    """节点带 skill_id=repoB → ref 落 repoB（不再被 official 覆盖）。"""
    sid = three_scopes["scenes"][0]
    _rebuild()(sid, [{"id": "coding", "tag": "coding",
                      "skill": three_scopes["name"],
                      "skill_id": three_scopes["ids"]["repo_b"]}],
               "probe", three_scopes["repo_b"])
    assert _refs(sid) == [(three_scopes["ids"]["repo_b"], three_scopes["name"])]


def test_name_fallback_prefers_scene_repo(three_scopes):
    """无 skill_id（老场景/老前端）→ 名字解析优先本仓 repo 档，而不是 official。"""
    sid = three_scopes["scenes"][1]
    _rebuild()(sid, [{"id": "coding", "tag": "coding", "skill": three_scopes["name"]}],
               "probe", three_scopes["repo_b"])
    assert _refs(sid) == [(three_scopes["ids"]["repo_b"], three_scopes["name"])]


def test_name_fallback_without_repo_keeps_official(three_scopes):
    """无仓库上下文（市场模板/老数据）→ 维持 official 优先的原语义。"""
    sid = three_scopes["scenes"][2]
    _rebuild()(sid, [{"id": "coding", "tag": "coding", "skill": three_scopes["name"]}], "probe", "")
    assert _refs(sid) == [(three_scopes["ids"]["official"], three_scopes["name"])]


def test_inactive_skill_id_degrades_by_name(three_scopes):
    """skill_id 指向已停用技能 → 回落名字解析（不写 0、不静默丢）。"""
    sid = three_scopes["scenes"][3]
    dead = _mk_skill(three_scopes["name"], "repo", three_scopes["repo_a"] + "-dead", active=0)
    _rebuild()(sid, [{"id": "coding", "tag": "coding",
                      "skill": three_scopes["name"], "skill_id": dead}],
               "probe", three_scopes["repo_b"])
    # 名字回落：本仓 repoB（不是那条停用的 repoA，也不是 official）
    assert _refs(sid) == [(three_scopes["ids"]["repo_b"], three_scopes["name"])]


def test_unknown_name_still_zero(three_scopes):
    """名字查不到（官方内置包形态）→ skill_id=0，保持既有兼容语义。"""
    sid = three_scopes["scenes"][4]
    _rebuild()(sid, [{"id": "coding", "tag": "coding", "skill": "not-exist-" + uuid.uuid4().hex[:6]}],
               "probe", three_scopes["repo_b"])
    assert _refs(sid) == [(0, _refs(sid)[0][1])]

# ---------------------------------------------------------------- 详情取 id 的口径

def test_detail_prefers_node_skill_id_over_ref():
    """详情展示：节点自带 skill_id 优先于 ref 表（ref 是按名重建，可能已被覆盖）。"""
    from src.domains.mgmt.routes.scenes import _pick_node_skill_id
    ref_map = {"review-skill": 101}          # ref 表里落的是 official
    assert _pick_node_skill_id({"skill": "review-skill", "skill_id": 103}, ref_map) == 103
    # 老场景（节点无 id）→ 回退 ref
    assert _pick_node_skill_id({"skill": "review-skill"}, ref_map) == 101
    # 都没有 → 0
    assert _pick_node_skill_id({"skill": "unknown"}, ref_map) == 0
    # 脏值（非数字）→ 视为无 id，回退 ref
    assert _pick_node_skill_id({"skill": "review-skill", "skill_id": "x"}, ref_map) == 101
