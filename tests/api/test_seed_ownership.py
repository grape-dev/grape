"""种子表归属自检（`db_setup._seed_owners_missing`）回归。

背景：`_owned_skip` 只拦「别家的表」；`table_ownership.json` **没有条目**时它一律放行，
三库模式下这种种子表会变成「谁都不种」——非 owner 库没建表直接 1146，owner 库以为别家会种，
数据静默缺失。所以本测试锁两件事：

1. 当前仓库的 `_SEED_FILES` 全部有归属（清单与种子表不许漂移）；
2. 新增一张无归属的种子表时，自检必须把它点出来（否则等于没加检查）。
"""
import pytest

from src.api import db_setup


def test_current_seed_tables_all_owned():
    assert db_setup._seed_owners_missing() == []


def test_unowned_seed_table_is_reported(monkeypatch):
    monkeypatch.setattr(
        db_setup, "_SEED_FILES",
        [("demo_seed", "demo.json", "table_without_owner_for_test")],
    )
    assert db_setup._seed_owners_missing() == ["table_without_owner_for_test"]


def test_manifest_unavailable_does_not_block(monkeypatch):
    """归属清单读不出来 → 放行（不因守卫故障阻断启动）。"""
    import src.kernel.table_ownership as own

    def _boom():
        raise RuntimeError("manifest broken")

    monkeypatch.setattr(own, "owner_map", _boom)
    assert db_setup._seed_owners_missing() == []