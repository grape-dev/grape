"""Gitee 方言（卡驱动基座 + 组织空间）。

Gitee 是 config-only 平台：API 形态由 configs/platforms/gitee.json 的能力位驱动
（dialect=github、query_token、owner_param、token_compare、bare_ref），因此这里
**只补代码无法表达的一件事**：「所属空间」清单与解析。

为什么需要代码：`/user/orgs` + `/orgs/{login}` 是 Gitee 的组织端点，卡能力位里没有
「空间清单」这个字段（它不是形态开关，而是一次额外的读请求），所以放在
NamespaceCapability 的方言实现里，与 GitLab 同一抽象、同一事实源。
"""
from __future__ import annotations

from src.kernel.config import log

from .base import GenericVcsProvider, _require_card


class GiteeProvider(GenericVcsProvider):
    """Gitee：卡驱动基座 + NamespaceCapability（组织空间）。"""

    def __init__(self):
        super().__init__(_require_card("gitee"))

    def list_create_spaces(self, api_token: str) -> "list[dict]":
        """可建仓的组织空间：GET /user/orgs → [{path, name, kind}]。

        个人空间不在此列（前端固定渲染「个人账号」一行）。上游失败降级为空，
        不阻断个人空间建仓（与 GitLab 实现同语义）。
        """
        try:
            rows = self._http("GET", "/user/orgs?per_page=100", api_token)
        except Exception as e:  # noqa: BLE001
            log.warning(f"gitee list_create_spaces failed: {e}")
            return []
        out: list[dict] = []
        for org in rows if isinstance(rows, list) else []:
            if not isinstance(org, dict):
                continue
            path = str(org.get("login") or org.get("path") or "")
            if path:
                out.append({"path": path, "name": str(org.get("name") or path),
                            "kind": "org"})
        return out

    def resolve_namespace(self, api_token: str, owner: str) -> "dict | None":
        """组织登录名 → {id, path}；与 list_create_spaces 同一事实源。

        Gitee 建仓用 owner path（POST /orgs/{owner}/repos），不需要数字 id；
        这里仍返回 id 供上层统一展示/审计。fail-closed：查不到组织即 None。
        """
        if not owner:
            return None
        try:
            org = self._http("GET", f"/orgs/{owner}", api_token)
        except Exception as e:  # noqa: BLE001 - 不存在/无权限都按「不可用」处理
            log.debug(f"gitee namespace lookup failed ({owner}): {e}")
            return None
        if isinstance(org, dict) and str(org.get("login") or "").lower() == owner.lower():
            try:
                return {"id": int(org.get("id") or 0),
                        "path": str(org.get("login") or owner)}
            except Exception:  # noqa: BLE001
                return None
        return None