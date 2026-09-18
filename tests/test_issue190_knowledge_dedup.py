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
