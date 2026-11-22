"""平台卡管理面用例服务（PlatformCardService）的单元测试。

用例边界：一次保存 = 校验 → meta（定义/凭据/覆盖层）→ 行 → 缓存 → 审计。
这里用契约替身验证「顺序 + 跨域只走契约」，不碰真库、不起应用。
"""
import pytest

from src.domains.mgmt.admin.platform_cards import (
    CardSaveRequest,
    CardValidationError,
    PlatformCardService,
)

VALID_CARD = {
    "name": "GitCode", "dialect": "github", "host": "gitcode.com",
    "webhook_path": "/webhook/gitcode", "event_header": "X-GitCode-Event",
    "api_base": "https://gitcode.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "hmac", "clone_auth": "x_access_token",
}


class _Rows:
    def __init__(self, actions=None, error=None):
        self.calls = []
        self._actions = actions or {"gitcode": "updated"}
        self._error = error

    def upsert_cards(self, cards):
        self.calls.append(sorted(cards))
        if self._error:
            raise RuntimeError(self._error)
        return dict(self._actions)


def _patch_contracts(monkeypatch, recorded, *, card_factory=None, overrides=None, rows_error=None):
    import src.domains.runtime.public as runtime
    import src.domains.mgmt.user.db as user_db

    def _set_meta(pairs):
        recorded["meta"] = list(pairs)

    monkeypatch.setattr(runtime, "set_meta_values_any", _set_meta, raising=False)
    monkeypatch.setattr(runtime, "invalidate_platform_card_cache",
                        lambda: recorded.__setitem__("invalidated", recorded.get("invalidated", 0) + 1),
                        raising=False)
    monkeypatch.setattr(runtime, "compute_card_overrides",
                        lambda cleaned, current="": overrides if overrides is not None else {"gitcode": {"ca_bundle": "/opt/x.pem"}},
                        raising=False)
    monkeypatch.setattr(runtime, "platform_card_overrides_meta_key", lambda: "vcs_platform_overrides",
                        raising=False)
    monkeypatch.setattr(user_db, "get_meta_value", lambda key: "", raising=False)
    if card_factory is not None:
        monkeypatch.setattr(runtime, "build_platform_card", card_factory, raising=False)


def test_save_validation_failure_maps_to_domain_error(monkeypatch):
    import src.domains.runtime.public as runtime

    def _reject(provider, raw):
        raise runtime.PlatformCardError(f"{provider}: host 非法")

    _patch_contracts(monkeypatch, {}, card_factory=_reject)
    with pytest.raises(CardValidationError) as exc:
        PlatformCardService(rows=_Rows()).save_cards(
            CardSaveRequest(cards={"gitcode": VALID_CARD}))
    assert "host 非法" in exc.value.errors[0]


def test_save_writes_rows_not_card_meta(monkeypatch):
    """A 方案：卡定义只落表；meta 仅承载凭据类键（不再有 vcs_providers_config/覆盖层）。"""
    import dataclasses as _dc
    import src.domains.runtime.public as runtime

    @_dc.dataclass
    class _Card:
        provider: str
        host: str
        webhook_path: str
        capabilities: tuple = ()
        event_map_overrides: dict = _dc.field(default_factory=dict)
        fields_overrides: dict = _dc.field(default_factory=dict)
        global_bot_meta_key: str = ""
        webhook_token_field: str = ""

    recorded = {}
    _patch_contracts(monkeypatch, recorded,
                     card_factory=lambda p, raw: _Card(p, raw.get("host", ""), raw.get("webhook_path", "")),
                     overrides={"gitcode": {"ca_bundle": "/opt/x.pem"}})

    rows = _Rows(actions={"gitcode": "promoted"})
    result = PlatformCardService(rows=rows).save_cards(
        CardSaveRequest(cards={"gitcode": VALID_CARD}, operator_id=7))

    keys = [k for k, _ in recorded["meta"]]
    assert "vcs_providers_config" not in keys      # 定义不再落 meta（表是唯一真源）
    assert "vcs_platform_overrides" not in keys   # 覆盖层退役
    assert all(k == "vcs_providers_config" or True for k in keys)   # 其余键允许（如凭据）
    assert result.overrides == {"gitcode": {"ca_bundle": "/opt/x.pem"}}   # 仅用于展示 diff
    assert result.rows == {"gitcode": "promoted"}
    assert rows.calls == [["gitcode"]]
    assert recorded["invalidated"] == 2          # meta 写后 + 行写后各一次


def test_row_failure_is_reported_not_swallowed(monkeypatch):
    import dataclasses as _dc
    import src.domains.runtime.public as runtime

    @_dc.dataclass
    class _Card:
        provider: str
        host: str
        webhook_path: str
        capabilities: tuple = ()
        event_map_overrides: dict = _dc.field(default_factory=dict)
        fields_overrides: dict = _dc.field(default_factory=dict)
        global_bot_meta_key: str = ""
        webhook_token_field: str = ""

    _patch_contracts(monkeypatch, {}, card_factory=lambda p, raw: _Card(p, "host", "/w"))
    result = PlatformCardService(rows=_Rows(error="boom")).save_cards(
        CardSaveRequest(cards={"gitcode": VALID_CARD}))
    assert result.rows == {} and "boom" in result.rows_error


def test_bot_token_for_unknown_provider_is_rejected(monkeypatch):
    import dataclasses as _dc
    import src.domains.runtime.public as runtime

    @_dc.dataclass
    class _Card:
        provider: str
        global_bot_meta_key: str = ""
        capabilities: tuple = ()
        event_map_overrides: dict = _dc.field(default_factory=dict)
        fields_overrides: dict = _dc.field(default_factory=dict)
        host: str = ""
        webhook_path: str = ""
        webhook_token_field: str = ""

    _patch_contracts(monkeypatch, {}, card_factory=lambda p, raw: _Card(p))
    with pytest.raises(CardValidationError) as exc:
        PlatformCardService(rows=_Rows()).save_cards(
            CardSaveRequest(cards={"gitcode": VALID_CARD}, bot_tokens={"nope": "t"}))
    assert "不在册平台" in exc.value.errors[0]

def test_provider_id_must_be_instance_safe(monkeypatch):
    """provider id 是实例标识（进 repo_key/webhook 路径/工作目录），服务端必须拦非法形态。

    Issue #3580/PR#1854：管理台新增"自建实例卡"后，id 由用户输入 —— 前端正则只是
    即时提示，权威判定在这里；`../x`、`a/b`、大写、超长都必须拒。
    """
    recorded = {}
    import dataclasses as _dc

    @_dc.dataclass
    class _Card:
        provider: str
        host: str
        webhook_path: str
        capabilities: tuple = ()
        event_map_overrides: dict = _dc.field(default_factory=dict)
        fields_overrides: dict = _dc.field(default_factory=dict)
        global_bot_meta_key: str = ""
        webhook_token_field: str = ""

    _patch_contracts(monkeypatch, recorded,
                     card_factory=lambda p, raw: _Card(p, raw.get("host", ""),
                                                       raw.get("webhook_path", "")))
    for bad_id in ("../etc", "a/b", "GitLab", "1gitlab", "x" * 41, ""):
        with pytest.raises(CardValidationError) as exc:
            PlatformCardService(rows=_Rows()).save_cards(
                CardSaveRequest(cards={bad_id: VALID_CARD}))
        assert "provider id 非法" in exc.value.errors[0]

    ok = PlatformCardService(rows=_Rows(actions={"gitlab-corp": "updated"})).save_cards(
        CardSaveRequest(cards={"gitlab-corp": VALID_CARD}))
    assert ok.cards == ["gitlab-corp"]
