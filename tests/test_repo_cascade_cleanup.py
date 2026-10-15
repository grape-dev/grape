"""Issue #340：解除托管/仓库删除级联清空全部关联数据 — 测试。

覆盖（docs/issue-340/spec.md 验收标准）：
- 触发路径一（平台解除托管）：cascade_cleanup_repo_data(trigger=platform) 全表清理；
- 触发路径二（代码仓删除）：webhook 仓库删除事件 → _handle_repo_deleted 等价清理；
- 事务化：DB 删除中途失败 → 回滚，无半清理，RepoCleanupError 可重试；
- dry-run 预检；
- 审计日志（audit_logs）；
- 文件清理补偿（meta pending 标记 + retry_pending_file_cleanups）。
"""
import json
import os
import sys

import pytest

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.repo_cleanup import (
    cascade_cleanup_repo_data,
    retry_pending_file_cleanups,
    RepoCleanupError,
)

REPO = "owner/issue340-repo"
REPO_KEY = "gitcode:" + REPO


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLES = ("issue", "issue_state", "issue_agent_session", "events", "event_skipped",
           "event_thread", "event_route_config", "agent_tasks", "task_messages",
           "agent_shared_knowledge", "scene_knowledge_ref", "devops_pipeline_scene",
           "chat_conversations", "chat_messages", "chat_agent_session",
           "devops_agent_session", "vod_conversations", "repo_im_group_binding",
           "pipeline_instances", "skill", "meta", "managed_repos", "audit_logs")


def _clean():
    """容错清空：个别历史表可能不存在（老库），逐条 try。"""
    stmts = [
        "DELETE FROM issue WHERE repo_key IN (?, ?)",
        "DELETE FROM issue_state WHERE repo_key IN (?, ?)",
        "DELETE FROM issue_agent_session WHERE repo_key IN (?, ?)",
        "DELETE FROM events WHERE repo_key IN (?, ?)",
        "DELETE FROM event_skipped WHERE repo_key IN (?, ?)",
        "DELETE FROM event_thread WHERE repo_key IN (?, ?)",
        "DELETE FROM event_route_config WHERE repo_key IN (?, ?)",
        "DELETE FROM agent_tasks WHERE repo_key IN (?, ?)",
        "DELETE FROM task_messages WHERE task_id LIKE 'task-340-%'",
        "DELETE FROM agent_shared_knowledge WHERE repo_key IN (?, ?)",
        "DELETE FROM scene_knowledge_ref WHERE scene_id='sc-340'",
        "DELETE FROM devops_pipeline_scene WHERE repo_key IN (?, ?)",
        "DELETE FROM chat_conversations WHERE repo_key IN (?, ?)",
        "DELETE FROM chat_messages WHERE conversation_id='conv-340'",
        "DELETE FROM chat_agent_session WHERE repo_key IN (?, ?)",
        "DELETE FROM devops_agent_session WHERE repo_key IN (?, ?)",
        "DELETE FROM vod_conversations WHERE agent_session_id='sess-340'",
        "DELETE FROM repo_im_group_binding WHERE repo_key IN (?, ?)",
        "DELETE FROM pipeline_instances WHERE repo_key IN (?, ?)",
        "DELETE FROM skill WHERE scope='repo' AND owner_id IN (?, ?)",
        "DELETE FROM meta WHERE `key` LIKE 'cleanup_pending:%' OR `key` IN (?, ?)",
        "DELETE FROM managed_repos WHERE repo_key IN (?, ?)",
        "DELETE FROM audit_logs WHERE detail LIKE '%issue340-repo%'",
    ]
    with db_lock:
        c = db.cursor()
        for s in stmts:
            try:
                if "?" in s:
                    c.execute(s, (REPO, REPO_KEY))
                else:
                    c.execute(s)
            except Exception:
                pass
        db.commit()


def _insert(table, cols, vals):
    with db_lock:
        c = db.cursor()
        ph = ",".join("?" for _ in cols)
        try:
            c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", vals)
        except Exception:
            pass  # 历史表可能不存在
        db.commit()


def _count(table, where, params):
    with db_lock:
        c = db.cursor()
        return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def _seed_all():
    """按全部关联表造数据（含前缀双写）。"""
    # 需求中心
    _insert("issue", ["user_id", "assignee", "repo_key", "issue_number", "title"],
            (1, "bot", REPO_KEY, 5, "卡片A"))
    _insert("issue", ["user_id", "assignee", "repo_key", "issue_number", "title"],
            (1, "bot", REPO, 6, "卡片B（无前缀旧数据）"))
    _insert("issue_state", ["repo_key", "issue_id", "stage"], (REPO_KEY, "#5", "coding"))
    _insert("issue_agent_session", ["id", "repo_key", "agent_type", "issue_id"],
            ("ias-340", REPO_KEY, "coding", "#5"))
    # 事件流水线
    _insert("events", ["repo_key", "event_type", "title", "payload", "status"],
            (REPO_KEY, "issue_opened", "t", "{}", "completed"))
    _insert("event_skipped", ["repo_key", "event_type", "reason"], (REPO_KEY, "x", "no_route"))
    _insert("event_thread", ["anchor_seq", "repo_key", "agent_type", "role", "content"],
            (9001, REPO_KEY, "vod", "user", "hi"))
    _insert("event_route_config", ["repo_key", "event_type", "skill"],
            (REPO_KEY, "issue_opened", "issue"))
    _insert("agent_tasks", ["id", "agent_session_id", "runtime_id", "repo_key",
                            "agent_type", "prompt"],
            ("task-340-1", "sess-340", 1, REPO_KEY, "coding", "do work"))
    _insert("task_messages", ["task_id", "seq", "type", "content"],
            ("task-340-1", 1, "text", "msg"))
    # 知识 + 场景关联
    _insert("agent_shared_knowledge", ["scope", "repo_key", "file_path"],
            ("project", REPO_KEY, "project-info/knowledge.md"))
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM agent_shared_knowledge WHERE repo_key=?", (REPO_KEY,))
        kn_id = c.fetchone()[0]
        db.commit()
    _insert("scene_knowledge_ref", ["knowledge_id", "scene_id"], (kn_id, "sc-340"))
    _insert("devops_pipeline_scene", ["scene_id", "name", "repo_key"], ("sc-340", "s", REPO_KEY))
    # 会话
    _insert("chat_conversations", ["id", "user_id", "repo_key"], ("conv-340", 1, REPO_KEY))
    _insert("chat_messages", ["conversation_id", "role", "content"], ("conv-340", "user", "hi"))
    _insert("chat_agent_session", ["id", "repo_key"], ("cas-340", REPO_KEY))
    _insert("devops_agent_session", ["id", "repo_key", "agent_type"], ("sess-340", REPO_KEY, "vod"))
    _insert("vod_conversations", ["agent_session_id", "im_chat_id"], ("sess-340", "v340"))
    _insert("repo_im_group_binding", ["im_chat_id", "repo_key"], ("g340", REPO_KEY))
    # 技能 + onboarding 标记
    _insert("skill", ["skill_name", "scope", "owner_id"], ("sk-340", "repo", REPO_KEY))
    _insert("meta", ["key", "value"], (f"onboarded:{REPO_KEY}", "1"))
    return kn_id


# ---------------------------------------------------------------------------
# 触发路径一：平台解除托管 → 级联清理
# ---------------------------------------------------------------------------

class TestPlatformUnhostCascade:
    def test_clears_all_related_tables(self):
        _clean()
        _seed_all()
        stats = cascade_cleanup_repo_data(REPO_KEY, "owner", "issue340-repo", trigger="platform")
        assert stats["dry_run"] is False
        # 需求中心卡片清空
        assert _count("issue", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("issue_state", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("issue_agent_session", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        # 流水线/事件
        assert _count("events", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("event_skipped", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("event_thread", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("event_route_config", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("agent_tasks", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("task_messages", "task_id LIKE 'task-340-%'", ()) == 0
        # 知识 + 场景关联
        assert _count("agent_shared_knowledge", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("scene_knowledge_ref", "scene_id='sc-340'", ()) == 0
        assert _count("devops_pipeline_scene", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        # 会话
        assert _count("chat_conversations", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("chat_messages", "conversation_id='conv-340'", ()) == 0
        assert _count("chat_agent_session", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("devops_agent_session", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("vod_conversations", "agent_session_id='sess-340'", ()) == 0
        assert _count("repo_im_group_binding", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        # 技能 + onboarding 标记
        assert _count("skill", "scope='repo' AND owner_id IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("meta", "`key` IN (?, ?)",
                      (f"onboarded:{REPO}", f"onboarded:{REPO_KEY}")) == 0
        assert stats["tables"] > 0

    def test_audit_logged(self):
        _clean()
        _seed_all()
        before = _count("audit_logs", "detail LIKE '%issue340-repo%'", ())
        cascade_cleanup_repo_data(REPO_KEY, "owner", "issue340-repo",
                                  trigger="platform", operator_id=7)
        after = _count("audit_logs", "detail LIKE '%issue340-repo%'", ())
        assert after == before + 1
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT event_type, detail FROM audit_logs WHERE detail LIKE ? "
                            "ORDER BY seq DESC LIMIT 1", ("%issue340-repo%",)).fetchone()
        assert row[0] == "repo_cascade_cleanup"
        detail = json.loads(row[1])
        assert detail["repo_key"] == REPO_KEY
        assert detail["trigger"] == "platform"

    def test_dry_run_does_not_delete(self):
        _clean()
        _seed_all()
        stats = cascade_cleanup_repo_data(REPO_KEY, "owner", "issue340-repo",
                                          trigger="platform", dry_run=True)
        assert stats["dry_run"] is True
        assert stats["tables"] > 0
        # 数据未动
        assert _count("issue", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 2
        assert _count("events", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 1

    def test_invalid_owner_repo_skipped(self):
        _clean()
        stats = cascade_cleanup_repo_data(REPO_KEY, "../etc", "passwd", trigger="platform")
        assert stats["tables"] == 0

    def test_transaction_rollback_no_half_cleanup(self, monkeypatch):
        """DB 删除中途失败 → 全量回滚 + RepoCleanupError（可重试）"""
        _clean()
        _seed_all()
        import src.repo_cleanup as rc
        # 强制事务中后段失败：把假表加入清理表清单 + 探测结果，
        # 前面的表（issue 等）删除成功后，DELETE 假表报错 → 整体回滚
        real_existing = rc._existing_tables

        def _fake_existing(c):
            return real_existing(c) | {"__bogus_table_340__"}

        monkeypatch.setattr(rc, "_existing_tables", _fake_existing)
        monkeypatch.setattr(rc, "_REPO_KEY_TABLES",
                            rc._REPO_KEY_TABLES + ("__bogus_table_340__",))
        with pytest.raises(RepoCleanupError):
            cascade_cleanup_repo_data(REPO_KEY, "owner", "issue340-repo", trigger="platform")
        # 回滚后无半清理：全部数据仍在
        assert _count("issue", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 2
        assert _count("events", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 1
        assert _count("agent_tasks", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 1
        assert _count("chat_conversations", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 1
        # 失败也留审计（error 记录）
        assert _count("audit_logs", "detail LIKE '%issue340-repo%'", ()) >= 1


# ---------------------------------------------------------------------------
# 触发路径二：代码仓删除 → webhook 事件 → 等价清理
# ---------------------------------------------------------------------------

class TestWebhookRepoDeletedCascade:
    def test_is_repo_delete_event_variants(self):
        from src.api.routes.webhook import _is_repo_delete_event
        # GitCode 头
        assert _is_repo_delete_event("repo.destroyed", {}) is True
        assert _is_repo_delete_event("Repository Deleted", {}) is True
        assert _is_repo_delete_event("repo.destroy", {}) is True
        # GitHub：repository + action=deleted
        assert _is_repo_delete_event("repository", {"action": "deleted"}) is True
        # 非删除事件
        assert _is_repo_delete_event("issue_comment", {"action": "created"}) is False
        assert _is_repo_delete_event("push", {}) is False
        assert _is_repo_delete_event("repository", {"action": "created"}) is False
        assert _is_repo_delete_event("", {}) is False

    def test_handle_repo_deleted_cascades_and_unmanages(self):
        _clean()
        _seed_all()
        _insert("managed_repos",
                ["repo_key", "repo_owner", "repo_name", "full_name", "webhook_id",
                 "dev_scene", "gitcode_webhook_token", "bot_username"],
                (REPO_KEY, "owner", "issue340-repo", REPO, 0, "default", "wt-340", "bot"))
        from src.api.routes.webhook import _handle_repo_deleted
        result = _handle_repo_deleted(REPO, "gitcode", "event=repo.destroyed")
        assert result["status"] == "repo_deleted"
        # 等价清理完成
        assert _count("issue", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("events", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("agent_tasks", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        assert _count("devops_pipeline_scene", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        # 托管记录移除（webhook 订阅解除）
        assert _count("managed_repos", "repo_key IN (?, ?)", (REPO, REPO_KEY)) == 0
        # 审计
        assert _count("audit_logs", "detail LIKE '%issue340-repo%'", ()) >= 1

    def test_github_delete_event_header_path(self):
        from src.api.routes.webhook import _is_repo_delete_event
        assert _is_repo_delete_event("repository", {"action": "deleted"}) is True
        assert _is_repo_delete_event("repository", {"action": "transferred"}) is False


# ---------------------------------------------------------------------------
# 文件清理补偿（pending 标记 + 重试）
# ---------------------------------------------------------------------------

class TestFileCleanupCompensation:
    def test_retry_pending_file_cleanups(self, tmp_path, monkeypatch):
        _clean()
        from src.config import WORKSPACE_DIR
        ws_dir = tmp_path / "ws" / "gitcode" / "owner" / "issue340-repo"
        ws_dir.mkdir(parents=True)
        (ws_dir / "project-info.md").write_text("knowledge baseline")
        monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path / "ws"))
        # 模拟历史失败残留：pending 标记 + 目录仍在
        _insert("meta", ["key", "value"], (f"cleanup_pending:{REPO_KEY}", "1"))
        remaining = retry_pending_file_cleanups()
        assert remaining == 0
        assert not ws_dir.exists()
        assert _count("meta", "`key`=?", (f"cleanup_pending:{REPO_KEY}",)) == 0
