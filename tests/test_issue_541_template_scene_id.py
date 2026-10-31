"""回归测试（Issue #541）：模板建仓场景 ID 传递断裂修复。

背景：模板覆盖逻辑把模板场景名（如 hello-world）写进 managed_repos.dev_scene，
而仓内副本真实 scene_id（custom-{uuid}）未回传，导致 onboarding Issue 绑定到
仓内不存在的场景名，派发侧回退全局模板行（无引擎）→ 统一报「引擎全部离线」。

修复后（四类回归）：
1. 模板写入真实 ID：apply_market_scene_by_name 返回仓内副本 scene_id（None 表失败），
   模板覆盖逻辑 _apply_template_scene 写真实 ID；失败保留默认场景。
2. set_repo_scene 校验：仓内不存在的 scene_id 告警但不阻断写入。
3. 报错区分：scene_dispatch_failure_reason 区分「仓内不存在 / 未绑定引擎 / 全部离线」；
   get_scene_pool_engine_ids SRE 仓内未命中不回退全局模板行（None）。
4. 存量改绑：repair_scene_bindings 幂等扫描 dev_scene 与 issue.scene_id，
   按 market_scene_id 匹配仓内副本改绑。

测试风格对齐 tests/test_issue_524_scene_apply_receipt.py（真实 sqlite 测试库）。
"""
import logging
import os
import sys
import uuid

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
if os.environ["DEVOPS_DB_PATH"] not in sys.path[:0]:
    pass

from src.session_manager import db, db_lock  # noqa: E402
from src.user.db import add_managed_repo, get_repo_scene, set_repo_scene  # noqa: E402
from src.api.pipeline_config import (  # noqa: E402
    apply_market_scene_by_name, create_custom_scene, invalidate_scene_cache)
from src.api.session_store import set_issue_scene  # noqa: E402

LOGGER_NAME = "devops-bot"


def _unique_repo() -> str:
    return f"repo-541-{uuid.uuid4().hex[:10]}"


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _insert_market_scene(name: str, nodes_json: str = "[]") -> int:
    """插入 scene_market 官方模板行，返回 market_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_market (name, display_name, source, is_active, nodes_json, onboarding) "
            "VALUES (?,?,?,?,?,?)",
            (name, name, "official", 1, nodes_json, "repo-onboarding"))
        db.commit()
        return c.lastrowid


def _link_market(scene_id: str, repo_key: str, market_id: int):
    """把仓内场景副本回写 market_scene_id 来源标记。"""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE devops_pipeline_scene SET market_scene_id=? WHERE scene_id=? AND repo_key=?",
                  (market_id, scene_id, repo_key))
        db.commit()


def _create_repo_copy(repo_key: str, market_id: int, scene_id: str = "") -> str:
    """创建仓内场景副本并标记来源，返回 scene_id。"""
    scene_id = scene_id or f"custom-{uuid.uuid4().hex[:8]}"
    create_custom_scene(
        scene_id=scene_id, name=_unique_name("copy541"), description="issue 541 test copy",
        repo_key=repo_key, nodes=[])
    _link_market(scene_id, repo_key, market_id)
    return scene_id


def _cleanup(repo_key: str, scene_ids: list, market_ids: list):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        for mid in market_ids:
            c.execute("DELETE FROM scene_market WHERE id=?", (mid,))
        db.commit()
    for sid in scene_ids:
        invalidate_scene_cache(sid, repo_key)


class TestTemplateSceneOverride:
    """1. 模板覆盖写真实 scene_id；失败保留默认场景。"""

    def test_apply_market_scene_by_name_returns_real_scene_id(self):
        """market 模板应用后返回仓内副本 scene_id（custom- 前缀），且行带 market_scene_id。"""
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        name = _unique_name("tpl541")
        market_id = _insert_market_scene(name)
        try:
            real_id = apply_market_scene_by_name(repo_key, name)
            assert real_id, "应返回真实 scene_id 而非 None/空"
            assert real_id.startswith("custom-"), f"应为仓内副本 custom-id，实际 {real_id}"
            with db_lock:
                c = db.cursor()
                row = c.execute(
                    "SELECT market_scene_id FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key=? AND is_active=1",
                    (real_id, repo_key)).fetchone()
            assert row and row[0] == market_id, "仓内副本应带 market_scene_id 来源标记"
            _cleanup(repo_key, [real_id], [market_id])
        finally:
            _cleanup(repo_key, [], [market_id])

    def test_apply_market_scene_by_name_missing_template_returns_none(self):
        """market 模板名不存在 → 返回 None（失败语义）。"""
        repo_key = f"gitcode:test541/{_unique_repo()}"
        assert apply_market_scene_by_name(repo_key, _unique_name("nope")) is None

    def test_apply_template_scene_writes_real_id_to_dev_scene(self):
        """模板覆盖：dev_scene 写入真实 scene_id（而非模板名）。"""
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        name = _unique_name("tpl541")
        market_id = _insert_market_scene(name)
        add_managed_repo(repo_key, "test541", repo, dev_scene="default-scene")
        try:
            from src.api.routes.templates import _apply_template_scene
            real_id = _apply_template_scene(repo_key, name, 0)
            assert real_id and real_id.startswith("custom-")
            assert get_repo_scene(repo_key) == real_id, "dev_scene 应为仓内副本真实 ID"
        finally:
            _cleanup(repo_key, [], [market_id])

    def test_apply_template_scene_failure_keeps_default(self):
        """模板名查不到 → 返回空且 dev_scene 保留默认场景。"""
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        add_managed_repo(repo_key, "test541", repo, dev_scene="keep-default")
        try:
            from src.api.routes.templates import _apply_template_scene
            ret = _apply_template_scene(repo_key, _unique_name("nope"), 0)
            assert ret == ""
            assert get_repo_scene(repo_key) == "keep-default", "失败应保留默认场景"
        finally:
            _cleanup(repo_key, [], [])


class TestSetRepoSceneValidation:
    """2. set_repo_scene 仓内存在性校验：告警不阻断。"""

    def test_set_repo_scene_unknown_scene_warns_but_writes(self, caplog):
        """仓内不存在的 scene_id：写前告警，但仍写入（不阻断历史直写路径）。"""
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        add_managed_repo(repo_key, "test541", repo, dev_scene="")
        try:
            unknown = _unique_name("ghost541")
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                set_repo_scene(repo_key, unknown)
            assert get_repo_scene(repo_key) == unknown, "校验仅告警，不应阻断写入"
            assert any("not found in repo" in r.getMessage() for r in caplog.records), \
                "应产生仓内存在性告警日志"
        finally:
            _cleanup(repo_key, [], [])

    def test_set_repo_scene_existing_scene_no_warning(self, caplog):
        """仓内存在的 scene_id：正常写入，无存在性告警。"""
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        name = _unique_name("tpl541")
        market_id = _insert_market_scene(name)
        add_managed_repo(repo_key, "test541", repo, dev_scene="")
        scene_id = _create_repo_copy(repo_key, market_id)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                set_repo_scene(repo_key, scene_id)
            assert get_repo_scene(repo_key) == scene_id
            assert not any("not found in repo" in r.getMessage() for r in caplog.records)
        finally:
            _cleanup(repo_key, [scene_id], [market_id])


class TestDispatchFailureReason:
    """3. 派发侧报错区分 + SRE 仓内未命中不回退全局模板行。"""

    def _scene_with_sre_node(self, repo_key: str, engine_ids: list) -> str:
        scene_id = f"custom-{uuid.uuid4().hex[:8]}"
        nodes = [{"id": "n1", "tag": "sre", "enabled": True, "engine_runtime_ids": engine_ids}]
        create_custom_scene(
            scene_id=scene_id, name=_unique_name("disp541"), description="",
            repo_key=repo_key, nodes=nodes)
        return scene_id

    def test_reason_scene_not_in_repo(self):
        """仓内不存在的 scene_id → 报「仓内不存在」，不再笼统报全部离线。"""
        from src.runtime.engine_select import scene_dispatch_failure_reason
        repo_key = f"gitcode:test541/{_unique_repo()}"
        ghost = _unique_name("ghost541")
        reason = scene_dispatch_failure_reason(ghost, "sre", repo_key=repo_key)
        assert "仓内不存在" in reason, reason
        assert ghost in reason

    def test_reason_scene_unbound(self):
        """场景仓内存在但 sre 节点未绑引擎 → 报「未绑定引擎」。"""
        from src.runtime.engine_select import scene_dispatch_failure_reason
        repo_key = f"gitcode:test541/{_unique_repo()}"
        scene_id = self._scene_with_sre_node(repo_key, [])
        try:
            reason = scene_dispatch_failure_reason(scene_id, "sre", repo_key=repo_key)
            assert "未绑定引擎" in reason, reason
        finally:
            _cleanup(repo_key, [scene_id], [])

    def test_reason_all_offline(self):
        """场景绑定了引擎（但池判定全灭）→ 报「全部离线」。"""
        from src.runtime.engine_select import scene_dispatch_failure_reason
        repo_key = f"gitcode:test541/{_unique_repo()}"
        scene_id = self._scene_with_sre_node(repo_key, [987654])
        try:
            reason = scene_dispatch_failure_reason(scene_id, "sre", repo_key=repo_key)
            assert "全部离线" in reason, reason
            assert "未绑定引擎" not in reason
        finally:
            _cleanup(repo_key, [scene_id], [])

    def test_sre_pool_none_when_scene_not_in_repo(self):
        """SRE：仓内未命中 → None（不静默回退全局模板行绑定）。"""
        from src.runtime.engine_select import get_scene_pool_engine_ids
        repo_key = f"gitcode:test541/{_unique_repo()}"
        ghost = _unique_name("ghost541")
        # 兜底：全局行也不存在，避免依赖全局数据
        assert get_scene_pool_engine_ids(ghost, "sre", repo_key=repo_key) is None

    def test_sre_pool_none_when_scene_unbound(self):
        """SRE：仓内场景未绑引擎 → None（空 = 无引擎，不广播）。"""
        from src.runtime.engine_select import get_scene_pool_engine_ids
        repo_key = f"gitcode:test541/{_unique_repo()}"
        scene_id = self._scene_with_sre_node(repo_key, [])
        try:
            assert get_scene_pool_engine_ids(scene_id, "sre", repo_key=repo_key) is None
        finally:
            _cleanup(repo_key, [scene_id], [])


class TestRepairSceneBindings:
    """4. 存量修复：幂等扫描改绑 dev_scene 与 issue.scene_id。"""

    def test_repair_rebinds_dev_scene_and_issue(self):
        """仓内不存在 + market 同名模板存在 → 按 market_scene_id 改绑仓内副本。"""
        from src.api.scene_binding_repair import repair_scene_bindings
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        tpl_name = _unique_name("tpl541")
        market_id = _insert_market_scene(tpl_name)
        add_managed_repo(repo_key, "test541", repo, dev_scene=tpl_name)
        copy_id = _create_repo_copy(repo_key, market_id)
        set_issue_scene(repo_key, "#1", tpl_name)
        try:
            result = repair_scene_bindings()
            assert result["repos_fixed"] >= 1, "至少修复本用例的 dev_scene"
            assert result["issues_fixed"] >= 1, "至少修复本用例的 issue.scene_id"
            assert get_repo_scene(repo_key) == copy_id, "dev_scene 应改绑仓内副本"
            with db_lock:
                c = db.cursor()
                row = c.execute(
                    "SELECT scene_id FROM issue WHERE repo_key=? AND issue_number=1",
                    (repo_key,)).fetchone()
            assert row and row[0] == copy_id, "issue.scene_id 应改绑仓内副本"
            # 幂等：二跑无新修复
            result2 = repair_scene_bindings()
            assert result2["repos_fixed"] == 0 and result2["issues_fixed"] == 0
        finally:
            _cleanup(repo_key, [copy_id, tpl_name], [market_id])

    def test_repair_skips_when_no_market_template(self):
        """仓内不存在且 market 无同名模板 → 跳过不改。"""
        from src.api.scene_binding_repair import repair_scene_bindings
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        ghost = _unique_name("ghost541")
        add_managed_repo(repo_key, "test541", repo, dev_scene=ghost)
        try:
            repair_scene_bindings()
            assert get_repo_scene(repo_key) == ghost, "无匹配模板时不应改绑"
        finally:
            _cleanup(repo_key, [ghost], [])

    def test_repair_skips_valid_binding(self):
        """仓内已存在的 scene_id → 跳过（正常绑定不动）。"""
        from src.api.scene_binding_repair import repair_scene_bindings
        repo = _unique_repo()
        repo_key = f"gitcode:test541/{repo}"
        market_id = _insert_market_scene(_unique_name("tpl541"))
        copy_id = _create_repo_copy(repo_key, market_id)
        add_managed_repo(repo_key, "test541", repo, dev_scene=copy_id)
        try:
            repair_scene_bindings()
            assert get_repo_scene(repo_key) == copy_id
        finally:
            _cleanup(repo_key, [copy_id], [market_id])
