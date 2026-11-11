"""Git provider 中性翻译入口（git-repo-provider-neutral-interface-design.md §4.2）。

对外统一：translate_inbound(provider, resource, op, neutral, ctx) / translate_query_state。
provider 由 token repo_key 前缀决定；每 provider 一个模块，新增 provider = 加模块 + 注册。
"""
import re

from . import gitcode, github
from .base import apply_rules

_MODULES = {"gitcode": gitcode, "github": github}


def translate_inbound(provider: str, resource: str, op: str, neutral: dict,
                      ctx: dict | None = None) -> dict:
    """中性字段 dict → provider 原生 payload（inbound 翻译）。"""
    mod = _MODULES.get(provider)
    if mod is None:
        raise ValueError(f"Git provider 未支持: {provider}")
    return mod.translate(resource, op, neutral or {}, ctx or {})


def translate_query_state(provider: str, state: str) -> str:
    """中性 state（open/closed/all）→ provider query 值。"""
    mod = _MODULES.get(provider)
    if mod is None:
        raise ValueError(f"Git provider 未支持: {provider}")
    return mod.query_state(state)