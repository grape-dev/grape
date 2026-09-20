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


def workspace_repo_dir(repo_key: str) -> str:
    """返回仓库在工作空间中的根目录（含 provider 隔离）。

    路径模式：{WORKSPACE_DIR}/{provider}/{owner}/{repo}
    provider 从 repo_key 前缀解析（gitcode: / github:），无前缀默认 'gitcode'。

    这样 gitcode:mm6667/blog 和 github:mm6667/blog 落到不同物理目录，
    避免 codebase mirror / issue worktree / project-info 跨平台冲突。
    """
    import os
    from src.config import WORKSPACE_DIR
    provider, owner, repo = parse_repo_key(repo_key)
    return os.path.join(WORKSPACE_DIR, provider or "gitcode", owner, repo)


def knowledge_obs_prefix(repo_key: str) -> str:
    """生成 OBS key 前缀：knowledge/{provider}/{owner}/{repo}

    Issue #190：统一 obs_key 生成，含 provider 前缀，避免跨 provider 同名仓库冲突。
    """
    provider, owner, repo = parse_repo_key(repo_key)
    return f"knowledge/{provider or 'gitcode'}/{owner}/{repo}"


# baseline 知识文件名 → 中文名/描述映射（统一常量，避免多处硬编码不一致）
BASELINE_NAME_MAP = {
    "project-graph.md": "项目结构图谱",
    "project-capabilities.md": "项目能力清单",
    ".metadata.json": "项目元数据",
}
BASELINE_DESC_MAP = {
    "project-graph.md": "VOD 基线分析：项目代码结构图谱（模块/依赖/技术栈）",
    "project-capabilities.md": "VOD 基线分析：项目能力清单（功能特性/支持能力）",
    ".metadata.json": "VOD 基线分析：项目元数据（分析时间/分支/提交哈希）",
}
# fallback：文件名不在 MAP 中时使用
BASELINE_NAME_FALLBACK = "项目基线分析"
BASELINE_DESC_FALLBACK = "VOD 基线分析自动生成"
