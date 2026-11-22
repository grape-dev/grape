"""repo_key 统一转换工具：跨平台全局唯一标识的单一入口。

约定：
- 内部/DB 存储：repo_key = "<provider>:<owner>/<repo>"，如 "gitcode:owner/repo"。
- 外部/前端：一律无前缀 "owner/repo"。
- 业务代码禁止自行拼/剥前缀，统一走本模块。
"""

import logging

from src.kernel.ports import vcs_cards as _vcs_cards

# 与 src.kernel.config 同一 logger（stdlib 单例，同名字即同对象）：不引入
# kernel→config 的导入依赖，同时保留统一的 handler/上下文过滤器。
_log = logging.getLogger("devops-bot")


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


def _namespace_provider(provider: str) -> bool:
    """该 provider 是否支持多级命名空间（provider/subgroup/repo）。

    GitLab 家族：仓库全路径 = `<组>/<子组>/.../<仓>`，故 owner 段本身可能含 `/`。
    Issue #3580 复审（对齐 PR #1854）：判据走平台卡方言（card.dialect == "gitlab"），
    自建实例卡（如 gitlab-corp）与内置 gitlab 同族——不引入平台名字面量枚举。
    卡不可用（未绑定端口/装载失败）时按不支持处理 = 存量语义零变化。
    """
    if not provider:
        return False
    try:
        card = _vcs_cards.get_platform_card(provider)
    except Exception:
        return False
    return bool(card is not None and getattr(card, "dialect", "") == "gitlab")


def parse_repo_key(repo_key: str) -> tuple[str, str, str]:
    """gitcode:owner/repo → (provider, owner, repo)。无前缀时 provider 默认 'gitcode'。

    GitLab 家族按**最后一段**切分（owner 可含子组）：`gitlab-corp:grp/sub/proj`
    → ('gitlab-corp', 'grp/sub', 'proj')；其它平台保持首个 `/` 切分（零回归）。
    """
    if not repo_key:
        return "gitcode", "", ""
    provider, _, owner_repo = repo_key.partition(":")
    if not owner_repo:
        provider, owner_repo = "gitcode", repo_key
    provider = provider or "gitcode"
    if _namespace_provider(provider):
        owner, _, repo = owner_repo.rpartition("/")
        return provider, owner, repo
    owner, _, repo = owner_repo.partition("/")
    return provider, owner, repo


def split_owner_repo(owner_repo: str) -> tuple[str, str]:
    """owner/repo（或 gitcode:owner/repo）→ (owner, repo)，兼容带/不带前缀。

    GitLab 家族按最后一段切分（子组命名空间）；其它平台首个 `/` 切分（零回归）。
    """
    if not owner_repo:
        return "", ""
    provider = ""
    if ":" in owner_repo:
        provider, _, owner_repo = owner_repo.partition(":")
    if _namespace_provider(provider):
        owner, _, repo = owner_repo.rpartition("/")
        return owner, repo
    owner, _, repo = owner_repo.partition("/")
    return owner, repo


def _warn(msg: str) -> None:
    _log.warning(msg)


def _catalog_providers() -> set[str]:
    """平台前缀名单：数据驱动单一入口，禁止平台字面量枚举。

    权威 = 平台卡注册表（configs/platforms/*.json + DB 卡，platform_card 统一
    装载）；注册表导入失败**或返回空集**时直接扫卡目录文件名兜底——与主路径
    同源同集（对称，不会出现主路径可见、兜底不可见的 403），仍不引入字面量。

    空集不等于「没有平台」：未绑定 vcs_cards 端口的进程（装配缺失的 insight
    角色等）调用注册表会抛 PortNotBound，已绑定但卡装载失败的进程会返回空集，
    两者都必须兜底，否则本进程算出的候选集只剩裸键 —— 非默认前缀仓库在
    check_repo_access 恒 403。
    """
    try:
        providers = {str(p).lower() for p in _vcs_cards.list_vcs_platforms() if p}
    except Exception:
        providers = set()
    return providers or _catalog_providers_from_dir()


def _repo_root_dir() -> str:
    """仓库根（含 configs/platforms 的目录）：向上查找，源码/部署层级变化都稳。

    旧实现用 dirname(dirname(abspath(__file__))) 得 `<repo>/src`，再拼
    configs/platforms → `<repo>/src/configs/platforms`（不存在），目录扫描
    恒空集且被 except 静默吞掉。

    2026-11-19：worker 安装目录（`grape-worker/src/kernel/...`）在旧版本里没有
    `configs/`，向上找不到就返回一个不存在的猜测路径，日志每进程一条
    `platform catalog fallback failed: '<安装目录>/configs/platforms'`。
    现在：① 支持 `GRAPE_CONFIG_DIR` 显式指定；② 卡目录已随 worker 包下发，
    查找会命中它；③ 真找不到时返回空串，让调用方按「本安装未带卡」处理。
    """
    import os as _os
    env_dir = _os.environ.get("GRAPE_CONFIG_DIR", "").strip()
    if env_dir:
        # GRAPE_CONFIG_DIR 允许指向仓库根或 configs 目录；返回「configs 的父目录」口径。
        for cand in (env_dir, _os.path.join(env_dir, "configs")):
            if _os.path.isdir(_os.path.join(cand, "platforms")):
                return _os.path.dirname(cand.rstrip("/\\"))
    start = _os.path.dirname(_os.path.abspath(__file__))
    cur = start
    while cur and cur != _os.path.dirname(cur):
        if _os.path.isdir(_os.path.join(cur, "configs", "platforms")):
            return cur
        cur = _os.path.dirname(cur)
    return ""


def _catalog_providers_from_dir() -> set[str]:
    """卡目录兜底：configs/platforms/*.json 文件名即平台名（无字面量）。

    缺席（未随包下发的安装，如老 worker）→ debug 一条，不当告警：该进程不用卡。
    目录在但为空 → warning（部署布局异常，需定位）。
    """
    import os as _os
    root = _repo_root_dir()
    if not root:
        _log.debug("platform catalog not present in this install; provider catalog fallback skipped")
        return set()
    _dir = _os.path.join(root, "configs", "platforms")
    try:
        names = {f[:-len(".json")].lower() for f in _os.listdir(_dir)
                 if f.endswith(".json")}
    except Exception as _e:
        _warn(f"platform catalog fallback failed: {_e}")
        return set()
    if not names:
        _warn(f"platform catalog fallback empty: {_dir}")
    return names


def strip_provider_prefix(owner: str) -> str:
    """剥离 owner 段的 provider 前缀（gitcode:/github:/gitee:…），URL/workspace 拼接用。

    Issue #466：带前缀路径（/api/repos/gitcode:owner/repo、/api/git-repo/github:owner/repo）
    的 owner path 段含前缀；token 解析需要带前缀 repo_key（幂等），但上游 URL /
    clone_url / workspace 须用裸 owner。前缀集由 _catalog_providers 数据驱动派生
    ——禁止平台字面量枚举（S85 全仓派生清零 / platform_whitelist_ratchet）。
    未知前缀原样返回。
    """
    s = str(owner or "")
    low = s.lower()
    for p in _catalog_providers():
        if low.startswith(f"{p}:"):
            return s[len(p) + 1:]
    return s


def derive_repo_identity(repository: str = "", repo_key: str = "") -> tuple[str, str, str]:
    """由 REPO_FULL_NAME（repository）统一派生 (full_name, owner, name)。

    Issue #1660：Agent 任务 env 注入的唯一事实源是 REPO_FULL_NAME，**禁止**用触发人
    账号（ACCOUNT/exec_account）充当 REPO_OWNER。规则：
    - 优先拆 `repository`（允许带 provider 前缀，先剥前缀）；
    - `repository` 为空/无法拆出 owner+name 时，回退拆分 `repo_key` 并告警；
    - 二者均为空或伪仓库（`chat:`）→ 返回 ("", "", "")，由调用方决定不注入。
    """
    _log = None

    def _warn(msg: str) -> None:
        nonlocal _log
        try:
            if _log is None:
                from src.kernel.config import log as _log
            _log.warning(msg)
        except Exception:
            pass

    candidate = from_repo_key((repository or "").strip())
    if candidate and "/" in candidate:
        owner, _, name = candidate.partition("/")
        if owner and name:
            return f"{owner}/{name}", owner, name

    rk = (repo_key or "").strip()
    if rk and not rk.startswith("chat:"):
        fallback = from_repo_key(rk)
        owner, _, name = fallback.partition("/")
        if owner and name:
            _warn(f"derive_repo_identity: REPO_FULL_NAME missing/invalid "
                  f"(repository={repository!r}), fallback to repo_key={rk!r}")
            return f"{owner}/{name}", owner, name

    if (repository or "").strip() or (rk and not rk.startswith("chat:")):
        _warn(f"derive_repo_identity: cannot derive owner/name "
              f"(repository={repository!r}, repo_key={repo_key!r})")
    return "", "", ""


def workspace_repo_dir(repo_key: str) -> str:
    """返回仓库在工作空间中的根目录（含 provider 隔离）。

    路径模式：{WORKSPACE_DIR}/{provider}/{owner}/{repo}
    provider 从 repo_key 前缀解析（gitcode: / github:），无前缀默认 'gitcode'。

    这样 gitcode:mm6667/blog 和 github:mm6667/blog 落到不同物理目录，
    避免 codebase mirror / issue worktree / project-info 跨平台冲突。
    """
    import os
    from src.kernel.config import WORKSPACE_DIR
    provider, owner, repo = parse_repo_key(repo_key)
    return os.path.join(WORKSPACE_DIR, provider or "gitcode", owner, repo)


def knowledge_obs_prefix(repo_key: str) -> str:
    """生成 OBS key 前缀：knowledge/{provider}/{owner}/{repo}

    Issue #190：统一 obs_key 生成，含 provider 前缀，避免跨 provider 同名仓库冲突。
    """
    provider, owner, repo = parse_repo_key(repo_key)
    return f"knowledge/{provider or 'gitcode'}/{owner}/{repo}"


def um_column_for_provider(provider: str) -> str:
    """根据 provider 字符串返回 unified_user_mapping 对应列名。

    gitcode → "gitcode_user"；github → "github_login"。
    D39/S59：其余平台（gitee 等无 um 列）返回空串，调用方须跳过——
    此前未知平台折叠 gitcode_user 会造成双向污染（绑定链把 gitee
    登录名写进 gitcode_user、归属兜底跨平台同名误命中，FR-17.1 反例）。
    """
    return _UM_PROVIDER_COLUMNS.get(provider or "", "")


# D39/S59：um 平台列白名单（存量列名；um 表退役归批次 1，新平台不再加列）
_UM_PROVIDER_COLUMNS = {"gitcode": "gitcode_user", "github": "github_login"}

# D44/S66：平台 login 与 grape 账号名**同源**的白名单（历史迁移形态：grape
# 账号名即 gitcode 登录名）。同源平台的协作者 login 可直接写 um 平台列；
# 异源平台（github login 是平台真名）必须按 login 解析 grape 账号（uid），
# 只绑 IM 身份——FR-21.2 语义，白名单派生而非平台名 if 分支。
_UM_LOGIN_IS_ACCOUNT = {"gitcode"}


def um_login_is_grape_account(provider: str) -> bool:
    """平台 login 是否与 grape 账号名同源（决定 um 写入策略，见上白名单注释）。"""
    return (provider or "") in _UM_LOGIN_IS_ACCOUNT


def um_column_for_repo(repo_key: str) -> str:
    """根据 repo_key 的 provider 返回 unified_user_mapping 对应列名。

    repo_key 为空时默认 gitcode_user（向后兼容，调用方从 grape_accounts.username 取值）。
    """
    provider, _, _ = parse_repo_key(repo_key)
    return um_column_for_provider(provider)


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


def repo_key_candidates(base: str) -> "list[str]":
    """裸 repo_key 的跨平台前缀候选集（D16/S25，抽象解）。

    候选集 = 全部平台卡 provider 前缀（含裸 key 本身），按"已有平台"派生，
    不再硬编码 gitcode/github 双前缀——新平台（gitee 等）配置即加入候选，
    check_repo_access / knowledge / chat 等跨平台解析统一走此函数。
    """
    if not base:
        return []
    cands = [base] if ":" not in base else [base]
    stripped = base.split(":", 1)[-1]
    # 候选集与目录兜底走同一入口（端口未绑定 / 端口返回空集都在
    # _catalog_providers 内收敛）；直接调端口会把空集漏成「只有裸键」，
    # 非默认前缀仓库随之在 check_repo_access 恒 403。
    providers = sorted(_catalog_providers())
    for p in sorted(providers):
        _rk = f"{p}:{stripped}"
        if _rk not in cands:
            cands.append(_rk)
    return cands


def repo_key_in_params(repo: str) -> "tuple[str, tuple[str, ...]]":
    """repo_key 候选集 IN 参数原子产物（S101/D76）：一次调用同时产出
    (占位符串, 参数元组)——占位符数与参数数由同一构造保证，杜绝
    36ca1cfb4 形态的"参数动态化、占位符半截工程"（两次独立调用之间
    卡缓存 TTL 失效重载可致数量漂移）。

    空候选集防御（恒假语义，非语法防御）：repo_key_candidates("") == []
    → 返回 ("NULL", ())——IN (NULL) 三值逻辑恒 UNKNOWN，不匹配任何行，
    优于 IN ('')（会误匹配 repo_key='' 的行）。注意与旧代码不等价：
    旧形态空集生成 IN () 语法炸；本 helper 恒假，逐批替换站点需带
    "空候选集不匹配任何行"用例。

    已知边界（记档不阻塞）：调用点仍需自行拼 `IN (` + _ph + `)` 与列名/
    表别名——这是灵活性（owner_id/repo_key/别名各异）与收敛度的折衷。

    用法：
        _ph, _args = repo_key_in_params(repo)
        f"... WHERE repo_key IN ({_ph})", _args
    """
    _cands = tuple(repo_key_candidates(repo))
    if not _cands:
        return "NULL", ()
    return ",".join("?" * len(_cands)), _cands


def issue_web_url(repo_key: str, issue_number) -> str:
    """issue 的平台 web 链接（D16/S26）：host/路径从平台卡方言派生，无平台分支。

    gitlab 方言路径为 /-/issues/<n>，github 系为 /issues/<n>。
    """
    provider, owner, repo = parse_repo_key(repo_key)
    host = _host_for_provider(provider)
    seg = "-/issues" if _dialect_of(provider) == "gitlab" else "issues"
    return f"https://{host}/{owner}/{repo}/{seg}/{issue_number}"


def pr_web_url(repo_key: str, pr_number) -> str:
    """PR 的平台 web 链接（D16/S26）：host 从平台卡派生；gitlab 方言用 merge_requests 路径。"""
    provider, owner, repo = parse_repo_key(repo_key)
    host = _host_for_provider(provider)
    path = "merge_requests" if _dialect_of(provider) == "gitlab" else "pulls"
    return f"https://{host}/{owner}/{repo}/{path}/{pr_number}"


def _dialect_of(provider: str) -> str:
    try:
        card = _vcs_cards.get_platform_card(provider)
        if card is not None:
            return card.dialect
    except Exception:
        pass
    # D44/S65：方言回退=映射表（数据非分支）
    return _DIALECT_FALLBACK.get(provider or "", "github")


# D44/S65：无卡平台方言回退映射（存量双平台；新平台应有卡，不走此回退）
_DIALECT_FALLBACK = {"github": "github", "gitlab": "gitlab"}


def _host_for_provider(provider: str) -> str:
    """host 统一来源：平台卡（缺省回退存量双平台映射）。"""
    _legacy = {"gitcode": "gitcode.com", "github": "github.com", "gitlab": "gitlab.com"}
    if provider in _legacy:
        return _legacy[provider]
    try:
        card = _vcs_cards.get_platform_card(provider)
        if card is not None:
            return card.host
    except Exception:
        pass
    return "gitcode.com"
