"""Tests for login whitelist (is_gitcode_user_allowed)."""
import pytest, os, sys, json

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


@pytest.fixture
def whitelist_path(tmp_path, monkeypatch):
    """临时白名单文件路径，隔离不触碰 configs/ 真实文件"""
    import src.config as cfg
    p = tmp_path / "login-whitelist.json"
    monkeypatch.setattr(cfg, "LOGIN_WHITELIST_PATH", str(p))
    return p


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_no_config_allows_all(whitelist_path, monkeypatch):
    """配置文件不存在 → 全部放行"""
    import src.config as cfg
    assert not os.path.exists(whitelist_path)
    assert cfg.is_gitcode_user_allowed("anyone") is True
    assert cfg.is_gitcode_user_allowed("Jason7602") is True


def test_empty_whitelist_allows_all(whitelist_path):
    """whitelist 为空列表 → 全部放行"""
    import src.config as cfg
    _write(whitelist_path, {"whitelist": []})
    assert cfg.is_gitcode_user_allowed("userA") is True
    assert cfg.is_gitcode_user_allowed("userB") is True


def test_member_in_whitelist_allowed(whitelist_path):
    """白名单含 userA → userA 放行"""
    import src.config as cfg
    _write(whitelist_path, {"whitelist": ["userA", "Jason7602"]})
    assert cfg.is_gitcode_user_allowed("userA") is True
    assert cfg.is_gitcode_user_allowed("Jason7602") is True


def test_non_member_denied(whitelist_path, monkeypatch):
    """白名单含 userA → userB 拒绝。

    新逻辑（issue #333 问题14）：login_whitelist_enabled 开关启用时才校验。
    文件白名单是 DB 不可用时的降级路径。测试需 mock 开关启用 + DB 异常强制走文件降级。
    """
    import src.config as cfg
    # 模拟开关启用
    monkeypatch.setattr(cfg, "_is_login_whitelist_enabled", lambda: True)
    # 模拟 DB 查询抛异常——强制走文件白名单降级路径
    from src.session_manager import db_lock
    mock_db = type('MockDB', (), {'cursor': lambda self: (_ for _ in ()).throw(RuntimeError('DB unavailable'))})()
    monkeypatch.setattr('src.session_manager.db', mock_db)
    _write(whitelist_path, {"whitelist": ["userA"]})
    # 重置缓存确保走新 mock
    cfg._WL_ENABLED_CACHE = True
    cfg._WL_CACHE_TS = float('inf')
    assert cfg.is_gitcode_user_allowed("userB") is False


def test_broken_config_allows_all(whitelist_path):
    """配置损坏（非法 JSON）→ 保守放行"""
    import src.config as cfg
    whitelist_path.write_text("{not-valid-json", encoding="utf-8")
    assert cfg.is_gitcode_user_allowed("userA") is True


def test_empty_login_denied(whitelist_path):
    """空 gitcode_login → 拒绝"""
    import src.config as cfg
    _write(whitelist_path, {"whitelist": ["userA"]})
    assert cfg.is_gitcode_user_allowed("") is False
    assert cfg.is_gitcode_user_allowed(None) is False


def test_non_dict_config_allows_all(whitelist_path):
    """配置不是对象（如数组）→ 全部放行"""
    import src.config as cfg
    whitelist_path.write_text('["userA"]', encoding="utf-8")
    assert cfg.is_gitcode_user_allowed("userB") is True
