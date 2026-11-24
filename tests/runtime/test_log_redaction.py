"""Issue #4064：VCS 客户端日志凭据脱敏门禁。

覆盖：
- helper 三载体（query / header / text）纯函数语义；
- `api_client.py` 6 条日志路径（下载分支 4 处 + 请求失败 2 处）；
- `feedback_issue_converter.py` 去重探测异常分支；
- 负例：故意不打码的日志必须被门禁断言拦下（证明断言有效，而非自证式空洞测试）。
"""
import io
import json
import re
import urllib.error
import urllib.request

import pytest

from src.kernel.config import log
from src.domains.runtime.vcs.redact import redact_headers, redact_text, redact_url
from src.domains.runtime.vcs.api_client import VcsApiClient, _download_authed

TOKEN = "tk_4064_prod_secret_do_not_log"
SIG = "SIG4064PRESIGNEDSECRET"
PLACEHOLDER = "<url-redacted>"

# 门禁①：任何敏感参数名后接非 *** 的值 → 违规（含 access_token=、X-Amz-Signature= 等）
_UNMASKED_KEY_RE = re.compile(
    r"(?i)(?:access_token|private_token|client_secret|secret|signature|token"
    r"|x-amz-signature|x-amz-credential|x-amz-security-token)"
    r"[\"']?\s*[=:]\s*[\"']?(?!\*\*\*)[^&\s\"'<>,;]+")
# 门禁②：Bearer/token 形态后接明文凭据 → 违规
_UNMASKED_BEARER_RE = re.compile(
    r"(?i)\b(?:bearer|token)\s+(?!\*\*\*)([A-Za-z0-9._~+/=\-]{6,})")


def assert_redacted(text, *secrets):
    """门禁断言：日志文本不得含原始凭据，且敏感参数值必须已掩码。"""
    for s in (secrets or (TOKEN, SIG)):
        assert s not in text, f"未脱敏凭据落日志: {text}"
    assert not _UNMASKED_KEY_RE.search(text), f"未脱敏敏感参数落日志: {text}"
    assert not _UNMASKED_BEARER_RE.search(text), f"未脱敏 Bearer/ token 落日志: {text}"


# ---------------------------------------------------------------- helper 单元

def test_redact_url_masks_value_keeps_name_and_structure():
    url = (f"https://gitcode.com/api/v5/repos/o/r/issues/1"
           f"?state=opened&access_token={TOKEN}&per_page=100")
    assert redact_url(url) == (
        "https://gitcode.com/api/v5/repos/o/r/issues/1"
        "?state=opened&access_token=***&per_page=100")


def test_redact_url_case_insensitive_and_presigned_keys():
    url = ("https://obs.example.com/bucket/a.png"
           f"?X-Amz-Signature={SIG}&X-Amz-Credential=CRED%2F20260101%2Fcn-north-4"
           "&X-Amz-Security-Token=SECTOK123&x-amz-date=20260101T000000Z")
    out = redact_url(url)
    assert "X-Amz-Signature=***" in out
    assert "X-Amz-Credential=***" in out
    assert "X-Amz-Security-Token=***" in out
    assert SIG not in out and "SECTOK123" not in out and "CRED" not in out
    # 非敏感参数逐字节保留（不重排、不改编码）
    assert "x-amz-date=20260101T000000Z" in out


@pytest.mark.parametrize("key", ["token", "secret", "private_token", "client_secret",
                                 "signature", "code", "ACCESS_TOKEN", "Private-Token"])
def test_redact_url_each_sensitive_key(key):
    assert redact_url(f"https://h/x?{key}=LEAKVALUE&a=1") == f"https://h/x?{key}=***&a=1"


def test_redact_url_non_sensitive_untouched():
    url = "https://h/x?state=opened&status_code=404&per_page=100#frag"
    assert redact_url(url) == url


@pytest.mark.parametrize("bad", [None, 123, b"https://h/?access_token=x", object(), ["u"]])
def test_redact_url_non_str_returns_placeholder_without_raise(bad):
    assert redact_url(bad) == PLACEHOLDER


def test_redact_url_unparsable_returns_placeholder_without_raise():
    assert redact_url("http://[::1/?access_token=leak") == PLACEHOLDER


def test_redact_headers_masks_credential_headers_case_insensitive():
    headers = {"Authorization": f"Bearer {TOKEN}", "PRIVATE-TOKEN": TOKEN,
               "x-gitee-token": TOKEN, "Accept": "application/json"}
    out = redact_headers(headers)
    assert out["Authorization"] == "***"
    assert out["PRIVATE-TOKEN"] == "***"
    assert out["x-gitee-token"] == "***"
    assert out["Accept"] == "application/json"
    # 纯函数：不修改入参
    assert headers["PRIVATE-TOKEN"] == TOKEN


def test_redact_headers_non_mapping_returns_empty():
    assert redact_headers(None) == {}
    assert redact_headers("Authorization: Bearer x") == {}


def test_redact_text_masks_key_value_bearer_and_prefixed_tokens():
    text = (f"GET https://api.example.com/x?access_token={TOKEN}&per_page=1 failed; "
            f"Authorization: Bearer {TOKEN}; header PRIVATE-TOKEN: {TOKEN}; "
            f'body {{"private_token": "{TOKEN}"}}; raw ghp_abcdef123456')
    out = redact_text(text)
    assert TOKEN not in out
    assert "access_token=***" in out
    assert "Bearer ***" in out
    assert "per_page=1" in out           # 非敏感内容保留，便于排障
    assert "ghp_***" in out


def test_redact_text_non_str_never_raises():
    assert redact_text(None) == ""
    assert "***" in redact_text(RuntimeError(f"boom {TOKEN}"))


# ------------------------------------------------------- api_client 下载分支 4 处

_DL_PREFIX = "https://gitcode.com/user-images/"
_DL_URL = (f"{_DL_PREFIX}a.png?X-Amz-Signature={SIG}"
           "&X-Amz-Credential=CRED%2F20260101%2Fcn-north-4")


def _download():
    return _download_authed(_DL_URL, TOKEN, allowed_prefix=_DL_PREFIX,
                            ctype_prefixes=("image/",), max_size=1024, timeout=3,
                            label="image")


@pytest.mark.parametrize("exc", [
    urllib.error.HTTPError(_DL_URL, 403, "Forbidden", {}, io.BytesIO(b"")),
    urllib.error.URLError("timed out"),
    urllib.error.URLError(OSError("connection refused")),
    RuntimeError(f"transport boom {TOKEN}"),
], ids=["http_error", "url_error_timeout", "url_error", "generic_exception"])
def test_download_failure_logs_redacted_url(caplog, monkeypatch, exc):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(exc))
    with caplog.at_level("WARNING"):
        res = _download()
    assert "error" in res
    assert "download failed" in caplog.text
    assert_redacted(caplog.text)


# --------------------------------------------------- api_client 请求失败 2 处

class _FakeResp:
    def __init__(self, payload=b"{}"):
        self._payload = payload
        self.headers = {}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gitcode_client(token=TOKEN):
    return VcsApiClient("gitcode", token, "own", "repo")


def test_http_error_log_redacted_and_error_contract_unchanged(caplog, monkeypatch):
    import src.kernel.upstream_http as uh
    body = ('{"message":"401 Unauthorized","access_token":"%s"}' % TOKEN).encode()

    def boom(req, **kw):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr(uh, "open_with_policy", boom)
    with caplog.at_level("WARNING"):
        res = _gitcode_client().get_issue(1)
    # 日志脱敏
    assert "VcsApiClient: GET" in caplog.text
    assert "?access_token=***" in caplog.text
    assert_redacted(caplog.text)
    # 契约不变：error/status_code/detail 保持上游语义（#558 口径，detail 原文）
    assert res["error"] == "HTTP 401"
    assert res["status_code"] == 401
    assert res["detail"] == body.decode()


def test_generic_exception_log_redacted(caplog, monkeypatch):
    import src.kernel.upstream_http as uh

    def boom(req, **kw):
        raise ConnectionError(f"connection reset while calling {req.full_url}")

    monkeypatch.setattr(uh, "open_with_policy", boom)
    with caplog.at_level("WARNING"):
        res = _gitcode_client().get_issue(1)
    assert "VcsApiClient: GET" in caplog.text
    assert_redacted(caplog.text)
    assert res["error"].startswith("connection reset")


def test_request_url_keeps_token_redaction_is_log_only(monkeypatch):
    """脱敏只作用于日志：实际请求 URL 仍带 token（不改鉴权形态，零功能回归）。"""
    import src.kernel.upstream_http as uh
    seen = {}

    def fake(req, **kw):
        seen["url"] = req.full_url
        return _FakeResp(json.dumps({"id": 1}).encode())

    monkeypatch.setattr(uh, "open_with_policy", fake)
    res = _gitcode_client().get_issue(1)
    assert "error" not in res
    assert seen["url"].endswith(f"?access_token={TOKEN}")


# ------------------------------------------- feedback 去重探测异常分支（约 185 行）

def test_title_probe_exception_log_redacted(caplog, monkeypatch):
    from src.api import feedback_issue_converter as conv
    from src.domains.runtime.vcs.providers import _MODULES

    mod = _MODULES["gitcode"]

    def boom(method, url, token, body=None):
        raise RuntimeError(f"probe exploded: {method} {url}")

    monkeypatch.setattr(mod, "request", boom)
    with caplog.at_level("WARNING"):
        found = conv._find_existing_open_issue("gitcode", "o", "r", TOKEN, "标题")
    assert found is None
    assert "title probe failed" in caplog.text
    assert_redacted(caplog.text)


# -------------------------------------------------------------------- 门禁负例

def test_gate_rejects_deliberately_unredacted_log(caplog):
    """负例：故意不打码的日志必须被门禁拦下，证明 assert_redacted 真的有效。"""
    with caplog.at_level("WARNING"):
        log.warning("VcsApiClient: GET https://gitcode.com/api/v5/repos/o/r/issues/1"
                    "?access_token=%s → HTTP 401", TOKEN)
    with pytest.raises(AssertionError):
        assert_redacted(caplog.text)
    # 对照：同一文本经 helper 处理后必须通过门禁
    assert_redacted(redact_text(caplog.text))


def test_gate_rejects_unredacted_bearer(caplog):
    with caplog.at_level("WARNING"):
        log.warning("upstream rejected Authorization: Bearer %s", TOKEN)
    with pytest.raises(AssertionError):
        assert_redacted(caplog.text)
    assert_redacted(redact_text(caplog.text))