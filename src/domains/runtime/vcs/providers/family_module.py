"""方言族模块代理（Issue #3580 复审，对齐 PR #1854 的自建实例接入）。

背景：`providers/__init__.module_for()` 只为「内置四平台（代码模块）」与「卡带
`resources` 段的纯配卡平台（card_module 垫片）」产出消费面。**自建实例卡**
（`gitlab-corp` 一类：`vcs_providers_config` 里新增的卡，无 `resources` 段）两者都不
命中 → 返回 None → `git_repo_api._provider_module()` 抛 `ValueError: Git provider
未支持`，实例仓的所有网关调用（Issue/MR/树/文件/标签…）直接 502。

实例卡的方言（`card.dialect`）与内置模块同名（gitlab/gitcode/github/gitee），故这里做
「方言族模块代理」：**属性全部委托给同族代码模块，只有 `API_BASE` 取自实例卡**——
一实例一 host，绝不能退回内置默认域名（否则凭据会打到 gitlab.com）。
"""
from __future__ import annotations


class FamilyProviderModule:
    """把内置方言模块套到实例卡上：API_BASE 走卡，其余属性/函数委托给族模块。"""

    def __init__(self, card, family):
        self._card = card
        self._family = family
        self.API_BASE = str(getattr(card, "api_base", "") or "")
        self.provider = str(getattr(card, "provider", "") or "")

    def __getattr__(self, name: str):
        # 未定义属性一律回族模块（repo_segment / PATH_MAP / tree_url / _RULES …）
        return getattr(self._family, name)

    def request(self, method: str, url: str, token: str, body: dict | None = None):
        return self._family.request(method, url, token, body)

    def __repr__(self) -> str:  # 便于日志定位
        return f"<FamilyProviderModule {self.provider or '?'} api_base={self.API_BASE}>"


def family_module_for(card, family) -> FamilyProviderModule:
    return FamilyProviderModule(card, family)