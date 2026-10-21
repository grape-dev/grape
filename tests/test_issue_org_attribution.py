"""需求归属 org 修复测试（issue-244-org-attribution-fix-design.md 方案 B/D）。

背景：#244 issue.org_id 被「请求上下文 user.org_id」写坏（32，他人个人组织），
与仓库托管 org（2）不一致 → 需求中心按 org 隔离过滤后不可见。
修复：repo 关联需求的 org 唯一权威 = `managed_repos.org_id`，拒绝任何调用方/请求 org 写入。
"""
import pytest

from src.session_manager import db, db_lock
from src.api import issues_center as ic


REPO = "gitcode:test/orgrepo"
REPO_NOORG = "gitcode:test/noorgrepo"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key IN (?,?)", (REPO, REPO_NOORG))
        c.execute("DELETE FROM issue WHERE repo_key IN (?,?)", (REPO, REPO_NOORG))
        c.execute("DELETE FROM issue_state WHERE repo_key IN (?,?)", (REPO, REPO_NOORG))
        c.execute("DELETE FROM events WHERE repo_key IN (?,?)", (REPO, REPO_NOORG))
        c.execute("DELETE FROM agent_tasks WHERE repo_key IN (?,?)", (REPO, REPO_NOORG))
        db.commit()


def _seed_repo(repo_key: str, org: int):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        # 兼容无 org_id 列的旧库（正常由 db_setup 迁移补齐）
        cols = [r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()]
        if "org_id" not in cols:
            c.execute("ALTER TABLE managed_repos ADD COLUMN org_id INTEGER DEFAULT 0")
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, org_id) VALUES (?,?,?,?,?)",
            (repo_key, "test", repo_key.split("/")[-1], repo_key.split(":")[1], org))
        db.commit()


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


def _issue_org(issue_number: int, repo_key: str = REPO) -> int:
    with db_lock:
        row = db.cursor().execute(
            "SELECT org_id FROM issue WHERE repo_key=? AND issue_number=?",
            (repo_key, issue_number)).fetchone()
        return row[0] if row else None


class TestGetGitcodeCredByGitcodeId:
    """凭据错位兜底：OAuth 存在旧 user_id(7)，登录账号(2) 按 gitcode_id 跨 user_id 取到有效 token。"""

    def test_cred_on_legacy_user_id_matched_by_gitcode_id(self):
        import json as _json
        _clean()
        gid = "000-guid-jason"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=2 OR username='Jason7602'")
            c.execute("INSERT INTO grape_accounts (id, username, password_hash, token_version) VALUES (2,'Jason7602','x',0)")
            c.execute("DELETE FROM external_identities WHERE grape_user_id=2 AND provider='gitcode'")
            c.execute("INSERT INTO external_identities (grape_user_id, provider, external_id, external_login, status) "
                      "VALUES (2,'gitcode',?,'Jason7602',1)", (gid,))
            c.execute("DELETE FROM user_credentials WHERE provider='gitcode_oauth' AND user_id IN (2,7)")
            c.execute("INSERT INTO user_credentials (user_id, provider, config_json) VALUES (7,'gitcode_oauth',?)",
                      (_json.dumps({"gitcode_id": gid, "access_token": "LIVE-TOKEN", "token_expires_at": 0}),))
            db.commit()
        from src.api import deps
        assert deps._get_gitcode_credential_by_gitcode_id("Jason7602").get("access_token") == "LIVE-TOKEN"
        assert deps.get_user_token_with_refresh("Jason7602", user_id=2) == "LIVE-TOKEN", \
            "登录账号(2) 下无凭据时，应按 gitcode_id 命中旧 user_id(7) 的有效 token"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM external_identities WHERE grape_user_id=2 AND provider='gitcode'")
            c.execute("DELETE FROM grape_accounts WHERE id=2 OR username='Jason7602'")
            c.execute("DELETE FROM user_credentials WHERE provider='gitcode_oauth' AND user_id IN (2,7)")
            db.commit()


    def test_cred_matched_by_gitcode_id_when_token_expired(self):
        """F3 修复（R9.6）：status=4（token 过期）的 identity 记录其 external_id 仍然有效，
        应能跨 user_id 匹配到 user_credentials 中的凭据。"""
        import json as _json
        _clean()
        gid = "000-guid-f3-test"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=2 OR username='F3User'")
            c.execute("INSERT INTO grape_accounts (id, username, password_hash, token_version) VALUES (2,'F3User','x',0)")
            c.execute("DELETE FROM external_identities WHERE grape_user_id=2 AND provider='gitcode'")
            # status=4: token 过期，但 external_id（gitcode_id）仍然有效
            c.execute("INSERT INTO external_identities (grape_user_id, provider, external_id, external_login, status) "
                      "VALUES (2,'gitcode',?,'F3User',4)", (gid,))
            c.execute("DELETE FROM user_credentials WHERE provider='gitcode_oauth' AND user_id IN (2,7)")
            c.execute("INSERT INTO user_credentials (user_id, provider, config_json) VALUES (7,'gitcode_oauth',?)",
                      (_json.dumps({"gitcode_id": gid, "access_token": "EXPIRED-IDENTITY-LIVE-CRED", "token_expires_at": 0}),))
            db.commit()
        from src.api import deps
        # 修复前（status=1）：查不到 external_id → 返回 {} → 兜底失效
        # 修复后（status IN (1,4)）：查到 external_id → 匹配 user_credentials → 返回凭据
        cred = deps._get_gitcode_credential_by_gitcode_id("F3User")
        assert cred.get("access_token") == "EXPIRED-IDENTITY-LIVE-CRED", \
            "status=4 的 identity 记录 external_id 仍有效，应能匹配 user_credentials 凭据"
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM external_identities WHERE grape_user_id=2 AND provider='gitcode'")
            c.execute("DELETE FROM grape_accounts WHERE id=2 OR username='F3User'")
            c.execute("DELETE FROM user_credentials WHERE provider='gitcode_oauth' AND user_id IN (2,7)")
            db.commit()


class TestResolveIssueOrgId:
    def test_repo_authority_over_explicit(self):
        _seed_repo(REPO, org=7)
        assert ic._resolve_issue_org_id(REPO, explicit_org=999) == 7, "repo 关联必须仓库 org 权威"

    def test_repo_no_managed_org_zero(self):
        _seed_repo(REPO, org=0)
        assert ic._resolve_issue_org_id(REPO, explicit_org=31) == 0

    def test_org_level_future_uses_explicit(self):
        assert ic._resolve_issue_org_id("", explicit_org=31) == 31

    def test_org_level_future_zero(self):
        assert ic._resolve_issue_org_id("", explicit_org=0) == 0


class TestGetUserIdByGitcodePrefersLoginAccount:
    """unified_user_mapping 存在新旧双 id 时，必须优先返回登录账号(grape_accounts) id。"""

    def test_prefers_grape_accounts_id(self):
        _clean()
        with db_lock:
            c = db.cursor()
            # 双映射：新(登录)id=2 与 遗留 id=7
            for uid in (2, 7):
                c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='Jason7602' AND user_id=?", (uid,))
                c.execute("INSERT INTO unified_user_mapping (user_id, gitcode_user) VALUES (?,?)", (uid, "Jason7602"))
            # grape_accounts 有登录账号 Jason7602=2
            c.execute("DELETE FROM grape_accounts WHERE id=2 OR username='Jason7602'")
            c.execute("INSERT INTO grape_accounts (id, username, password_hash, token_version) VALUES (2,'Jason7602','x',0)")
            db.commit()
        assert ic._get_user_id_by_gitcode("Jason7602") == 2, "必须返回登录账号 id（grape_accounts），而非遗留映射 id"
        # 清理
        with db_lock:
            db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user='Jason7602'")
            db.cursor().execute("DELETE FROM grape_accounts WHERE id=2 OR username='Jason7602'")
            db.commit()


class TestEnsureIssueTaskOrgAuthority:
    def test_writes_repo_org_not_passed_org(self):
        """#244 等价：即便调用方传入错误的 org（99），落库 org 仍 = 仓库托管 org（7）。"""
        _seed_repo(REPO, org=7)
        tid = ic.ensure_issue_task(REPO, 5, "t", org_id=99)
        assert _issue_org(5) == 7, "repo 关联需求 org 必须 = managed_repos.org_id"

    def test_repo_without_managed_org_writes_zero(self):
        _seed_repo(REPO_NOORG, org=0)
        ic.ensure_issue_task(REPO_NOORG, 6, "t", org_id=0)
        assert _issue_org(6, REPO_NOORG) == 0

    def test_reuse_path_corrects_existing_wrong_org(self):
        """复用路径：已有卡 org 错（非仓库 org）→ ensure 时校准回仓库 org。"""
        _seed_repo(REPO, org=7)
        from src.api.session_store import set_issue_stage
        set_issue_stage(REPO, "#8", "vod", "vod")  # 建档（ensure_row）
        with db_lock:
            db.cursor().execute("UPDATE issue SET org_id=32 WHERE repo_key=? AND issue_number=?",
                                (REPO, 8))
            db.commit()
        assert _issue_org(8) == 32
        ic.ensure_issue_task(REPO, 8, "t", org_id=0)
        assert _issue_org(8) == 7, "ensure 复用时应校准回仓库 org"