"""VCS 平台适配（建仓 / 协作 / webhook / 命名空间）。

对外只暴露工厂与抽象：调用方 import 本包，不依赖具体方言模块。
"""
from .base import (GenericVcsProvider, NamespaceCapability, RepoCapability,
                   TokenCapability, VCSProvider, WebhookCapability)
from .factory import (create_provider, create_provider_from_repo_key,
                      create_repo_kwargs)
from .gitee import GiteeProvider
from .gitcode import GitCodeProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

__all__ = [
    "VCSProvider", "GenericVcsProvider", "TokenCapability", "RepoCapability",
    "WebhookCapability", "NamespaceCapability",
    "GitCodeProvider", "GiteeProvider", "GitHubProvider", "GitLabProvider",
    "create_provider", "create_provider_from_repo_key", "create_repo_kwargs",
]
