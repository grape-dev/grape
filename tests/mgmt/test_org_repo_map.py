"""组织仓库地图（MD-first）MVP：数据、摘要与组织仓库镜像。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock

UID = "22960"
REPO_KEY = "gitcode:map-org/order-service"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    def _purge():
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM org_member WHERE org_id IN "
                "(SELECT id FROM organization WHERE owner_id=?)",
                (UID,),
            )
            c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            c.execute("DELETE FROM grape_t_org_repo_map WHERE repo_key=?", (REPO_KEY,))
            db.commit()

    _purge()
    yield
    _purge()


def _seed():
    from src.domains.mgmt.user.db import add_managed_repo
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(name="Map Team", slug="map-team-test", owner_id=UID)
    assert org
    add_managed_repo(REPO_KEY, "map-org", "order-service", webhook_id=1, org_id=org["id"])
    return org


def test_analyze_repo_builds_summary_and_mirror(monkeypatch):
    from src.domains.mgmt import org_repo_map as mod

    org = _seed()
    monkeypatch.setattr(
        mod,
        "_repo_sources",
        lambda org, row: {
            "readme": "# 订单服务\n\n订单域主服务，负责下单、订单状态机与履约。\n\n"
                      "## 功能\n- 创建订单\n- 订单状态机\n\n"
                      "## 对外接口\n- `POST /api/orders`\n- `GET /api/orders/{id}`\n",
            "capabilities": "## Supported Features\n- 履约回调\n",
            "graph": "",
            "org_context": "",
            "description": "",
            "source_commit": "abc123",
            "provider": "gitcode",
            "owner": "map-org",
            "name": "order-service",
        },
    )

    before = mod.list_repo_map(org["id"])
    assert before["summary"] == {"total": 1, "ready": 0, "coverage": 0.0}
    assert before["repos"][0]["status"] == "none"

    result = mod.analyze_repo(org["id"], REPO_KEY)
    assert result["status"] == "ready"
    assert "订单域主服务" in result["purpose"]
    assert "POST /api/orders" in result["interfaces"] or any(
        "POST /api/orders" in x for x in result["interfaces"])
    assert result["confidence"] > 0.5
    assert result["summary_md"].startswith("---")
    assert "## 一句话定位" in result["summary_md"]

    after = mod.list_repo_map(org["id"])
    assert after["summary"]["ready"] == 1
    assert after["repos"][0]["has_summary"] is True

    files = mod.repo_map_markdown_files(org["id"])
    assert "org-context/repo-map/README.md" in files
    assert "org-context/repo-map/index.json" in files
    assert "org-context/repo-map/repo-map-summary.md" in files
    assert "订单域主服务" in files["org-context/repo-map/repo-map-summary.md"]
    assert (
        "org-context/repo-map/repos/gitcode/map-org/order-service/summary.md" in files
    )
    assert "订单域主服务" in files[
        "org-context/repo-map/repos/gitcode/map-org/order-service/summary.md"
    ]


def test_get_map_unknown_returns_none():
    from src.domains.mgmt.org_repo_map import get_map

    org = _seed()
    assert get_map(org["id"], "gitcode:map-org/missing") is None


def test_recall_targets_prefers_keyword_match(monkeypatch):
    from src.domains.mgmt import org_repo_map as mod
    from src.domains.mgmt.user.db import add_managed_repo

    org = _seed()
    add_managed_repo("gitcode:map-org/user-service", "map-org", "user-service",
                     webhook_id=2, org_id=org["id"])

    def _fake_sources(org, row):
        name = str(row.get("repo_name") or row.get("repo_key") or "").rsplit("/", 1)[-1]
        if "order" in name:
            readme = "# 订单服务\n\n订单域主服务，负责下单与履约。\n\n- 创建订单\n"
        else:
            readme = "# 用户服务\n\n用户注册登录与资料管理。\n\n- 用户注册\n"
        return {
            "readme": readme, "capabilities": "", "graph": "", "org_context": "",
            "description": "", "source_commit": "", "provider": "gitcode",
            "owner": "map-org", "name": name,
        }

    monkeypatch.setattr(mod, "_repo_sources", _fake_sources)
    assert mod.analyze_repo(org["id"], REPO_KEY)["status"] == "ready"
    assert mod.analyze_repo(org["id"], "gitcode:map-org/user-service")["status"] == "ready"

    result = mod.recall_targets(org["id"], "下单 创建订单", limit=3)
    assert result["targets"][0] == REPO_KEY
    assert result["fallback"] is False
    assert result["candidates"][0]["score"] > 0


def test_recall_targets_defaults_to_no_guess_when_no_map_match(monkeypatch):
    from src.domains.mgmt import org_repo_map as mod
    from src.domains.mgmt.user.db import add_managed_repo

    monkeypatch.setattr(mod, "_RECALL_FALLBACK", "none")
    org = _seed()
    add_managed_repo("gitcode:map-org/user-service", "map-org", "user-service",
                     webhook_id=2, org_id=org["id"])
    result = mod.recall_targets(org["id"], "订单", limit=3)
    assert result["fallback"] is False
    assert result["needs_review"] is True
    assert result["targets"] == []


def test_recall_targets_legacy_fallback_all(monkeypatch):
    from src.domains.mgmt import org_repo_map as mod
    from src.domains.mgmt.user.db import add_managed_repo

    monkeypatch.setattr(mod, "_RECALL_FALLBACK", "all")
    org = _seed()
    add_managed_repo("gitcode:map-org/user-service", "map-org", "user-service",
                     webhook_id=2, org_id=org["id"])
    result = mod.recall_targets(org["id"], "订单", limit=3)
    assert result["fallback"] is True
    assert "gitcode:map-org/user-service" in result["targets"]


def test_recall_targets_matches_multiple_repos_via_chinese_ngram(monkeypatch):
    from src.domains.mgmt import org_repo_map as mod
    from src.domains.mgmt.user.db import add_managed_repo

    org = _seed()
    add_managed_repo("gitcode:map-org/user-service", "map-org", "user-service",
                     webhook_id=2, org_id=org["id"])
    add_managed_repo("gitcode:map-org/inventory-service", "map-org", "inventory-service",
                     webhook_id=3, org_id=org["id"])

    def _fake_sources(org, row):
        name = str(row.get("repo_name") or row.get("repo_key") or "").rsplit("/", 1)[-1]
        if "order" in name:
            readme = "# 订单服务\n\n订单域主服务。\n\n## 能力\n- 创建订单\n- 订单状态机\n"
        elif "inventory" in name:
            readme = "# 库存服务\n\n库存域服务。\n\n## 能力\n- 锁定库存\n- 扣减库存\n"
        else:
            readme = "# 用户服务\n\n用户与认证服务。\n\n## 能力\n- 用户登录\n- 用户注册\n"
        return {
            "readme": readme, "readme_local": "", "repo_map_doc": "",
            "capabilities": "", "graph": "", "org_context": "",
            "description": "", "source_commit": "", "provider": "gitcode",
            "owner": "map-org", "name": name,
        }

    monkeypatch.setattr(mod, "_repo_sources", _fake_sources)
    for key in (REPO_KEY, "gitcode:map-org/user-service",
                "gitcode:map-org/inventory-service"):
        assert mod.analyze_repo(org["id"], key)["status"] == "ready"

    result = mod.recall_targets(org["id"], "创建订单并锁定库存，同时校验用户登录", limit=3)
    assert result["fallback"] is False
    assert len(result["targets"]) >= 2
    assert "gitcode:map-org/inventory-service" in result["targets"]
    assert REPO_KEY in result["targets"]


def test_repo_map_summary_covers_all_linked_repos():
    """汇总文件必须列出所有关联仓库（未分析也要出现），供 UI 卡片与分支查看。"""
    from src.domains.mgmt import org_repo_map as mod
    from src.domains.mgmt.user.db import add_managed_repo

    org = _seed()
    add_managed_repo("gitcode:map-org/user-service", "map-org", "user-service",
                     webhook_id=2, org_id=org["id"])

    summary = mod.repo_map_summary(org["id"])
    assert summary["path"] == "org-context/repo-map/repo-map-summary.md"
    assert REPO_KEY in summary["markdown"]
    assert "gitcode:map-org/user-service" in summary["markdown"]
    assert summary["summary"]["total"] == 2

    files = mod.repo_map_markdown_files(org["id"])
    assert summary["path"] in files
    assert files[summary["path"]] == summary["markdown"].rstrip("\n") + "\n"


def test_repo_map_file_url_for_content_branch():
    from src.domains.mgmt.org_repo_map import _repo_file_url

    assert _repo_file_url(
        "gitcode:map-org/org-context",
        "org-context/repo-map/repo-map-summary.md",
        "grape-org",
    ) == ("https://gitcode.com/map-org/org-context/blob/grape-org/"
          "org-context/repo-map/repo-map-summary.md")
    assert _repo_file_url(
        "github:map-org/org-context",
        "org-context/repo-map/repo-map-summary.md",
        "grape-org",
    ) == ("https://github.com/map-org/org-context/blob/grape-org/"
          "org-context/repo-map/repo-map-summary.md")


def test_repo_map_exposes_summary_links_for_ui_buttons():
    """关联仓库能力卡与汇总卡都需要可直接跳转的组织仓库文件 URL。"""
    from src.domains.mgmt import org_repo_map as mod

    org = _seed()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE organization SET org_repo_key=? WHERE id=?",
                  ("gitcode:map-org/org-context", org["id"]))
        db.commit()
    mod.save_ai_result(org["id"], REPO_KEY, {
        "repo_key": REPO_KEY,
        "full_name": "map-org/order-service",
        "purpose": "订单域主服务",
        "confidence": 0.8,
    }, triggered_by="test")

    data = mod.list_repo_map(org["id"])
    row = data["repos"][0]
    assert row["summary_path"] == (
        "org-context/repo-map/repos/gitcode/map-org/order-service/summary.md")
    assert row["summary_branch"] == "grape-org"
    assert row["summary_url"].endswith(
        "/blob/grape-org/org-context/repo-map/repos/gitcode/map-org/order-service/summary.md")
    assert data["summary_file"]["path"] == "org-context/repo-map/repo-map-summary.md"
    assert data["summary_file"]["branch"] == "grape-org"
    assert data["summary_file"]["url"].endswith(
        "/blob/grape-org/org-context/repo-map/repo-map-summary.md")


def test_content_branch_migrates_legacy_and_keeps_context_and_map(monkeypatch, tmp_path):
    """旧 knowledge/analysis 只迁移一次；后续地图同步写 grape-org 并保留上下文。"""
    import subprocess

    import src.api.deps as deps
    import src.domains.mgmt.auth.grape_account_db as account_db
    import src.kernel.repo_key as repo_key_mod
    from src.domains.mgmt import org_context_repo as mod

    org = _seed()
    org_repo_key = "gitcode:map-org/org-repo"
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE organization SET org_repo_key=? WHERE id=?",
                  (org_repo_key, org["id"]))
        db.commit()

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)
    work = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(work)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)

    def _git(*args, **kwargs):
        command = args[0] if len(args) == 1 and isinstance(args[0], list) else list(args)
        return subprocess.run(["git", *command], check=False,
                              capture_output=True, text=True)

    (work / "README.md").write_text("main\n", encoding="utf-8")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-m", "main")
    _git("-C", str(work), "push", "origin", "main")
    _git("-C", str(work), "checkout", "-B", "grape-repo-knowledge")
    (work / "org-context").mkdir()
    (work / "org-context" / "rules.md").write_text("rules\n", encoding="utf-8")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-m", "knowledge")
    _git("-C", str(work), "push", "origin", "grape-repo-knowledge")
    _git("-C", str(work), "checkout", "--orphan", "grape-repo-analysis")
    for name in ("README.md", "org-context"):
        path = work / name
        if path.is_dir():
            import shutil
            shutil.rmtree(path)
        else:
            path.unlink()
    (work / "org-context" / "repo-map").mkdir(parents=True)
    (work / "org-context" / "repo-map" / "README.md").write_text("map\n", encoding="utf-8")
    _git("-C", str(work), "add", "-A")
    _git("-C", str(work), "commit", "-m", "analysis")
    _git("-C", str(work), "push", "origin", "grape-repo-analysis")

    monkeypatch.setattr(mod, "_clone_url", lambda key: str(remote))
    monkeypatch.setattr(repo_key_mod, "workspace_repo_dir",
                        lambda key: str(tmp_path / "workspace"))
    monkeypatch.setattr(mod.runtime_public, "run_git", _git)
    monkeypatch.setattr(account_db, "get_grape_account_by_id",
                        lambda uid: {"username": "tester"})
    monkeypatch.setattr(deps, "get_user_token_for_repo", lambda *a, **k: "token")

    assert mod.sync_org_analysis_to_repo(org["id"]) is True
    tree = subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "grape-org"],
        check=True, capture_output=True, text=True).stdout
    assert "org-context/rules.md" in tree
    assert "org-context/repo-map/README.md" in tree
    assert "org-context/repo-map/repo-map-summary.md" in tree

    # 上下文同步再次写 grape-org 时，不能删掉同分支上的仓库地图。
    assert mod.sync_org_context_to_repo(org["id"]) is True
    tree = subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "grape-org"],
        check=True, capture_output=True, text=True).stdout
    assert "org-context/repo-map/repo-map-summary.md" in tree
    assert "org-context/repo-map/README.md" in tree
