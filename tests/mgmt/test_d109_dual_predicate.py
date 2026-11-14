"""D109 判据收敛对拍（UC-D109-27）：能力视图与旧判据必须一致。"""
from __future__ import annotations

from src.domains.platform import capabilities


def test_hosting_providers_matches_module_declaration():
    from src.domains.runtime.vcs.providers import _MODULES

    legacy = frozenset(p for p, m in _MODULES.items() if getattr(m, "HOSTING_FULL_FLOW", False))
    assert capabilities.hosting_providers() == legacy


def test_repos_predicate_delegates_to_capabilities():
    from src.domains.mgmt.routes.repos import _hosting_implemented_providers

    assert _hosting_implemented_providers() == capabilities.hosting_providers()


def test_known_providers_superset_of_legacy_trio():
    from src.domains.mgmt.user.repo_manager import _KNOWN_VCS_PROVIDERS

    known = _KNOWN_VCS_PROVIDERS()
    assert {"gitcode", "github", "gitlab"} <= set(known)
    assert known == capabilities.known_providers()


def test_platform_capabilities_fields_and_no_token_in_bot_summary():
    caps = capabilities.platform_capabilities("gitcode")
    for key in ("login", "login_supported", "configured", "hosting",
                "supports_bot_token", "supports_webhook_secret", "bot_username", "bot_accounts"):
        assert key in caps
    for item in caps["bot_accounts"]:
        assert set(item) == {"username", "is_default", "status", "last_used_at"}  # 不含 token
