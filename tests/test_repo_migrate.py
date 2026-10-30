"""Issue #540：仓库迁移（repo_key 原子改写）— 测试。

覆盖：
- 批量改写：全部关联表 repo_key 由 old → new，托管字段（owner/name/full_name）同步更新；
- UNIQUE(user_id, repo_key) 冲突：目标 repo_key 已有行时删旧留新（保留迁移行）；
- 失败回滚：DB 事务中途异常 → rollback，无半改写；
- 工作区目录物理迁移：old 目录内容移到 new 目录；
- skill(scope='repo') owner_id / meta(onboarded:*) 改写。
"""
import json
import os
import sys

import pytest

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.repo_migrate import migrate_repo_key, RepoMigrateError

SRC = "ownerA/repo540"
SRC_KEY = "gitcode:" + SRC
DST = "ownerB/repo540"
DST_KEY = "gitcode:" + DST

_TABLES = ("issue", "issue_state", "issue_agent_session", "events", "event_skipped",
           "event_thread", "event_route_config", "agent_tasks", "task_messages",
           "agent_shared_knowledge", "scene_knowledge_ref", "devops_pipeline_scene",
           "chat_conversations", "chat_messages", "chat_agent_session",
           "devops_agent_session", "vod_conversations", "repo_im_group_binding",
           "pipeline_instances", "skill", "meta", "managed_repos", "audit_logs",
           "user_repos")


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
        "DELETE FROM task_messages WHERE task_id LIKE 'task-540-%'",
        "DELETE FROM agent_shared_knowledge WHERE repo_key IN (?, ?)",
        "DELETE FROM scene_knowledge_ref WHERE scene_id='sc-540'",
        "DELETE FROM devops_pipeline_scene WHERE repo_key IN (?, ?)",
        "DELETE FROM chat_conversations WHERE repo_key IN (?, ?)",
        "DELETE FROM chat_messages WHERE conversation_id='conv-540'",
        "DELETE FROM chat_agent_session WHERE repo_key IN (?, ?)",
        "DELETE FROM devops_agent_session WHERE repo_key IN (?, ?)",
        "DELETE FROM vod_conversations WHERE agent_session_id='sess-540'",
        "DELETE FROM repo_im_group_binding WHERE repo_key IN (?, ?)",
        "DELETE FROM pipeline_instances WHERE repo_key IN (?, ?)",
        "DELETE FROM skill WHERE scope='repo' AND owner_id IN (?, ?)",
        "DELETE FROM meta WHERE `key` LIKE 'onboarded:%' OR `key` LIKE 'cleanup_pending:%' OR `key` IN (?, ?)",
        "DELETE FROM managed_repos WHERE repo_key IN (?, ?)",
        "DELETE FROM user_repos WHERE repo_key IN (?, ?)",
        "DELETE FROM audit_logs WHERE detail LIKE '%repo540%'",
    ]
    with db_lock:
        c = db.cursor()
        for s in stmts:
            try:
                if "?" in s:
                    c.execute(s, (SRC_KEY, DST_KEY))
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


def _seed():
    """按全部关联表造数据（源 repo_key）。"""
    # managed_repos
    _insert("managed_repos",
            ["repo_key", "repo_owner", "repo_name", "full_name", "webhook_id",
             "dev_scene", "bot_gitcode_token", "gitcode_webhook_token", "bot_username",
             "org_id"],
            (SRC_KEY, "ownerA", "repo540", SRC, 42, "dev-scene-x",
             "tok-x", "wh-tok-x", "grape_bot", 0))
    # user_repos
    _insert("user_repos",
            ["user_id", "repo_key", "owner", "name", "role", "is_admin", "is_join"],
            (1, SRC_KEY, "ownerA", "repo540", "admin", 1, 1))
    # 需求中心
    _insert("issue", ["user_id", "assignee", "repo_key", "issue_number", "title"],
            (1, "bot", SRC_KEY, 7, "迁移卡片"))
    _insert("issue_state", ["repo_key", "issue_id", "stage"], (SRC_KEY, "#7", "coding"))
    _insert("issue_agent_session", ["id", "repo_key", "agent_type", "issue_id"],
            ("ias-540", SRC_KEY, "coding", "#7"))
    # 事件流水线
    _insert("events", ["repo_key", "event_type", "title", "payload", "status"],
            (SRC_KEY, "issue_opened", "t", "{}", "completed"))
    _insert("event_skipped", ["repo_key", "event_type", "reason"], (SRC_KEY, "x", "no_route"))
    _insert("event_thread", ["anchor_seq", "repo_key", "agent_type", "role", "content"],
            (9101, SRC_KEY, "vod", "user", "hi"))
    _insert("event_route_config", ["repo_key", "event_type", "skill"],
            (SRC_KEY, "issue_opened", "issue"))
    _insert("agent_tasks", ["id", "agent_session_id", "runtime_id", "repo_key",
                            "agent_type", "prompt"],
            ("task-540-1", "sess-540", 1, SRC_KEY, "coding", "do work"))
    _insert("task_messages", ["task_id", "role", "content"],
            ("task-540-1", "assistant", "done"))
    _insert("agent_shared_knowledge", ["repo_key", "title", "content"],
            (SRC_KEY, "kb", "body"))
    _insert("scene_knowledge_ref", ["scene_id", "knowledge_id"],
            ("sc-540", 1))
    _insert("devops_pipeline_scene", ["repo_key", "scene_id"],
            (SRC_KEY, "dev-scene-x"))
    # 会话
    _insert("chat_conversations", ["repo_key", "title"],
            (SRC_KEY, "c"))
    _insert("chat_agent_session", ["repo_key", "agent_type"],
            (SRC_KEY, "coding"))
    _insert("devops_agent_session", ["repo_key", "agent_type"],
            (SRC_KEY, "coding"))
    _insert("repo_im_group_binding", ["repo_key", "group_id"],
            (SRC_KEY, "g1"))
    _insert("pipeline_instances", ["repo_key", "status"],
            (SRC_KEY, "running"))
    # skill (skill_name, scope, owner_id) — content 列不存在
    _insert("skill", ["skill_name", "scope", "owner_id"],
            ("repo-skill-540", "repo", SRC_KEY))
    # meta
    _insert("meta", ["key", "value"], (f"onboarded:{SRC_KEY}", "1"))
    _insert("meta", ["key", "value"], (f"cleanup_pending:{SRC_KEY}", "1"))


@pytest.fixture(autouse=True)
def _reset():
    _clean()
    _seed()
    yield
    _clean()


# ---------------------------------------------------------------------------
# 1. 批量改写全部生效
# ---------------------------------------------------------------------------

def test_migrate_rewrites_all_tables():
    """迁移后：源 repo_key 行全部消失，目标 repo_key 行全部出现；托管字段同步更新。"""
    stats = migrate_repo_key(SRC_KEY, "ownerB", "repo540", operator_id=1)
    assert stats["tables"] > 0
    # managed_repos: 源消失、目标出现，字段更新
    assert _count("managed_repos", "repo_key=?", (SRC_KEY,)) == 0
    assert _count("managed_repos", "repo_key=?", (DST_KEY,)) == 1
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT repo_owner, repo_name, full_name, webhook_id, dev_scene, "
            "bot_gitcode_token, gitcode_webhook_token, bot_username, org_id "
            "FROM managed_repos WHERE repo_key=?", (DST_KEY,)).fetchone()
    assert row[0] == "ownerB"
    assert row[1] == "repo540"
    assert row[2] == DST
    assert row[3] == 42  # webhook_id 保留
    assert row[4] == "dev-scene-x"  # dev_scene 保留
    assert row[5] == "tok-x"  # bot token 保留
    assert row[6] == "wh-tok-x"  # webhook token 保留
    assert row[7] == "grape_bot"  # bot username 保留
    # user_repos
    assert _count("user_repos", "repo_key=?", (SRC_KEY,)) == 0
    assert _count("user_repos", "repo_key=?", (DST_KEY,)) == 1
    # issue / events / agent_tasks 等
    assert _count("issue", "repo_key=?", (SRC_KEY,)) == 0
    assert _count("issue", "repo_key=?", (DST_KEY,)) == 1
    assert _count("events", "repo_key=?", (SRC_KEY,)) == 0
    assert _count("events", "repo_key=?", (DST_KEY,)) == 1
    assert _count("agent_tasks", "repo_key=?", (SRC_KEY,)) == 0
    assert _count("agent_tasks", "repo_key=?", (DST_KEY,)) == 1
    # skill(scope='repo')
    assert _count("skill", "scope='repo' AND owner_id=?", (SRC_KEY,)) == 0
    assert _count("skill", "scope='repo' AND owner_id=?", (DST_KEY,)) == 1
    # meta
    assert _count("meta", "`key`=?", (f"onboarded:{SRC_KEY}",)) == 0
    assert _count("meta", "`key`=?", (f"onboarded:{DST_KEY}",)) == 1
    assert _count("meta", "`key`=?", (f"cleanup_pending:{SRC_KEY}",)) == 0
    assert _count("meta", "`key`=?", (f"cleanup_pending:{DST_KEY}",)) == 1


# ---------------------------------------------------------------------------
# 2. UNIQUE(user_id, repo_key) 冲突 → 删旧留新
# ---------------------------------------------------------------------------

def test_migrate_user_repos_conflict_drops_old():
    """目标 repo_key 在 user_repos 已有行（同一 user_id）→ 删旧行留迁移行。"""
    # 预置冲突行：user_id=1 已有 DST_KEY
    _insert("user_repos",
            ["user_id", "repo_key", "owner", "name", "role", "is_admin", "is_join"],
            (1, DST_KEY, "ownerB", "repo540", "member", 0, 0))
    # 迁移后 user_id=1 + DST_KEY 仅一行，且 is_join/is_admin 来自源行（1/1）
    migrate_repo_key(SRC_KEY, "ownerB", "repo540", operator_id=1)
    assert _count("user_repos", "user_id=1 AND repo_key=?", (DST_KEY,)) == 1
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT is_join, is_admin FROM user_repos WHERE user_id=1 AND repo_key=?",
            (DST_KEY,)).fetchone()
    assert row[0] == 1  # 源行 is_join=1 保留
    assert row[1] == 1  # 源行 is_admin=1 保留


# ---------------------------------------------------------------------------
# 3. 失败回滚：DB 事务中途异常 → rollback，无半改写
# ---------------------------------------------------------------------------

def test_migrate_rollback_on_db_error(monkeypatch):
    """DB 更新中途抛异常 → rollback，managed_repos 保持源 repo_key 不变。"""

    def _boom(*a, **k):
        raise RuntimeError("simulated DB failure")
    # 在 user_repos UPDATE 阶段注入异常
    import src.repo_migrate as rm
    orig = rm._update_user_repos
    monkeypatch.setattr(rm, "_update_user_repos", _boom)
    with pytest.raises(RepoMigrateError):
        migrate_repo_key(SRC_KEY, "ownerB", "repo540", operator_id=1)
    # 回滚：源 repo_key 行仍在
    assert _count("managed_repos", "repo_key=?", (SRC_KEY,)) == 1
    assert _count("managed_repos", "repo_key=?", (DST_KEY,)) == 0
    assert _count("issue", "repo_key=?", (SRC_KEY,)) == 1
    assert _count("issue", "repo_key=?", (DST_KEY,)) == 0
    # 恢复避免影响其它用例
    monkeypatch.setattr(rm, "_update_user_repos", orig)


# ---------------------------------------------------------------------------
# 4. 工作区目录物理迁移
# ---------------------------------------------------------------------------

def test_migrate_workspace_dir(tmp_path, monkeypatch):
    """工作区目录从 old 路径迁移到 new 路径。"""
    from src.config import WORKSPACE_DIR
    from src.repo_key import workspace_repo_dir
    old_dir = workspace_repo_dir(SRC_KEY)
    new_dir = workspace_repo_dir(DST_KEY)
    os.makedirs(old_dir, exist_ok=True)
    marker = os.path.join(old_dir, "project-graph.md")
    with open(marker, "w") as f:
        f.write("baseline")
    try:
        migrate_repo_key(SRC_KEY, "ownerB", "repo540", operator_id=1)
        assert not os.path.exists(old_dir)
        assert os.path.isdir(new_dir)
        assert os.path.exists(os.path.join(new_dir, "project-graph.md"))
    finally:
        for d in (old_dir, new_dir):
            if os.path.isdir(d):
                import shutil
                shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. 空源 repo_key / 目标与源相同 → 不改写
# ---------------------------------------------------------------------------

def test_migrate_same_key_noop():
    """目标 owner/repo 与源相同 → 不改写（返回空统计）。"""
    stats = migrate_repo_key(SRC_KEY, "ownerA", "repo540", operator_id=1)
    assert stats["tables"] == 0
    assert _count("managed_repos", "repo_key=?", (SRC_KEY,)) == 1


def test_migrate_empty_key():
    """空 repo_key → 返回空统计，不报错。"""
    stats = migrate_repo_key("", "ownerB", "repo540", operator_id=1)
    assert stats["tables"] == 0
