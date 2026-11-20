"""Issue #2269/#2270: 组织上下文自动沉淀 + 聚合节点等待批次终态。

覆盖：
- compute_node_statuses 对组织节点（id=org_* / tag=issue|coding|qa）按 tag 回退匹配；
- 组织创建自动写「组织概览」brief（active，幂等）；
- upsert_context 幂等更新，不重复堆条目；
- 下派结论 decision 自动写入（目标仓库/子 Issue/PR/状态）；
- org_context_repo 文件渲染（org-context/ 目录布局）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.api.pipeline_status import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    compute_node_statuses,
)
from src.kernel.session_manager import db, db_lock

UID = 22691
UUID_UID = "01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b68"
SLUG = "issue2269-autoseed"
UUID_SLUG = "issue2269-autoseed-uuid"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup() -> None:
    with db_lock:
        c = db.cursor()
        org_ids = [
            r[0] for r in c.execute(
                "SELECT id FROM organization WHERE owner_id IN (?, ?)",
                (UID, UUID_UID)).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM grape_t_org_context WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM agent_shared_knowledge WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id IN (?, ?)",
                  (UID, UUID_UID))
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE ?",
                  ("gitcode:issue2269/%",))
        db.commit()


def _seed_org():
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(name=SLUG, slug=SLUG, owner_id=UID, is_personal=0)
    assert org
    return org


def test_compute_node_statuses_matches_org_nodes_by_tag():
    nodes = [
        {"id": "org_intake", "tag": "issue"},
        {"id": "org_dispatch", "tag": "coding"},
        {"id": "org_aggregate", "tag": "qa"},
    ]
    events = [
        {"seq": 1, "agent_type": "issue", "status": "completed"},
        {"seq": 2, "agent_type": "coding", "status": "running"},
    ]
    _nodes, statuses = compute_node_statuses(nodes, events, "coding")
    assert statuses == [STATUS_COMPLETED, STATUS_RUNNING, 0]


def test_create_org_seeds_overview_brief():
    org = _seed_org()
    from src.domains.mgmt.org_context_store import list_contexts

    rows = [r for r in list_contexts(org["id"], kind="brief")
            if r.get("ctx_key") == "brief"]
    assert rows, "团队组织创建后应自动写入组织概览"
    assert rows[0]["lifecycle_status"] == "active"
    assert "组织概览" in rows[0]["content"]


def test_upsert_context_is_idempotent_and_republishes():
    org = _seed_org()
    from src.domains.mgmt.org_context_store import list_contexts, upsert_context

    upsert_context(org["id"], "decision", "dispatch-1", "下派 #1 结论", "# v1", user_id=UID)
    upsert_context(org["id"], "decision", "dispatch-1", "下派 #1 结论", "# v2", user_id=UID)

    rows = [r for r in list_contexts(org["id"], kind="decision")
            if r.get("ctx_key") == "dispatch-1"]
    assert len(rows) == 1, "同 key 不应重复插入"
    assert rows[0]["lifecycle_status"] == "active"
    assert rows[0]["version"] == 1
    assert rows[0]["content"].strip() == "# v2"


def test_record_dispatch_decision_contains_target_details():
    org = _seed_org()
    dispatch = {
        "id": 42, "org_id": org["id"], "org_issue_number": 7,
        "org_repo_key": "gitcode:issue2269/org",
    }
    targets = [{
        "repo_key": "gitcode:issue2269/svc", "child_issue_number": 3,
        "pr_number": 8, "status": "needs_review", "error": "",
    }]
    from src.domains.mgmt import org_dispatch as od

    od._record_dispatch_decision(dispatch, targets, "needs_review",
                                 {"status": "finalized", "comment_posted": 1,
                                  "issue_status": "pending_review", "error": ""})
    from src.domains.mgmt.org_context_store import list_contexts

    rows = [r for r in list_contexts(org["id"], kind="decision")
            if r.get("ctx_key") == "dispatch-42"]
    assert rows and rows[0]["lifecycle_status"] == "active"
    content = rows[0]["content"]
    assert "gitcode:issue2269/svc" in content and "#3" in content and "#8" in content
    assert "needs_review" in content


def test_render_org_context_files_layout(monkeypatch):
    from src.domains.mgmt import org_context_repo as repo_mod

    monkeypatch.setattr(
        "src.domains.mgmt.org_context_store.list_contexts",
        lambda org_id, kind="", status="", limit=0, offset=0: {
            "brief": [{"content": "# brief", "ctx_key": "brief",
                       "title": "组织概览", "lifecycle_status": "active"}],
            "policy": [{"content": "policy body", "ctx_key": "no-force-push",
                        "title": "禁止强推", "lifecycle_status": "active"}],
        }.get(kind, []))
    monkeypatch.setattr(
        "src.domains.mgmt.org_knowledge.list_knowledge",
        lambda org_id, **kw: [{"id": 9, "name": "组织概览", "kind": "summary",
                              "lifecycle_status": "active"}])
    monkeypatch.setattr(
        "src.domains.mgmt.org_knowledge.read_knowledge_content",
        lambda item: "# knowledge")
    monkeypatch.setattr(
        "src.domains.mgmt.user.org_db.get_org",
        lambda org_id: {"id": org_id, "name": "N", "slug": "n",
                        "org_repo_key": "gitcode:a/b",
                        "org_scene_id": "custom-org-x"})
    monkeypatch.setattr(
        "src.domains.mgmt.user.org_db.list_org_repos",
        lambda org_id: [{"repo_key": "gitcode:a/b", "full_name": "a/b",
                         "is_org_repo": True, "dev_scene": "scene-a"}])
    monkeypatch.setattr(repo_mod, "_org_demand_rows", lambda org_id: [{
        "issue_number": 1, "title": "需求一", "description": "需求正文",
        "status": "pending_review", "stage": "qa", "created_at": "2026-11-16 10:00:00",
    }])
    monkeypatch.setattr(repo_mod, "_org_dispatch_rows", lambda org_id: [{
        "id": 2, "org_issue_number": 1, "status": "needs_review",
        "scene_id": "scene-a", "plan": {"targets": ["gitcode:a/b"]},
        "context": {"summary": "摘要", "acceptance": ["验收一"]},
        "created_at": "2026-11-16 10:01:00", "updated_at": "2026-11-16 10:02:00",
    }])
    monkeypatch.setattr(repo_mod, "_org_aggregate_map", lambda org_id: {
        2: {"status": "finalized", "issue_status": "pending_review",
            "comment_posted": 1, "error": ""}})
    monkeypatch.setattr(repo_mod, "_org_decision_map", lambda org_id: {
        "dispatch-2": {"content": "# 批次结论\n\ndecision body",
                       "lifecycle_status": "active"}})

    files = repo_mod._render_files("org-1")
    assert "# brief" in files["org-context/architecture.md"]
    assert "policy body" in files["org-context/standards/no-force-push.md"]
    assert "# knowledge" in files["org-context/knowledge/9-组织概览.md"]
    assert "N" in files["org-context/profile.md"]
    assert "组织硬规则" in files["org-context/rules.md"]
    assert "a/b" in files["org-context/repos/gitcode/a/b/context.md"]
    assert "repo_key" in files["org-context/repos/index.json"]
    assert "需求正文" in files["org-context/demands/1/brief.md"]
    assert "批次 #2" in files["org-context/demands/1/dispatch-plan.md"]
    assert "decision body" in files["org-context/demands/1/acceptance.md"]
    assert "files" in files["org-context/index.json"]

def test_upsert_knowledge_is_idempotent(monkeypatch):
    import hashlib

    from src.domains.mgmt import org_knowledge as kn

    contents = {}

    def _fake_write(org_id, repo_key, content):
        h = hashlib.sha256(content.encode()).hexdigest()
        contents[h] = content
        return "rel/path.md", "", h

    monkeypatch.setattr(kn, "_write_content", _fake_write)
    monkeypatch.setattr(kn, "read_knowledge_content",
                        lambda item: contents.get(item.get("content_hash"), ""))
    org = _seed_org()
    kn.upsert_knowledge(org["id"], "gitcode:issue2269/org", kind="summary",
                        name="组织概览", content="# v1", description="d",
                        tags=["org"], owner_id=UID, created_by=str(UID))
    kn.upsert_knowledge(org["id"], "gitcode:issue2269/org", kind="summary",
                        name="组织概览", content="# v2", description="d",
                        tags=["org"], owner_id=UID, created_by=str(UID))
    rows = [r for r in kn.list_knowledge(org["id"], kind="summary")
            if r.get("name") == "组织概览"]
    assert len(rows) == 1
    assert rows[0]["lifecycle_status"] == "active"
    assert rows[0]["version"] == 1
    assert kn.read_knowledge_content(rows[0]).strip() == "# v2"


def test_sync_org_context_skips_personal_and_unprovisioned(monkeypatch):
    from src.domains.mgmt import org_context_repo as repo_mod

    monkeypatch.setattr(
        "src.domains.mgmt.user.org_db.get_org",
        lambda org_id: {"id": org_id, "is_personal": 1})
    assert repo_mod.sync_org_context_to_repo("personal-org") is False

    monkeypatch.setattr(
        "src.domains.mgmt.user.org_db.get_org",
        lambda org_id: {"id": org_id, "is_personal": 0, "org_repo_key": ""})
    assert repo_mod.sync_org_context_to_repo("unprovisioned-org") is False


def test_uuid_owner_context_and_knowledge(monkeypatch):
    """D105：uuid7 用户 id 不得被 int() 强转，组织上下文/知识全链路可写可读。"""
    import hashlib

    from src.domains.mgmt import org_knowledge as kn
    from src.domains.mgmt.org_context_store import (
        create_context,
        list_contexts,
        publish_context,
        update_context,
    )
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(name=UUID_SLUG, slug=UUID_SLUG,
                     owner_id=UUID_UID, is_personal=0)
    assert org

    seeded = [r for r in list_contexts(org["id"], kind="brief")
              if r.get("ctx_key") == "brief"]
    assert seeded and seeded[0]["owner_id"] == UUID_UID

    ctx = create_context(org["id"], "decision", "uuid-1", "结论", "内容",
                         owner_id=UUID_UID, user_id=UUID_UID)
    assert ctx["owner_id"] == UUID_UID
    assert ctx["created_by"] == UUID_UID

    updated = update_context(org["id"], ctx["id"], owner_id=UUID_UID,
                             user_id=UUID_UID)
    assert updated["owner_id"] == UUID_UID
    assert updated["updated_by"] == UUID_UID

    published = publish_context(org["id"], ctx["id"], user_id=UUID_UID)
    assert published["lifecycle_status"] == "active"
    assert published["updated_by"] == UUID_UID

    contents = {}

    def _fake_write(org_id, repo_key, content):
        h = hashlib.sha256(content.encode()).hexdigest()
        contents[h] = content
        return "rel/path.md", "", h

    monkeypatch.setattr(kn, "_write_content", _fake_write)
    monkeypatch.setattr(kn, "read_knowledge_content",
                        lambda item: contents.get(item.get("content_hash"), ""))
    kn.upsert_knowledge(org["id"], "gitcode:uuid/org", kind="summary",
                        name="组织概览", content="# uuid",
                        owner_id=UUID_UID, created_by=UUID_UID)
    items = [r for r in kn.list_knowledge(org["id"], kind="summary")
             if r.get("name") == "组织概览"]
    assert items and items[0]["owner_id"] == UUID_UID


def test_record_dispatch_decision_extracts_task_memory(monkeypatch):
    """下派结论要保留子任务结论/验证/产物，而不是只写「是否完成」。"""
    import hashlib
    import json

    from src.domains.mgmt import org_knowledge as kn
    from src.domains.mgmt import org_dispatch as od
    from src.domains.mgmt.org_context_store import list_contexts
    from src.kernel.session_manager import db, db_lock

    contents = {}

    def _fake_write(org_id, repo_key, content):
        h = hashlib.sha256(content.encode()).hexdigest()
        contents[h] = content
        return "rel/path.md", "", h

    monkeypatch.setattr(kn, "_write_content", _fake_write)
    monkeypatch.setattr(kn, "read_knowledge_content",
                        lambda item: contents.get(item.get("content_hash"), ""))

    repo_key = "gitcode:issue2269/svc-rich"
    task_id = "t_issue2269_rich"
    result_text = json.dumps({
        "actions": [
            "修复 src/http_probe.py：非法 timeout/backoff 返回 ok=False",
            "新增回归测试：负 timeout、非 ASCII URL 编码（修复前 6 failed）",
            "pytest -q：16 passed / 8 subtests",
            "Agent feedback 审视：事件间工作目录被重置需重复 clone",
        ],
        "conclusion": "Required 已修复且先红后绿；剩余 1 Optional 不阻断",
        "artifacts": ["PR #9 https://example.test/merge_requests/9",
                      "分支 feat/issue-3"],
        "next_step": "等待用户确认合入",
        "issue_summary": "PR #9 修复完成 | 16 passed | 待合入确认",
    }, ensure_ascii=False)
    with db_lock:
        c = db.cursor()
        qa_task = "t_issue2269_qa"
        qa_result = json.dumps({
            "actions": ["复审 PR #9 并独立复跑 16 用例"],
            "conclusion": "QA 复核通过：上轮 Required 已修复，1 Optional 不阻断",
            "artifacts": ["PR #9 https://example.test/merge_requests/9"],
            "next_step": "等待用户确认合入",
            "issue_summary": "QA 复核通过 | 16 passed | 待合入确认",
        }, ensure_ascii=False)
        for tid, agent, text, done_at in (
            (task_id, "coding", result_text, "2026-11-16 10:00:00"),
            (qa_task, "qa", qa_result, "2026-11-16 10:10:00"),
        ):
            c.execute(
                "INSERT OR REPLACE INTO agent_tasks "
                "(id, agent_session_id, runtime_id, issue_id, repo_key, agent_type, "
                "status, prompt, result_text, created_at, completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, "s-rich", 0, "#3", repo_key, agent, "completed",
                 "prompt", text, "2026-11-16 09:00:00", done_at),
            )
        db.commit()

    org = _seed_org()
    dispatch = {
        "id": 43, "org_id": org["id"], "org_issue_number": 7,
        "org_repo_key": "gitcode:issue2269/org",
        "plan_json": json.dumps({
            "title": "TODO 并发安全重构",
            "description": "实现多进程安全、崩溃恢复与幂等重试",
        }, ensure_ascii=False),
        "context_json": json.dumps({
            "summary": "TODO CLI 生产级并发/恢复/幂等重构",
            "acceptance": ["并发写入不丢更新", "pytest 全绿"],
            "constraints": ["标准库优先"],
            "decisions": ["flock + 原子替换"],
        }, ensure_ascii=False),
    }
    targets = [{
        "repo_key": repo_key, "child_issue_number": 3, "child_task_id": task_id,
        "pr_number": 9, "status": "needs_review", "error": "",
    }]
    od._record_dispatch_decision(dispatch, targets, "needs_review",
                                 {"status": "finalized", "comment_posted": 1,
                                  "issue_status": "pending_review", "error": ""})

    rows = [r for r in list_contexts(org["id"], kind="decision")
            if r.get("ctx_key") == "dispatch-43"]
    assert rows and rows[0]["lifecycle_status"] == "active"
    content = rows[0]["content"]
    assert "PR #9" in content
    assert "16 passed" in content
    assert "Required 已修复" in content
    assert "Agent feedback" in content
    assert "等待用户确认合入" in content
    assert "QA 复核通过" in content
    assert "> qa" in content and "> coding" in content
    assert "## 关键信息" in content
    assert "验收：并发写入不丢更新；pytest 全绿" in content
    assert "约束：标准库优先" in content
    assert "设计决策：flock + 原子替换" in content
    assert "## 目标执行摘要" in content

    items = [r for r in kn.list_knowledge(org["id"], kind="retrospective")
             if r.get("name") == "下派 #43 经验沉淀"]
    assert items and items[0]["lifecycle_status"] == "draft"
    knowledge_text = kn.read_knowledge_content(items[0])
    assert "Required 已修复" in knowledge_text
    assert "QA 复核通过" in knowledge_text
    assert "Agent feedback" in knowledge_text
