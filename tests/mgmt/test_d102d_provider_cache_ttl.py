"""D102-D（R1）：providers get_provider TTL 重载——行为锁。

P1-5（实弹）：get_provider 原 `if not _loaded` 形态——TTL 过期后登录端点
永不重载（带外写 oauth_providers_config 后 40 分钟 /auth/login/huawei 仍
400"不支持的平台"，重启才收敛）。list_providers 同款形态一并修。
对齐 platform_card.get_platform_card 现行形态（无条件 _load，TTL 函数内）。
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import inspect
import time
from unittest.mock import patch


def test_d102d_get_provider_unconditional_load():
    """get_provider 无条件走 _load_providers（TTL 过期后仍会重载）。"""
    import src.domains.mgmt.auth.providers as prov
    src = inspect.getsource(prov)
    # 旧形态清零（if not _loaded: _load_providers 漏网模式）
    for fn in ("get_provider", "list_providers"):
        fsrc = inspect.getsource(getattr(prov, fn))
        assert "if not _loaded" not in fsrc, \
            f"{fn} 仍保留 if not _loaded 旧形态（P1-5 回潮）"
        assert "_load_providers()" in fsrc, f"{fn} 未调 _load_providers"


def test_d102d_ttl_expiry_triggers_reload():
    """行为级：TTL 过期后 get_provider 拿到新配置（带外写库可见）。

    场景重建：首次加载拿到旧值 → 时间推进超 TTL → meta 已被带外改写 →
    get_provider 应返回新值（旧形态下返回旧值=本用例 RED）。
    """
    import src.domains.mgmt.auth.providers as prov
    from src.kernel.session_manager import db, db_lock
    key = "d102d_probe"
    try:
        with patch.object(prov, "_CACHE_TTL", 0.05):  # 50ms TTL 加速测试
            prov.invalidate_provider_cache()
            # 首次加载：写入旧配置
            with db_lock:
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                           ("oauth_providers_config",
                            '__import__'.replace('__import__', '') or ""))
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                           ("oauth_providers_config",
                            '{"d102d_probe": {"name": "OldName", "user_url": "https://old.example/u", "id_field": "id", "login_field": "login", "name_field": "name"}}'))
                db.commit()
            old_cfg = prov.get_provider(key)
            assert old_cfg and old_cfg["name"] == "OldName"

            # 带外改写 meta（绕过 invalidate_provider_cache——带外写库形态）
            time.sleep(0.06)  # 超 TTL
            with db_lock:
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                           ("oauth_providers_config",
                            '{"d102d_probe": {"name": "NewName", "user_url": "https://new.example/u", "id_field": "id", "login_field": "login", "name_field": "name"}}'))
                db.commit()
            new_cfg = prov.get_provider(key)
            assert new_cfg and new_cfg["name"] == "NewName", \
                "TTL 过期后 get_provider 未重载（P1-5 缺陷回潮）"
    finally:
        with db_lock:
            db.execute("DELETE FROM meta WHERE key='oauth_providers_config'")
            db.commit()
        prov.invalidate_provider_cache()


def test_d102d_invalidate_still_works():
    """admin 主动失效（invalidate_provider_cache）路径不回归——P2-2 语义保持。"""
    import src.domains.mgmt.auth.providers as prov
    # invalidate 后 _loaded=False → 下次 get_provider 触发重载（既有热生效语义）
    prov.invalidate_provider_cache()
    # _load_providers 无 DB 配置时落内置默认——不炸即可（行为与改前一致）
    cfg = prov.get_provider("gitcode")
    assert isinstance(cfg, dict), "invalidate 后 get_provider 应正常返回（内置默认或 DB 配置）"
