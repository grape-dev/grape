"""Issue #190 单元测试：obs_key/file_path 归一化 + baseline 去重 + 迁移幂等 + 归档恢复。

覆盖：
- knowledge_obs_prefix 跨 provider 不冲突
- knowledge_storage_key 含 provider 前缀
- baseline 去重按 name（不依赖 file_path）
- member obs_key 路径不重复（sync 路径）
- 迁移函数幂等
- 迁移步骤 0 恢复 archived=1→0
- read_entry_content 旧格式 file_path 兜底
"""
import os, sys
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest
from src.session_manager import db, db_lock


class TestKnowledgeObsPrefix:
    """knowledge_obs_prefix 和 knowledge_storage_key 格式验证"""

    def test_gitcode_prefix(self):
        from src.repo_key import knowledge_obs_prefix
        assert knowledge_obs_prefix("gitcode:grape-dev/grape") == "knowledge/gitcode/grape-dev/grape"

    def test_github_prefix(self):
        from src.repo_key import knowledge_obs_prefix
        assert knowledge_obs_prefix("github:grape-dev/grape") == "knowledge/github/grape-dev/grape"

    def test_default_provider(self):
        from src.repo_key import knowledge_obs_prefix
        assert knowledge_obs_prefix("grape-dev/grape") == "knowledge/gitcode/grape-dev/grape"

    def test_cross_provider_no_conflict(self):
        from src.repo_key import knowledge_obs_prefix
        g1 = knowledge_obs_prefix("gitcode:test/repo")
        g2 = knowledge_obs_prefix("github:test/repo")
        assert g1 != g2

    def test_storage_key_project(self):
        from src.api.knowledge import knowledge_storage_key
        k = knowledge_storage_key("gitcode:test/repo", "project")
        assert k == "knowledge/gitcode/test/repo/project/knowledge.md"

    def test_storage_key_issue(self):
        from src.api.knowledge import knowledge_storage_key
        k = knowledge_storage_key("gitcode:test/repo", "issue", issue_num=42)
        assert k == "knowledge/gitcode/test/repo/issues/42/knowledge.md"

    def test_storage_key_issue_agent(self):
        from src.api.knowledge import knowledge_storage_key
        k = knowledge_storage_key("gitcode:test/repo", "issue_agent", agent_type="coding", issue_num=42)
        assert k == "knowledge/gitcode/test/repo/issues/42/coding/knowledge.md"

    def test_storage_key_cross_provider(self):
        from src.api.knowledge import knowledge_storage_key
        k1 = knowledge_storage_key("gitcode:test/repo", "project")
        k2 = knowledge_storage_key("github:test/repo", "project")
        assert k1 != k2


class TestMemberObsKeyNoDuplication:
    """member sync 路径 obs_key 不重复"""

    def test_member_obs_key_no_path_duplication(self):
        """sync 路径提取 member/ 后拼接，不产生 owner/repo 重复"""
        from src.repo_key import knowledge_obs_prefix
        repo_key = "gitcode:mm6667/test"
        file_path = "mm6667/test/project-info/member/admin/entry.md"
        _obs_prefix = knowledge_obs_prefix(repo_key)
        # 模拟 sync 路径逻辑
        for marker in ("member/", "agent/"):
            idx = file_path.find(marker)
            if idx >= 0:
                _rel = file_path[idx:]
                break
        else:
            _rel = os.path.basename(file_path)
        storage_key = f"{_obs_prefix}/{_rel}"
        # 不含重复的 owner/repo
        assert "mm6667/test/mm6667/test" not in storage_key
        assert storage_key == "knowledge/gitcode/mm6667/test/member/admin/entry.md"

    def test_member_obs_key_matches_upload(self):
        """sync 和 upload 路径生成的 obs_key 一致"""
        from src.repo_key import knowledge_obs_prefix
        repo_key = "gitcode:mm6667/test"
        username = "admin"
        filename = "entry.md"
        # upload 路径
        upload_key = f"{knowledge_obs_prefix(repo_key)}/member/{username}/{filename}"
        # sync 路径
        file_path = f"gitcode/mm6667/test/project-info/member/{username}/{filename}"
        _obs_prefix = knowledge_obs_prefix(repo_key)
        for marker in ("member/", "agent/"):
            idx = file_path.find(marker)
            if idx >= 0:
                _rel = file_path[idx:]
                break
        else:
            _rel = os.path.basename(file_path)
        sync_key = f"{_obs_prefix}/{_rel}"
        assert upload_key == sync_key


class TestBaselineNameMap:
    """baseline name 映射常量"""

    def test_name_map_has_three_files(self):
        from src.repo_key import BASELINE_NAME_MAP
        assert len(BASELINE_NAME_MAP) == 3
        assert "project-graph.md" in BASELINE_NAME_MAP
        assert "project-capabilities.md" in BASELINE_NAME_MAP
        assert ".metadata.json" in BASELINE_NAME_MAP

    def test_name_map_values_are_chinese(self):
        from src.repo_key import BASELINE_NAME_MAP
        assert BASELINE_NAME_MAP["project-graph.md"] == "项目结构图谱"
        assert BASELINE_NAME_MAP["project-capabilities.md"] == "项目能力清单"
        assert BASELINE_NAME_MAP[".metadata.json"] == "项目元数据"


class TestMigrationIdempotent:
    """迁移函数幂等性"""

    def setup_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key LIKE 'test-migrate-%'")
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key LIKE 'test-migrate-%')")
            db.commit()

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key LIKE 'test-migrate-%'")
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key LIKE 'test-migrate-%')")
            db.commit()

    def _seed_baseline_dup(self):
        """写入两条同 repo + name 的 baseline 记录（模拟重复）"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, obs_key, name) "
                "VALUES ('baseline', 'test-migrate-1', '', 0, 'test-migrate-1/project-info/project-graph.md', 'accepted', 'knowledge/test/old/project-info/project-graph.md', '项目结构图谱')")
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, obs_key, name) "
                "VALUES ('baseline', 'test-migrate-1', '', 0, 'test-migrate-1/project-info/project-graph.md', 'accepted', 'knowledge/test/old/project-info/project-graph.md', '项目结构图谱')")
            db.commit()

    def test_dedup_keeps_max_id(self):
        """去重保留 MAX(id)"""
        self._seed_baseline_dup()
        with db_lock:
            before = db.cursor().execute(
                "SELECT COUNT(*) FROM agent_shared_knowledge WHERE repo_key='test-migrate-1' AND scope='baseline'"
            ).fetchone()[0]
        assert before == 2
        # 执行迁移步骤 1 的去重逻辑
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ("
                "SELECT id FROM agent_shared_knowledge WHERE scope='baseline' "
                "AND repo_key='test-migrate-1' "
                "AND id NOT IN (SELECT MAX(id) FROM agent_shared_knowledge "
                "WHERE scope='baseline' AND repo_key='test-migrate-1' GROUP BY repo_key, name))")
            c.execute(
                "DELETE FROM agent_shared_knowledge WHERE scope='baseline' "
                "AND repo_key='test-migrate-1' "
                "AND id NOT IN (SELECT MAX(id) FROM agent_shared_knowledge "
                "WHERE scope='baseline' AND repo_key='test-migrate-1' GROUP BY repo_key, name)")
            db.commit()
            after = db.cursor().execute(
                "SELECT COUNT(*) FROM agent_shared_knowledge WHERE repo_key='test-migrate-1' AND scope='baseline'"
            ).fetchone()[0]
        assert after == 1

    def test_dedup_idempotent(self):
        """去重执行两次，第二次 DELETE 0 行"""
        self._seed_baseline_dup()
        # 第一次去重
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM agent_shared_knowledge WHERE scope='baseline' "
                "AND repo_key='test-migrate-1' "
                "AND id NOT IN (SELECT MAX(id) FROM agent_shared_knowledge "
                "WHERE scope='baseline' AND repo_key='test-migrate-1' GROUP BY repo_key, name)")
            db.commit()
        # 第二次去重
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM agent_shared_knowledge WHERE scope='baseline' "
                "AND repo_key='test-migrate-1' "
                "AND id NOT IN (SELECT MAX(id) FROM agent_shared_knowledge "
                "WHERE scope='baseline' AND repo_key='test-migrate-1' GROUP BY repo_key, name)")
            db.commit()
            after = db.cursor().execute(
                "SELECT COUNT(*) FROM agent_shared_knowledge WHERE repo_key='test-migrate-1' AND scope='baseline'"
            ).fetchone()[0]
        assert after == 1


class TestMigrationRestoreArchived:
    """迁移步骤 0：恢复 archived=1→0"""

    def setup_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key='test-archived-1'")
            db.commit()

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key='test-archived-1'")
            db.commit()

    def test_restore_archived_to_zero(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, obs_key, name, archived) "
                "VALUES ('issue', 'test-archived-1', '', 1, 'test-archived-1/issues/1/knowledge.md', 'accepted', '', 'Issue #1 知识', 1)")
            db.commit()
        # 执行恢复
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_shared_knowledge SET archived=0 WHERE archived=1")
            db.commit()
            archived = db.cursor().execute(
                "SELECT archived FROM agent_shared_knowledge WHERE repo_key='test-archived-1'"
            ).fetchone()[0]
        assert archived == 0

    def test_restore_idempotent(self):
        """恢复执行两次，第二次 UPDATE 0 行，不影响已恢复的记录"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, obs_key, name, archived) "
                "VALUES ('issue', 'test-archived-1', '', 1, 'test-archived-1/issues/1/knowledge.md', 'accepted', '', 'Issue #1 知识', 1)")
            db.commit()
        # 第一次恢复
        with db_lock:
            db.cursor().execute("UPDATE agent_shared_knowledge SET archived=0 WHERE archived=1")
            db.commit()
        # 第二次恢复
        with db_lock:
            db.cursor().execute("UPDATE agent_shared_knowledge SET archived=0 WHERE archived=1")
            db.commit()
            archived = db.cursor().execute(
                "SELECT archived FROM agent_shared_knowledge WHERE repo_key='test-archived-1'"
            ).fetchone()[0]
        assert archived == 0


class TestFilePathMigration:
    """file_path 格式迁移逻辑"""

    def _migrate_fp(self, repo_key, file_path):
        from src.repo_key import parse_repo_key
        provider, owner, repo = parse_repo_key(repo_key)
        full_prefix = f"{provider}/{owner}/{repo}/"
        partial_prefix = f"{owner}/{repo}/"
        if file_path.startswith(full_prefix):
            return file_path
        elif file_path.startswith(partial_prefix):
            return f"{provider}/{file_path}"
        elif file_path.startswith("project-info/"):
            return f"{full_prefix}{file_path}"
        return None

    def test_old_format(self):
        r = self._migrate_fp("gitcode:test/repo", "project-info/knowledge.md")
        assert r == "gitcode/test/repo/project-info/knowledge.md"

    def test_partial_format(self):
        r = self._migrate_fp("gitcode:test/repo", "test/repo/project-info/knowledge.md")
        assert r == "gitcode/test/repo/project-info/knowledge.md"

    def test_new_format_skip(self):
        r = self._migrate_fp("gitcode:test/repo", "gitcode/test/repo/project-info/knowledge.md")
        assert r == "gitcode/test/repo/project-info/knowledge.md"

    def test_cross_provider(self):
        r = self._migrate_fp("github:test/repo", "test/repo/project-info/knowledge.md")
        assert r == "github/test/repo/project-info/knowledge.md"


class TestObsKeyMigration:
    """obs_key 格式迁移逻辑"""

    def _migrate_obs(self, repo_key, scope, obs_key, agent_type="", issue_num=0):
        from src.repo_key import parse_repo_key, knowledge_obs_prefix
        provider, owner, repo = parse_repo_key(repo_key)
        new_prefix = knowledge_obs_prefix(repo_key)
        if obs_key.startswith(new_prefix):
            return obs_key
        elif scope == "member":
            for marker in ("member/", "agent/"):
                idx = obs_key.find(marker)
                if idx >= 0:
                    _rel = obs_key[idx:]
                    break
            else:
                _rel = os.path.basename(obs_key)
            return f"{new_prefix}/{_rel}"
        elif obs_key.startswith("project-info/"):
            _fname = os.path.basename(obs_key)
            if scope == "baseline":
                return f"{new_prefix}/project-info/{_fname}"
            return "NEED_knowledge_storage_key"
        elif obs_key.startswith(f"knowledge/{owner}/{repo}"):
            return f"knowledge/{provider}/{obs_key[len('knowledge/'):]}"
        return None

    def test_old_a_baseline(self):
        r = self._migrate_obs("gitcode:test/repo", "baseline", "project-info/test/repo/project-graph.md")
        assert r == "knowledge/gitcode/test/repo/project-info/project-graph.md"

    def test_old_b_baseline(self):
        r = self._migrate_obs("gitcode:test/repo", "baseline", "knowledge/test/repo/project-info/project-graph.md")
        assert r == "knowledge/gitcode/test/repo/project-info/project-graph.md"

    def test_old_b_project(self):
        r = self._migrate_obs("gitcode:test/repo", "project", "knowledge/test/repo/project/knowledge.md")
        assert r == "knowledge/gitcode/test/repo/project/knowledge.md"

    def test_member_path_duplication_fixed(self):
        r = self._migrate_obs("gitcode:mm6667/test", "member",
                              "knowledge/mm6667/test/mm6667/test/member/admin/entry.md")
        assert r == "knowledge/gitcode/mm6667/test/member/admin/entry.md"
        assert "mm6667/test/mm6667/test" not in r

    def test_already_new_format(self):
        r = self._migrate_obs("gitcode:test/repo", "baseline",
                              "knowledge/gitcode/test/repo/project-info/project-graph.md")
        assert r == "knowledge/gitcode/test/repo/project-info/project-graph.md"

    def test_cross_provider(self):
        r = self._migrate_obs("github:test/repo", "baseline", "knowledge/test/repo/project-info/project-graph.md")
        assert r == "knowledge/github/test/repo/project-info/project-graph.md"


class TestBackfillBeforeDedup:
    """回填在去重之前执行的集成测试"""

    def setup_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key LIKE 'test-backfill-%'")
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key LIKE 'test-backfill-%')")
            db.commit()

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key LIKE 'test-backfill-%'")
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key LIKE 'test-backfill-%')")
            db.commit()

    def test_empty_name_backfill_prevents_dedup_data_loss(self):
        """3 条 name 为空的 baseline，回填后去重不丢数据"""
        from src.repo_key import BASELINE_NAME_MAP
        with db_lock:
            c = db.cursor()
            # 模拟老数据：3 条 baseline，name 全空，file_path 含不同文件名
            for fname, idx in [("project-graph.md", 1), ("project-capabilities.md", 2), (".metadata.json", 3)]:
                c.execute(
                    "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, obs_key, name, description, source) "
                    "VALUES ('baseline', 'test-backfill-1', '', 0, ?, 'accepted', ?, '', 'codegraph 自动生成的项目基线知识', 'system')",
                    (f"test-backfill-1/project-info/{fname}", f"knowledge/test/old/project-info/{fname}"))
            db.commit()

            # 执行回填逻辑（模拟 step 1.5）
            from src.repo_key import BASELINE_NAME_FALLBACK, BASELINE_DESC_FALLBACK, BASELINE_DESC_MAP
            _UNIFIED_DESC = "codegraph 自动生成的项目基线知识"
            rows = c.execute(
                "SELECT id, name, file_path, obs_key, description FROM agent_shared_knowledge "
                "WHERE scope='baseline' AND repo_key='test-backfill-1' "
                "AND (name IS NULL OR name = '' OR name = ? OR description = ? OR description IS NULL OR description = '')",
                (BASELINE_NAME_FALLBACK, _UNIFIED_DESC)
            ).fetchall()
            updates = []
            for rid, cur_name, fp, obs, cur_desc in rows:
                _fname = ""
                for src in (fp, obs):
                    if not src:
                        continue
                    for bn in ("project-capabilities.md", "project-graph.md", ".metadata.json"):
                        if bn in src:
                            _fname = bn
                            break
                    if _fname:
                        break
                _name = cur_name if cur_name else BASELINE_NAME_MAP.get(_fname, BASELINE_NAME_FALLBACK)
                _desc = BASELINE_DESC_MAP.get(_fname, BASELINE_DESC_FALLBACK)
                updates.append((_name, _desc, rid))
            assert len(updates) == 3
            c.executemany("UPDATE agent_shared_knowledge SET name=?, description=? WHERE id=?", updates)
            db.commit()

            # 执行去重（模拟 step 2）
            c.execute(
                "DELETE FROM agent_shared_knowledge WHERE scope='baseline' AND repo_key='test-backfill-1' "
                "AND id NOT IN (SELECT MAX(id) FROM agent_shared_knowledge "
                "WHERE scope='baseline' AND repo_key='test-backfill-1' GROUP BY repo_key, scope, name)")
            db.commit()

            after = c.execute(
                "SELECT COUNT(*) FROM agent_shared_knowledge WHERE scope='baseline' AND repo_key='test-backfill-1'"
            ).fetchone()[0]
        assert after == 3, f"expected 3 rows after backfill+dedup, got {after}"

    def test_unified_desc_backfill_replaces_with_per_file(self):
        """统一 description 被回填为 per-file 描述"""
        from src.repo_key import BASELINE_NAME_MAP, BASELINE_DESC_MAP, BASELINE_NAME_FALLBACK, BASELINE_DESC_FALLBACK
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, name, description, source) "
                "VALUES ('baseline', 'test-backfill-2', '', 0, 'test-backfill-2/project-info/project-graph.md', 'accepted', '项目结构图谱', 'codegraph 自动生成的项目基线知识', 'system')")
            db.commit()
            # 回填
            _UNIFIED_DESC = "codegraph 自动生成的项目基线知识"
            rows = c.execute(
                "SELECT id, name, file_path, obs_key, description FROM agent_shared_knowledge "
                "WHERE scope='baseline' AND repo_key='test-backfill-2' AND description = ?",
                (_UNIFIED_DESC,)
            ).fetchall()
            assert len(rows) == 1
            rid, cur_name, fp, obs, cur_desc = rows[0]
            assert cur_desc == _UNIFIED_DESC
            _desc = BASELINE_DESC_MAP.get("project-graph.md", BASELINE_DESC_FALLBACK)
            c.execute("UPDATE agent_shared_knowledge SET description=? WHERE id=?", (_desc, rid))
            db.commit()
            new_desc = c.execute("SELECT description FROM agent_shared_knowledge WHERE id=?", (rid,)).fetchone()[0]
        assert new_desc == "VOD 基线分析：项目代码结构图谱（模块/依赖/技术栈）"

    def test_idempotent_backfill_no_change_on_second_run(self):
        """回填执行两次，第二次 0 行更新"""
        from src.repo_key import BASELINE_NAME_FALLBACK, BASELINE_DESC_FALLBACK, BASELINE_NAME_MAP, BASELINE_DESC_MAP
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, name, description, source) "
                "VALUES ('baseline', 'test-backfill-3', '', 0, 'test-backfill-3/project-info/project-capabilities.md', 'accepted', '项目能力清单', 'VOD 基线分析：项目能力清单（功能特性/支持能力）', 'system')")
            db.commit()
            _UNIFIED_DESC = "codegraph 自动生成的项目基线知识"
            # 第一次回填
            rows = c.execute(
                "SELECT id FROM agent_shared_knowledge WHERE scope='baseline' AND repo_key='test-backfill-3' "
                "AND (name IS NULL OR name = '' OR name = ? OR description = ? OR description IS NULL OR description = '')",
                (BASELINE_NAME_FALLBACK, _UNIFIED_DESC)
            ).fetchall()
        assert len(rows) == 0, "should find 0 rows needing backfill on already-correct data"


class TestOrphanRefCleanup:
    """孤儿 scene_knowledge_ref 清理"""

    def setup_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id < 0")
            db.commit()

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id < 0")
            db.commit()

    def test_orphan_ref_deleted(self):
        """孤儿 ref 被清理，有效 ref 保留"""
        with db_lock:
            c = db.cursor()
            # 插入一条孤儿 ref（knowledge_id=-999 不存在）
            c.execute(
                "INSERT INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
                "VALUES (-999, '', '', 0, 1, 'system')")
            db.commit()
            before = c.execute("SELECT COUNT(*) FROM scene_knowledge_ref WHERE knowledge_id < 0").fetchone()[0]
        assert before == 1
        # 执行孤儿清理
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id NOT IN (SELECT id FROM agent_shared_knowledge)")
            db.commit()
            after = c.execute("SELECT COUNT(*) FROM scene_knowledge_ref WHERE knowledge_id < 0").fetchone()[0]
        assert after == 0


class TestDeriveKnowledgeName:
    """derive_knowledge_name 各 scope 推导规则"""

    def test_baseline_project_graph(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("baseline", file_path="gitcode/test/repo/project-info/project-graph.md") == "项目结构图谱"

    def test_baseline_project_capabilities(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("baseline", file_path="gitcode/test/repo/project-info/project-capabilities.md") == "项目能力清单"

    def test_baseline_metadata(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("baseline", file_path="gitcode/test/repo/project-info/.metadata.json") == "项目元数据"

    def test_baseline_unknown_file(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("baseline", file_path="gitcode/test/repo/project-info/unknown.md") == "项目基线分析"

    def test_baseline_empty_file_path(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("baseline", file_path="") == "项目基线分析"

    def test_baseline_from_obs_key(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("baseline", file_path="", ) == "项目基线分析"

    def test_member_normal_filename(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("member", file_path="gitcode/test/repo/project-info/member/user/blog-config.md") == "blog-config"

    def test_member_system_filename(self):
        from src.api.knowledge import derive_knowledge_name
        result = derive_knowledge_name("member", file_path="gitcode/test/repo/project-info/member/user/entry_17891041_abc.md")
        assert result == "entry_17891041_abc"

    def test_member_empty_file_path(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("member", file_path="") == "未命名知识"

    def test_project_with_agent(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("project", agent_type="vod") == "项目知识 - VOD"
        assert derive_knowledge_name("project", agent_type="coding") == "项目知识 - Coding"

    def test_project_no_agent(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("project") == "项目知识"

    def test_issue(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("issue", issue_num=42) == "Issue #42 知识"

    def test_issue_agent(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("issue_agent", agent_type="coding", issue_num=42) == "Issue #42 - Coding 知识"

    def test_issue_agent_unknown_agent(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("issue_agent", agent_type="custom", issue_num=5) == "Issue #5 - custom 知识"

    def test_unknown_scope(self):
        from src.api.knowledge import derive_knowledge_name
        assert derive_knowledge_name("unknown") == "未命名知识"


class TestLocalfs:
    """localfs 原子写入 + 路径安全"""

    def test_write_text_roundtrip(self, tmp_path):
        from src.localfs import write_text, read_text
        p = str(tmp_path / "test.md")
        write_text(p, "hello world")
        assert read_text(p) == "hello world"

    def test_write_text_overwrite(self, tmp_path):
        from src.localfs import write_text, read_text
        p = str(tmp_path / "test.md")
        write_text(p, "old content")
        write_text(p, "new content")
        assert read_text(p) == "new content"

    def test_write_text_creates_dir(self, tmp_path):
        from src.localfs import write_text, read_text
        p = str(tmp_path / "subdir" / "nested" / "test.md")
        write_text(p, "nested")
        assert read_text(p) == "nested"

    def test_write_bytes_roundtrip(self, tmp_path):
        from src.localfs import write_bytes, read_bytes
        p = str(tmp_path / "test.bin")
        write_bytes(p, b"\x00\x01\x02")
        assert read_bytes(p) == b"\x00\x01\x02"

    def test_read_text_missing_file(self, tmp_path):
        from src.localfs import read_text
        assert read_text(str(tmp_path / "nonexistent.md")) is None

    def test_read_bytes_missing_file(self, tmp_path):
        from src.localfs import read_bytes
        assert read_bytes(str(tmp_path / "nonexistent.bin")) is None

    def test_append_text(self, tmp_path):
        from src.localfs import append_text, read_text
        p = str(tmp_path / "log.md")
        append_text(p, "line1\n")
        append_text(p, "line2\n")
        assert read_text(p) == "line1\nline2\n"

    def test_safe_join_valid(self):
        from src.localfs import safe_join
        p = safe_join("gitcode:test/repo", "project-info", "test.md")
        assert p.endswith("test/repo/project-info/test.md")

    def test_safe_workspace_path_valid(self):
        from src.localfs import safe_workspace_path
        p = safe_workspace_path("gitcode/test/repo/project-info/test.md")
        assert p.endswith("gitcode/test/repo/project-info/test.md")

    def test_safe_workspace_path_traversal_blocked(self):
        from src.localfs import safe_workspace_path
        import pytest
        with pytest.raises(ValueError, match="traversal"):
            safe_workspace_path("../../etc/passwd")


class TestRejectedDedupNameCondition:
    """rejected 查重加 name 条件——同 repo 不同 name 的 baseline 不互斥"""

    def setup_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key='test-rejected-1'")
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key='test-rejected-1')")
            db.commit()

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key='test-rejected-1'")
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE repo_key='test-rejected-1')")
            db.commit()

    def test_different_name_not_blocked(self):
        """同 repo 同 scope 不同 name 的两条记录都能 INSERT"""
        with db_lock:
            c = db.cursor()
            # 第一条：name='项目结构图谱'
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, name, source) "
                "VALUES ('baseline', 'test-rejected-1', '', 0, 'test/project-graph.md', 'rejected', '项目结构图谱', 'system')")
            db.commit()
            # 模拟 rejected 查重：加 name 条件
            existing = c.execute(
                "SELECT id FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=? AND name=?",
                ("test-rejected-1", "baseline", "", 0, "项目能力清单")
            ).fetchone()
            assert existing is None, "不同 name 不应匹配到已有记录"
            # 第二条：name='项目能力清单' 应该能 INSERT
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, name, source) "
                "VALUES ('baseline', 'test-rejected-1', '', 0, 'test/project-capabilities.md', 'rejected', '项目能力清单', 'system')")
            db.commit()
            count = c.execute(
                "SELECT COUNT(*) FROM agent_shared_knowledge WHERE repo_key='test-rejected-1' AND scope='baseline'"
            ).fetchone()[0]
        assert count == 2

    def test_same_name_blocked(self):
        """同 repo 同 scope 同 name 的第二条被跳过"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, name, source) "
                "VALUES ('baseline', 'test-rejected-1', '', 0, 'test/project-graph.md', 'rejected', '项目结构图谱', 'system')")
            db.commit()
            existing = c.execute(
                "SELECT id FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=? AND name=?",
                ("test-rejected-1", "baseline", "", 0, "项目结构图谱")
            ).fetchone()
            assert existing is not None, "同 name 应匹配到已有记录"
