"""git 基建：共享 bare mirror 基线 + Issue 级 git worktree（多 Issue 并行）。

见 multi-issue-isolation-design.md §3.4.1。X 工作模型（用户拍板）：coding/qa/sre 使用本地 git 命令，
跨平台复用（GitHub 只需在 _HOSTS 加映射）。

- 基线：{WORKSPACE_DIR}/{owner}/{repo}/codebase/ 的 bare mirror（每机懒建立，fetch --prune 刷新）
- worktree：{WORKSPACE_DIR}/{owner}/{repo}/issues/{issue_num}/{agent_type}/（任务 cwd，按 Issue 统一隔离）
- 鉴权安全：clone 用 `-c http.extraheader` 内联注入（不写 config、不入日志）；push 用 GIT_ASKPASS
  辅助脚本读已注入的 `$BOT_GITCODE_TOKEN`（不落盘、随目录清理）。
"""
import base64, os, re, shutil, subprocess, threading, time
from src import config
from src.config import log

_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}

_GIT_AGENTS = ("coding", "qa", "sre")

_lock_guard = threading.Lock()
_repo_locks = {}


def _lock_for(repo_key: str) -> threading.Lock:
    with _lock_guard:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


def issue_num_from(issue_id: str) -> str:
    """"#5" / "5" → "5"；#0/空 → ''。"""
    s = (issue_id or "").strip().lstrip("#")
    return s if s and s != "0" else ""


def is_git_agent(agent_type: str) -> bool:
    """仅 coding/qa/sre 需要本地 git 工作树。"""
    return agent_type in _GIT_AGENTS


def repo_base_dir(repo_key: str) -> str:
    from src.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), "codebase")


def issue_dir_for(repo_key: str, agent_type: str, issue_num) -> str:
    from src.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), "issues", str(issue_num), agent_type)


def ensure_feature_branch(wt_dir: str, target_branch: str, base_branch: str,
                          logger=None) -> bool:
    """分支护栏：确保 worktree 在 target_branch 上，禁止直推基线分支（Issue #15）。

    先检查当前分支；若不在 target_branch 上，先尝试 `git checkout target_branch`
    （分支已存在场景），失败再 `git checkout -b target_branch origin/{base_branch}`
    （新建分支）。记 warning 但不阻断后续流程（避免误杀）。

    Args:
        wt_dir: worktree 目录路径
        target_branch: 目标分支名（如 feat/issue-{N}）
        base_branch: 基线分支名（如 main），用于新建分支时追踪
        logger: 可选 logger（默认用 src.config.log）

    Returns:
        True 表示已在目标分支上（或成功切换），False 表示切换失败。
    """
    _log = logger or log
    try:
        _r_br = subprocess.run(
            ["git", "-C", wt_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10)
        _current = (_r_br.stdout or "").strip()
        if _current and _current == target_branch:
            return True
        if _current:
            _log.warning(f"branch guard: on '{_current}', switching to {target_branch}")
        # 先尝试 checkout 已存在的分支
        _r_co = subprocess.run(
            ["git", "-C", wt_dir, "checkout", target_branch],
            capture_output=True, text=True, timeout=30)
        if _r_co.returncode == 0:
            return True
        # 分支不存在 → 新建（优先追踪 origin/{base_branch}，不存在则从 HEAD）
        _r_co2 = subprocess.run(
            ["git", "-C", wt_dir, "checkout", "-b", target_branch, f"origin/{base_branch}"],
            capture_output=True, text=True, timeout=30)
        if _r_co2.returncode != 0:
            # origin/{base_branch} 不存在（如纯本地仓库）→ 从当前 HEAD 新建
            _r_co3 = subprocess.run(
                ["git", "-C", wt_dir, "checkout", "-b", target_branch],
                capture_output=True, text=True, timeout=30)
            if _r_co3.returncode != 0:
                _log.warning(f"branch guard checkout failed {target_branch}: {(_r_co3.stderr or '')[:200]}")
                return False
        return True
    except Exception as _e:
        _log.warning(f"branch guard check failed {target_branch}: {_e}")
        return False


def clone_url_for(repo_key: str) -> str:
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    host = _HOSTS.get(provider, _HOSTS["gitcode"])
    return f"https://{host}/{owner}/{repo}.git"


def _basic_auth(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _run_git(args: list, token: str = "", timeout: int = 900):
    """执行 git；token 经 `-c http.extraheader` 注入（不落盘、不进日志）。"""
    cmd = ["git"]
    if token:
        cmd += ["-c", f"http.extraheader=Authorization: Basic {_basic_auth(token)}"]
    cmd += args
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        log.warning(f"repogit git exec failed {args[:3]}: {e}")
        return None


# Issue #455: 拉取网络抖动重试——偶发 HTTP/2 帧层错误 / 连接超时 / RPC failed，
# 有限次退避重试可显著提升 0→1 安装/拉取成功率；4xx 权限/资源类快速失败不放大上游压力。
_NETWORK_ERR_RE = re.compile(
    r"(could not resolve|connection (timed out|refused|reset by peer)|failed to connect|"
    r"http/2|rpc failed|the remote end hung up|early eof|operation timed out|"
    r"unable to access|network is unreachable|ssl connect|recv failure|"
    r"could not read from remote repository|"
    r"connection reset by peer|timed out)", re.IGNORECASE)
_AUTH_STATUS_RE = re.compile(r"returned error: (401|403|404)")


def _is_git_network_error(result) -> bool:
    """git 结果是否命中网络类错误（可重试）；4xx/无错误返回 False。"""
    if result is None:
        return True  # 未产生结果（超时/进程异常）→ 可重试
    err = ((result.stderr or "") + "\n" + (result.stdout or ""))
    if _AUTH_STATUS_RE.search(err):
        return False  # 401/403/404 权限/资源类，快速失败
    return bool(_NETWORK_ERR_RE.search(err))


def _run_git_retry(args: list, token: str = "", timeout: int = 900,
                   retries: int = 3, base_delay: float = 1.0):
    """执行 git；网络类错误有限次退避重试（默认 3 次：1s/2s/4s）。

    - 成功（returncode==0）立即返回；
    - 仅网络类错误重试；4xx（401/403/404）与非网络类立即返回；
    - 全部重试失败返回最后一次结果（调用方按原有 None/非 0 逻辑降级）。
    """
    delay = base_delay
    last = None
    for attempt in range(retries):
        last = _run_git(args, token=token, timeout=timeout)
        if last is not None and last.returncode == 0:
            return last
        if not _is_git_network_error(last):
            return last
        if attempt < retries - 1:
            log.warning(
                f"repogit git failed (attempt {attempt + 1}/{retries}), retrying in {delay:.1f}s: "
                f"{(last.stderr if last else 'git exec failed')[:200]}")
            time.sleep(delay)
            delay *= 2
    return last


def ensure_repo_base(repo_key: str, token: str = "") -> str | None:
    """确保本机 bare 基线存在并刷新（每机懒建立）。

    用 `git init --bare` + refspec 拉取到 `refs/remotes/origin/*`，而非 `--mirror`：
    mirror 会把 per-Issue 本地分支 refs/heads/feat/issue-* 误当远程跟踪引用，被 `fetch --prune` 误删。
    """
    base = repo_base_dir(repo_key)
    url = clone_url_for(repo_key)
    with _lock_for(repo_key):
        try:
            if not os.path.isdir(base):
                os.makedirs(os.path.dirname(base), exist_ok=True)
                r = _run_git_retry(["init", "--bare", base], token=token)
                if r is None or r.returncode != 0:
                    log.warning(f"repogit init failed {repo_key}: {(r.stderr if r else 'git exec failed')[:300]}")
                    return None
                _run_git_retry(["-C", base, "remote", "add", "origin", url], token=token)
            r = _run_git_retry(["-C", base, "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"], token=token)
            if r is None or r.returncode != 0:
                log.warning(f"repogit fetch failed {repo_key}: {(r.stderr if r else 'git exec failed')[:300]}")
                return None
            # Issue #1841: 记录远端默认分支（init --bare + fetch 不会自动写 origin/HEAD）
            _run_git(["-C", base, "remote", "set-head", "origin", "--auto"], token=token, timeout=60)
            _mark_mirror_fresh(repo_key)
            return base
        except Exception as e:
            log.warning(f"repogit ensure_repo_base failed {repo_key}: {e}")
            return None


def ensure_repo_base_multi(linked_repos: list, token: str = "", env: dict = None) -> dict:
    """批量初始化多个关联仓库的 bare mirror（Issue #242）。

    R20.26.1/R20.26.2: 每个关联仓库用各自的 Repo.bot_token 独立获取 token，
    不复用主仓库 token。跨平台关联仓库（主 gitcode + 关联 github）自动分流。

    Issue #466 断点二: token 解析改为 env（GRAPE_LINKED_REPO_TOKENS，服务端派发时
    随 env 分发）优先，本地 Repo.bot_token 回退——worker 端本地 sqlite 无
    managed_repos 数据，纯本地解析必然为空导致全部 skip。

    Args:
        linked_repos: [{repo_key, branch}] 列表
        token: 主仓库 token（向后兼容，关联仓库独立获取时忽略此参数）
        env: 任务 env（含 GRAPE_LINKED_REPO_TOKENS）；缺省读 os.environ
    Returns:
        {repo_key: base_dir_or_None} 映射
    """
    result = {}
    for lr in (linked_repos or []):
        if not isinstance(lr, dict):
            continue
        rk = lr.get("repo_key") or ""
        if not rk:
            continue
        # Issue #466: env 分发 token 优先，本地解析回退
        _repo_token = _linked_repo_token(rk, env)
        # R20.26.4: token 短路时跳过该仓库
        if not _repo_token:
            log.warning(f"ensure_repo_base_multi: no token for linked repo {rk}, skipping")
            result[rk] = None
            continue
        base = ensure_repo_base(rk, token=_repo_token)
        result[rk] = base
    return result


def linked_repo_token_from_env(repo_key: str, env: dict = None) -> str:
    """从 env 的 GRAPE_LINKED_REPO_TOKENS（JSON {repo_key: token}）解析关联仓 token。

    Issue #466: 服务端派发任务时把各关联仓 bot token 随 env_extra 下发（与
    GRAPE_SESSION_TOKEN 同链路），worker 端本地无库也能完成 worktree 初始化。
    env 缺省读 os.environ；JSON 缺失/损坏/键不存在均返回空串（由调用方回退）。
    """
    import json as _json
    _env = os.environ if env is None else (env or {})
    raw = _env.get("GRAPE_LINKED_REPO_TOKENS", "") or ""
    if not raw:
        return ""
    try:
        m = _json.loads(raw)
        return str(m.get(repo_key, "")) if isinstance(m, dict) else ""
    except Exception:
        return ""


def _linked_repo_token(repo_key: str, env: dict = None) -> str:
    """关联仓 token 解析：服务端 env 分发优先 → 本地 Repo.bot_token 回退。

    Issue #466 断点二：worker 本地 sqlite 的 managed_repos 为空表（无 MySQL），
    纯本地解析跨平台/同平台关联仓都拿不到 token；服务端托管的有效 token 随
    env 分发后以此为准。env 缺省（本地/直连模式）仍走本地解析。
    """
    tok = linked_repo_token_from_env(repo_key, env)
    if tok:
        return tok
    try:
        from src.repo import Repo
        return Repo(repo_key).bot_token
    except Exception as e:
        log.warning(f"linked repo token resolve failed for {repo_key}: {e}")
        return ""


def ensure_linked_repo_worktrees(linked_repos: list, agent_type: str, issue_num,
                                  token: str = "", env: dict = None) -> list:
    """为关联仓库列表创建 Issue 级 worktree（Issue #242）。

    遍历 linked_repos，为每个 repo_key 调用 ensure_issue_worktree。
    每个仓库使用各自的 branch 配置（linked_repos 中的 branch 字段）。

    R20.26.1: 每个关联仓库独立获取 token。
    Issue #466 断点二: token 解析 env（GRAPE_LINKED_REPO_TOKENS）优先，
    本地 Repo.bot_token 回退（env 由调用方传任务 env，缺省读 os.environ）。

    Returns:
        [{repo_key, branch, worktree_dir}] 成功创建的 worktree 列表
    """
    if not is_git_agent(agent_type):
        return []
    worktrees = []
    for lr in (linked_repos or []):
        if not isinstance(lr, dict):
            continue
        rk = lr.get("repo_key") or ""
        branch = lr.get("branch") or ""
        if not rk:
            continue
        # Issue #466: env 分发 token 优先，本地解析回退
        _repo_token = _linked_repo_token(rk, env)
        if not _repo_token:
            log.warning(f"ensure_linked_repo_worktrees: no token for {rk}, skipping")
            continue
        wt = ensure_issue_worktree(rk, agent_type, issue_num, token=_repo_token, work_branch=branch)
        if wt:
            worktrees.append({"repo_key": rk, "branch": branch, "worktree_dir": wt})
        else:
            log.warning(f"repogit: linked repo worktree failed for {rk} issue={issue_num}")
    return worktrees


def _read_scene_work_branch(repo_key: str, issue_id: str = "") -> str:
    """读场景高级选项的 work_branch。

    优先读 issue_state.scene_id（Issue 级快照），空则回退 managed_repos.dev_scene →
    devops_pipeline_scene.advanced_options。这样流水线跑到一半时仓库场景切换不影响已在跑的 Issue。
    """
    try:
        from src.session_manager import db, db_lock
        import json as _json
        # 1. 优先读 issue_state.scene_id（Issue 级快照），空则回退 managed_repos.dev_scene
        scene_id = ""
        if issue_id:
            try:
                from src.api.session_store import get_issue_scene
                scene_id, _ = get_issue_scene(repo_key, issue_id)
            except Exception:
                scene_id = ""
        if not scene_id:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
                row = c.fetchone()
            scene_id = (row[0] if row else "") or ""
        if not scene_id:
            return ""
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        opts = _json.loads(row[0]) if row[0] else {}
        return opts.get("work_branch", "") or ""
    except Exception:
        return ""


_BRANCH_LIST_TTL = 90.0
_BRANCH_LIST_FAIL_TTL = 30.0
_BRANCH_MIRROR_TTL = 120.0
_MAX_BRANCH_PAGES = 50
_branch_list_cache = {}  # repo_key -> (timestamp, branches: set|None, default_branch)
_branch_list_force_remote = set()
_branch_mirror_fresh_at = {}  # repo_key -> 最近一次成功的 mirror 刷新时间
_branch_list_lock = threading.Lock()


def _mark_mirror_fresh(repo_key: str) -> None:
    with _branch_list_lock:
        _branch_mirror_fresh_at[repo_key] = time.time()


def _mirror_is_stale(repo_key: str) -> bool:
    """本地 mirror 是否超过刷新 TTL（未知按 stale 处理，避免长期信任陈旧 refs）。"""
    with _branch_list_lock:
        ts = _branch_mirror_fresh_at.get(repo_key)
    if ts is None:
        base = repo_base_dir(repo_key)
        try:
            ts = os.path.getmtime(base) if base and os.path.isdir(base) else 0.0
        except OSError:
            ts = 0.0
    return (time.time() - ts) > _BRANCH_MIRROR_TTL


def _refresh_local_mirror(repo_key: str, token: str = "") -> bool:
    """`fetch --prune` 刷新本地 bare mirror（含平台外新建/删除的分支）。"""
    base = repo_base_dir(repo_key)
    if not base or not os.path.isdir(base):
        return False
    with _lock_for(repo_key):
        r = _run_git_retry(
            ["-C", base, "fetch", "--prune", "origin",
             "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"],
            token=token)
        if r is None or r.returncode != 0:
            log.warning(f"work_branch: refresh mirror failed repo={repo_key}: "
                        f"{(r.stderr if r else 'git exec failed')[:200]}")
            return False
        _run_git(["-C", base, "remote", "set-head", "origin", "--auto"],
                 token=token, timeout=60)
    _mark_mirror_fresh(repo_key)
    return True


def _branch_list_cache_get(repo_key: str = "", refresh: bool = False):
    """读分支列表缓存；失败结果短 TTL，避免注入链路反复穿透外部 API。"""
    now = time.time()
    with _branch_list_lock:
        if refresh:
            return None
        entry = _branch_list_cache.get(repo_key)
        if not entry:
            return None
        ttl = _BRANCH_LIST_TTL if entry[1] is not None else _BRANCH_LIST_FAIL_TTL
        if now - entry[0] < ttl:
            return entry[1], entry[2]
    return None


def _branch_list_cache_put(repo_key: str, branches, default_branch: str = "") -> None:
    with _branch_list_lock:
        _branch_list_cache[repo_key] = (time.time(), branches, default_branch or "")


def _consume_force_remote(repo_key: str) -> bool:
    """取出并清除「下次必须走远端」标记（建分支后 mirror 可能尚未刷新）。"""
    with _branch_list_lock:
        if repo_key not in _branch_list_force_remote:
            return False
        _branch_list_force_remote.discard(repo_key)
        return True


def invalidate_branch_list_cache(repo_key: str = "", force_remote: bool = True) -> None:
    """失效分支列表缓存（建分支等写操作后调用；空 repo_key = 全清）。

    force_remote=True 时下一次查询跳过本地 bare mirror，直接走 VCS API，
    避免「刚建分支但 mirror 未刷新」被误判为不存在。
    """
    with _branch_list_lock:
        if repo_key:
            _branch_list_cache.pop(repo_key, None)
            if force_remote:
                _branch_list_force_remote.add(repo_key)
            else:
                _branch_list_force_remote.discard(repo_key)
        else:
            _branch_list_cache.clear()
            _branch_list_force_remote.clear()


def _local_repo_branch_names(repo_key: str):
    """本地 bare mirror 的 remote-tracking 分支列表（不依赖外部 API；不存在返回 None）。"""
    base = repo_base_dir(repo_key)
    if not base or not os.path.isdir(base):
        return None, ""
    r = _run_git(["-C", base, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"])
    if r is None or r.returncode != 0:
        return None, ""
    branches = set()
    for line in (r.stdout or "").splitlines():
        name = line.strip()
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        if name and name != "HEAD":
            branches.add(name)
    default = ""
    rh = _run_git(["-C", base, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if rh is not None and rh.returncode == 0:
        default = (rh.stdout or "").strip()
        if default.startswith("origin/"):
            default = default[len("origin/"):]
    return (branches or None), default


def _branch_lookup_token(repo_key: str) -> str:
    """分支查询 token：bot token 优先，GitCode 路径 B / GitHub 用户 token 兜底。"""
    try:
        from src.repo import Repo
        tok = Repo(repo_key).bot_token or ""
    except Exception:
        tok = ""
    if tok:
        return tok
    provider = repo_key.split(":", 1)[0] if ":" in repo_key else "gitcode"
    try:
        if provider == "github":
            from src.user.db import get_github_token
            return get_github_token(repo_key=repo_key) or ""
        from src.user.db import get_repo_token_with_refresh
        return get_repo_token_with_refresh(repo_key) or ""
    except Exception:
        return ""


def _api_request_for(repo_key: str):
    """按仓库平台返回统一 HTTP helper（GitCode/GitHub），避免直写 urllib。"""
    from src.user.repo_manager import _github_req, _req
    provider = repo_key.split(":", 1)[0] if ":" in repo_key else "gitcode"
    return _github_req if provider == "github" else _req


def _fetch_remote_default_branch(repo_key: str, token: str = "") -> str:
    """经 VCS 仓库信息 API 读取默认分支；失败返回空串。"""
    from src.repo import Repo
    from src.repo_key import split_owner_repo
    owner, name = split_owner_repo(repo_key)
    tok = token or _branch_lookup_token(repo_key)
    if not owner or not name or not tok:
        return ""
    try:
        info = _api_request_for(repo_key)(
            "GET", f"{Repo(repo_key).api_base}/repos/{owner}/{name}", tok)
        return (info.get("default_branch") or "") if isinstance(info, dict) else ""
    except Exception as e:
        log.warning(f"work_branch: read default branch failed repo={repo_key}: {e}")
        return ""


def fetch_repo_branch_records(repo_key: str, token: str = "",
                              max_pages: int = _MAX_BRANCH_PAGES):
    """经 VCS 分支列表 API 拉取分支记录（含 protected）；供 scenes 路由复用。

    返回 (records: list[dict]|None, truncated: bool)。None 表示请求失败。
    统一分页实现，避免 repogit 与 scenes.list_repo_branches 两套维护漂移。
    """
    from src.repo import Repo
    from src.repo_key import split_owner_repo
    owner, name = split_owner_repo(repo_key)
    tok = token or _branch_lookup_token(repo_key)
    if not owner or not name or not tok:
        return None, False
    api = Repo(repo_key).api_base
    req = _api_request_for(repo_key)
    records = []
    try:
        page = 1
        while page <= max_pages:
            data = req("GET", f"{api}/repos/{owner}/{name}/branches"
                              f"?per_page=100&page={page}", tok)
            if isinstance(data, dict):
                data = data.get("branches") or []
            if not isinstance(data, list) or not data:
                break
            for b in data:
                if isinstance(b, dict) and b.get("name"):
                    records.append({"name": b["name"],
                                    "protected": bool(b.get("protected", False))})
                elif isinstance(b, str) and b:
                    records.append({"name": b, "protected": False})
            if len(data) < 100:
                break
            page += 1
        else:
            return records, True
    except Exception as e:
        log.warning(f"work_branch: list branches failed repo={repo_key}: {e}")
        return None, False
    return records, False


def _fetch_remote_branch_names(repo_key: str, token: str = ""):
    """经 VCS 分支列表 API 拉取真实分支集合 + 默认分支；失败返回 (None, "")。

    分页上限 50 页（5000 分支）。超过上限仍满页时返回 None（不可核实），
    避免把「列表被截断」误判为「分支确实不存在」。
    """
    records, truncated = fetch_repo_branch_records(repo_key, token=token)
    if records is None:
        return None, ""
    if truncated:
        log.warning(f"work_branch: branch list truncated at {_MAX_BRANCH_PAGES} pages "
                    f"for repo={repo_key}; treat as unverifiable")
        return None, ""
    branches = {r["name"] for r in records if r.get("name")}
    default = _fetch_remote_default_branch(repo_key, token=token)
    return (branches or None), default


def list_repo_branches(repo_key: str, token: str = "", refresh: bool = False,
                       remote_only: bool = False):
    """返回 (branches: set|None, default_branch)。

    - 先读本地 bare mirror（无网络开销）；mirror 超过 TTL 时先 `fetch --prune`
      刷新，确保平台外新建/删除的分支可见，避免陈旧 refs 双向误判。
    - 无镜像或 remote_only=True 时穿透 VCS 分支列表 API（保存校验以此为权威）。
    - 建分支后的失效会强制下一次走远端。
    - 结果按 repo 缓存（成功 90s / 失败 30s）。branches=None 表示无法核实。
    """
    forced = remote_only or _consume_force_remote(repo_key)
    cached = _branch_list_cache_get(repo_key, refresh=refresh or forced)
    if cached is not None:
        return cached
    branches, default = None, ""
    if not forced:
        branches, default = _local_repo_branch_names(repo_key)
        if branches is not None and _mirror_is_stale(repo_key):
            if _refresh_local_mirror(repo_key, token=token):
                branches, default = _local_repo_branch_names(repo_key)
    if branches is None:
        branches, default = _fetch_remote_branch_names(repo_key, token=token)
    elif not default:
        default = _fetch_remote_default_branch(repo_key, token=token)
    _branch_list_cache_put(repo_key, branches, default)
    return branches, default


def refresh_repo_branch_mirror(repo_key: str, branch: str, token: str = "") -> bool:
    """建分支后增量刷新本地 bare mirror，避免 mirror-first 读取不到刚建的分支。"""
    branch = (branch or "").strip()
    base = repo_base_dir(repo_key)
    if not branch or not base or not os.path.isdir(base):
        return False
    r = _run_git(["-C", base, "fetch", "origin",
                  f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                 token=token, timeout=120)
    ok = r is not None and r.returncode == 0
    if not ok:
        log.warning(f"work_branch: refresh mirror branch '{branch}' failed repo={repo_key}: "
                    f"{(r.stderr if r else 'git exec failed')[:200]}")
    else:
        _mark_mirror_fresh(repo_key)
    return ok


def pick_default_branch(branches: set, default_branch: str = "") -> str:
    """选仓库默认分支：优先 API/mirror 报告的默认分支，其次 main/master，最后兜底。"""
    if default_branch and (not branches or default_branch in branches):
        return default_branch
    for cand in ("main", "master"):
        if cand in branches:
            return cand
    return sorted(branches)[0] if branches else (default_branch or "main")


def _classify_work_branch(repo_key: str, branch: str, token: str = "",
                          authoritative: bool = False):
    """统一分支判定：返回 (exists: bool|None, default_branch, branches)。

    - authoritative=True（保存校验）：直接以 VCS 远端分支列表为准，避免 mirror
      仍含平台外已删除分支而误放行。
    - authoritative=False（注入）：mirror 未命中目标分支时强制远端复核，避免
      平台外新建分支（如 weekly 发布流水线）被误判失效；mirror 超 TTL 已由
      list_repo_branches 自动 fetch --prune 刷新。
    exists=None 表示分支列表不可核实（无 token/网络失败/分页截断）。
    """
    branches, default = list_repo_branches(repo_key, token=token,
                                           remote_only=authoritative)
    if branches is not None and branch not in branches and not authoritative:
        remote_branches, remote_default = list_repo_branches(
            repo_key, token=token, refresh=True, remote_only=True)
        if remote_branches is None:
            # 远端不可核实 → 不把 mirror 未命中当作确定失效，保持原配置
            return None, default or remote_default, None
        branches, default = remote_branches, remote_default or default
    if branches is None:
        return None, default, None
    return (branch in branches), default, branches


def resolve_work_branch(repo_key: str, configured: str, token: str = "") -> tuple[str, str]:
    """注入/保存前校验 work_branch 是否存在，失效则回退仓库默认分支并告警。

    Returns (effective_branch, fallback_note)。fallback_note 为空表示未回退。
    - 仅用「分支列表显式比对」，不依赖 GitCode ref/tree 查询（ref 不存在会静默回退默认分支）。
    - 无法拉取分支列表（无 token/网络失败）时保持原配置，不误判回退。
    """
    cfg = (configured or "").strip()
    if not cfg:
        return "", ""
    exists, default, branches = _classify_work_branch(repo_key, cfg, token=token)
    if exists is None:
        log.warning(f"work_branch: cannot verify '{cfg}' for repo={repo_key} (branch list unavailable); keep configured value")
        return cfg, ""
    if exists:
        return cfg, ""
    fallback = pick_default_branch(branches, default)
    note = (f"configured work_branch '{cfg}' does not exist in repo {repo_key}; "
            f"fallback to default branch '{fallback}'")
    log.warning(note)
    return fallback, note


def validate_work_branch_exists(repo_key: str, branch: str, token: str = "") -> tuple[bool, str, str]:
    """保存前校验 work_branch 是否存在。

    Returns (ok, message, default_branch)。
    - ok=True 也包含「无法核实」（无 token/网络失败），避免离线阻塞保存。
    - ok=False 时 message 为可直接展示给用户的明确校验错误。
    """
    cfg = (branch or "").strip()
    if not cfg:
        return False, "工作分支不能为空", ""
    exists, default, branches = _classify_work_branch(
        repo_key, cfg, token=token, authoritative=True)
    if exists is None:
        log.warning(f"work_branch: cannot verify '{cfg}' for repo={repo_key} on save (branch list unavailable)")
        return True, "", default
    if exists:
        return True, "", default
    suggestion = pick_default_branch(branches, default)
    return False, (f"工作分支 '{cfg}' 在仓库中不存在，请从现有分支选择（默认分支：{suggestion}）"), suggestion


def read_effective_scene_work_branch(repo_key: str, issue_id: str = "") -> str:
    """读取场景 work_branch 并返回校验后的实际生效分支（三条注入路径统一入口）。"""
    configured = _read_scene_work_branch(repo_key, issue_id)
    effective, _ = resolve_work_branch(repo_key, configured)
    return effective


def _default_base_branch(base: str, token: str = "", repo_key: str = "", env_work_branch: str = "") -> str:
    """推断默认分支：优先 env work_branch → 场景 DB work_branch → main/master 兜底。

    env_work_branch 由 API 服务器从 MySQL 读取后通过 env_extra 注入（worker 端无 MySQL，
    本地 sqlite 的 managed_repos 为空表，_read_scene_work_branch 读不到）。
    """
    # 1. 优先读环境变量注入的 work_branch（API 服务器从 MySQL 读取后注入）
    _wb = env_work_branch
    # 2. 环境变量没有 → 读本地 DB（API 服务器直连 MySQL 时有效）
    if not _wb and repo_key:
        _wb = _read_scene_work_branch(repo_key)
    if _wb:
        # 校验该分支在远程存在（避免配置错误导致 worktree 失败）
        r = _run_git(["-C", base, "show-ref", "--verify", "--quiet",
                      f"refs/remotes/origin/{_wb}"], token=token)
        if r is not None and r.returncode == 0:
            return _wb
        log.warning(f"repogit: scene work_branch '{_wb}' not found in remote, fallback to default")
    # 3. 兜底
    for cand in ("main", "master"):
        r = _run_git(["-C", base, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{cand}"], token=token)
        if r is not None and r.returncode == 0:
            return cand
    return "main"


def _write_askpass(issue_dir: str, repo_key: str = "") -> str:
    """写 GIT_ASKPASS 辅助脚本：从环境变量读 token，token 不落盘。

    根据 repo_key 前缀选择正确的 token 环境变量 fallback 链：
    - github: BOT_GITHUB_TOKEN → USER_GITHUB_TOKEN → BOT_TOKEN → BOT_GITCODE_TOKEN
    - gitcode: BOT_GITCODE_TOKEN → USER_GITCODE_TOKEN → BOT_TOKEN → BOT_GITHUB_TOKEN

    R20.24.3: 路径 B 仓库 BOT_*_TOKEN 为空时 fallback 到 USER_*_TOKEN（用户 token），
    保证 git clone/push 在路径 B 仓库可用。
    R20.24.4: agent 同时收到 bot_token + user_token 两参数，分别注入 BOT_*_TOKEN / USER_*_TOKEN。

    chmod 后检查执行权限，失败则用 sh wrapper 兜底（不依赖 +x）。
    返回实际可执行的脚本路径（原脚本或 wrapper）。
    """
    # R20.24.3: fallback 链加入 USER_*_TOKEN（路径 B 仓库用户 token）
    if repo_key.startswith("github:"):
        token_var = "${BOT_GITHUB_TOKEN:-${USER_GITHUB_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}}"
    else:
        token_var = "${BOT_GITCODE_TOKEN:-${USER_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITHUB_TOKEN}}}"
    script = os.path.join(issue_dir, ".git-askpass.sh")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho "{token_var}"\n')
        try:
            os.chmod(script, 0o700)
        except Exception:
            pass
        # chmod 后检查执行权限，失败则写 sh wrapper 兜底
        if os.access(script, os.X_OK):
            return script
        wrapper = os.path.join(issue_dir, ".git-askpass-wrapper.sh")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nsh "{script}"\n')
        try:
            os.chmod(wrapper, 0o700)
        except Exception:
            pass
        log.warning(f"_write_askpass: chmod failed, using sh wrapper: {wrapper}")
        return wrapper
    except Exception:
        return ""


def askpass_path(issue_dir: str) -> str:
    """返回实际可执行的 askpass 脚本路径（wrapper 优先，否则原脚本）。"""
    wrapper = os.path.join(issue_dir, ".git-askpass-wrapper.sh")
    if os.path.isfile(wrapper):
        return wrapper
    return os.path.join(issue_dir, ".git-askpass.sh")


def _install_branch_guard_hook(issue_dir: str, repo_key: str, branch: str) -> None:
    """Issue #993：安装 worktree 级 pre-push 钩子（失败仅告警，不阻断建树）。"""
    try:
        from src.runtime.branch_guard import install_pre_push_hook
        if not install_pre_push_hook(issue_dir, repo_key):
            log.warning(f"repogit branch guard hook install failed {repo_key}:{branch}")
    except Exception as e:
        log.warning(f"repogit branch guard hook install error {repo_key}:{branch}: {e}")


def ensure_issue_worktree(repo_key: str, agent_type: str, issue_num, token: str = "", work_branch: str = "") -> str | None:
    """确保共享基线 + Issue 级 worktree（任务 cwd）。已存在则直接复用。

    work_branch 由调用方从 env_extra["GRAPE_WORK_BRANCH"] 传入（API 服务器从 MySQL 读取后注入）。
    """
    if not is_git_agent(agent_type):
        return None
    base = ensure_repo_base(repo_key, token)
    if not base:
        return None
    issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
    os.makedirs(os.path.dirname(issue_dir), exist_ok=True)
    branch = f"feat/issue-{issue_num}"
    if os.path.isdir(issue_dir) and (os.path.isdir(os.path.join(issue_dir, ".git"))
                                     or os.path.isfile(os.path.join(issue_dir, ".git"))):
        _write_askpass(issue_dir, repo_key)
        # Issue #752（M2）：复用 worktree（如复测重跑）时同样重置保真基线——
        # 基线=本次任务开始（清理前）的功能文件指纹，任务结束时比对（worktree_guard.verify_fidelity）
        try:
            from src.runtime.worktree_guard import save_baseline
            save_baseline(issue_dir, token=token)
        except Exception as _fg_e:
            log.warning(f"repogit fidelity baseline failed {repo_key}: {_fg_e}")
        # Issue #993：复用 worktree 同样安装 pre-push 守卫（覆盖 Agent 手工 push）
        _install_branch_guard_hook(issue_dir, repo_key, branch)
        return issue_dir
    with _lock_for(repo_key):
        try:
            base_branch = _default_base_branch(base, token, repo_key, env_work_branch=work_branch)
            br = _run_git(["-C", base, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], token=token)
            if br is not None and br.returncode == 0:
                r = _run_git(["-C", base, "worktree", "add", "--force", issue_dir, branch], token=token)
            else:
                r = _run_git(["-C", base, "worktree", "add", issue_dir, "-b", branch, f"origin/{base_branch}"], token=token)
            if r is None or r.returncode != 0:
                log.warning(f"repogit worktree add failed {repo_key}: {(r.stderr if r else 'git exec failed')[:300]}")
                return None
            _write_askpass(issue_dir, repo_key)
            # Issue #752（M2）：新建 worktree 后保存功能文件指纹基线（清理前快照）
            try:
                from src.runtime.worktree_guard import save_baseline
                save_baseline(issue_dir, token=token)
            except Exception as _fg_e:
                log.warning(f"repogit fidelity baseline failed {repo_key}: {_fg_e}")
            # Issue #993：worktree 建好后安装 pre-push 分支守卫（租约/merged/漂移）
            _install_branch_guard_hook(issue_dir, repo_key, branch)
            return issue_dir
        except Exception as e:
            log.warning(f"repogit worktree failed {repo_key}: {e}")
            return None


def cleanup_issue_worktree(repo_key: str, agent_type: str, issue_num):
    """移除 Issue 级 worktree + 删除目录（sweeper 用）。"""
    base = repo_base_dir(repo_key)
    issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
    if os.path.isdir(base):
        with _lock_for(repo_key):
            try:
                _run_git(["-C", base, "worktree", "remove", "--force", issue_dir])
            except Exception:
                pass
    shutil.rmtree(issue_dir, ignore_errors=True)


def remove_issue_worktree_if_pushed(repo_key: str, agent_type: str, issue_num, token: str = "") -> bool:
    """任务结束后安全删除 worktree：仅当本地 HEAD 已与远程分支一致（已 push）才删。

    用 `git ls-remote` 直连远程核对（不依赖 bare mirror 的 fetch，避免 fetch 滞后误判）。
    返回 True=已删除；False=保留（未 push / 远程无此分支 / 异常）。宁可残留，不可误删丢代码。
    """
    if not is_git_agent(agent_type):
        return False
    try:
        issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
        if not os.path.isdir(issue_dir):
            return False
        branch = f"feat/issue-{issue_num}"
        head = _run_git(["-C", issue_dir, "rev-parse", "HEAD"], token=token)
        if head is None or not (head.stdout or "").strip():
            return False
        local_head = head.stdout.strip()
        remote = _run_git_retry(["ls-remote", clone_url_for(repo_key), f"refs/heads/{branch}"], token=token)
        if remote is None:
            return False
        remote_out = (remote.stdout or "").strip()
        if not remote_out:
            return False  # 远程无此分支 = 从未 push，保留
        remote_head = remote_out.split()[0]
        if local_head != remote_head:
            return False  # 本地与远程不一致 = 有未推送提交，保留
        cleanup_issue_worktree(repo_key, agent_type, issue_num)
        return True
    except Exception as e:
        log.warning(f"repogit remove_issue_worktree_if_pushed failed {repo_key}: {e}")
        return False
