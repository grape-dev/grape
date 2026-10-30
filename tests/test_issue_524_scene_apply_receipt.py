"""回归测试（Issue #524）：场景 apply 按节点结构化回执。

背景：src/api/routes/scenes.py apply_scene 旧实现中，目标引擎 online 但
healthy=false 时不命中选择，静默回退 devops_agent_session 旧绑定（引擎 A），
回执仍报 updated=N 全部成功，无跳过提示。

修复后：
- apply_scene 返回 nodes: [{agent_type, skill, status, effective_runtime_id,
  declared_runtime_ids, reason}]，仅 applied 节点计入 updated；
- 声明引擎不健康 → 该节点 skipped，reason 提示当前仍生效 runtime；
- strict=true 时任一节点 skipped → 整单拒绝（409，不落库）；
- agents detail 增加 declared_runtime_ids（声明配置）与 effective_runtime_id
  （生效配置）区分字段。

测试风格对齐 tests/test_health_scorer.py（按场景分类 + 中文 docstring）。
"""
import asyncio
import os
import sys
import uuid

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi import HTTPException  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402
from src.api.routes.scenes import apply_scene, ApplySceneRequest  # noqa: E402
from src.api.pipeline_config import create_custom_scene  # noqa: E402
from src.api.session_store import get_or_create_agent_session  # noqa: E402

SCENE_NAME = "issue-524-scene"


def _unique_repo() -> str:
    return f"scene-524-{uuid.uuid4().hex[:10]}"


def _insert_runtime(name: str, status: str, healthy: int) -> int:
    """插入 agent_runtimes 引擎行，返回自增 id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, provider, status, healthy) "
            "VALUES (?,?,?,?,?)",
            (name, name, "test", status, healthy))
        db.commit()
        return c.lastrowid


def _set_runtime_health(rid: int, status: str, healthy: int):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET status=?, healthy=? WHERE id=?",
                  (status, healthy, rid))
        db.commit()


def _create_scene(scene_id: str, repo_key: str, nodes: list):
    create_custom_scene(
        scene_id=scene_id, name=SCENE_NAME, description="issue 524 test",
        repo_key=repo_key, nodes=nodes)


def _bind_session(repo_key: str, agent_type: str, runtime_id: int):
    """预置旧绑定（模拟引擎 A 已生效）。"""
    get_or_create_agent_session(repo_key, agent_type, "opencode")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE devops_agent_session SET runtime_id=? WHERE repo_key=? AND agent_type=?",
                  (runtime_id, repo_key, agent_type))
        db.commit()


def _session_runtime(repo_key: str, agent_type: str):
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
            (repo_key, agent_type)).fetchone()
        return row[0] if row else None


def _cleanup(repo_key: str, scene_id: str, runtime_ids: list):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                  (scene_id, repo_key))
        for rid in runtime_ids:
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        db.commit()
    from src.api.pipeline_config import invalidate_scene_cache
    invalidate_scene_cache(scene_id, repo_key)


def _apply(owner: str, repo: str, scene_id: str, strict: bool = False):
    return apply_scene(
        owner, repo, ApplySceneRequest(scene_id=scene_id, strict=strict), {})


def _receipt(result: dict, agent_type: str) -> dict:
    for r in result.get("nodes", []):
        if r["agent_type"] == agent_type:
            return r
    return {}


class TestUnhealthyDeclaredEngine:
    """目标引擎 online/healthy=false：节点 skipped，提示仍生效引擎，不计入 updated。"""

    def test_unhealthy_engine_marked_skipped_with_reason(self):
        """引擎 B online 但 healthy=false → skipped，reason 提示 keep runtime A。"""
        owner, repo = "s524owner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        rid_a = _insert_runtime("engine-a-524", "online", 1)
        rid_b = _insert_runtime("engine-b-524", "online", 0)  # online 但不健康
        try:
            _bind_session(repo_key, "issue", rid_a)  # 旧绑定引擎 A
            _create_scene(scene_id, repo_key, [
                {"tag": "issue", "skill": "issue-cli", "engine_runtime_ids": [rid_b]}])
            result = _apply(owner, repo, scene_id)

            rec = _receipt(result, "issue")
            assert rec["status"] == "skipped", f"不健康目标引擎应 skipped，实际 {rec}"
            assert rec["effective_runtime_id"] == rid_a, "skipped 节点应提示当前仍生效引擎 A"
            assert rec["declared_runtime_ids"] == [rid_b]
            assert "healthy=0" in rec["reason"] and f"keep runtime {rid_a}" in rec["reason"]
            assert result["updated"] == 0, "skipped 节点不应计入 updated"
            assert result["skipped"] == 1
            # session 绑定未被改写（保持 A）
            assert _session_runtime(repo_key, "issue") == rid_a
        finally:
            _cleanup(repo_key, scene_id, [rid_a, rid_b])

    def test_strict_mode_rejects_whole_apply(self):
        """strict=true 且存在 skipped 节点 → 409 整单拒绝，不落库。"""
        owner, repo = "s524owner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        rid_a = _insert_runtime("engine-a-524", "online", 1)
        rid_b = _insert_runtime("engine-b-524", "online", 0)
        rid_ok = _insert_runtime("engine-c-524", "online", 1)
        try:
            _bind_session(repo_key, "issue", rid_a)
            _bind_session(repo_key, "coding", rid_ok)
            _create_scene(scene_id, repo_key, [
                {"tag": "issue", "skill": "issue-cli", "engine_runtime_ids": [rid_b]},
                {"tag": "coding", "skill": "coding-cli", "engine_runtime_ids": [rid_ok]}])
            try:
                _apply(owner, repo, scene_id, strict=True)
                raise AssertionError("strict 模式下不健康节点应触发 409 整单拒绝")
            except HTTPException as e:
                assert e.status_code == 409
                assert "strict" in e.detail["message"]
                assert e.detail["nodes"], "409 detail 应携带按节点回执"
            # 整单拒绝：健康节点 coding 也不应被改写/创建新绑定语义（保持预置绑定）
            assert _session_runtime(repo_key, "issue") == rid_a
            assert _session_runtime(repo_key, "coding") == rid_ok
        finally:
            _cleanup(repo_key, scene_id, [rid_a, rid_b, rid_ok])


class TestReapplyAfterRecovery:
    """引擎恢复 healthy=1 后重新 apply：节点 applied，实际切换到目标引擎。"""

    def test_reapply_after_engine_recovers(self):
        """B 恢复 healthy=true → 重新 apply → applied，生效引擎切到 B。"""
        owner, repo = "s524owner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        rid_a = _insert_runtime("engine-a-524", "online", 1)
        rid_b = _insert_runtime("engine-b-524", "online", 0)
        try:
            _bind_session(repo_key, "issue", rid_a)
            _create_scene(scene_id, repo_key, [
                {"tag": "issue", "skill": "issue-cli", "engine_runtime_ids": [rid_b]}])

            # 第一次：B 不健康 → skipped，保持 A
            r1 = _apply(owner, repo, scene_id)
            assert _receipt(r1, "issue")["status"] == "skipped"
            assert r1["updated"] == 0

            # B 恢复健康 → 重新 apply → applied，切换到 B
            _set_runtime_health(rid_b, "online", 1)
            r2 = _apply(owner, repo, scene_id)
            rec = _receipt(r2, "issue")
            assert rec["status"] == "applied"
            assert rec["effective_runtime_id"] == rid_b, "恢复后应实际切换到引擎 B"
            assert rec["declared_runtime_ids"] == [rid_b]
            assert r2["updated"] == 1
            assert _session_runtime(repo_key, "issue") == rid_b
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
                db.commit()
            _cleanup(repo_key, scene_id, [rid_a, rid_b])


class TestPartialApply:
    """部分节点成功：混合回执，updated 只统计 applied 节点。"""

    def test_mixed_applied_and_skipped(self):
        """两节点：一个声明健康引擎、一个声明不健康引擎 → 1 applied + 1 skipped。"""
        owner, repo = "s524owner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        rid_a = _insert_runtime("engine-a-524", "online", 1)
        rid_bad = _insert_runtime("engine-bad-524", "online", 0)
        rid_ok = _insert_runtime("engine-ok-524", "online", 1)
        try:
            _bind_session(repo_key, "issue", rid_a)
            _create_scene(scene_id, repo_key, [
                {"tag": "issue", "skill": "issue-cli", "engine_runtime_ids": [rid_bad]},
                {"tag": "coding", "skill": "coding-cli", "engine_runtime_ids": [rid_ok]}])
            result = _apply(owner, repo, scene_id)

            rec_issue = _receipt(result, "issue")
            rec_coding = _receipt(result, "coding")
            assert rec_issue["status"] == "skipped"
            assert rec_issue["effective_runtime_id"] == rid_a
            assert rec_coding["status"] == "applied"
            assert rec_coding["effective_runtime_id"] == rid_ok
            assert result["updated"] == 1, "updated 只统计 applied 节点"
            assert result["applied"] == 1 and result["skipped"] == 1 and result["failed"] == 0
            assert len(result["nodes"]) == 2
            # 部分应用：issue 保持 A，coding 切到 rid_ok
            assert _session_runtime(repo_key, "issue") == rid_a
            assert _session_runtime(repo_key, "coding") == rid_ok
        finally:
            _cleanup(repo_key, scene_id, [rid_a, rid_bad, rid_ok])

    def test_undeclared_engine_node_still_applied(self):
        """未声明引擎的节点维持旧回退链（session 旧绑定）→ applied。"""
        owner, repo = "s524owner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        rid_a = _insert_runtime("engine-a-524", "online", 1)
        try:
            _bind_session(repo_key, "issue", rid_a)
            _create_scene(scene_id, repo_key, [
                {"tag": "issue", "skill": "issue-cli"}])  # 无 engine_runtime_ids
            result = _apply(owner, repo, scene_id)

            rec = _receipt(result, "issue")
            assert rec["status"] == "applied"
            assert rec["declared_runtime_ids"] == []
            assert rec["effective_runtime_id"] == rid_a
            assert result["updated"] == 1
        finally:
            _cleanup(repo_key, scene_id, [rid_a])


class TestAgentDetailDeclaredVsEffective:
    """agents detail：声明配置（declared_runtime_ids）与生效配置（effective_runtime_id）区分。"""

    def test_detail_returns_declared_and_effective(self):
        """detail 返回体同时携带场景声明引擎集合与 session 实际生效引擎。"""
        from src.api.routes.agents import get_agent_detail
        owner, repo = "s524owner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        rid_a = _insert_runtime("engine-a-524", "online", 1)
        rid_b = _insert_runtime("engine-b-524", "online", 0)
        try:
            _bind_session(repo_key, "issue", rid_a)
            # 仓库当前应用场景 = scene_id（agents detail 声明配置来源）
            from src.user.db import add_managed_repo, set_repo_scene
            add_managed_repo(repo_key, owner, repo)
            set_repo_scene(repo_key, scene_id)
            _create_scene(scene_id, repo_key, [
                {"tag": "issue", "skill": "issue-cli", "engine_runtime_ids": [rid_b]}])
            # auth_type=api_key 跳过仓库归属校验（本测试聚焦声明/生效字段，非权限）
            detail = get_agent_detail("issue", owner, repo, issue_number="",
                                      keyword="", page=1, page_size=10,
                                      user={"id": 1, "auth_type": "api_key"})
            assert detail["declared_runtime_ids"] == [rid_b], "声明配置应来自场景 node"
            assert detail["effective_runtime_id"] == rid_a, "生效配置应来自 session 实际绑定"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
                db.commit()
            _cleanup(repo_key, scene_id, [rid_a, rid_b])
