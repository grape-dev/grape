"""仓库网页地址派生（平台卡驱动的单一入口）。

背景：组织下派/仓库地图此前各自维护 provider→host 字典并回落 gitcode.com。
自建 GitLab/GitHub、gitee 或新平台会拼出错误域名；GitLab 的 issue/文件路径还带
`/-/` 段。统一改为读取平台卡的 `host` 与 `dialect`，无卡时返回空串而不是
静默落到 gitcode（错误链接比无链接更难排查）。
"""
from __future__ import annotations

from urllib.parse import quote

from src.kernel.issue_ident import native_issue_number
from src.kernel.repo_key import parse_repo_key


def _platform_card(provider: str):
    try:
        from src.domains.runtime.public import get_platform_card

        return get_platform_card(str(provider or "").lower())
    except Exception:  # noqa: BLE001 - 链接派生失败不阻断业务
        return None


def _host(provider: str) -> str:
    card = _platform_card(provider)
    return str(getattr(card, "host", "") or "").strip().strip("/")


def _is_gitlab(provider: str) -> bool:
    card = _platform_card(provider)
    return str(getattr(card, "dialect", "") or "").strip().lower() == "gitlab"


def issue_url(repo_key: str, issue_number) -> str:
    """Issue 浏览器地址；无平台卡/参数不完整返回空串。"""
    provider, owner, name = parse_repo_key(str(repo_key or ""))
    host = _host(provider)
    ident = native_issue_number(issue_number)
    if not (host and owner and name) or str(ident) == "":
        return ""
    segment = "-/issues" if _is_gitlab(provider) else "issues"
    return f"https://{host}/{owner}/{name}/{segment}/{ident}"


def file_url(repo_key: str, path: str, ref: str = "") -> str:
    """仓库文件浏览器地址；无平台卡/参数不完整返回空串。"""
    provider, owner, name = parse_repo_key(str(repo_key or ""))
    host = _host(provider)
    rel = str(path or "").strip().lstrip("/")
    if not (host and owner and name and rel):
        return ""
    branch = quote(str(ref or "").strip() or "main", safe="")
    rel_q = quote(rel, safe="/")
    segment = "-/blob" if _is_gitlab(provider) else "blob"
    return f"https://{host}/{owner}/{name}/{segment}/{branch}/{rel_q}"