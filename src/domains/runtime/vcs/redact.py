"""Issue #4064：VCS 客户端日志统一脱敏入口（query / header / 文本三载体）。

日志是凭据的唯一泄露通道：请求 URL、请求头、上游 `err_body` 与异常串里出现的 token
与预签名参数，一律在「写日志的那一刻」过一次本模块；**请求本身与返回值逐字节不变**
（`error` / `status_code` / `detail` 契约按 #558 口径保持，`detail` 仍为上游原文）。

设计取舍（见 docs/issue-4064/plan.md）：
- 按参数名屏蔽，而非按已知 token 值匹配 —— 凭据轮换后自动生效；
- 掩码为整值 `***`，不留前后缀（长度/前缀本身即信息）；
- 纯函数、只依赖标准库；非 str / 解析失败返回占位，**绝不抛异常**
  （日志路径不能因脱敏失败而中断请求处理）。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

MASK = "***"

#: 非 str / 无法解析的 URL 占位（保留「有东西被吞掉」的信号，便于排障）
URL_PLACEHOLDER = "<url-redacted>"
#: 非 str / 替换失败时的文本占位
TEXT_PLACEHOLDER = "<text-redacted>"

#: 敏感 query 参数名（大小写不敏感）：token 家族 + 预签名家族（OBS/S3）
SENSITIVE_PARAMS = (
    "access_token",
    "token",
    "secret",
    "private_token",
    "client_secret",
    "signature",
    "code",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
)

# 长名优先，避免 `token` 抢先匹配 `access_token` 的前半段；
# `_` / `-` 两种分隔符互通（query 用 `private_token`，请求头用 `PRIVATE-TOKEN`）。
def _param_alt(name: str) -> str:
    return r"[-_]".join(re.escape(part) for part in re.split(r"[-_]", name))


_PARAM_ALT = "|".join(sorted((_param_alt(p) for p in SENSITIVE_PARAMS),
                             key=len, reverse=True))

# URL query 段：`?name=value` / `&name=value`，只吃值，其余字节（参数名/顺序/编码）原样保留。
# 不用 parse_qsl + urlencode 重排：那会重写百分号编码与空参数形态。
_URL_PARAM_RE = re.compile(
    r"(?i)(?P<lead>[?&])(?P<name>" + _PARAM_ALT + r")=(?P<value>[^&#\s]*)")

#: 需要整值掩码的请求头（小写比较）
CREDENTIAL_HEADERS = frozenset({"authorization", "private-token", "x-gitee-token"})

# 文本里的 key=value / key: value（容忍引号、JSON 形态、行内标点）。
# `\b` 保证 `status_code=` / `error_code=` 不被误掩（`_` 是 word 字符）。
_TEXT_KEY_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:" + _PARAM_ALT + r")\b[\"']?\s*[=:]\s*[\"']?)"
    r"(?P<value>[^\s&\"'<>,;)\]]+)")

# `Bearer <凭据>` / `token <凭据>` / `Basic <凭据>`（已掩码的 *** 不变，幂等）
_TEXT_BEARER_RE = re.compile(
    r"(?i)\b(Bearer|token|Basic)\s+(?!\*\*\*)([A-Za-z0-9._~+/=\-]{6,})")

# 裸 token 前缀（异常串里可能只有一个凭据、没有参数名）
_TEXT_PREFIX_RE = re.compile(r"\b(grape_|gcw_|rta_|tk_|ghp_|glpat_)[A-Za-z0-9_\-]{6,}")


def redact_url(url) -> str:
    """把 URL 中敏感 query 参数值替换为 `***`，保留参数名与其余结构。

    非 str 或无法解析（如非法 IPv6 字面量）返回 `URL_PLACEHOLDER`，不抛异常。
    """
    if not isinstance(url, str):
        return URL_PLACEHOLDER
    try:
        urlsplit(url)
    except Exception:
        return URL_PLACEHOLDER
    try:
        return _URL_PARAM_RE.sub(
            lambda m: f"{m.group('lead')}{m.group('name')}={MASK}", url)
    except Exception:
        return URL_PLACEHOLDER


def redact_headers(headers) -> dict:
    """返回掩码后的 headers 副本（不修改入参）；非 Mapping 返回 `{}`。

    掩码 `Authorization` / `PRIVATE-TOKEN` / `X-Gitee-Token`，键名大小写不敏感。
    """
    if not isinstance(headers, Mapping):
        return {}
    out = {}
    for key, value in headers.items():
        try:
            sensitive = str(key).strip().lower() in CREDENTIAL_HEADERS
        except Exception:
            sensitive = False
        out[key] = MASK if sensitive else value
    return out


def redact_text(text) -> str:
    """对任意串做 `key=value` / `Bearer|token <凭据>` / 裸 token 前缀打码。

    None → ""；非 str 先 `str()` 兜底；任何失败返回 `TEXT_PLACEHOLDER`，不抛异常。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return TEXT_PLACEHOLDER
    try:
        out = _TEXT_KEY_RE.sub(lambda m: m.group("prefix") + MASK, text)
        out = _TEXT_BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", out)
        out = _TEXT_PREFIX_RE.sub(lambda m: m.group(1) + MASK, out)
        return out
    except Exception:
        return TEXT_PLACEHOLDER