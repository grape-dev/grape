"""S94 用例（D69，先测后改）：observability 可见性候选集统一 + health 聚合动态化。

1. 结构守卫：observability 族零 to_repo_key(·, "github") 双键字面量
   （统一 repo_key_candidates——gitee 等卡平台仓此前被可见性过滤排除）；
2. health 聚合：遍历全部已巡检平台（config-only 不再漏计）。
"""
import pathlib


def test_s94_no_dual_key_literals_in_observability():
    root = pathlib.Path(__file__).resolve().parent.parent / "src/observability"
    extra = pathlib.Path(__file__).resolve().parent.parent / "src/domains/insight/routes/observability.py"
    bad = []
    for py in list(root.rglob("*.py")) + [extra]:
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if 'to_repo_key(' in line and '"github"' in line:
                bad.append(f"{py.name}:{i}")
    assert not bad, f"observability 残留双键字面量（统一 repo_key_candidates，S94）: {bad}"


def test_s94_health_aggregation_covers_all_platforms():
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/domains/runtime/engine/bot_token_health.py").read_text()
    assert 'for prov in ("gitcode", "github")' not in src, (
        "health 聚合硬编码双平台——config-only 平台漏计（S94 改遍历 platforms）")


def test_s94_health_results_includes_config_only():
    """行为锁：配了全局 token 的 config-only 平台进 results（D19 语义保持）。"""
    from src.kernel.session_manager import db, db_lock
    from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
    with db_lock:
        db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES "
                   "('global_bot_gitee_token','gtok')")
        db.commit()
    invalidate_platform_card_cache()
    try:
        from src.domains.runtime.engine.bot_token_health import _verify_global_bot_token
        results = _verify_global_bot_token()
        assert "gitee" in results.get("platforms", {})
    finally:
        with db_lock:
            db.execute("DELETE FROM meta WHERE key='global_bot_gitee_token'")
            db.commit()
        invalidate_platform_card_cache()
