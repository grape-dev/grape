"""Issue #3632：平台卡来源/契约的对象模型测试。

分层验证（不依赖 DB、不依赖 HTTP 服务）：
1. `PlatformCardContractClient` —— worker 侧契约客户端（地址/令牌/超时/错误归类）；
2. `PlatformCardContractServer` —— 主控侧契约服务端（跨域走 mgmt 契约、失败降级）；
3. `PlatformCardAssembler` —— 优先级链（meta 逐键覆盖 > 文件 > 代码默认 > 表/契约）；
4. `platform_card._build_cards` —— 门面装配：表读不到时由契约补卡（gitee 场景）；
5. `repogit._host_for_provider` —— 未注册平台抛 `platform_not_registered`。
"""
from __future__ import annotations

import json

import pytest

from src.domains.runtime.vcs import card_contract, card_sources
from src.domains.runtime.vcs import platform_card as pc


GITEE_RAW = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "webhook_dialect": "gitee_v5",
    "clone_auth": "basic_password", "issue_write_form": "owner_param",
    "branch_create_form": "form", "brand_color": "#C71D23",
}


class _FakeResp:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------- 客户端 ----

def test_contract_client_fetches_cards():
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, timeout=timeout, auth=req.get_header("Authorization"))
        return _FakeResp(json.dumps({"cards": {"gitee": dict(GITEE_RAW)}}))

    # 回环 + 常见端口；opener 是假实现，测试不发起任何真实请求
    client = card_contract.PlatformCardContractClient(
        base_url="http://127.0.0.1:8080/", token="rta_fake_test_token",
        timeout=1.5, opener=opener)

    assert client.fetch_cards() == {"gitee": GITEE_RAW}
    assert seen["url"] == "http://127.0.0.1:8080/api/runtime/platform-cards"
    assert seen["auth"] == "Bearer rta_fake_test_token"
    assert seen["timeout"] == 1.5


def test_card_timeout_has_a_named_source(monkeypatch):
    """架构守护 §timeout：超时必须来自具名来源（常量/环境变量），不得是裸字面量。"""
    monkeypatch.delenv(card_contract._TIMEOUT_ENV, raising=False)
    assert card_contract._DEFAULT_CARD_TIMEOUT == 2.0
    assert card_contract.PlatformCardContractClient().timeout == 2.0

    monkeypatch.setenv(card_contract._TIMEOUT_ENV, "0.5")
    assert card_contract.PlatformCardContractClient().timeout == 0.5, "环境变量必须生效"

    # 显式构造参数优先于环境变量
    assert card_contract.PlatformCardContractClient(timeout=3.0).timeout == 3.0


def test_card_timeout_env_reaches_the_opener(monkeypatch):
    """设置环境变量后，真正传给 opener 的 timeout 也随之变化。"""
    monkeypatch.setenv(card_contract._TIMEOUT_ENV, "0.25")
    seen = {}

    def opener(req, timeout=None):
        seen["timeout"] = timeout
        return _FakeResp(json.dumps({"cards": {}}))

    client = card_contract.PlatformCardContractClient(
        base_url="http://127.0.0.1:8080", token="rta_fake_test_token", opener=opener)
    client.fetch_cards()
    assert seen["timeout"] == 0.25


@pytest.mark.parametrize("raw", ["abc", "-1", "0", "999"])
def test_card_timeout_invalid_env_falls_back_with_warning(monkeypatch, caplog, raw):
    """坏配置必须回落默认并告警，不能被当成"网络不可达"吞掉。"""
    monkeypatch.setenv(card_contract._TIMEOUT_ENV, raw)
    with caplog.at_level("WARNING"):
        assert card_contract.PlatformCardContractClient().timeout == card_contract._DEFAULT_CARD_TIMEOUT
    assert any(card_contract._TIMEOUT_ENV in r.getMessage() for r in caplog.records)


def test_contract_client_without_env_is_not_configured(monkeypatch):
    monkeypatch.delenv("RUNTIME_SERVER_URL", raising=False)
    monkeypatch.delenv("RUNTIME_TOKEN", raising=False)
    client = card_contract.PlatformCardContractClient()
    assert client.configured() is False
    with pytest.raises(card_contract.PlatformCardContractError):
        client.fetch_cards()


@pytest.mark.parametrize("body,status", [
    ("not-json", 200),         # 非法 JSON
    ('{"no_cards": 1}', 200),  # 缺 cards
    ('{"cards": {}}', 502),    # 非 2xx
])
def test_contract_client_classifies_failures(body, status):
    client = card_contract.PlatformCardContractClient(
        base_url="http://x", token="rta_fake_test_token",
        opener=lambda req, timeout=None: _FakeResp(body, status))
    with pytest.raises(card_contract.PlatformCardContractError):
        client.fetch_cards()


def test_contract_client_network_error_is_wrapped():
    def boom(req, timeout=None):
        raise OSError("connection refused")

    client = card_contract.PlatformCardContractClient(
        base_url="http://x", token="rta_fake_test_token", opener=boom)
    with pytest.raises(card_contract.PlatformCardContractError) as exc:
        client.fetch_cards()
    assert "connection refused" in str(exc.value)


# ---------------------------------------------------------------- 服务端 ----

class _FakeMgmtService:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def get_repo_platform_cards(self, enabled_only: bool = True):
        self.calls.append(enabled_only)
        if self.error:
            raise self.error
        return self.rows


def test_contract_server_shapes_rows_into_cards():
    svc = _FakeMgmtService([
        {"provider": "gitcode", "name": "GitCode", "host": "gitcode.com"},
        {"provider": "gitee", "name": "Gitee", "host": "gitee.com"},
        {"provider": "", "name": "junk"},
    ])
    payload = card_contract.PlatformCardContractServer(mgmt_service=svc).snapshot()

    assert set(payload["cards"]) == {"gitcode", "gitee"}
    assert svc.calls == [True], "必须经 mgmt 契约取数，enabled_only 语义透传"
    flat = json.dumps(payload)
    for leaked in ("bot_token_enc", "client_id", "client_secret"):
        assert leaked not in flat, f"响应不得含凭据列 {leaked}"


def test_contract_server_degrades_without_5xx(caplog):
    svc = _FakeMgmtService(error=RuntimeError("1146 table missing"))
    with caplog.at_level("WARNING"):
        payload = card_contract.PlatformCardContractServer(mgmt_service=svc).snapshot()

    assert payload == {"cards": {}, "error": "platform_cards_unavailable"}
    assert any("读 mgmt 平台卡失败" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- 来源/装配 --

class _StaticSource(card_sources.PlatformCardSource):
    def __init__(self, name, raws):
        self.name, self._raws = name, raws

    def raws(self):
        return dict(self._raws)


def _assembler(meta=None, file_source=None, builtin=None, fallbacks=()):
    return card_sources.PlatformCardAssembler(
        meta=card_sources.MetaOverlaySource(lambda: json.dumps(meta or {})),
        file_source=file_source or card_sources.FileDirCardSource(
            "/nonexistent", lambda p: {}),
        builtin=card_sources.BuiltinCardSource(builtin or {}),
        fallback_sources=fallbacks,
    )


def test_assembler_meta_overlays_per_key_over_file_card():
    file_source = card_sources.FileDirCardSource(
        "/cfg", lambda p: {"gitee": dict(GITEE_RAW)}, isdir=lambda p: True)
    asm = _assembler(meta={"gitee": {"host": "mirror.example.com", "name": "GiteeMirror"}},
                     file_source=file_source)

    raws = asm.assemble_raws()
    assert raws["gitee"]["host"] == "mirror.example.com"
    assert raws["gitee"]["clone_auth"] == "basic_password", "逐键覆盖：缺席键落文件卡"


def test_assembler_falls_back_to_builtin_when_dir_missing(caplog):
    asm = _assembler(builtin={"gitcode": {"host": "gitcode.com"}})
    with caplog.at_level("WARNING"):
        raws = asm.assemble_raws()
    assert set(raws) == {"gitcode"}
    assert any("配置目录缺失" in r.getMessage() for r in caplog.records)


def test_assembler_contract_fills_provider_absent_everywhere_else():
    """#3632 的核心场景：文件卡缺失 + 代码默认没有 gitee → 契约补上。"""
    asm = _assembler(builtin={"gitcode": {"host": "gitcode.com"}},
                     fallbacks=[_StaticSource("server contract", {"gitee": dict(GITEE_RAW)})])
    raws = asm.assemble_raws()
    assert set(raws) == {"gitcode", "gitee"}


def test_assembler_prefers_table_over_contract():
    table = _StaticSource("table", {"gitee": dict(GITEE_RAW, host="tbl.gitee.io")})
    contract = _StaticSource("server contract", {"gitee": dict(GITEE_RAW, host="srv.gitee.io")})
    asm = _assembler(builtin={}, fallbacks=[table, contract])
    assert asm.assemble_raws()["gitee"]["host"] == "tbl.gitee.io"


def test_assembler_rejects_invalid_card_and_keeps_others():
    asm = _assembler(builtin={"gitcode": {"host": "gitcode.com"},
                              "bad": {"dialect": "svn", "host": ""}})
    cards = asm.build(builder=pc.build_platform_card, error_type=pc.PlatformCardError)
    assert "gitcode" in cards and "bad" not in cards


# ---------------------------------------------------------------- 门面 ----

def test_build_cards_uses_contract_when_table_unreadable(monkeypatch, tmp_path):
    """worker 引擎进程：表读不到、无文件卡、代码默认无 gitee → 契约兜底。"""
    monkeypatch.setattr(card_sources.OwnerTableCardSource, "raws", lambda self: {})
    monkeypatch.setattr(card_sources.ServerContractCardSource, "raws",
                        lambda self: {"gitee": dict(GITEE_RAW)})
    monkeypatch.setattr(pc, "_CONFIGS_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr("src.domains.mgmt.public.get_meta_value", lambda key: "")

    cards = pc._build_cards()

    assert cards["gitee"].host == "gitee.com"
    assert {"gitcode", "github"} <= set(cards), "代码默认卡仍在（优先级链不变）"


def test_build_cards_table_source_wins_over_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(card_sources.OwnerTableCardSource, "raws",
                        lambda self: {"gitee": dict(GITEE_RAW, host="tbl.gitee.io")})
    monkeypatch.setattr(card_sources.ServerContractCardSource, "raws",
                        lambda self: pytest.fail("表可读时不该再打契约"))
    monkeypatch.setattr(pc, "_CONFIGS_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr("src.domains.mgmt.public.get_meta_value", lambda key: "")

    assert pc._build_cards()["gitee"].host == "tbl.gitee.io"


def test_owner_table_source_omits_empty_columns_but_keeps_falsy():
    """Issue #3580（空列投毒）：NULL 列答成的 "" 不得写进卡 raw，0/False 要保留。

    跨域读原语把 NULL 答成 ""；照抄进 raw 会让 Builder 把可选枚举列
    （branch_create_form / issue_write_form …）当成"显式非法值"整卡拒绝，
    于是 `_build_cards` 回退文件卡 —— 表行加了也不生效。
    """
    source = card_sources.OwnerTableCardSource(
        read_rows=lambda enabled_only=True: [{
            "provider": "gitee", "host": "gitee.com",
            "branch_create_form": "",       # 空串 → 必须缺席
            "issue_write_form": None,       # NULL → 必须缺席
            "issue_search": 0,              # 有效假值 → 必须保留
            "create_repo_options": False,   # 有效假值 → 必须保留
            "id": 7,                        # 非卡字段 → 被 skip
        }],
        ready=lambda: True,
        skip_columns=("id", "enabled"),
    )

    raw = source.raws()["gitee"]
    assert "branch_create_form" not in raw
    assert "issue_write_form" not in raw
    assert "id" not in raw
    assert raw["issue_search"] == 0
    assert raw["create_repo_options"] is False
    # 关键：空列缺席后整卡能构建（否则会"卡非法 → 回退文件卡"）
    assert pc.build_platform_card("gitee", raw).host == "gitee.com"


def test_owner_table_source_is_quiet_when_ready_but_loud_on_failure(caplog):
    source = card_sources.OwnerTableCardSource(
        read_rows=lambda enabled_only=True: [],
        ready=lambda: True,
        has_owner_dsn=lambda: True,
        skip_columns=("id", "enabled"),
    )
    assert source.raws() == {}

    failing = card_sources.OwnerTableCardSource(
        read_rows=lambda enabled_only=True: (_ for _ in ()).throw(RuntimeError("1146")),
        ready=lambda: True,
    )
    with caplog.at_level("WARNING"):
        assert failing.raws() == {}
    assert any("repo_platforms table read failed" in r.getMessage() for r in caplog.records)


def test_server_source_is_silent_for_non_worker(monkeypatch, caplog):
    monkeypatch.delenv("RUNTIME_SERVER_URL", raising=False)
    monkeypatch.delenv("RUNTIME_TOKEN", raising=False)
    with caplog.at_level("DEBUG"):
        assert card_sources.ServerContractCardSource().raws() == {}
    assert not [r for r in caplog.records if r.levelname != "DEBUG"], \
        "非 worker 进程属正常路径，不该刷 WARNING"


# ---------------------------------------------------------------- 错误语义 ----

def test_unknown_provider_raises_platform_not_registered(monkeypatch):
    from src.domains.runtime.engine import repogit

    monkeypatch.setattr(pc, "get_platform_card", lambda provider: None)

    with pytest.raises(pc.PlatformCardMissingError) as exc:
        repogit._host_for_provider("gitee")

    assert exc.value.code == "platform_not_registered"
    assert exc.value.provider == "gitee"
    assert isinstance(exc.value, ValueError), "保持 ValueError 兼容既有 except"
    assert "no platform card" in str(exc.value)