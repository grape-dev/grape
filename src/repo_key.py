"""repo_key 统一转换工具：跨平台全局唯一标识的单一入口。

约定：
- 内部/DB 存储：repo_key = "<provider>:<owner>/<repo>"，如 "gitcode:owner/repo"。
- 外部/前端：一律无前缀 "owner/repo"。
- 业务代码禁止自行拼/剥前缀，统一走本模块。
"""


def to_repo_key(owner_repo: str, provider: str = "gitcode") -> str:
    """owner/repo → gitcode:owner/repo；已带前缀则幂等返回原值。"""
    if not owner_repo:
        return ""
    if ":" in owner_repo:
        return owner_repo
    return f"{provider}:{owner_repo}"


def from_repo_key(repo_key: str) -> str:
    """gitcode:owner/repo → owner/repo（前端展示 / URL / API 输出）。"""
    if not repo_key:
        return ""
    return repo_key.split(":", 1)[-1] if ":" in repo_key else repo_key


def parse_repo_key(repo_key: str) -> tuple[str, str, str]:
    """gitcode:owner/repo → (provider, owner, repo)。无前缀时 provider 默认 'gitcode'。"""
    if not repo_key:
        return "gitcode", "", ""
    provider, _, owner_repo = repo_key.partition(":")
    if not owner_repo:
        provider, owner_repo = "gitcode", repo_key
    owner, _, repo = owner_repo.partition("/")
    return provider or "gitcode", owner, repo


def split_owner_repo(owner_repo: str) -> tuple[str, str]:
    """owner/repo（或 gitcode:owner/repo）→ (owner, repo)，兼容带/不带前缀。"""
    if not owner_repo:
        return "", ""
    if ":" in owner_repo:
        owner_repo = owner_repo.split(":", 1)[-1]
    owner, _, repo = owner_repo.partition("/")
    return owner, repo
