"""Issue #955：场景节点 tag 无角色枚举校验导致派发引擎绑定静默失效。

验证点：
1. 写侧：_validate_scene_node_roles / create_scene / update_scene 对节点 id
   （角色名）做枚举校验（∈ _KNOWN_AGENT_ROLES ∪ {"chat"}），非法 → 400
   （报错含非法值与角色全集）；tag=中文阶段名 + id=角色名正常放行。
2. 读侧：get_scene_engine_binding / get_node_by_tag 改 id 优先、tag 兜底，
   非标准 tag（中文阶段名）场景的引擎绑定恢复。
3. 合成回归场景（tag=中文阶段名、id=角色名、engine_runtime_ids=[eng]）：
   es.get_scene_pool_engine_ids 返回限定名单（含指定引擎），不再全量广播。
4. _validate_scene_engine_ids 按节点 id（角色名）校验引擎能力标签，
   堵住 tag='all' 通配（仅含 'all' 能力不再放行）。

注：redispatch 46108 对照为线上环境回归（真实引擎 ID），此处以合成场景等价复现。
"""
import datetime
import json

import pytest
from fastapi import HTTPException

from src.session_manager import db, db_lock
from src.runtime import engine_select as es


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    with db_lock:
        c = db.cursor()
        for t in ("agent_runtimes", "runtime_credentials", "devops_pipeline_scene",
                   "unified_user_mapping", "user_repos", "engine_capabilities"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE '955/%' OR repo_key LIKE 'gitcode:955/%'")
            except Exception:
                pass
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        for t in ("agent_runtimes", "runtime_credentials", "devops_pipeline_scene",
                   "unified_user_mapping", "user_repos", "engine_capabilities"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE '955/%' OR repo_key LIKE 'gitcode:955/%'")
            except Exception:
                pass
        db.commit()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, scope, owner_id, "tester"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, cap_tags=("all",), status="online", healthy=1) -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, 1, _now(), "global", ""))
        rid = c.lastrowid
        for t in cap_tags:
            c.execute(
                "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)",
                (rid, t))
        db.commit()
        return rid


def _mk_scene(scene_id, nodes, repo_key="955/repo", user_id=1):
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) "
                      "VALUES (?, 'gitcode:955/repo', '955', 'repo', 1)", (user_id,))
        except Exception:
            pass
        db.commit()
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                        repo_key=repo_key, nodes=nodes)


class TestSceneNodeRoleValidation:
    """Issue #955 写侧：节点 id 角色枚举校验（非法 → 400）。"""

    def test_invalid_role_rejected_400(self):
        from src.api.routes.scenes import _validate_scene_node_roles
        with pytest.raises(HTTPException) as ei:
            _validate_scene_node_roles([{"id": "prepare", "tag": "准备工作"}])
        assert ei.value.status_code == 400
        msg = ei.value.detail["message"]
        assert "prepare" in msg
        for role in ("chat", "coding", "issue", "qa", "sre", "vod"):
            assert role in msg

    def test_chinese_tag_with_valid_id_passes(self):
        from src.api.routes.scenes import _validate_scene_node_roles
        _validate_scene_node_roles([{"id": "vod", "tag": "准备工作", "skill": "vod"}])

    def test_chat_role_passes(self):
        from src.api.routes.scenes import _validate_scene_node_roles
        _validate_scene_node_roles([{"id": "chat", "tag": "对话阶段"}])

    def test_create_scene_invalid_role_400(self):
        from src.api.routes.scenes import create_scene, CreateSceneRequest, SceneNodeInput
        body = CreateSceneRequest(
            name="t955", nodes=[SceneNodeInput(id="prepare", tag="准备工作", skill="vod")])
        with pytest.raises(HTTPException) as ei:
            create_scene(body, user={"username": "tester", "id": 1}, repo="955/repo")
        assert ei.value.status_code == 400
        assert "prepare" in ei.value.detail["message"]

    def test_update_scene_invalid_role_400(self):
        from src.api.pipeline_config import create_custom_scene
        from src.api.routes.scenes import update_scene, UpdateSceneRequest, SceneNodeInput
        create_custom_scene(scene_id="s955-upd", name="s955-upd", description="",
                            repo_key="gitcode:955/repo",
                            nodes=[{"id": "vod", "tag": "vod", "skill": "vod"}])
        body = UpdateSceneRequest(
            nodes=[SceneNodeInput(id="bad_role", tag="阶段", skill="vod")])
        with pytest.raises(HTTPException) as ei:
            update_scene("s955-upd", body, user={"username": "tester", "id": 1}, repo="955/repo")
        assert ei.value.status_code == 400
        assert "bad_role" in ei.value.detail["message"]

    def test_role_source_single_definition(self):
        """角色全集统一引用 engine_select._KNOWN_AGENT_ROLES，避免重复定义漂移。"""
        from src.api.routes.scenes import _validate_scene_node_roles
        from src.runtime.engine_select import _KNOWN_AGENT_ROLES
        with pytest.raises(HTTPException) as ei:
            _validate_scene_node_roles([{"id": "drift_probe_role", "tag": "x"}])
        msg = ei.value.detail["message"]
        for role in _KNOWN_AGENT_ROLES:
            assert role in msg


class TestEngineBindingIdFirst:
    """Issue #955 读侧：get_scene_engine_binding / get_node_by_tag id 优先、tag 兜底。"""

    def test_binding_chinese_tag_id_role_bound(self):
        eng = _mk_engine("opencode-955a", credential_id=_mk_cred("c-955a"), cap_tags=("vod",))
        _mk_scene("s955-bind", [{"id": "vod", "tag": "准备工作", "skill": "vod",
                                 "mode": "emit", "next": "done", "handoff": "complete",
                                 "engine_runtime_ids": [eng]}])
        from src.api.pipeline_config import get_scene_engine_binding
        b = get_scene_engine_binding("s955-bind", "vod", repo_key="955/repo", user_id=1)
        assert b["status"] == "bound"
        assert b["engine_ids"] == [eng]

    def test_binding_tag_fallback_legacy(self):
        """id 不匹配但 tag 匹配（存量语义）→ tag 兜底仍生效。"""
        eng = _mk_engine("opencode-955b", credential_id=_mk_cred("c-955b"), cap_tags=("issue",))
        _mk_scene("s955-fb", [{"id": "stage_1", "tag": "issue", "skill": "issue-advance",
                               "mode": "emit", "next": "done", "handoff": "complete",
                               "engine_runtime_ids": [eng]}])
        from src.api.pipeline_config import get_scene_engine_binding
        b = get_scene_engine_binding("s955-fb", "issue", repo_key="955/repo", user_id=1)
        assert b["status"] == "bound"
        assert b["engine_ids"] == [eng]

    def test_get_node_by_tag_id_first(self):
        """tag=中文阶段名 / id=角色名 → get_node_by_tag 按角色解析到技能节点。"""
        _mk_scene("s955-nbt", [
            {"id": "vod", "tag": "需求收敛", "skill": "vod", "mode": "emit",
             "next": "coding", "handoff": "a2a"},
            {"id": "coding", "tag": "开发阶段", "skill": "implementation", "mode": "emit",
             "next": "done", "handoff": "complete"}])
        from src.api.pipeline_config import get_node_by_tag
        node = get_node_by_tag("s955-nbt", "coding", repo_key="955/repo")
        assert node is not None
        assert node["skill"] == "implementation"

    def test_get_node_by_tag_tag_fallback(self):
        _mk_scene("s955-nbt2", [
            {"id": "stage_x", "tag": "coding", "skill": "impl-x", "mode": "emit",
             "next": "done", "handoff": "complete"}])
        from src.api.pipeline_config import get_node_by_tag
        node = get_node_by_tag("s955-nbt2", "coding", repo_key="955/repo")
        assert node is not None
        assert node["skill"] == "impl-x"


class TestSyntheticDispatchNoBroadcast:
    """合成回归场景：非标准 tag 派发不再全量广播（pool 含指定引擎）。"""

    def test_pool_engine_ids_contains_bound_engine(self):
        eng = _mk_engine("opencode-955p", credential_id=_mk_cred("c-955p"), cap_tags=("vod",))
        _mk_scene("s955-pool", [{"id": "vod", "tag": "准备工作", "skill": "vod",
                                 "mode": "emit", "next": "done", "handoff": "complete",
                                 "engine_runtime_ids": [eng]}])
        result = es.get_scene_pool_engine_ids("s955-pool", "vod", repo_key="955/repo", user_id=1)
        assert result is not None and json.loads(result) == [eng]

    def test_pool_engine_ids_tag_fallback_legacy(self):
        eng = _mk_engine("opencode-955q", credential_id=_mk_cred("c-955q"), cap_tags=("qa",))
        _mk_scene("s955-pool2", [{"id": "st1", "tag": "qa", "skill": "qa",
                                  "mode": "emit", "next": "done", "handoff": "complete",
                                  "engine_runtime_ids": [eng]}])
        result = es.get_scene_pool_engine_ids("s955-pool2", "qa", repo_key="955/repo", user_id=1)
        assert result is not None and json.loads(result) == [eng]

    def test_role_only_node_unbound_broadcast(self):
        """id=角色名、未绑定引擎 → unbound 保持全量广播（存量语义）。"""
        _mk_scene("s955-unb", [{"id": "vod", "tag": "准备工作", "skill": "vod",
                                "mode": "emit", "next": "done", "handoff": "complete",
                                "engine_runtime_ids": []}])
        assert es.get_scene_pool_engine_ids("s955-unb", "vod", repo_key="955/repo", user_id=1) == ""


class TestValidateSceneEngineIdsCapability:
    """Issue #955：能力标签按节点 id（角色）校验，堵住 tag='all' 通配。"""

    def test_all_capability_wildcard_blocked(self):
        """引擎仅有 tag='all' 能力 → 不再放行（原通配漏洞）。"""
        eng = _mk_engine("opencode-955w", credential_id=_mk_cred("c-955w"), cap_tags=("all",))
        from src.api.routes.scenes import _validate_scene_engine_ids
        with pytest.raises(HTTPException) as ei:
            _validate_scene_engine_ids(
                [{"id": "coding", "tag": "开发阶段", "skill": "implementation",
                  "engine_runtime_ids": [eng]}], "gitcode:955/repo")
        assert ei.value.status_code == 400
        assert "coding" in ei.value.detail["message"]

    def test_role_capability_passes_with_chinese_tag(self):
        """引擎有角色能力标签 + 节点 tag 为中文阶段名 → 校验按 id 角色通过。"""
        eng = _mk_engine("opencode-955r", credential_id=_mk_cred("c-955r"), cap_tags=("coding",))
        from src.api.routes.scenes import _validate_scene_engine_ids
        _validate_scene_engine_ids(
            [{"id": "coding", "tag": "开发阶段", "skill": "implementation",
              "engine_runtime_ids": [eng]}], "gitcode:955/repo")

    def test_display_tag_capability_not_accepted(self):
        """引擎只挂展示 tag（中文阶段名）能力 → 按角色校验不通过。"""
        eng = _mk_engine("opencode-955t", credential_id=_mk_cred("c-955t"), cap_tags=("开发阶段",))
        from src.api.routes.scenes import _validate_scene_engine_ids
        with pytest.raises(HTTPException) as ei:
            _validate_scene_engine_ids(
                [{"id": "coding", "tag": "开发阶段", "skill": "implementation",
                  "engine_runtime_ids": [eng]}], "gitcode:955/repo")
        assert ei.value.status_code == 400

    def test_tag_only_legacy_payload_sre_guard_kept(self):
        """纯函数历史调用（仅 tag 无 id）兜底仍生效：SRE 空 400。"""
        from src.api.routes.scenes import _validate_scene_engine_ids
        with pytest.raises(HTTPException) as ei:
            _validate_scene_engine_ids(
                [{"tag": "sre", "enabled": True, "engine_runtime_ids": []}],
                "gitcode:955/repo")
        assert ei.value.status_code == 400
