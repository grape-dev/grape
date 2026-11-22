"""Issue #3737：VcsApiClient 日志脱敏回归（P1 凭据泄露）。

事故形态：query_token 鉴权（GitCode/Gitee bot）把 `access_token=` 拼进 URL，
`_download_authed` 与 `_do_request` 的失败日志直接输出完整 URL / 上游 body，
凭据随 WARNING 落入 Loki（实测 923 条/日）。

测试边界：只 mock 网络层 `urllib.request.urlopen`，断言「写出的日志行」这一用户可见产物，
不 mock 脱敏函数本身；同时锁定单一日志出口（源码 ratchet）与返回语义（#558）不变。
脱敏实现已收敛到共享模块 `src/domains/runtime/vcs/redact.py`（Issue #4064），
本用例按该模块的公开口径断言，并覆盖 `access_key/api_key/password` 与
`%26access_token%3D…` 编码形态（Issue #3737 复审 Required）。
"""
import logging
import pathlib
import urllib.error

import pytest

from src.domains.runtime.vcs import api_client as vcs_api
from src.domains.runtime.vcs.api_client import VcsApiClient, _download_authed
from src.domains.runtime.vcs.redact import redact_text, redact_url
from src.kernel import upstream_http

LOGGER = "devops-bot"
# 假凭据（非真实 token）：长度 32，便于断言任意 ≥8 位连续片段也不泄漏
TOKEN = "fakebot0token9f3a7c2e5b1d8046f"
TOKEN8 = TOKEN[:8]

_DL_PREFIX = "https://gitcode.com/user-attachments"
_DL_URL = f"{_DL_PREFIX}/a.png?access_token={TOKEN}&page=3"
_API_URL = (f"https://gitee.com/api/v5/repos/o/r/issues"
            f"?per_page=50&page=2&state=open&access_token={TOKEN}")


def _assert_no_token_fragment(text: str) -> None:
    """token 全文与任意 ≥8 位连续片段均不得出现。"""
    assert TOKEN not in text, f"日志泄漏 token 全文: {text!r}"
    for i in range(len(TOKEN) - 7):
        fragment = TOKEN[i:i + 8]
        assert fragment not in text, f"日志泄漏 token 片段 {fragment!r}: {text!r}"


def _logs(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    class _Err(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://x", code, "err", None, None)

        def read(self):
            return body.encode()

    return _Err()


def _raise(exc):
    def _fake_urlopen(req, timeout=30):
        raise exc

    return _fake_urlopen


def _raise_upstream(monkeypatch, exc):
    """打桩 `_do_request` 的真实上游入口（Issue #3580 起走 upstream_http.open_with_policy）。

    直接打 `urllib.request.urlopen` 已拦不住该路径：`_do_request` 构造的是真实
    `urllib.request.Request`，会被 opener 分支接管，测试因此会打到真网络。
    """
    def _fake_open(req, *, timeout=30.0, ca_bundle="", allow_redirects=None):
        raise exc

    monkeypatch.setattr(upstream_http, "open_with_policy", _fake_open)


def _dl(url: str = _DL_URL):
    """带 token 下载的唯一实现（参数与生产调用形态一致）。"""
    return _download_authed(url, TOKEN, allowed_prefix=_DL_PREFIX,
                            ctype_prefixes=("image/",), max_size=1024,
                            timeout=5, label="image")


# ── 纯函数：URL 脱敏 ────────────────────────────────────────────────────────

def test_redact_url_masks_sensitive_key_and_keeps_paging():
    out = redact_url(_API_URL)
    _assert_no_token_fragment(out)
    assert "access_token=***" in out
    # 非敏感 query 原样保留（排障需要）
    assert "per_page=50" in out and "page=2" in out and "state=open" in out
    assert out.startswith("https://gitee.com/api/v5/repos/o/r/issues?")


def test_redact_url_masks_other_sensitive_keys():
    url = (f"https://gitlab.example/api/v4/projects/1/issues"
           f"?private_token={TOKEN}&per_page=20&access_key={TOKEN}")
    out = redact_url(url)
    _assert_no_token_fragment(out)
    assert out.count("***") == 2
    assert "per_page=20" in out


def test_redact_url_is_noop_without_query():
    assert redact_url("https://x/api/v3/repos/o/r/issues") == "https://x/api/v3/repos/o/r/issues"
    assert redact_url("") == ""


def test_redact_url_keeps_fragment_and_encoded_path():
    url = f"https://x/a%2Fb?page=9&token={TOKEN}#frag"
    out = redact_url(url)
    _assert_no_token_fragment(out)
    assert "/a%2Fb" in out and "page=9" in out and out.endswith("#frag")


# ── 纯函数：文本兜底脱敏 ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    f"GET {_API_URL}",
    f'{{"token": "{TOKEN}", "message": "bad"}}',
    f'{{"access_token":"{TOKEN}"}}',
    f"Authorization: Bearer {TOKEN}",
    f"authorization=token {TOKEN}",
    f"private_token={TOKEN}&per_page=5",
    f"URLError: <urlopen error access_token%3D{TOKEN}>",
    # 嵌套 URL：编码 & 把参数名顶到 `%26` 之后，词边界（`\b`）不成立
    f"fetch failed: https://x/y?per_page=2%26access_token%3D{TOKEN}",
])
def test_redact_text_masks_token_forms(text):
    out = redact_text(text)
    _assert_no_token_fragment(out)


def test_redact_text_masks_encoded_form_after_encoded_separator():
    """复审 Required：`%26access_token%3D` 形态必须打码，且编码分隔符原样保留。"""
    out = redact_text(f"nested https://x/y?page=2%26access_token%3D{TOKEN}")
    _assert_no_token_fragment(out)
    assert "%26access_token%3D***" in out
    assert "page=2" in out


@pytest.mark.parametrize("key", ["access_key", "api_key", "password"])
def test_redact_url_masks_key_family_beyond_token(key):
    """Issue #3737 覆盖面：access_key / api_key / password 与 token 同风险，不得漏掩码。"""
    out = redact_url(f"https://h/x?{key}={TOKEN}&per_page=7")
    _assert_no_token_fragment(out)
    assert f"{key}=***" in out and "per_page=7" in out


def test_redact_text_keeps_non_sensitive_context():
    out = redact_text(f"GET {_API_URL} failed")
    assert "per_page=50" in out and "failed" in out


def test_redact_text_tolerates_empty():
    assert redact_text("") == ""
    assert redact_text(None) == ""


# ── _do_request：400/401 失败日志 ───────────────────────────────────────────

@pytest.mark.parametrize("code", [400, 401])
def test_do_request_http_error_log_redacts(monkeypatch, caplog, code):
    body = f'{{"message": "bad credentials", "access_token": "{TOKEN}"}}'
    _raise_upstream(monkeypatch, _http_error(code, body))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        resp = VcsApiClient._do_request(_API_URL, "GET")
    logs = _logs(caplog)
    _assert_no_token_fragment(logs)
    assert f"HTTP {code}" in logs
    assert "repos/o/r/issues" in logs and "per_page=50" in logs
    # #558 契约：返回结构不变，detail 仍是上游全文（脱敏只作用于日志）
    assert resp["status_code"] == code
    assert resp["error"] == f"HTTP {code}"
    assert resp["detail"] == body


def test_do_request_http_error_log_truncates_body(monkeypatch, caplog):
    body = "z" * 2000 + f" access_token={TOKEN}"
    _raise_upstream(monkeypatch, _http_error(400, body))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        VcsApiClient._do_request(_API_URL, "POST", {"a": 1})
    logs = _logs(caplog)
    _assert_no_token_fragment(logs)
    assert logs.count("z") <= 500


def test_do_request_urlerror_log_redacts_url_in_exception(monkeypatch, caplog):
    exc = urllib.error.URLError(f"connection refused for {_API_URL}")
    _raise_upstream(monkeypatch, exc)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        resp = VcsApiClient._do_request(_API_URL, "POST", {"a": 1})
    logs = _logs(caplog)
    _assert_no_token_fragment(logs)
    assert "per_page=50" in logs
    assert "error" in resp


# ── _download_authed：四条失败日志 ─────────────────────────────────────────

def test_download_authed_http_error_log_redacts(monkeypatch, caplog):
    monkeypatch.setattr(vcs_api.urllib.request, "urlopen",
                        _raise(_http_error(403, "forbidden")))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = _dl()
    logs = _logs(caplog)
    _assert_no_token_fragment(logs)
    assert "download failed" in logs and "HTTP 403" in logs and "page=3" in logs
    assert out["error"] == "HTTP 403"


def test_download_authed_timeout_log_redacts(monkeypatch, caplog):
    monkeypatch.setattr(vcs_api.urllib.request, "urlopen",
                        _raise(urllib.error.URLError("timed out")))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = _dl()
    logs = _logs(caplog)
    _assert_no_token_fragment(logs)
    assert "timeout (5s)" in logs and "page=3" in logs
    assert out["error"] == "timeout"


def test_download_authed_urlerror_log_redacts(monkeypatch, caplog):
    monkeypatch.setattr(vcs_api.urllib.request, "urlopen",
                        _raise(urllib.error.URLError(f"bad host {_DL_URL}")))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _dl()
    logs = _logs(caplog)
    _assert_no_token_fragment(logs)
    assert "download failed" in logs and "page=3" in logs


def test_download_authed_generic_exception_log_redacts(monkeypatch, caplog):
    monkeypatch.setattr(vcs_api.urllib.request, "urlopen",
                        _raise(RuntimeError(f"boom {_DL_URL}")))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _dl()
    logs = _logs(caplog)
    _assert_no_token_fragment(logs)
    assert "download failed" in logs and "page=3" in logs


# ── 源码 ratchet：日志路径单出口 ───────────────────────────────────────────

def test_no_unredacted_url_in_log_calls():
    """日志行出现 {url} 时必须经 redact_url（防新增日志点回归）。"""
    src = pathlib.Path(vcs_api.__file__).read_text(encoding="utf-8")
    offenders = [ln.strip() for ln in src.splitlines()
                 if "log." in ln and "{url}" in ln and "redact_url(url)" not in ln]
    assert offenders == [], f"存在未脱敏的 URL 日志: {offenders}"