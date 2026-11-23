"""凭据密钥版本化 + 写入口收口 S1–S7。

事故：rc 表归 insight，但 mgmt 侧曾直写另一套密钥派生的行（kid="derived"），
两台主机 JWT_SECRET 不同 → 同一 kid 不同字节 → 解不开却看不出原因；
新代码又把“解不开”当硬失败 → gitee 流水线被判凭据不可用。
口径：
  ① derived 密钥 kid 必须能区分字节（derived-<8hex>）
  ② 未知 kid = 不可用 → 回落下一档，不硬失败
  ③ rc 表只有 insight 一个写口（其它域只能调契约）
"""
from __future__ import annotations

import pathlib

import pytest


def test_s1_derived_kid_is_versioned(monkeypatch):
    from src.kernel import security_config as sc

    monkeypatch.setattr(sc, "_load", lambda: {})
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-0123456789abcdef")
    kid, b64 = sc.get_credential_keys().split(":", 1)
    assert kid.startswith("derived-") and len(kid) == len("derived-") + 8
    assert b64


def test_s2_two_secrets_give_two_kids(monkeypatch):
    from src.kernel import security_config as sc

    monkeypatch.setattr(sc, "_load", lambda: {})
    monkeypatch.setenv("JWT_SECRET", "a" * 40)
    k1 = sc.get_credential_keys().split(":", 1)[0]
    monkeypatch.setenv("JWT_SECRET", "b" * 40)
    k2 = sc.get_credential_keys().split(":", 1)[0]
    assert k1 != k2, "两台主机必须得到不同的 kid，否则解不开也无法归因"


def test_s3_unknown_kid_raises_its_own_error():
    from src.domains.mgmt.auth.credential_crypto import UnknownKeyError, decrypt

    with pytest.raises(UnknownKeyError) as ei:
        decrypt("k-other:AAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAA==")
    assert ei.value.kid == "k-other"


def test_s4_meta_reports_unknown_key_reason(monkeypatch):
    from src.domains.mgmt.user import db as mdb

    monkeypatch.setattr(
        mdb._insight_public, "repo_credential_meta",
        lambda rk, ct: ("k-other:AAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAA==", "id-1"),
    )
    meta = mdb.get_repo_credential_meta("gitee:a/b", "bot")
    assert meta["undecryptable"] is True and meta["reason"] == "unknown_key"


def test_s5_resolver_skips_undecryptable_row(monkeypatch):
    """解不开 → ("", "")：不返回 stage、不中断，交给下一档。"""
    from src.kernel import token_resolver as tr

    strat = tr.ExplicitRepoBotToken("gitee")
    monkeypatch.setattr(strat, "_read_row",
                        lambda rk: {"value": "", "id": "x", "undecryptable": True,
                                    "reason": "unknown_key"})
    monkeypatch.setattr(strat, "_is_expired", lambda rk: False)
    assert strat.resolve("gitee:a/b") == ("", "")
    assert strat.stage("gitee:a/b") == ""


def test_s6_only_the_insight_domain_writes_repo_credentials():
    """写入口径棘轮：rc 表的 INSERT/UPDATE/DELETE 只允许出现在 insight 域。"""
    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    verbs = ("insert into grape_t_repo_credentials", "update grape_t_repo_credentials",
             "delete from grape_t_repo_credentials", "replace into grape_t_repo_credentials")
    bad = []
    for f in root.rglob("*.py"):
        rel = f.relative_to(root)
        if rel.parts[0] == "domains" and rel.parts[1] == "insight":
            continue
        text = f.read_text(encoding="utf-8").lower()
        for v in verbs:
            if v in text:
                bad.append(f"{rel}: {v}")
    assert not bad, f"非 owner 域直写 rc 表：{bad}"


def test_s7_platform_migration_uses_the_insight_contract():
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src/domains/platform/migration.py").read_text(encoding="utf-8")
    assert "set_repo_credential_derived_from" in src
    assert "UPDATE grape_t_repo_credentials" not in src
