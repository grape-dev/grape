"""个人组织 / org_id 存量回填迁移的场景 S1–S9（2026-11-22 恢复）。

事故：清退废表 `user_accounts` 时把 `_migrate_create_personal_orgs()` 与
`_migrate_data_to_personal_orgs()` 整段删除；前者读废表本就空转，但后者里
“补个人组织 / 回填 org_id”并不依赖废表，属删过头。现在按 owner 域拆成两个迁移：
  · mgmt：个人组织 + managed_repos.org_id（账号源=grape_accounts）
  · runtime：runtime_credentials / agent_runtimes / skill / repo-keyed 表
"""
from __future__ import annotations

import sqlite3

import pytest

from src.domains.mgmt.migrations import personal_org_backfill as mg_mig
from src.domains.runtime.migrations import org_id_backfill as rt_mig
from src.api import db_setup as ds


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE grape_accounts (id TEXT PRIMARY KEY, username TEXT, status INTEGER);
    CREATE TABLE organization (id TEXT PRIMARY KEY, name TEXT, slug TEXT UNIQUE,
                               description TEXT DEFAULT '', owner_id TEXT, is_personal INTEGER DEFAULT 0);
    CREATE TABLE org_member (org_id TEXT, user_id TEXT, role TEXT);
    CREATE TABLE managed_repos (repo_key TEXT, org_id TEXT DEFAULT '');
    CREATE TABLE user_repos (user_id TEXT, repo_key TEXT, is_admin INTEGER DEFAULT 0);
    CREATE TABLE runtime_credentials (id TEXT, owner_name TEXT, scope TEXT, org_id TEXT DEFAULT '');
    CREATE TABLE agent_runtimes (id TEXT, credential_id TEXT, org_id TEXT DEFAULT '');
    CREATE TABLE skill (id TEXT, owner_id TEXT, scope TEXT, org_id TEXT DEFAULT '');
    """)
    return c


def test_s1_creates_personal_org_for_accounts_without_one():
    c = _conn()
    c.executemany("INSERT INTO grape_accounts VALUES (?,?,1)", [("1", "alice"), ("2", "bob")])
    c.execute("INSERT INTO organization (id,name,slug,owner_id,is_personal) "
              "VALUES ('o1','alice 的空间','personal-1','1',1)")
    stats = mg_mig.migrate_personal_orgs_from_accounts(c)
    assert stats["orgs_created"] == 1
    rows = c.execute("SELECT owner_id, slug FROM organization WHERE is_personal=1 "
                     "ORDER BY owner_id").fetchall()
    assert rows == [("1", "personal-1"), ("2", "personal-2")]
    assert c.execute("SELECT user_id, role FROM org_member WHERE org_id="
                     "(SELECT id FROM organization WHERE slug='personal-2')").fetchall() == [("2", "owner")]


def test_s2_is_idempotent():
    c = _conn()
    c.execute("INSERT INTO grape_accounts VALUES ('1','alice',1)")
    assert mg_mig.migrate_personal_orgs_from_accounts(c)["orgs_created"] == 1
    assert mg_mig.migrate_personal_orgs_from_accounts(c)["orgs_created"] == 0


def test_s3_slug_collision_falls_back_to_suffixed_slug():
    c = _conn()
    c.execute("INSERT INTO grape_accounts VALUES ('7','carol',1)")
    c.execute("INSERT INTO organization (id,name,slug,owner_id,is_personal) "
              "VALUES ('x','已占用','personal-7','99',0)")
    mg_mig.migrate_personal_orgs_from_accounts(c)
    slug = c.execute("SELECT slug FROM organization WHERE owner_id='7'").fetchone()[0]
    assert slug == "personal-7-carol"


def test_s4_skips_disabled_accounts():
    c = _conn()
    c.executemany("INSERT INTO grape_accounts VALUES (?,?,?)", [("1", "a", 1), ("2", "b", 0)])
    mg_mig.migrate_personal_orgs_from_accounts(c)
    assert c.execute("SELECT COUNT(*) FROM organization WHERE is_personal=1").fetchone()[0] == 1


def test_s5_backfills_managed_repos_org_id_only_when_empty():
    c = _conn()
    c.execute("INSERT INTO grape_accounts VALUES ('1','alice',1)")
    c.executemany("INSERT INTO managed_repos VALUES (?,?)",
                  [("gitee:a/r1", ""), ("gitee:a/r2", "keep")])
    c.execute("INSERT INTO user_repos VALUES ('1','gitee:a/r1',1)")
    mg_mig.migrate_personal_orgs_from_accounts(c)
    org = c.execute("SELECT id FROM organization WHERE owner_id='1'").fetchone()[0]
    assert c.execute("SELECT org_id FROM managed_repos WHERE repo_key='gitee:a/r1'").fetchone()[0] == org
    assert c.execute("SELECT org_id FROM managed_repos WHERE repo_key='gitee:a/r2'").fetchone()[0] == "keep"


def test_s6_non_owner_database_is_rejected():
    c = sqlite3.connect(":memory:")   # 没有 grape_accounts
    with pytest.raises(ds._NonOwnerObject):
        mg_mig.migrate_personal_orgs_from_accounts(c)


def test_s7_runtime_credentials_and_runtimes_follow_the_org(monkeypatch):
    c = _conn()
    c.executemany("INSERT INTO runtime_credentials VALUES (?,?,?,?)",
                  [("c1", "alice", "user", ""), ("c2", "alice", "global", "")])
    c.execute("INSERT INTO agent_runtimes VALUES ('r1','c1','')")
    import src.domains.mgmt.public as mgmt

    monkeypatch.setattr(mgmt, "get_account_id_by_username", lambda n: {"alice": "1"}.get(n, ""))
    monkeypatch.setattr(mgmt, "personal_org_id", lambda uid: {"1": "org-1"}.get(str(uid), ""))
    monkeypatch.setattr(mgmt, "repo_org_map", lambda: {})
    monkeypatch.setattr(mgmt, "backfill_org_id_by_repo", lambda m: {})
    stats = rt_mig.migrate_runtime_org_ids(c)
    assert stats["runtime_credentials"] == 1 and stats["agent_runtimes"] == 1
    assert c.execute("SELECT org_id FROM runtime_credentials WHERE id='c1'").fetchone()[0] == "org-1"
    assert c.execute("SELECT org_id FROM runtime_credentials WHERE id='c2'").fetchone()[0] == ""


def test_s8_skills_follow_user_and_repo(monkeypatch):
    c = _conn()
    c.executemany("INSERT INTO skill VALUES (?,?,?,?)",
                  [("s1", "1", "user", ""), ("s2", "gitee:a/r", "repo", "")])
    import src.domains.mgmt.public as mgmt
    import src.domains.runtime.public as runtime

    monkeypatch.setattr(mgmt, "get_account_id_by_username", lambda n: "")
    monkeypatch.setattr(mgmt, "personal_org_id", lambda uid: {"1": "org-1"}.get(str(uid), ""))
    monkeypatch.setattr(mgmt, "repo_org_map", lambda: {"gitee:a/r": "org-repo"})
    monkeypatch.setattr(mgmt, "backfill_org_id_by_repo", lambda m: {"devops_pipeline_scene": 0})
    monkeypatch.setattr(runtime, "backfill_org_id_by_repo", lambda m: {"events": 0})
    stats = rt_mig.migrate_runtime_org_ids(c)
    assert stats["skill_user"] == 1 and stats["skill_repo"] == 1
    assert c.execute("SELECT org_id FROM skill WHERE id='s1'").fetchone()[0] == "org-1"
    assert c.execute("SELECT org_id FROM skill WHERE id='s2'").fetchone()[0] == "org-repo"


def test_s9_repo_keyed_tables_go_through_each_domain_contract(monkeypatch):
    c = _conn()
    import src.domains.mgmt.public as mgmt
    import src.domains.runtime.public as runtime

    seen = {}

    monkeypatch.setattr(mgmt, "get_account_id_by_username", lambda n: "")
    monkeypatch.setattr(mgmt, "personal_org_id", lambda uid: "")
    monkeypatch.setattr(mgmt, "repo_org_map", lambda: {"gitee:a/r": "org-repo"})
    def _mgmt_bf(m):
        seen["mgmt"] = m
        return {"devops_pipeline_scene": 2}

    def _runtime_bf(m):
        seen["runtime"] = m
        return {"events": 3}

    monkeypatch.setattr(mgmt, "backfill_org_id_by_repo", _mgmt_bf)
    monkeypatch.setattr(runtime, "backfill_org_id_by_repo", _runtime_bf)
    stats = rt_mig.migrate_runtime_org_ids(c)
    assert seen["mgmt"] == {"gitee:a/r": "org-repo"} and seen["runtime"] == seen["mgmt"]
    assert stats["by_repo_mgmt"]["devops_pipeline_scene"] == 2
    assert stats["by_repo_runtime"]["events"] == 3