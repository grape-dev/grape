"""Tests for configs/global-bot-token.json 结构校验（不校验具体密钥值，只校验结构与格式）。"""
import os
import json
import urllib.parse

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "configs", "global-bot-token.json")

REQUIRED_KEYS = [
    "gitcode_api_token",
    "gitcode_webhook_token",
    "gitcode_oauth_client_id",
    "gitcode_oauth_client_secret",
    "gitcode_oauth_redirect_uri",
    "bot_usernames",
]


def _load_config():
    assert os.path.exists(CONFIG_PATH), f"config file missing: {CONFIG_PATH}"
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class TestGlobalBotTokenConfig:
    def test_json_parses_and_has_required_keys(self):
        cfg = _load_config()
        for key in REQUIRED_KEYS:
            assert key in cfg, f"missing key: {key}"

    def test_api_token_and_oauth_credentials_non_empty(self):
        cfg = _load_config()
        for key in ["gitcode_api_token", "gitcode_oauth_client_id", "gitcode_oauth_client_secret"]:
            assert isinstance(cfg[key], str) and cfg[key].strip(), f"{key} must be a non-empty string"

    def test_redirect_uri_is_valid_http_url(self):
        cfg = _load_config()
        parsed = urllib.parse.urlparse(cfg["gitcode_oauth_redirect_uri"])
        assert parsed.scheme in ("http", "https"), "redirect_uri scheme must be http/https"
        assert parsed.netloc, "redirect_uri must have a host"
        assert parsed.path.endswith("/api/auth/callback/gitcode"), \
            f"redirect_uri path must end with /api/auth/callback/gitcode, got: {parsed.path}"

    def test_bot_usernames_is_non_empty_list(self):
        cfg = _load_config()
        assert isinstance(cfg["bot_usernames"], list) and cfg["bot_usernames"], \
            "bot_usernames must be a non-empty list"

    def test_webhook_token_is_string(self):
        cfg = _load_config()
        assert isinstance(cfg["gitcode_webhook_token"], str), "gitcode_webhook_token must be a string"
