"""组织记忆保留/滚动归档与列表上限（防止上下文随运行次数膨胀）。"""
from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.mgmt import org_context_retention as retention
from src.domains.mgmt import org_context_store as ctx
from src.domains.mgmt import org_knowledge as kn
from src.kernel.session_manager import db, db_lock

UID = 22696
SLUG = "issue2269-retention"


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
        ids = [r[0] for r in c.execute(
            "SELECT id FROM organization WHERE owner_id=?", (UID,)).fetchall()]
        for org_id in ids:
            c.execute("DELETE FROM grape_t_org_context WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM agent_shared_knowledge WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
        db.commit()


def _seed_org() -> dict:
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(name=SLUG, slug=SLUG, owner_id=UID, is_personal=0)
    assert org
    return org


def _decision_content(num: int, reusable: str = "通用经验 Y") -> str:
    return (
        f"# 下派 #{num} 结论（needs_review）\n\n"
        f"**结论**\n\n第 {num} 批关键结论：完成参数校验并复跑用例。\n\n"
        "## 跨仓可复用经验\n"
        f"- 批次 {num} 独有经验\n"
        f"- {reusable}\n"
    )


def test_compacts_old_decisions_into_bounded_archive(monkeypatch):
    monkeypatch.setattr(ctx, "_schedule_repo_sync", lambda org_id: None)
    monkeypatch.setattr(
        "src.domains.mgmt.org_context_repo.schedule_sync", lambda org_id: None)
    monkeypatch.setenv("ORG_CONTEXT_ACTIVE_DECISIONS", "3")
    monkeypatch.setenv("ORG_CONTEXT_ARCHIVE_ENTRIES", "10")
    monkeypatch.setenv("ORG_CONTEXT_ARCHIVE_MAX_BYTES", "32768")

    org = _seed_org()
    for num in range(1, 6):
        ctx.upsert_context(org["id"], "decision", f"dispatch-{num}",
                           f"下派 #{num} 结论", _decision_content(num),
                           owner_id="", user_id="")

    result = retention.compact_org_memory(org["id"])
    assert result["decisions_deprecated"] == 2
    assert result["archive_written"] is True

    rows = {r["ctx_key"]: r for r in ctx.list_contexts(org["id"], kind="decision")}
    for num in (3, 4, 5):
        assert rows[f"dispatch-{num}"]["lifecycle_status"] == "active"
    for num in (1, 2):
        assert rows[f"dispatch-{num}"]["lifecycle_status"] == "deprecated"

    archive = rows["dispatch-archive"]
    assert archive["lifecycle_status"] == "active"
    assert "历史下派经验归档" in archive["title"]
    assert "#1" in archive["content"] and "#2" in archive["content"]
    assert "第 1 批关键结论" in archive["content"]
    # 重复经验跨批次去重，只保留一份
    assert archive["content"].count("通用经验 Y") == 1

    # 幂等：第二次执行不再降级、不再重写归档
    second = retention.compact_org_memory(org["id"])
    assert second["decisions_deprecated"] == 0
    assert second["archive_written"] is False


def test_archives_old_retrospective_knowledge(monkeypatch):
    monkeypatch.setattr(ctx, "_schedule_repo_sync", lambda org_id: None)
    monkeypatch.setattr(
        "src.domains.mgmt.org_context_repo.schedule_sync", lambda org_id: None)
    monkeypatch.setenv("ORG_CONTEXT_ACTIVE_RETROSPECTIVES", "2")
    monkeypatch.setenv("ORG_CONTEXT_ARCHIVE_ENTRIES", "10")

    contents = {}

    def _fake_write(org_id, repo_key, content):
        h = hashlib.sha256(content.encode()).hexdigest()
        contents[h] = content
        return "rel/path.md", "", h

    monkeypatch.setattr(kn, "_write_content", _fake_write)
    monkeypatch.setattr(kn, "read_knowledge_content",
                        lambda item: contents.get(item.get("content_hash"), ""))
    monkeypatch.setattr(kn, "_schedule_repo_sync", lambda org_id: None)

    org = _seed_org()
    for num in range(1, 5):
        kn.upsert_knowledge(
            org["id"], "gitcode:issue2269/org", kind="retrospective",
            name=f"下派 #{num} 经验沉淀", content=f"# 经验 {num}",
            owner_id="", created_by="")

    result = retention.compact_org_memory(org["id"])
    assert result["knowledge_archived"] == 2
    active = kn.list_knowledge(
        org["id"], kind="retrospective", status="active")
    archived = kn.list_knowledge(
        org["id"], kind="retrospective", status="archived")
    assert len(active) == 2
    assert len(archived) == 2
    assert {r["name"] for r in active} == {"下派 #3 经验沉淀", "下派 #4 经验沉淀"}

    second = retention.compact_org_memory(org["id"])
    assert second["knowledge_archived"] == 0


def test_list_limits_and_filters(monkeypatch):
    monkeypatch.setattr(ctx, "_schedule_repo_sync", lambda org_id: None)
    org = _seed_org()
    for num in range(1, 6):
        ctx.upsert_context(org["id"], "decision", f"dispatch-{num}",
                           f"下派 #{num} 结论", f"# {num}", owner_id="", user_id="")

    page = ctx.list_contexts(org["id"], kind="decision", limit=2, offset=1)
    assert len(page) == 2
    assert "dispatch-archive" not in {r["ctx_key"] for r in page}

    all_rows = ctx.list_contexts(org["id"], kind="decision")
    assert len(all_rows) == 5


def test_render_truncates_but_keeps_core_files(monkeypatch):
    from src.domains.mgmt import org_context_repo as repo_mod

    monkeypatch.setattr(repo_mod, "_RENDER_MAX_FILES", 4)
    monkeypatch.setattr(repo_mod, "_RENDER_MAX_BYTES", 4096)
    monkeypatch.setattr(
        "src.domains.mgmt.org_context_store.list_contexts",
        lambda org_id, kind="", status="", limit=0, offset=0: {
            "brief": [{"title": "brief", "content": "# brief",
                       "ctx_key": "brief", "lifecycle_status": "active"}],
            "decision": [{"title": f"d{i}", "content": "x" * 200,
                          "ctx_key": f"dispatch-{i}",
                          "lifecycle_status": "active"} for i in range(10)],
        }.get(kind, []))
    monkeypatch.setattr(
        "src.domains.mgmt.org_knowledge.list_knowledge",
        lambda org_id, **kw: [{"id": i, "name": f"k{i}", "kind": "summary",
                              "lifecycle_status": "active"} for i in range(10)])
    monkeypatch.setattr(
        "src.domains.mgmt.org_knowledge.read_knowledge_content", lambda item: "k" * 200)
    monkeypatch.setattr(
        "src.domains.mgmt.user.org_db.get_org",
        lambda org_id: {"id": org_id, "name": "N", "slug": "n",
                        "org_repo_key": "gitcode:a/b",
                        "org_scene_id": "custom-org-x"})
    monkeypatch.setattr(
        "src.domains.mgmt.user.org_db.list_org_repos", lambda org_id: [])

    files = repo_mod._render_files("org-1")
    assert "org-context/rules.md" in files
    assert "org-context/profile.md" in files
    assert "org-context/architecture.md" in files
    assert "org-context/repos/index.json" in files
    assert "org-context/TRUNCATED.md" in files
    assert "org-context/context.md" not in files
    assert "org-context/repos.json" not in files
    assert not [f for f in files if f.startswith("org-context/decisions/")]

def test_clean_managed_org_context_removes_only_generated_files(tmp_path):
    from src.domains.mgmt import org_context_repo as repo_mod

    base = tmp_path / "org-context"
    (base / "decisions").mkdir(parents=True)
    (base / "knowledge").mkdir(parents=True)
    (base / "standards").mkdir(parents=True)
    (base / "demands" / "1").mkdir(parents=True)
    (base / "repos" / "gitcode" / "a" / "b").mkdir(parents=True)
    (base / "demands" / "1" / "brief.md").write_text("old", encoding="utf-8")
    (base / "repos" / "gitcode" / "a" / "b" / "context.md").write_text("old", encoding="utf-8")
    (base / "decisions" / "dispatch-1.md").write_text("old", encoding="utf-8")
    (base / "knowledge" / "1-old.md").write_text("old", encoding="utf-8")
    (base / "standards" / "old.md").write_text("old", encoding="utf-8")
    (base / "README.md").write_text("old", encoding="utf-8")
    (base / "custom-notes.md").write_text("keep", encoding="utf-8")

    repo_mod._clean_managed_org_context(str(tmp_path))
    assert not (base / "decisions" / "dispatch-1.md").exists()
    assert not (base / "knowledge" / "1-old.md").exists()
    assert not (base / "standards" / "old.md").exists()
    assert not (base / "demands" / "1" / "brief.md").exists()
    assert not (base / "repos" / "gitcode" / "a" / "b" / "context.md").exists()
    assert not (base / "README.md").exists()
    assert (base / "custom-notes.md").read_text(encoding="utf-8") == "keep"
