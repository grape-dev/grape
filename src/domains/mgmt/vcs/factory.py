"""provider 工厂与卡解析（开闭原则：新增平台只注册一行）。"""

from src.kernel.config import log
from src.kernel.ports import vcs_cards as _vcs_cards

from .base import VCSProvider, _require_card

def _supports_create_repo_kwargs(provider: str) -> bool:
    """卡驱动能力位 `create_repo_options`：true = 适配器接受 auto_init/default_branch。

    禁止 kernel 内二元平台字面量（platform-whitelist ratchet）；新平台只需在
    configs/platforms/<provider>.json 声明该能力位即可获得建仓可选参数。
    """
    try:
        card = _vcs_cards.get_platform_card(str(provider or "").lower())
    except Exception as exc:
        log.warning(f"create_repo_kwargs: platform card lookup failed provider={provider!r}: {exc}")
        return False
    return bool(card and getattr(card, "create_repo_options", False))


def create_repo_kwargs(provider: str, auto_init: bool = False,
                       default_branch: str = "") -> dict:
    """建仓可选参数（仅卡声明 create_repo_options 的平台才透传）。

    provider 差异收敛在 kernel 层，避免调用方（mgmt/repos.py）出现平台名比较。
    """
    if _supports_create_repo_kwargs(provider):
        return {"auto_init": bool(auto_init),
                "default_branch": (default_branch or "main")}
    return {}

def create_provider(provider: str) -> VCSProvider:
    """根据 provider 标识创建 VCSProvider 实例（D22 统一工厂）。

    - 覆盖层注册表（S72 收口：if 链→注册表，新增平台不再改本函数体）：
      gitcode/github/gitlab = GenericVcsProvider 子类（方言偏离基座的显式
      例外——gitcode/github 存量委托、gitlab /projects 形态）；
    - config-only 平台（无方言偏离者）：GenericVcsProvider（纯卡驱动，零代码）；
      仅补「所属空间」等卡无法表达的读能力时，注册一个薄方言（如 gitee）；
    - 未知 provider：GitCodeProvider（存量兜底语义不变）。
    """
    cls = _provider_overrides().get(provider)
    if cls is not None:
        return cls()
    try:
        card = _vcs_cards.get_platform_card(provider)
    except Exception:
        card = None
    if card is not None:
        # Issue #3580：gitlab 方言的实例卡（含自建 gitlab-corp）必须走
        # GitLabProvider（/projects 寻址、PRIVATE-TOKEN、members/hooks 形态）；
        # 纯卡驱动 GenericVcsProvider 只适用于 github 系 API 形态。
        if getattr(card, "dialect", "") == "gitlab":
            from .gitlab import GitLabProvider  # 延迟导入：防 factory ↔ 方言循环

            return GitLabProvider(card)
        from .base import GenericVcsProvider

        return GenericVcsProvider(card)
    # D105i：逃逸口可观测——纯配卡平台静默落 GitCode 兜底 = 卡缺能力字段，
    # 是“github 系假设”类静默错误的源头，必须留痕而非无声吞掉
    log.warning("provider factory fallback: provider=%r has no card override/module, "
                "returning GitCodeProvider (check platform card completeness)", provider)
    from .gitcode import GitCodeProvider

    return GitCodeProvider()

def create_provider_from_repo_key(repo_key: str) -> VCSProvider:
    """根据 repo_key 前缀创建 VCSProvider（D21 补：卡驱动工厂，消二分）。

    repo_key 前缀 → create_provider 统一分派：config-only 平台（gitee 等）
    返回 GenericVcsProvider（卡能力），存量三平台返回原类（行为不变）。
    """
    from src.kernel.repo_key import parse_repo_key
    provider, _, _ = parse_repo_key(repo_key or "")
    return create_provider(provider)

def _provider_overrides():
    """方言覆盖层（延迟导入：factory 与方言模块互不阻塞 import）。"""
    from .gitcode import GitCodeProvider
    from .gitee import GiteeProvider
    from .github import GitHubProvider
    from .gitlab import GitLabProvider

    return {"gitcode": GitCodeProvider, "gitee": GiteeProvider,
            "github": GitHubProvider, "gitlab": GitLabProvider}
