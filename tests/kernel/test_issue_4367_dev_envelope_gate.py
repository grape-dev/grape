"""Issue #4367 B：写入侧 fail-closed——dev 信封签发禁令（判据叠加 + 显式开关）。

事故（#4367）：security.json 缺失/env 误配让 ``is_production()`` 恒 False，
「生产拒 dev」守卫全部旁路 → 生产进程持续签发 kid=dev 信封（内置公开常量），
执行侧解不开 → 空 env 静默降级。本组用例锁定修复后的公开行为：

- 判据叠加：目标库 DSN/env 命中 prod、进程在 prod 主机同样触发禁令
  （不再单看 ``is_production()``），三个判据的证据可收集；
- 显式开关 ``GRAPE_ALLOW_DEV_ENVELOPE`` 默认关，命中时须显式放行才可签发；
- 开发/测试（判据未命中）行为不变——dev 信封照常可用（双向用例）；
- 禁令只拦「即将落 dev 信封 / 明文降级」的瞬间，真实 kid（cred1）在 prod 判据
  命中下照常可用（不误伤生产入队）。
"""
from __future__ import annotations

import base64
import sys

import pytest

from src.kernel import security_config as sc
import src.domains.mgmt.auth.credential_crypto as cc

REAL_KEY_B64 = base64.b64encode(b"k" * 32).decode()      # 真实 32B 密钥（cred1）


def _reset_ring(monkeypatch):
    """credential_crypto 密钥环每用例重置（_load_keys 有进程级 latch）。"""
    monkeypatch.setattr(cc, "_loaded", False)
    monkeypatch.setattr(cc, "_current_key_id", "")
    monkeypatch.setattr(cc, "_keys", {})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """判据输入全部显式控制：任何残留环境变量不得影响用例口径。"""
    for name in ("GRAPE_DSN_RUNTIME", "GRAPE_DSN_MGMT", "GRAPE_DSN_INSIGHT",
                 "DEVOPS_DB_NAME", "GRAPE_RUNTIME_DB_NAME", "DEVOPS_DB_HOST",
                 "DEVOPS_DB_PATH", "GRAPE_HOSTNAME", "GRAPE_HOST_ENV",
                 "GRAPE_ALLOW_DEV_ENVELOPE", "JWT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sc, "_config", None)
    _reset_ring(monkeypatch)
    yield
    _reset_ring(monkeypatch)


def _prod_dsn(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_prod_runtime")


# ---------- 判据收集（可观测证据） ----------

def test_forbidden_reasons_collect_all_three_criteria(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda: {"env": "production"})
    monkeypatch.setenv("GRAPE_DSN_RUNTIME", "mysql://u:p@10.0.0.1:3306/grape_prod_runtime")
    monkeypatch.setenv("GRAPE_HOSTNAME", "prod-master-001")

    reasons = sc.dev_envelope_forbidden_reasons()

    assert any("env=production" in r for r in reasons)
    assert any("GRAPE_DSN_RUNTIME" in r for r in reasons)
    assert any("host=" in r for r in reasons)
    assert sc.forbid_dev_envelope_reason() == "; ".join(reasons)


def test_prod_markers_do_not_hit_product_like_names(monkeypatch):
    """`productbox` / `reproduction` / `grape_dev_*` 不误伤（带边界的 prod 标记）。"""
    monkeypatch.setattr(sc, "_load", lambda: {})
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_productbox")
    monkeypatch.setenv("DEVOPS_DB_HOST", "reproduction-db")
    monkeypatch.setenv("DEVOPS_DB_PATH", "data/grape_dev_runtime.db")
    monkeypatch.setenv("GRAPE_HOSTNAME", "devbox-001")

    assert sc.dev_envelope_forbidden_reasons() == []
    assert sc.forbid_dev_envelope_reason() == ""


def test_prod_host_env_marker_triggers_forbid(monkeypatch):
    monkeypatch.setattr(sc, "_load", lambda: {})
    monkeypatch.setenv("GRAPE_HOST_ENV", "production")

    assert any("GRAPE_HOST_ENV" in r for r in sc.dev_envelope_forbidden_reasons())


def test_allow_switch_is_explicit_and_default_off(monkeypatch):
    """开关默认关：判据命中即禁；显式 GRAPE_ALLOW_DEV_ENVELOPE=1 才放行（应急留痕）。"""
    _prod_dsn(monkeypatch)
    monkeypatch.setattr(sc, "_load", lambda: {})
    assert sc.allow_dev_envelope() is False
    assert sc.forbid_dev_envelope_reason()

    monkeypatch.setenv("GRAPE_ALLOW_DEV_ENVELOPE", "1")
    assert sc.allow_dev_envelope() is True
    assert sc.forbid_dev_envelope_reason() == ""


# ---------- get_credential_keys：dev 常量回落拒绝（双向） ----------

def test_get_credential_keys_refuses_dev_fallback_on_prod_dsn(monkeypatch):
    """security.json 缺失（is_production=False）但连着生产库 → 拒绝回落 dev 常量。

    这正是 #4367 的事故形态：/opt/grape 等无配置文件目录的引擎侧单元。
    """
    _prod_dsn(monkeypatch)
    monkeypatch.setattr(sc, "_load", lambda: {})

    with pytest.raises(RuntimeError, match="dev 密钥"):
        sc.get_credential_keys()


def test_get_credential_keys_still_falls_back_in_dev(monkeypatch):
    """开发环境（判据未命中）照常回落内置 dev 常量——既有行为零回归。"""
    monkeypatch.setattr(sc, "_load", lambda: {})

    keys = sc.get_credential_keys()

    assert keys.startswith("dev:")
    assert keys.split(":", 1)[1] == base64.b64encode(b"0" * 32).decode()


# ---------- credential_crypto.encrypt：三个执行点 ----------

def test_encrypt_refuses_dev_only_ring_in_forbidden_state(monkeypatch):
    """密钥环只剩内置 dev 常量（security.json 显式配错）+ prod 判据命中 → 拒绝。"""
    _prod_dsn(monkeypatch)
    monkeypatch.setattr(sc, "_load",
                        lambda: {"credential_keys": "dev:" + base64.b64encode(b"0" * 32).decode()})

    with pytest.raises(RuntimeError, match="dev"):
        cc.encrypt("secret")


def test_encrypt_refuses_dev_kid_first_in_mixed_ring(monkeypatch):
    """混配环（dev 排首 → 当前签发 kid=dev）在禁令下同样拒发——最后闸口。"""
    _prod_dsn(monkeypatch)
    monkeypatch.setattr(sc, "_load", lambda: {
        "credential_keys": f"dev:{base64.b64encode(b'0' * 32).decode()},{REAL_KEY_B64}"})

    with pytest.raises(RuntimeError, match="dev"):
        cc.encrypt("secret")


def test_encrypt_allows_dev_envelope_when_switch_explicitly_enabled(monkeypatch):
    """应急放行：开关显式开（GRAPE_ALLOW_DEV_ENVELOPE=1）→ dev 信封可签发。"""
    _prod_dsn(monkeypatch)
    monkeypatch.setenv("GRAPE_ALLOW_DEV_ENVELOPE", "1")
    monkeypatch.setattr(sc, "_load", lambda: {
        "credential_keys": "dev:" + base64.b64encode(b"0" * 32).decode()})

    sealed = cc.encrypt("secret")

    assert sealed.startswith("dev:")


def test_encrypt_with_real_kid_unchanged_under_forbid(monkeypatch):
    """禁令不得误伤真实密钥：prod 判据命中 + cred1 正常配置 → 加密照常（写入侧可用）。"""
    _prod_dsn(monkeypatch)
    monkeypatch.setattr(sc, "_load", lambda: {"credential_keys": f"cred1:{REAL_KEY_B64}"})

    sealed = cc.encrypt("secret")

    assert sealed.startswith("cred1:")
    assert cc.decrypt(sealed) == "secret"


def test_encrypt_refuses_plaintext_downgrade_in_forbidden_state(monkeypatch):
    """无 cryptography + prod 判据命中 → 拒绝明文降级（今日 9 行明文的成因路径）。"""
    _prod_dsn(monkeypatch)
    monkeypatch.setattr(sc, "_load", lambda: {})
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)

    with pytest.raises(RuntimeError, match="plaintext"):
        cc.encrypt("secret")


def test_encrypt_plaintext_downgrade_still_works_in_dev(monkeypatch):
    """开发环境无 cryptography：明文降级照常（既有行为零回归）。"""
    monkeypatch.setattr(sc, "_load", lambda: {})
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)

    assert cc.encrypt("secret") == "secret"


# ---------- 写入侧入口端到端（env_extra_security.encrypt_env_extra） ----------

def test_encrypt_env_extra_fails_closed_end_to_end(monkeypatch):
    """入队路径可见失败：禁令命中时 encrypt_env_extra 抛错，不落注定解不开的行。"""
    from src.domains.runtime.engine.env_extra_security import ENVELOPE_PREFIX, encrypt_env_extra

    _prod_dsn(monkeypatch)
    monkeypatch.setattr(sc, "_load", lambda: {
        "credential_keys": "dev:" + base64.b64encode(b"0" * 32).decode()})

    with pytest.raises(RuntimeError, match="env_extra 加密失败"):
        encrypt_env_extra({"SECRET_AK": "x"})


def test_encrypt_env_extra_dev_mode_roundtrip_unchanged(monkeypatch):
    """开发环境（判据未命中）env_extra 信封加解密照常——双向用例的开发侧。"""
    from src.domains.runtime.engine.env_extra_security import (
        ENVELOPE_PREFIX, decrypt_env_extra, encrypt_env_extra)

    monkeypatch.setattr(sc, "_load", lambda: {})
    env = {"GRAPE_SESSION_TOKEN": "tk_x", "MY_API_KEY": "sek"}

    sealed = encrypt_env_extra(env)

    assert sealed.startswith(ENVELOPE_PREFIX)
    assert "tk_x" not in sealed and "sek" not in sealed
    assert decrypt_env_extra(sealed) == env
