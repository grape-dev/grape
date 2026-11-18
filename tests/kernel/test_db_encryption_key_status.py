"""数据库加密密钥自检（key_status）回归。

背景：沙箱凭据创建时会顺带登记一条云沙箱平台配置，登记要 encrypt(api_key)。
本机若既没有 GRAPE_DB_ENCRYPTION_KEY 也没有 configs/crypto.json，这个 encrypt 会抛
RuntimeError，而调用处把它当成 non-blocking 吞成 WARNING——结果"凭据建好了、平台没登记、
设备卡片上没有详情按钮"，用户完全看不出原因。

本轮修法是不改加密行为，只把"密钥是否可用"做成可探测的：key_status() 不抛异常，
供启动自检（main.py）与运维排查使用。这组测试锁定它的判定口径，避免与
encrypt/decrypt 的真实加载规则分叉。
"""
import base64
import importlib
import json
import os

import pytest


@pytest.fixture()
def crypto_mod(tmp_path, monkeypatch):
    """每个用例独立：清空环境变量、把 configs 目录指到 tmp_path、重置模块缓存。"""
    monkeypatch.delenv("GRAPE_DB_ENCRYPTION_KEY", raising=False)
    import src.kernel.crypto as crypto
    crypto = importlib.reload(crypto)
    crypto._CONFIG_DIR = str(tmp_path)
    crypto._aesgcm = None
    return crypto


def _write_key(crypto_mod, key_b64, mode=0o600):
    p = os.path.join(crypto_mod._CONFIG_DIR, "crypto.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"encryption_key": key_b64}, f)
    os.chmod(p, mode)
    return p


def test_missing_key_reports_not_configured(crypto_mod):
    st = crypto_mod.key_status()
    assert st["configured"] is False
    assert st["source"] == ""
    assert "not set" in st["error"]


def test_env_key_is_configured(crypto_mod, monkeypatch):
    monkeypatch.setenv("GRAPE_DB_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    st = crypto_mod.key_status()
    assert st["configured"] is True
    assert st["source"] == "env"
    assert st["error"] == ""


def test_file_key_is_configured(crypto_mod):
    _write_key(crypto_mod, base64.urlsafe_b64encode(os.urandom(32)).decode())
    st = crypto_mod.key_status()
    assert st["configured"] is True
    assert st["source"] == "file"


def test_open_permissions_fail_closed(crypto_mod):
    _write_key(crypto_mod, base64.urlsafe_b64encode(os.urandom(32)).decode(), mode=0o644)
    st = crypto_mod.key_status()
    assert st["configured"] is False
    assert "permissions too open" in st["error"]


def test_wrong_key_length_rejected(crypto_mod):
    _write_key(crypto_mod, base64.urlsafe_b64encode(os.urandom(16)).decode())
    st = crypto_mod.key_status()
    assert st["configured"] is False
    assert "32 bytes" in st["error"]


def test_status_matches_encrypt_roundtrip(crypto_mod):
    """自检通过 ⇒ 真实加密链路可用（防止"自检绿、加密红"的口径分裂）。"""
    _write_key(crypto_mod, base64.urlsafe_b64encode(os.urandom(32)).decode())
    assert crypto_mod.key_status()["configured"] is True
    enc = crypto_mod.encrypt("probe-secret")
    assert crypto_mod.decrypt(enc) == "probe-secret"


def test_corrupt_file_reports_unreadable(crypto_mod):
    """文件在但 JSON 损坏 → 报"读不出来"，不能报成"没配"。"""
    p = os.path.join(crypto_mod._CONFIG_DIR, "crypto.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not-json")
    os.chmod(p, 0o600)
    st = crypto_mod.key_status()
    assert st["configured"] is False
    assert "unreadable" in st["error"]


def test_invalid_base64_reports_encoding_error(crypto_mod):
    _write_key(crypto_mod, "not-base64-@@@")
    st = crypto_mod.key_status()
    assert st["configured"] is False
    assert "base64url" in st["error"]
