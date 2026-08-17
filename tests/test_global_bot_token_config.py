"""global-bot-token 配置结构校验（纯逻辑，不读真实部署配置文件）。

规则定义（配置结构契约）：
  - 必填键：gitcode_api_token / gitcode_webhook_token / gitcode_oauth_client_id /
            gitcode_oauth_client_secret / gitcode_oauth_redirect_uri / bot_usernames
  - gitcode_api_token / oauth_client_id / oauth_client_secret 必须为非空字符串
  - gitcode_webhook_token 必须为字符串（可空——未配置时按空串处理，不做密钥断言）
  - gitcode_oauth_redirect_uri 必须是 http(s) 且 path 以 /api/auth/callback/gitcode 结尾
  - bot_usernames 必须是非空列表

真实部署文件 configs/global-bot-token.json 的密钥校验属部署预检职责，
不在此处断言（测试不依赖/不读取含密钥的真实配置文件）。
"""
import urllib.parse

REQUIRED_KEYS = [
    "gitcode_api_token",
    "gitcode_webhook_token",
    "gitcode_oauth_client_id",
    "gitcode_oauth_client_secret",
    "gitcode_oauth_redirect_uri",
    "bot_usernames",
]


def _problems(cfg: dict) -> list[str]:
    """返回配置中违反契约的所有问题列表（空 = 合法）。"""
    probs = []
    for key in REQUIRED_KEYS:
        if key not in cfg:
            probs.append(f"missing key: {key}")
    if not probs:
        for key in ["gitcode_api_token", "gitcode_oauth_client_id", "gitcode_oauth_client_secret"]:
            if not isinstance(cfg[key], str) or not cfg[key].strip():
                probs.append(f"{key} must be a non-empty string")
        if not isinstance(cfg["gitcode_webhook_token"], str):
            probs.append("gitcode_webhook_token must be a string")
        parsed = urllib.parse.urlparse(cfg["gitcode_oauth_redirect_uri"])
        if parsed.scheme not in ("http", "https"):
            probs.append("redirect_uri scheme must be http/https")
        if not parsed.netloc:
            probs.append("redirect_uri must have a host")
        if not parsed.path.endswith("/api/auth/callback/gitcode"):
            probs.append(f"redirect_uri path must end with /api/auth/callback/gitcode, got: {parsed.path}")
        if not isinstance(cfg["bot_usernames"], list) or not cfg["bot_usernames"]:
            probs.append("bot_usernames must be a non-empty list")
    return probs


def _valid_cfg() -> dict:
    return {
        "gitcode_api_token": "tok",
        "gitcode_webhook_token": "webhook",
        "gitcode_oauth_client_id": "cli_x",
        "gitcode_oauth_client_secret": "secret",
        "gitcode_oauth_redirect_uri": "https://grape.example.com/api/auth/callback/gitcode",
        "bot_usernames": ["grape_bot"],
    }


class TestGlobalBotTokenConfig:
    def test_valid_config_has_no_problems(self):
        assert _problems(_valid_cfg()) == []

    def test_missing_required_key_reported(self):
        cfg = _valid_cfg()
        cfg.pop("gitcode_webhook_token")
        assert "missing key: gitcode_webhook_token" in _problems(cfg)

    def test_api_token_non_empty(self):
        cfg = _valid_cfg()
        cfg["gitcode_api_token"] = ""
        assert any("gitcode_api_token" in p for p in _problems(cfg))

    def test_redirect_uri_scheme_must_be_http_https(self):
        cfg = _valid_cfg()
        cfg["gitcode_oauth_redirect_uri"] = "ftp://grape.example.com/api/auth/callback/gitcode"
        probs = _problems(cfg)
        assert any("scheme" in p for p in probs)

    def test_redirect_uri_path_contract(self):
        cfg = _valid_cfg()
        cfg["gitcode_oauth_redirect_uri"] = "https://grape.example.com/other/callback"
        probs = _problems(cfg)
        assert any("path must end with /api/auth/callback/gitcode" in p for p in probs)

    def test_webhook_token_type(self):
        cfg = _valid_cfg()
        cfg["gitcode_webhook_token"] = 123
        assert "gitcode_webhook_token must be a string" in _problems(cfg)

    def test_bot_usernames_non_empty_list(self):
        cfg = _valid_cfg()
        cfg["bot_usernames"] = []
        assert "bot_usernames must be a non-empty list" in _problems(cfg)
