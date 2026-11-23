"""凭据注入与掩码（统一出口）。

明文约束：凭据明文仅在执行瞬间进入注入位（env/query/header/argv），
任何被写往日志/审计/结果回传的文本先经 redact() 掩码（含 URL 编码形态）。
"""
import urllib.parse

MASK = "***MASKED***"


def _secret_variants(secret: str) -> list[str]:
    """同一明文的等价形态：原文 + URL 编码（quote 默认/全量 + quote_plus），用于错误回显掩码。"""
    variants = [secret]
    encoders = (
        lambda: urllib.parse.quote(secret, safe=""),
        lambda: urllib.parse.quote(secret, safe="/"),
        lambda: urllib.parse.quote_plus(secret),
    )
    for encode in encoders:
        try:
            quoted = encode()
        except Exception:
            continue
        if quoted != secret and quoted not in variants:
            variants.append(quoted)
    return variants


def redact(text, secrets) -> str:
    """把 text 中出现的任一凭据明文（或其 URL 编码形态）替换为 MASK。text 为 None 时原样返回。"""
    if text is None:
        return None
    text = str(text)
    for s in secrets or ():
        if not s:
            continue
        for variant in _secret_variants(str(s)):
            if variant in text:
                text = text.replace(variant, MASK)
    return text


def format_template(template: str, secret: str) -> str:
    """模板渲染：{credential} 占位符替换为明文（如 \"Bearer {credential}\"）。"""
    return template.replace("{credential}", secret)


def build_env(auth: dict, secret: str) -> dict:
    """env 注入位：返回需并入子进程环境的键值。"""
    return {env_name(auth): secret}


def env_name(auth: dict) -> str:
    return auth.get("env") or auth["credential"]


def build_query(auth: dict, secret: str) -> dict:
    """query 注入位：返回需并入请求 query 的键值。"""
    return {env_name(auth): secret}


def build_headers(auth: dict, secret: str) -> dict:
    """header 注入位：auth.header 为 header 名，template 支持 Bearer 前缀。"""
    name = auth.get("header") or "Authorization"
    template = auth.get("template")
    value = format_template(template, secret) if template else secret
    return {name: value}


def build_argv(auth: dict, secret: str) -> list:
    """argv 注入位：template 含 {credential}（如 \"--token={credential}\"），缺省为裸值。"""
    template = auth.get("template")
    return [format_template(template, secret) if template else secret]
