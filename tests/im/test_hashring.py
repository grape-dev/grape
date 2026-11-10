"""哈希环测试（Issue #742 §7.5 A1）——现有行为固化（现状零覆盖）。"""
from src.im.engine.hashring import build_ring, owner, owners


def test_owner_empty_ring_returns_empty():
    assert owner([], "app1") == ""


def test_owner_is_stable_and_total():
    ring = build_ring(["r1", "r2"], vnodes=50)
    apps = [f"app_{i}" for i in range(200)]
    o = owners(ring, apps)
    assert set(o.values()) == {"r1", "r2"}
    # 稳定性：重复计算结果一致
    assert owners(ring, apps) == o


def test_ring_scale_moves_only_neighbors():
    """r1/r2 → r1/r2/r3：仅环上邻近段迁移，大部分 bot 归属不变。"""
    apps = [f"app_{i}" for i in range(500)]
    before = owners(build_ring(["r1", "r2"]), apps)
    after = owners(build_ring(["r1", "r2", "r3"]), apps)
    moved = sum(1 for a in apps if before[a] != after[a])
    # 三实例扩容理论迁移率 ≈ 1/3；断言 < 0.6（测试容差）
    assert moved / len(apps) < 0.6


def test_ring_only_owner_acquires_predicate():
    """非归属实例不 try_acquire（L1742 环预筛语义）。"""
    ring = build_ring(["r1", "r2"])
    app = "app_zhong"
    tried = {}
    for replica in ("r1", "r2"):
        tried[replica] = (owner(ring, app) == replica)
    assert sum(tried.values()) == 1  # 恰有一个实例会尝试抢占
