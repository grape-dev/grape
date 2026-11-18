"""台阶3：纯配置平台的资源 API 通用模块垫片（卡驱动，零平台 Python 模块）。

providers/<name>.py 四模块的内容 = 路径形态 + 字段映射 + 凭据钩子。其中
github 系 REST 形态可纯 JSON 表达（卡 resources 段声明），凭据钩子在通用链
（rc 单表 get_repo_bot_token / get_user_token_for）已平台无关——故新平台
无需再写模块：本垫片按 PlatformCard 生成等价的模块形态对象（API_BASE/
repo_segment/PATH_MAP/request/bot_token/user_token），供 git_repo_api 的
_vcs_url/_vcs_req/_repo_token 消费。

存量四平台不走本垫片（module_for 先查 _MODULES，行为零变更）。
"""
import json as _json
import urllib.error as _ue
import urllib.parse as _up
import urllib.request as _ur

from src.domains.mgmt.user.repo_manager import UpstreamAPIError


import re as _re


def _redact_query_token(url: str) -> str:
    """access_token 是 query 凭据，异常/日志 URL 内替换为 ***（N1，gitee 同款）。"""
    return _re.sub(r"([?&]access_token=)[^&]+", r"\1***", url)


def _make_request(card):
    """按卡 api_auth 生成 request(method, url, token, body)。"""
    auth = card.api_auth

    def request(method, url, token, body=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            if auth == "query_token":
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}access_token={_up.quote(token, safe='')}"
            elif auth in ("token_header", "bearer_header"):
                headers["Authorization"] = f"Bearer {token}"
            elif auth == "private_token":
                headers["PRIVATE-TOKEN"] = token
        data = _json.dumps(body).encode() if body else None
        req = _ur.Request(url, data=data, headers=headers, method=method)
        try:
            with _ur.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return _json.loads(raw) if raw.strip() else {}
        except _ue.HTTPError as e:
            raw = e.read().decode(errors="replace")
            shown = _redact_query_token(url) if auth == "query_token" else url
            raise UpstreamAPIError(method, shown, e.code, raw)
        except _ue.URLError as e:
            raise RuntimeError(f"Cannot reach {card.provider} API: {e.reason}")

    return request


class CardProviderModule:
    """卡驱动的模块形态对象（duck-type providers/<name>.py 的消费面）。"""

    def __init__(self, card):
        self.card = card
        self.provider = card.provider
        self.API_BASE = card.api_base
        self.PATH_MAP = dict((card.resources or {}).get("path_map") or {})
        self.request = _make_request(card)

    def repo_segment(self, owner: str, repo: str) -> str:
        seg = (self.card.resources or {}).get("repo_segment") or "repos/{owner}/{repo}"
        return seg.replace("{owner}", _up.quote(str(owner), safe="")
                         ).replace("{repo}", _up.quote(str(repo), safe=""))

    def bot_token(self, repo_key):
        # 通用 bot 链：rc 单表（bot 列→全局→用户降级；S82，平台通用）
        from src.domains.mgmt.public import get_repo_bot_token
        return get_repo_bot_token(repo_key=repo_key) or ""

    def user_token(self, username, user_id):
        from src.api.deps import get_user_token_for
        return get_user_token_for(username, user_id=user_id, provider=self.provider) or ""


_CARD_MODULES: dict = {}


def card_module_for(card) -> CardProviderModule:
    """按卡取（并缓存）垫片实例。"""
    mod = _CARD_MODULES.get(card.provider)
    if mod is None or mod.card is not card:
        mod = CardProviderModule(card)
        _CARD_MODULES[card.provider] = mod
    return mod
