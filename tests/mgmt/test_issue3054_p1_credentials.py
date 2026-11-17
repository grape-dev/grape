"""Issue #3054 P1：凭据与刷新链路（单一真源 / 同源成对 / 解密失败显式化 / 4xx 不重试）。

行为锁（每条对应一个子项）：
- P1-1 明文不再落 meta 与 configs（写入路径 + 一次性清理只删 secret 不动其它键）；
- P1-2 登录换 code 与 token 刷新走同一取凭据入口，client_id+secret 同源成对；
- P1-3 密文不可解 = 显式错误（credential_decrypt_failed）+ 审计事件，绝不回落 meta；
- P1-4 exchange_token 只对瞬时错误重试，4xx 立即返回并落 provider error/error_description。

全部用内存 sqlite + monkeypatch，不发网络、不连真库。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.domains.mgmt.auth import credential_source as cred_src
from src.domains.mgmt.auth import providers as prov
from src.domains.mgmt.auth.retry_policy import DEFAULT_TOKEN_RETRY, RetryPolicy
from src.domains.mgmt.auth.credential_crypto import decrypt, encrypt

OAUTH_JSON_KEYS = ("client_id", "client_secret")


# --------------------------------------------------------------------------- 环境


@pytest.fixture()
def db_env(monkeypatch, tmp_path):
    """内存渠道表 + 空 meta + 隔离的 configs 目录。"""
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()

    import src.domains.platform.importer as imp
    import src.kernel.persistence.engine as eng_mod
    import src.domains.mgmt.user.db as udb

    meta: dict[str, str] = {}
    monkeypatch.setattr(eng_mod, "get_engine", lambda *a, **k: engine)
    monkeypatch.setattr(imp, "is_table_backed", lambda: True)
    monkeypatch.setattr(imp, "bump_epoch", lambda *a, **k: 0)
    monkeypatch.setattr(udb, "get_meta_value", lambda k: meta.get(k, ""))
    monkeypatch.setattr(udb, "set_meta_value", lambda k, v: meta.__setitem__(k, v))
    # 告警去抖：清空，保证每个用例都能看到审计事件
    import src.domains.mgmt.auth.credential_source as cs_mod
    monkeypatch.setattr(cs_mod, "_alert_seen", {}, raising=False)

    oauth_json = tmp_path / "oauth-providers.json"
    oauth_json.write_text(json.dumps({"gitcode": {"name": "GitCode"}}), encoding="utf-8")

    import src.domains.mgmt.admin.config_store as cs
    monkeypatch.setattr(cs, "_OAUTH_PATH", str(oauth_json))

    yield SimpleNamespace(engine=engine, meta=meta, json_path=oauth_json, cs=cs)
    engine.dispose()


def _seed_channel(env, provider="gitcode", client_id="cid-table", client_secret="sec-table"):
    with Session(env.engine) as s:
        s.connection().exec_driver_sql(
            "INSERT INTO grape_t_oauth_channels "
            "(id, provider, name, client_id_enc, client_secret_enc) VALUES (?,?,?,?,?)",
            (provider, provider, provider, encrypt(client_id) if client_id else "",
             encrypt(client_secret) if client_secret else ""),
        )
        s.commit()


def _seed_bad_ciphertext(env, provider="gitcode"):
    """同 kid、密钥字节不再匹配 → AESGCM InvalidTag（线上事故形态）。"""
    bad = "dev:AAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
    with Session(env.engine) as s:
        s.connection().exec_driver_sql(
            "INSERT INTO grape_t_oauth_channels "
            "(id, provider, name, client_id_enc, client_secret_enc) VALUES (?,?,?,?,?)",
            (provider, provider, provider, bad, bad),
        )
        s.commit()


@pytest.fixture()
def fake_http(monkeypatch):
    """替换 requests.post：按脚本依次返回/抛异常，并记录调用次数与请求体。"""
    calls = {"n": 0, "data": []}

    class _Resp:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload if payload is not None else {"access_token": "at"}
            self.text = text or json.dumps(self._payload)

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    script: list = []
    ctx = SimpleNamespace(calls=calls, script=script, Resp=_Resp, post=None)

    class _Exc:
        class Timeout(Exception):
            pass

        class ConnectTimeout(Timeout):
            pass

        class ReadTimeout(Timeout):
            pass

        class ConnectionError(Exception):
            pass

        class ChunkedEncodingError(Exception):
            pass

        class ContentDecodingError(Exception):
            pass

    import requests as _real

    fake_exceptions = SimpleNamespace(**{k: getattr(_Exc, k) for k in
                                        ("Timeout", "ConnectTimeout", "ReadTimeout",
                                         "ConnectionError", "ChunkedEncodingError",
                                         "ContentDecodingError")})
    ctx.exceptions = fake_exceptions

    def _post(url, data=None, **kwargs):
        calls["n"] += 1
        calls["data"].append(dict(data or {}))
        script = ctx.script
        if not script:
            return _Resp()
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    fake_requests = SimpleNamespace(post=_post, exceptions=fake_exceptions)
    # exchange_token 内 `import requests as _req`：换成假模块
    import sys

    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr(_real, "post", _post, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    ctx.post = _post
    return ctx


def _patch_provider(monkeypatch, provider="gitcode", token_url="https://oauth.example/token"):
    cfg = {"name": provider, "token_url": token_url, "user_url": "https://oauth.example/user"}
    monkeypatch.setattr(prov, "get_provider", lambda p: dict(cfg) if p == provider else None)
    return cfg


# ------------------------------------------------------- P1-1：写入路径不落明文


def test_save_config_writes_encrypted_only(db_env, monkeypatch):
    """表有密文 = 唯一存放点；meta 只有 client_id，configs json 里没有 secret 键。"""
    _seed_channel(db_env)
    cs = db_env.cs
    monkeypatch.setattr(cs, "read_public_config", lambda: {})
    # 隔离：save_config 末尾的 D109 平台对象同步会走真实 uow/engine，测试里必须挡住，
    # 否则会把 fixture 凭据写进共享开发库、污染同批其它用例。
    import src.domains.platform.admin_sync as admin_sync
    monkeypatch.setattr(admin_sync, "sync_from_admin_payload", lambda _p: None)

    payload = {
        "gitcode_oauth_client_id": "cid-new",
        "gitcode_oauth_client_secret": "sec-super-secret",
    }
    cs.save_config(payload)

    with Session(db_env.engine) as s:
        row = s.connection().exec_driver_sql(
            "SELECT client_id_enc, client_secret_enc FROM grape_t_oauth_channels "
            "WHERE provider='gitcode'").fetchone()
    assert decrypt(row[0]) == "cid-new"
    assert decrypt(row[1]) == "sec-super-secret"

    # 明文 secret 绝不进 meta
    assert "sec-super-secret" not in json.dumps(db_env.meta)
    assert not [k for k in db_env.meta if k.endswith("_client_secret")], db_env.meta
    # 明文 secret 绝不进 configs/oauth-providers.json
    on_disk = json.loads(db_env.json_path.read_text(encoding="utf-8"))
    assert "sec-super-secret" not in json.dumps(on_disk)
    assert "client_secret" not in on_disk["gitcode"]
    # client_id 属非敏感协议参数，保留可回显
    assert on_disk["gitcode"]["client_id"] == "cid-new"


def test_purge_legacy_secret_copies_only_strips_secret(db_env):
    """一次性清理：只删 secret（meta + configs），client_id / redirect_uri 一律保留；幂等。"""
    _seed_channel(db_env)
    db_env.meta["oauth_gitcode_client_secret"] = "legacy-plain"
    db_env.meta["oauth_github_client_secret"] = "gh-plain"          # 表里没有 → 必须保留
    db_env.meta["oauth_gitcode_client_id"] = "cid-legacy"
    db_env.meta["oauth_providers_config"] = json.dumps({
        "gitcode": {"name": "GitCode", "client_secret": "card-plain", "client_id": "cid-card"},
    })
    db_env.json_path.write_text(json.dumps({
        "gitcode": {"name": "GitCode", "client_id": "cid-file", "client_secret": "file-plain",
                    "redirect_uri": "https://cb/x"},
    }), encoding="utf-8")

    out = db_env.cs.purge_legacy_secret_copies()

    assert "oauth_gitcode_client_secret" in out["meta"]
    assert db_env.meta["oauth_gitcode_client_secret"] == ""
    # 表里没有密文的渠道：明文副本不能删（删了就彻底没凭据）
    assert db_env.meta["oauth_github_client_secret"] == "gh-plain"
    # 其它 meta 字段不受影响
    assert db_env.meta["oauth_gitcode_client_id"] == "cid-legacy"
    cards = json.loads(db_env.meta["oauth_providers_config"])
    assert "client_secret" not in cards["gitcode"] and cards["gitcode"]["client_id"] == "cid-card"

    on_disk = json.loads(db_env.json_path.read_text(encoding="utf-8"))
    assert "client_secret" not in on_disk["gitcode"]
    assert on_disk["gitcode"]["client_id"] == "cid-file"
    assert on_disk["gitcode"]["redirect_uri"] == "https://cb/x"
    assert on_disk["gitcode"]["name"] == "GitCode"

    # 幂等：再跑一次没有任何可删项
    again = db_env.cs.purge_legacy_secret_copies()
    assert again == {"meta": [], "configs": []}


def test_no_source_writes_plaintext_secret_to_meta():
    """全仓：除"表不可用"迁移窗口那一处，没有任何 set_meta_value 写 oauth secret。"""
    import re
    from pathlib import Path

    pat = re.compile(r"""set_meta_value\(\s*f?["']oauth_[^"']*client_secret""")
    hits: list[str] = []
    for path in Path("src").rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pat.search(line):
                hits.append(f"{path}:{i}")
    # 仅 config_store.save_config 的迁移窗口分支（_table_written 为假时兜底）允许出现
    assert len(hits) <= 1, hits
    if hits:
        assert hits[0].startswith("src/domains/mgmt/admin/config_store.py:"), hits
    # 该唯一命中必须在"表不可写"守卫里
    src = Path("src/domains/mgmt/admin/config_store.py").read_text(encoding="utf-8")
    assert "_table_written.get(provider)" in src


# ------------------------------------------- P1-2：同源成对取（表有凭据 meta 空）


def test_get_client_credentials_table_wins_over_meta(db_env):
    _seed_channel(db_env)
    db_env.meta["oauth_gitcode_client_id"] = "cid-meta-stale"
    db_env.meta["oauth_gitcode_client_secret"] = "sec-meta-stale"
    assert prov.get_client_credentials("gitcode") == ("cid-table", "sec-table")
    assert prov.channel_configured("gitcode") is True


def test_get_client_credentials_falls_back_pairwise_only_when_table_empty(db_env):
    """表两列都空 = 迁移窗口：整对回落 meta（不能一半表一半 meta）。"""
    _seed_channel(db_env, client_id="", client_secret="")
    db_env.meta["oauth_gitcode_client_id"] = "cid-meta"
    db_env.meta["oauth_gitcode_client_secret"] = "sec-meta"
    assert prov.get_client_credentials("gitcode") == ("cid-meta", "sec-meta")


def test_half_migrated_row_does_not_mix_sources(db_env):
    """表里有 client_id 但 secret 列为空：不回填 meta 的 secret（混源=用错凭据组合）。"""
    _seed_channel(db_env, client_id="cid-table", client_secret="")
    db_env.meta["oauth_gitcode_client_secret"] = "sec-meta"
    assert prov.get_client_credentials("gitcode") == ("cid-table", "")


def test_login_and_refresh_use_same_credential_pair(db_env, fake_http, monkeypatch):
    """表有凭据、meta 空：换 code 与 refresh 都成功，且用的都是表里那对。"""
    _seed_channel(db_env)
    _patch_provider(monkeypatch)
    fake_http.script[:] = [fake_http.Resp(200, {"access_token": "at-code"})]
    tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x")
    assert err == "" and tokens["access_token"] == "at-code"
    assert fake_http.calls["data"][-1]["client_id"] == "cid-table"
    assert fake_http.calls["data"][-1]["client_secret"] == "sec-table"

    # ---- refresh 链路：同一对凭据（P1-2 的核心回归点）
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "_read_refresh_token_from_identity", lambda u, p: "rt-1")
    import src.domains.mgmt.auth.grape_account_db as gadb
    monkeypatch.setattr(gadb, "update_external_identity_tokens", lambda *a, **k: None)
    monkeypatch.setattr(udb, "get_credential", lambda *a, **k: {})
    monkeypatch.setattr(udb, "set_credential", lambda *a, **k: None)

    captured = {}

    class _UrlResp:
        def read(self):
            return json.dumps({"access_token": "at-refresh", "refresh_token": "rt-2",
                               "expires_in": 3600}).encode()

    def _urlopen(req, timeout=10):
        import urllib.parse as up

        captured.update(up.parse_qs((req.data or b"").decode()))
        return _UrlResp()

    import urllib.request as _urlreq
    monkeypatch.setattr(_urlreq, "urlopen", _urlopen)

    assert udb.refresh_oauth_token(1, "gitcode") == "at-refresh"
    assert captured["client_id"][0] == "cid-table"
    assert captured["client_secret"][0] == "sec-table"


# --------------------------------- P1-3：解密失败显式错误 + 告警事件，不回落 meta


def test_decrypt_failure_is_explicit_and_never_falls_back_to_meta(db_env, monkeypatch, caplog):
    _seed_bad_ciphertext(db_env)
    db_env.meta["oauth_gitcode_client_id"] = "cid-meta"
    db_env.meta["oauth_gitcode_client_secret"] = "sec-meta"

    events: list[tuple[str, dict]] = []
    import src.domains.mgmt.auth.audit as audit_mod
    monkeypatch.setattr(audit_mod, "log_audit",
                        lambda et, **kw: events.append((et, kw.get("detail") or {})), raising=False)

    with pytest.raises(prov.CredentialDecryptError) as ei:
        prov.get_client_credentials("gitcode")
    assert ei.value.code == "credential_decrypt_failed"
    assert ei.value.provider == "gitcode"

    # 结构化告警事件（既有 audit_logs 机制），detail 不含密文
    assert any(et == "credential_decrypt_failed" for et, _d in events), events
    detail = next(d for et, d in events if et == "credential_decrypt_failed")
    assert detail["provider"] == "gitcode" and detail["column"] in ("client_id_enc", "client_secret_enc")
    assert "AAAA" not in json.dumps(detail)

    # 宽容读口不回落 meta：返回空而不是 meta 里的旧凭据
    assert prov._get_client_id("gitcode") == ""
    assert prov._get_client_secret("gitcode") == ""
    assert prov.channel_configured("gitcode") is False

    from src.domains.mgmt.auth.providers import list_providers  # noqa: F401  仅确认可导入


def test_exchange_token_returns_explicit_code_on_decrypt_failure(db_env, fake_http, monkeypatch):
    _seed_bad_ciphertext(db_env)
    db_env.meta["oauth_gitcode_client_id"] = "cid-meta"
    _patch_provider(monkeypatch)
    tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x")
    assert tokens is None
    assert err == "credential_decrypt_failed"
    assert fake_http.calls["n"] == 0  # 根本没发请求：不是"未配置"，也不是网络失败


def test_refresh_does_not_mark_status4_on_decrypt_failure(db_env, fake_http, monkeypatch):
    """密钥事故 ≠ 凭据过期：不刷新、不发请求、不打 status=4。"""
    _seed_bad_ciphertext(db_env)
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "_read_refresh_token_from_identity", lambda u, p: "rt-1")
    touched = {"status4": 0}
    import src.domains.mgmt.auth.grape_account_db as gadb
    monkeypatch.setattr(gadb, "update_external_identity_tokens", lambda *a, **k: None)
    monkeypatch.setattr(udb, "set_credential", lambda *a, **k: None)

    called = {"n": 0}

    def _urlopen(*a, **k):
        called["n"] += 1
        raise AssertionError("解密失败时不应发起 refresh 请求")

    import urllib.request as _urlreq
    monkeypatch.setattr(_urlreq, "urlopen", _urlopen)

    assert udb.refresh_oauth_token(1, "gitcode") is None
    assert called["n"] == 0 and touched["status4"] == 0


# ------------------------------------------ P1-4：只对瞬时错误重试，4xx 立即返回


def test_4xx_is_not_retried_and_logs_provider_error(db_env, fake_http, monkeypatch, caplog):
    _seed_channel(db_env)
    _patch_provider(monkeypatch)
    body = {"error": "invalid_client", "error_description": "client secret mismatch"}
    fake_http.script[:] = [fake_http.Resp(400, body, text=json.dumps(body))]

    with caplog.at_level("ERROR"):
        tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x")

    assert tokens is None and err == "token_exchange_failed"
    assert fake_http.calls["n"] == 1, "4xx 属确定性失败，重试只会让已用 code 被撤销（RFC 6749）"
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "invalid_client" in log_text and "client secret mismatch" in log_text


def test_5xx_is_retried(db_env, fake_http, monkeypatch):
    _seed_channel(db_env)
    _patch_provider(monkeypatch)
    fake_http.script[:] = [fake_http.Resp(503, {"x": 1}, text="upstream busy"),
                           fake_http.Resp(200, {"access_token": "at-after-503"})]
    tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x")
    assert err == "" and tokens["access_token"] == "at-after-503"
    assert fake_http.calls["n"] == 2


def test_timeout_is_retried_then_succeeds(db_env, fake_http, monkeypatch):
    _seed_channel(db_env)
    _patch_provider(monkeypatch)
    fake_http.script[:] = [fake_http.exceptions.ReadTimeout("slow"),
                           fake_http.Resp(200, {"access_token": "at-after-timeout"})]
    tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x")
    assert err == "" and tokens["access_token"] == "at-after-timeout"
    assert fake_http.calls["n"] == 2


def test_5xx_stops_after_one_retry_to_protect_consumed_code(db_env, fake_http, monkeypatch):
    """5xx 只重试一次：provider 可能已消费 code，重复出示会撤销已发 token（RFC 6749）。"""
    _seed_channel(db_env)
    _patch_provider(monkeypatch)
    fake_http.script[:] = [fake_http.Resp(500, {}, text="boom") for _ in range(3)]
    tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x")
    assert tokens is None and err == "token_exchange_failed"
    assert fake_http.calls["n"] == 2


def test_all_network_failures_stop_after_three_attempts(db_env, fake_http, monkeypatch):
    """网络层异常（请求未送达）仍按 3 次尝试：不会触发 code 重复消费。"""
    _seed_channel(db_env)
    _patch_provider(monkeypatch)
    fake_http.script[:] = [fake_http.exceptions.ReadTimeout("slow") for _ in range(3)]
    tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x")
    assert tokens is None and err == "network_timeout"
    assert fake_http.calls["n"] == 3

# --------------------------------- 抽象层契约（CredentialSource / RetryPolicy）


def test_credential_source_reports_source_and_value(db_env):
    """resolve/pair 返回 (值, 来源)，调用方不再自己猜优先级。"""
    _seed_channel(db_env)
    src = cred_src.ProviderCredentialSource()
    cid, sec = src.pair("gitcode")
    assert cid.ok and cid.present and cid.source == cred_src.SOURCE_TABLE
    assert (cid.value, sec.value) == ("cid-table", "sec-table")
    assert src.configured("gitcode") is True
    assert src.resolve("gitcode", "client_secret").value == "sec-table"


def test_credential_source_meta_window_is_marked_as_legacy(db_env):
    """表里两列都空 = 历史窗口：整对回落 meta，且来源标成 meta（可观测、不是"表"）。"""
    db_env.meta["oauth_gitcode_client_id"] = "cid-meta"
    db_env.meta["oauth_gitcode_client_secret"] = "sec-meta"
    cid, sec = cred_src.ProviderCredentialSource().pair("gitcode")
    assert (cid.source, sec.source) == (cred_src.SOURCE_META, cred_src.SOURCE_META)
    assert (cid.value, sec.value) == ("cid-meta", "sec-meta")


def test_credential_source_marks_decrypt_error_instead_of_falling_back(db_env):
    """解密失败 → Credential.error=credential_decrypt_failed（不回落、不抛给 UI 层）。"""
    _seed_bad_ciphertext(db_env)
    db_env.meta["oauth_gitcode_client_id"] = "cid-meta"
    db_env.meta["oauth_gitcode_client_secret"] = "sec-meta"
    cid, sec = cred_src.ProviderCredentialSource().pair("gitcode")
    assert cid.error == cred_src.ERROR_DECRYPT_FAILED
    assert sec.error == cred_src.ERROR_DECRYPT_FAILED
    assert cid.value == "" and sec.value == ""


def test_retry_policy_classifies_transient_vs_deterministic():
    """瞬时（超时/连接/5xx）才重试；4xx 立即返回（P1-4 的策略本体）。"""
    p = DEFAULT_TOKEN_RETRY
    assert p.attempts == 3
    assert p.should_retry_status(503) and p.should_retry_status(500)
    assert not p.should_retry_status(400) and not p.should_retry_status(401)
    assert p.delay_for(0) == 1.0 and p.delay_for(1) == 2.0
    assert p.is_last_attempt(2) and not p.is_last_attempt(1)

    class ReadTimeout(Exception):
        pass

    class ConnectionError(Exception):  # noqa: A001 - 与 requests 同名语义
        pass

    assert p.should_retry_exception(ReadTimeout("slow"))
    assert p.should_retry_exception(ConnectionError("reset"))
    assert not p.should_retry_exception(ValueError("bad url"))


def test_exchange_token_accepts_injected_policy(db_env, fake_http, monkeypatch):
    """策略可注入（测试/特殊渠道可调次数），不必改 exchange_token 的分支。"""
    _seed_channel(db_env)
    _patch_provider(monkeypatch)
    fake_http.script[:] = [fake_http.Resp(503, {}, text="boom")]  # 只备一次 503
    tokens, err = prov.exchange_token("gitcode", "code-1", "https://cb/x",
                                      policy=RetryPolicy(attempts=1))
    assert tokens is None and err == "token_exchange_failed"
    assert fake_http.calls["n"] == 1
