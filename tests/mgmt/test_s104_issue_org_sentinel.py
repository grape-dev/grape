"""S104：审计续审九轮收口——issue org 哨兵族场景锁（D105-15 哨兵统一落地验证）。

背景：审计 F4——_resolve_issue_org_id explicit 路径 `or 0` 数值哨兵残留；
repo 路径对 DB 返 "0"（D105-15 迁移保值后的字符串哨兵）未归一。本文件把
哨兵族行为升格为场景锁：任何维度（0/"0"/''）的未归属都必须出 ''。
"""
from src.kernel.session_manager import db, db_lock
from src.api import issues_center as ic

REPO = "s104sen:repo"


def _seed_repo(repo_key: str, org):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, org_id) VALUES (?,?,?,?,?)",
            (repo_key, "test", repo_key.split("/")[-1], repo_key.split(":")[1], org))
        db.commit()


def teardown_function():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        db.commit()


def test_s104a_repo_int_zero_sentinel():
    """S104-a：repo 关联 org=0（int 哨兵）→ ''。"""
    _seed_repo(REPO, 0)
    assert ic._resolve_issue_org_id(REPO, explicit_org=31) == ""


def test_s104b_repo_string_zero_sentinel():
    """S104-b：repo 关联 org='0'（D105-15 TEXT 保值后的字符串哨兵）→ ''。"""
    _seed_repo(REPO, "0")
    assert ic._resolve_issue_org_id(REPO, explicit_org=31) == ""


def test_s104c_repo_real_org_preserved():
    """S104-c：repo 真实归属 → 原值返回（str 形态，不留哨兵误伤）。"""
    _seed_repo(REPO, 7)
    assert ic._resolve_issue_org_id(REPO, explicit_org=31) == "7"


def test_s104d_repo_unmanaged_empty():
    """S104-d：无托管记录 → ''（repo 权威路径缺省）。"""
    assert ic._resolve_issue_org_id("s104sen:nope", explicit_org=31) == ""


def test_s104e_explicit_passthrough_no_sentinel():
    """S104-e：组织级 explicit 31 → 31 原样透传；explicit 0/'' → ''（禁数值哨兵）。"""
    assert ic._resolve_issue_org_id("", explicit_org=31) == 31
    assert ic._resolve_issue_org_id("", explicit_org=0) == ""  # or '' 恒返 ''，无宽容形态
    assert ic._resolve_issue_org_id("", explicit_org="") == ""
