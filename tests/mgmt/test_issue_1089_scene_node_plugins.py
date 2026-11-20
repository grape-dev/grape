"""Issue #1089 Slice B：scene.json node.plugins 挂点（① 流水线节点多选）测试。

Spec：scene.json 节点 schema 扩展 node.plugins（数组多选），
应用场景时触发凭据配置（credential_required 协商）。

测试 seam（公开接口）：
- scene_sync._validate_scene_nodes：node.plugins 合法/非法校验
- 场景创建/查询 API：node.plugins 落库 round-trip（nodes_json）
- apply_scene / 共享 helper：应用场景返回 credential_required
  （节点插件凭据未配置时逐插件给出缺失字段；配置后消失）
"""
import os
import uuid

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.domains.runtime.storage.scene_sync import _validate_scene_nodes  # noqa: E402
from src.domains.mgmt.plugins import registry  # noqa: E402
from src.domains.mgmt.routes.scenes import (  # noqa: E402
    ApplySceneRequest, SceneNodeInput, CreateSceneRequest,
    apply_scene, create_scene, _collect_plugin_credential_requirements,
)

_BASE_NODE = {"id": "coding", "tag": "coding", "mode": "emit", "next": "done",
              "handoff": "complete"}


def _chain(nodes_spec):
    """构建合法 next 链（每个节点串到 done）。"""
    nodes = []
    for i, spec in enumerate(nodes_spec):
        n = dict(_BASE_NODE)
        n.update(spec)
        if i < len(nodes_spec) - 1:
            n["next"] = nodes_spec[i + 1]["id"]
            n["handoff"] = "a2a"
        nodes.append(n)
    return nodes


def _unique_repo():
    return f"issue1089-{uuid.uuid4().hex[:10]}"


class TestValidateSceneNodePlugins:
    def test_valid_plugins_pass(self):
        nodes = _chain([
            {"id": "coding", "tag": "coding", "skill": "s1", "plugins": ["obs-upload", "git-pr"]},
            {"id": "qa", "tag": "qa", "skill": "s2", "plugins": []},
        ])
        assert _validate_scene_nodes(nodes) == []

    def test_plugins_without_field_pass(self):
        nodes = _chain([
            {"id": "coding", "tag": "coding", "skill": "s1"},
            {"id": "qa", "tag": "qa", "skill": "s2"},
        ])
        assert _validate_scene_nodes(nodes) == []

    def test_plugins_not_list_rejected(self):
        nodes = _chain([
            {"id": "coding", "tag": "coding", "skill": "s1", "plugins": "obs-upload"},
            {"id": "qa", "tag": "qa", "skill": "s2"},
        ])
        errors = _validate_scene_nodes(nodes)
        assert any("plugins" in e for e in errors)

    def test_plugins_empty_entry_rejected(self):
        nodes = _chain([
            {"id": "coding", "tag": "coding", "skill": "s1", "plugins": ["ok", "  "]},
            {"id": "qa", "tag": "qa", "skill": "s2"},
        ])
        errors = _validate_scene_nodes(nodes)
        assert any("plugins" in e for e in errors)

    def test_plugins_duplicate_in_node_rejected(self):
        nodes = _chain([
            {"id": "coding", "tag": "coding", "skill": "s1", "plugins": ["p1", "p1"]},
            {"id": "qa", "tag": "qa", "skill": "s2"},
        ])
        errors = _validate_scene_nodes(nodes)
        assert any("plugins" in e for e in errors)


class TestSceneNodePluginsRoundTrip:
    def test_create_scene_persists_node_plugins(self):
        repo = _unique_repo()
        body = CreateSceneRequest(
            name=f"场景-{repo}", nodes=[
                SceneNodeInput(id="coding", tag="coding", skill="s1",
                               next="done", handoff="complete",
                               plugins=["obs-upload"]),
                SceneNodeInput(id="qa", tag="qa", skill="s2",
                               next="done", handoff="complete"),
            ])
        resp = create_scene(body, user={"id": 1, "username": "u1089"}, repo=f"o1089/{repo}")
        scene_id = resp["scene_id"]
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                (scene_id, f"gitcode:o1089/{repo}")).fetchone()
        assert row, "场景未落库"
        import json
        nodes = json.loads(row[0])
        assert nodes[0]["plugins"] == ["obs-upload"]
        assert nodes[1]["plugins"] == []


def _install_plugin(name="cred-req-plugin", fields=None):
    registry.install_plugin({
        "name": name,
        "version": "1.0.0",
        "display_name": f"插件 {name}",
        "credentials": fields if fields is not None else [
            {"key": "ak", "label": "Access Key", "required": True},
            {"key": "sk", "label": "Secret Key", "required": False},
        ],
    })


def _cleanup_repo(repo_key):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM user_credentials WHERE provider LIKE 'plugin:%'", ())
        db.commit()


class TestApplyCredentialNegotiation:

    def test_apply_scene_reports_missing_credentials(self):
        _install_plugin("cred-req-plugin")
        repo = _unique_repo()
        repo_key = f"gitcode:o1089/{repo}"
        scene_id = f"custom-plug-{uuid.uuid4().hex[:6]}"
        from src.api.pipeline_config import create_custom_scene
        create_custom_scene(
            scene_id=scene_id, name=scene_id, description="",
            repo_key=repo_key, nodes=_chain([
                {"id": "coding", "tag": "coding", "skill": "s1",
                 "plugins": ["cred-req-plugin"]},
                {"id": "qa", "tag": "qa", "skill": "s2"},
            ]))
        try:
            resp = apply_scene(
                "o1089", repo, ApplySceneRequest(scene_id=scene_id),
                {"id": 991, "username": "u1089"})
            req = resp.get("credential_required") or []
            assert len(req) == 1, f"应提示 1 个插件待配凭据，实际 {req}"
            entry = req[0]
            assert entry["plugin"] == "cred-req-plugin"
            missing_keys = {f["key"] for f in entry["missing_fields"]}
            assert missing_keys == {"ak", "sk"}  # 均未配置（含可选字段）
            assert entry["required_fields"] == ["ak"]
        finally:
            _cleanup_repo(repo_key)

    def test_configured_credentials_not_required(self):
        from src.domains.mgmt.user.db import set_credential
        _install_plugin("cred-req-plugin")
        repo = _unique_repo()
        repo_key = f"gitcode:o1089/{repo}"
        set_credential(991, "plugin:cred-req-plugin", {"ak": "AK", "sk": "SK"})
        scene_id = f"custom-plug-{uuid.uuid4().hex[:6]}"
        from src.api.pipeline_config import create_custom_scene
        create_custom_scene(
            scene_id=scene_id, name=scene_id, description="",
            repo_key=repo_key, nodes=_chain([
                {"id": "coding", "tag": "coding", "skill": "s1",
                 "plugins": ["cred-req-plugin"]},
                {"id": "qa", "tag": "qa", "skill": "s2"},
            ]))
        try:
            resp = apply_scene(
                "o1089", repo, ApplySceneRequest(scene_id=scene_id),
                {"id": 991, "username": "u1089"})
            assert (resp.get("credential_required") or []) == []
        finally:
            _cleanup_repo(repo_key)

    def test_unknown_plugin_never_blocks_apply(self):
        """未注册插件：软引用不阻塞应用场景，仅无凭据要求。"""
        repo = _unique_repo()
        repo_key = f"gitcode:o1089/{repo}"
        scene_id = f"custom-plug-{uuid.uuid4().hex[:6]}"
        from src.api.pipeline_config import create_custom_scene
        create_custom_scene(
            scene_id=scene_id, name=scene_id, description="",
            repo_key=repo_key, nodes=_chain([
                {"id": "coding", "tag": "coding", "skill": "s1",
                 "plugins": ["ghost-plugin"]},
                {"id": "qa", "tag": "qa", "skill": "s2"},
            ]))
        try:
            resp = apply_scene(
                "o1089", repo, ApplySceneRequest(scene_id=scene_id),
                {"id": 991, "username": "u1089"})
            assert (resp.get("credential_required") or []) == []
        finally:
            _cleanup_repo(repo_key)


class TestCredentialRequirementHelper:
    def test_helper_merges_nodes_and_reports(self):
        _install_plugin("cred-req-plugin")
        reqs = _collect_plugin_credential_requirements(
            [{"plugins": ["cred-req-plugin"]}, {"plugins": ["cred-req-plugin"]}],
            user_id=991)
        assert len(reqs) == 1  # 跨节点去重
        assert reqs[0]["plugin"] == "cred-req-plugin"

    def test_helper_empty_nodes(self):
        assert _collect_plugin_credential_requirements([], user_id=1) == []


class TestCredentialNegotiationScopes:
    """验收3：apply 凭据协商走继承链合并（node 多选 + agent 类型种子，A4 seam）。"""

    def test_agent_scope_seeds_join_negotiation(self, monkeypatch):
        # 挂点③：节点 tag 对应 Agent 类型的种子默认插件参与协商（此前仅查节点级多选）
        monkeypatch.setattr(
            "src.domains.mgmt.plugins.resolver._load_agent_description",
            lambda: {"coding": {"name": "Coding", "plugins": ["agent-seed-plugin"]}})
        _install_plugin("agent-seed-plugin", fields=[
            {"key": "token", "label": "Token", "required": True}])
        reqs = _collect_plugin_credential_requirements(
            [{"tag": "coding", "plugins": []}], user_id=991)
        assert [r["plugin"] for r in reqs] == ["agent-seed-plugin"]
        assert reqs[0]["required_fields"] == ["token"]

    def test_node_and_agent_scope_merge_dedup_with_priority(self, monkeypatch):
        # 同名插件同时出现在 node/agent 作用域 → merge_plugin_scopes 去重，单条协商
        monkeypatch.setattr(
            "src.domains.mgmt.plugins.resolver._load_agent_description",
            lambda: {"coding": {"name": "Coding", "plugins": ["cred-req-plugin"]}})
        _install_plugin("cred-req-plugin")
        reqs = _collect_plugin_credential_requirements(
            [{"tag": "coding", "plugins": ["cred-req-plugin"]}], user_id=991)
        assert len(reqs) == 1
        assert reqs[0]["plugin"] == "cred-req-plugin"

    def test_merged_credentials_via_resolve_cover_missing(self, monkeypatch):
        # 凭据值经 resolve_plugin_credentials 键级合并（个人级已配置 → 不再要求）
        monkeypatch.setattr(
            "src.domains.mgmt.plugins.resolver._load_agent_description",
            lambda: {"qa": {"name": "QA", "plugins": ["cred-req-plugin"]}})
        from src.domains.mgmt.user.db import set_credential
        _install_plugin("cred-req-plugin")
        set_credential(991, "plugin:cred-req-plugin", {"ak": "AK", "sk": "SK"})
        reqs = _collect_plugin_credential_requirements(
            [{"tag": "qa", "plugins": []}], user_id=991)
        assert reqs == []
        set_credential(991, "plugin:cred-req-plugin", {})

    def test_agent_scope_unknown_plugin_soft_skipped(self, monkeypatch):
        # Agent 种子引用未注册插件：软引用口径不阻塞、不产生协商条目
        monkeypatch.setattr(
            "src.domains.mgmt.plugins.resolver._load_agent_description",
            lambda: {"sre": {"name": "SRE", "plugins": ["ghost-seed-plugin"]}})
        reqs = _collect_plugin_credential_requirements(
            [{"tag": "sre", "plugins": []}], user_id=991)
        assert reqs == []
