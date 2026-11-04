"""Issue #980: skill display-name resolution must honor the actual bound scope.

三档同名技能（official/repo/user，唯一键 UNIQUE(skill_name, scope, owner_id)）
此前在场景列表（scenes.py with-pipeline）与 dashboard 被一律显示成官方名，
因为两处都只构建 `scope='official'` 的映射。

本套件钉住共用解析层（src/api/skill_display.py）的契约：
- 解析优先级：skill_id → (scope, skill_name) → user(owner=user_id) →
  repo(owner=repo_key) → official 兜底 → skill_name 本身
- 失活技能（is_active=0）视为未命中，沿回退链继续
- 无同名冲突时与旧 official-only 行为逐字一致（零回归）
- scenes.py / dashboard.py 两处调用点统一替换（行为 + 源码契约）
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_KEY = "gitcode:i980/repo"
UID = "98001"


def _seed_skill(skill_name, display_name, scope, owner_id="", is_active=1, skill_id=None):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        if skill_id:
            c.execute(
                "INSERT OR REPLACE INTO skill (id, skill_name, display_name, scope, owner_id, is_active)"
                " VALUES (?,?,?,?,?,?)",
                (skill_id, skill_name, display_name, scope, owner_id, is_active))
        else:
            c.execute(
                "INSERT OR REPLACE INTO skill (skill_name, display_name, scope, owner_id, is_active)"
                " VALUES (?,?,?,?,?)",
                (skill_name, display_name, scope, owner_id, is_active))
        db.commit()
    return skill_id


@pytest.fixture()
def clean_skills():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name LIKE 'i980-%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name LIKE 'i980-%'")
        db.commit()


SN = "i980-dup-skill"  # 三档同名技能


@pytest.fixture()
def three_scope_same_name(clean_skills):
    """造三档同名技能：official / repo(本仓库) / user(本用户)。"""
    _seed_skill(SN, "官方-显示名", "official")
    _seed_skill(SN, "仓库-显示名", "repo", REPO_KEY)
    _uid_skill = _seed_skill(SN, "个人-显示名", "user", UID)
    return _uid_skill


class TestResolveSkillDisplayName:
    def test_user_scope_wins_with_user_context(self, three_scope_same_name):
        from src.api.skill_display import resolve_skill_display_name
        assert resolve_skill_display_name(SN, repo_key=REPO_KEY, user_id=UID) == "个人-显示名"

    def test_repo_scope_wins_without_user_context(self, three_scope_same_name):
        from src.api.skill_display import resolve_skill_display_name
        assert resolve_skill_display_name(SN, repo_key=REPO_KEY, user_id="") == "仓库-显示名"

    def test_official_fallback_without_context(self, three_scope_same_name):
        from src.api.skill_display import resolve_skill_display_name
        # 无 user/repo 上下文 → official 兜底（旧 official-only 行为）
        assert resolve_skill_display_name(SN) == "官方-显示名"

    def test_skill_id_resolves_exact_scope(self, three_scope_same_name):
        from src.api.skill_display import resolve_skill_display_name
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='repo'", (SN,))
            repo_row_id = c.fetchone()[0]
        # skill_id 精确命中：即使上下文优先 user，也按 skill_id 指向的 repo 档显示
        assert resolve_skill_display_name(
            SN, skill_id=repo_row_id, repo_key=REPO_KEY, user_id=UID) == "仓库-显示名"

    def test_skill_id_inactive_falls_back(self, clean_skills):
        """回退路径①：skill_id 存在但技能已失活 → 视为未命中，走回退链。"""
        from src.api.skill_display import resolve_skill_display_name
        _seed_skill(SN, "个人-显示名", "user", UID, is_active=0, skill_id=9800999)
        _seed_skill(SN, "官方-显示名", "official")
        assert resolve_skill_display_name(
            SN, skill_id=9800999, repo_key=REPO_KEY, user_id=UID) == "官方-显示名"

    def test_skill_id_missing_falls_back(self, three_scope_same_name):
        """回退路径②：skill_id 缺失（0/None）→ 按 (scope, skill_name) + 上下文回退。"""
        from src.api.skill_display import resolve_skill_display_name
        assert resolve_skill_display_name(SN, skill_id=0, repo_key=REPO_KEY, user_id=UID) == "个人-显示名"
        assert resolve_skill_display_name(SN, skill_id=None, repo_key=REPO_KEY, user_id="") == "仓库-显示名"

    def test_explicit_scope_overrides_context(self, three_scope_same_name):
        from src.api.skill_display import resolve_skill_display_name
        # (scope, skill_name) 显式档位优先于上下文推断
        assert resolve_skill_display_name(
            SN, scope="repo", owner_id=REPO_KEY, repo_key=REPO_KEY, user_id=UID) == "仓库-显示名"

    def test_no_conflict_zero_regression(self, clean_skills):
        """无同名冲突时与旧 official-only 行为逐字一致。"""
        from src.api.skill_display import resolve_skill_display_name
        _seed_skill("i980-plain", "唯一技能显示名", "official")
        assert resolve_skill_display_name("i980-plain", repo_key=REPO_KEY, user_id=UID) == "唯一技能显示名"
        # 完全未命中（技能不存在）→ 回退 skill_name 本身（旧 map.get(_sk, _sk) 语义）
        assert resolve_skill_display_name("i980-ghost", repo_key=REPO_KEY) == "i980-ghost"

    def test_empty_skill_name_returns_empty(self, three_scope_same_name):
        from src.api.skill_display import resolve_skill_display_name
        assert resolve_skill_display_name("") == ""


class TestBuildSkillDisplayMap:
    def test_map_picks_user_scope_for_context(self, three_scope_same_name):
        from src.api.skill_display import build_skill_display_map
        m = build_skill_display_map(repo_key=REPO_KEY, user_id=UID)
        assert m[SN] == "个人-显示名"

    def test_map_picks_repo_scope_without_user(self, three_scope_same_name):
        from src.api.skill_display import build_skill_display_map
        m = build_skill_display_map(repo_key=REPO_KEY)
        assert m[SN] == "仓库-显示名"

    def test_map_ignores_inactive_and_other_owners(self, clean_skills):
        from src.api.skill_display import build_skill_display_map
        _seed_skill(SN, "别人的个人技能", "user", "other-user")
        _seed_skill(SN, "别的仓库技能", "repo", "gitcode:other/repo")
        _seed_skill(SN, "官方-显示名", "official")
        m = build_skill_display_map(repo_key=REPO_KEY, user_id=UID)
        assert m[SN] == "官方-显示名"


class TestScenesRouteUsesSharedResolver:
    """/scenes/with-pipeline 节点 skill_display_name 按实际档位显示。"""

    @pytest.fixture(autouse=True)
    def _seed_scene(self, clean_skills):
        _seed_skill("i980-plain", "唯一技能显示名", "official")
        from src.session_manager import db, db_lock
        nodes_json = (
            '[{"id": "coding", "tag": "coding", "skill": "%s"},'
            ' {"id": "qa", "tag": "qa", "skill": "i980-plain"}]' % SN
        )
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
            c.execute(
                "INSERT INTO devops_pipeline_scene (scene_id, name, display_name, repo_key, nodes_json, is_active, created_at)"
                " VALUES ('i980-scene', 'i980-scene', 'i980-scene', ?, ?, 1, datetime('now'))",
                (REPO_KEY, nodes_json))
            db.commit()
        yield
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    def test_node_display_matches_user_scope(self, three_scope_same_name):
        from src.api.routes.scenes import list_scenes_with_pipeline
        resp = list_scenes_with_pipeline(
            user={"auth_type": "session_token", "id": UID}, repo="i980/repo")
        nodes = {n["id"]: n for n in resp["scenes"][0]["pipeline"]["nodes"]}
        assert nodes["coding"]["skill_display_name"] == "个人-显示名"
        assert nodes["qa"]["skill_display_name"] == "唯一技能显示名"

    def test_node_display_matches_repo_scope_without_user_id(self, three_scope_same_name):
        from src.api.routes.scenes import list_scenes_with_pipeline
        resp = list_scenes_with_pipeline(
            user={"auth_type": "session_token", "id": ""}, repo="i980/repo")
        nodes = {n["id"]: n for n in resp["scenes"][0]["pipeline"]["nodes"]}
        assert nodes["coding"]["skill_display_name"] == "仓库-显示名"


class TestDashboardRouteUsesSharedResolver:
    """dashboard agent 卡片的 skill_display_name 按实际档位显示（行为级）。"""

    OWNER, REPO = "i980", "repo"

    @pytest.fixture(autouse=True)
    def _seed_scene(self, clean_skills):
        from src.session_manager import db, db_lock
        nodes_json = '[{"id": "coding", "tag": "coding", "skill": "%s"}]' % SN
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
            c.execute(
                "INSERT INTO devops_pipeline_scene (scene_id, name, display_name, repo_key, nodes_json, is_active, created_at)"
                " VALUES ('i980-scene', 'i980-scene', 'i980-scene', ?, ?, 1, datetime('now'))",
                (REPO_KEY, nodes_json))
            db.commit()
        yield
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    def _agents_map(self, user):
        from src.api.routes.dashboard import get_dashboard
        resp = get_dashboard(self.OWNER, self.REPO, user=user)
        return {a["agent_type"]: a for a in resp["agents"]}

    def test_agent_display_matches_user_scope(self, three_scope_same_name):
        agents = self._agents_map({"auth_type": "session_token", "id": UID})
        assert agents["coding"]["skill"] == SN
        assert agents["coding"]["skill_display_name"] == "个人-显示名"

    def test_agent_display_matches_repo_scope_without_user_id(self, three_scope_same_name):
        agents = self._agents_map({"auth_type": "session_token", "id": ""})
        assert agents["coding"]["skill_display_name"] == "仓库-显示名"


class TestSourceContract:
    """源码契约：两处调用点统一替换，official-only 查询不再出现。"""

    def test_routes_import_shared_resolver(self):
        for rel in ("src/api/routes/scenes.py", "src/api/routes/dashboard.py"):
            src = open(os.path.join(os.path.dirname(__file__), "..", rel), encoding="utf-8").read()
            assert "from src.api.skill_display import" in src, f"{rel} 未引入共用解析层"

    def test_no_official_only_display_map_left(self):
        pattern = re.compile(
            r"SELECT skill_name,\s*display_name FROM skill WHERE is_active=1 AND scope='official'")
        for rel in ("src/api/routes/scenes.py", "src/api/routes/dashboard.py"):
            src = open(os.path.join(os.path.dirname(__file__), "..", rel), encoding="utf-8").read()
            assert not pattern.search(src), f"{rel} 仍残留 official-only 显示名映射"
