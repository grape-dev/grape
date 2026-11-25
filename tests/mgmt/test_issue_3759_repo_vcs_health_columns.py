"""Issue #3759：managed_repos 的 VCS 熔断台账列（迁移 + mgmt.public 读写 + 列表暴露）。

验收口径：
- 新增 8 列并用幂等迁移补齐存量库（新装库 DDL 直接带列）；
- `mgmt.public.set_repo_vcs_health/get_repo_vcs_health` 可在跨域写白名单内往返；
- 仓库列表把 `vcs_missing` 暴露给前端标红（孤儿仓库卡运维可见）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.kernel.session_manager import db, db_lock  # noqa: E402

TABLE = "managed_repos"
COLS = ("vcs_status", "vcs_missing_since", "vcs_last_error_code", "vcs_last_error_at",
        "vcs_probe_result", "vcs_probe_at", "vcs_next_probe_at", "vcs_fail_count")
RK = "gitcode:issue3759/repo-vcs-health"


def _table_columns() -> set[str]:
    with db_lock:
        c = db.cursor()
        return {r[1] for r in c.execute(f"PRAGMA table_info({TABLE})").fetchall()}


def test_ddl_and_migration_columns_present():
    cols = _table_columns()
    assert cols, f"{TABLE} 表不存在（测试库未初始化）"
    assert not [c for c in COLS if c not in cols], f"缺列: {sorted(set(COLS) - cols)}"


def test_migration_is_idempotent():
    from src.domains.mgmt.migrations.repo_vcs_health_columns import (
        MIGRATION, add_repo_vcs_health_columns, verify_repo_vcs_health_columns)

    assert MIGRATION["version"] == "202611250001"
    assert MIGRATION["domains"] == ("mgmt",)
    # 列已在位 → 再跑一次零改动且不抛错（盖章后重跑/新库双路径都安全）
    add_repo_vcs_health_columns()
    add_repo_vcs_health_columns()
    verify_repo_vcs_health_columns()


def _seed_repo():
    """直接用主库连接插入仓库卡（`ensure_repo_row` 只在三库拓扑可用）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                  "VALUES (?,?,?,?)", (RK, "issue3759", "repo-vcs-health",
                                       "issue3759/repo-vcs-health"))
        db.commit()


def test_public_accessors_roundtrip():
    from src.domains.mgmt.public import get_repo_vcs_health, set_repo_vcs_health

    _seed_repo()
    empty = get_repo_vcs_health(RK)
    assert empty.get("vcs_status") in ("", None)

    changed = set_repo_vcs_health(RK, {
        "vcs_status": "vcs_missing",
        "vcs_missing_since": "2026-11-25T01:00:00+00:00",
        "vcs_last_error_code": "404",
        "vcs_probe_result": "missing",
        "vcs_next_probe_at": "2026-11-25T02:00:00+00:00",
        "vcs_fail_count": 3,
    })
    assert changed == 1
    row = get_repo_vcs_health(RK)
    assert row["vcs_status"] == "vcs_missing"
    assert row["vcs_last_error_code"] == "404"
    assert int(row["vcs_fail_count"]) == 3

    # 幂等：重复写同值不改变结果（SQLite 的 rowcount 计匹配行，MySQL 计变更行，
    # 故这里断言状态不变而不是 0 行）
    set_repo_vcs_health(RK, {"vcs_status": "vcs_missing"})
    assert get_repo_vcs_health(RK)["vcs_status"] == "vcs_missing"


def test_public_accessors_reject_unknown_columns():
    from src.domains.mgmt.public import set_repo_vcs_health

    _seed_repo()
    with pytest.raises(ValueError):
        set_repo_vcs_health(RK, {"repo_key": "x"})


def test_public_accessors_missing_repo_is_zero_not_error():
    from src.domains.mgmt.public import get_repo_vcs_health, set_repo_vcs_health

    assert get_repo_vcs_health("gitcode:nobody/absent-3759") == {}
    assert set_repo_vcs_health("gitcode:nobody/absent-3759", {"vcs_status": "ok"}) == 0


def test_managed_repo_list_exposes_vcs_missing():
    from src.domains.mgmt.user import db as user_db
    from src.domains.mgmt.public import set_repo_vcs_health

    _seed_repo()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO user_repos (user_id, repo_key, owner, name, is_join, is_admin) "
                  "VALUES (?,?,?,?,1,1)", (3759, RK, "issue3759", "repo-vcs-health"))
        db.commit()
    set_repo_vcs_health(RK, {"vcs_status": "vcs_missing", "vcs_last_error_code": "404",
                             "vcs_missing_since": "2026-11-25T01:00:00+00:00"})

    rows = user_db.get_managed_repos(3759, personal_org=True)
    row = next(r for r in rows if r["repo_key"] == RK)
    assert row["vcs_status"] == "vcs_missing"
    assert row["vcs_missing"] is True
    assert row["vcs_last_error_code"] == "404"
    assert row["vcs_missing_since"].startswith("2026-11-25")


def test_ops_reset_clears_ledger_through_mgmt(monkeypatch):
    """运维 reset：台账由 mgmt 侧直接清除（不跨域回写），before/after 可读。"""
    from src.domains.mgmt.migrations.repo_vcs_health_columns import (
        add_repo_vcs_health_columns)
    from src.domains.mgmt.public import get_repo_vcs_health, set_repo_vcs_health
    from src.domains.mgmt.routes import repos as repos_route

    add_repo_vcs_health_columns()
    _seed_repo()
    set_repo_vcs_health(RK, {"vcs_status": "vcs_missing", "vcs_last_error_code": "404",
                             "vcs_missing_since": "2026-11-25T01:00:00+00:00",
                             "vcs_next_probe_at": "2026-11-25T02:00:00+00:00",
                             "vcs_fail_count": 3})
    monkeypatch.setattr(
        "src.domains.mgmt.user.db.resolve_managed_repo_key", lambda o, r: RK)
    monkeypatch.setattr("src.api.deps.check_repo_access", lambda user, rk: True)

    resp = repos_route.reset_repo_vcs_health("issue3759", "repo-vcs-health",
                                             user={"id": 3759})
    assert resp["repo_key"] == RK
    assert resp["before"]["vcs_status"] == "vcs_missing"
    assert resp["after"]["vcs_status"] == "ok"
    assert resp["after"]["vcs_fail_count"] == 0
    row = get_repo_vcs_health(RK)
    assert row["vcs_status"] == "ok"
    assert row["vcs_next_probe_at"] == ""
    assert int(row["vcs_fail_count"]) == 0