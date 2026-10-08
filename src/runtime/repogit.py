"""git 基建：共享 bare mirror 基线 + Issue 级 git worktree（多 Issue 并行）。

见 multi-issue-isolation-design.md §3.4.1。X 工作模型（用户拍板）：coding/qa/sre 使用本地 git 命令，
跨平台复用（GitHub 只需在 _HOSTS 加映射）。

- 基线：{WORKSPACE_DIR}/{owner}/{repo}/codebase/ 的 bare mirror（每机懒建立，fetch --prune 刷新）
- worktree：{WORKSPACE_DIR}/{owner}/{repo}/issues/{issue_num}/{agent_type}/（任务 cwd，按 Issue 统一隔离）
- 鉴权安全：clone 用 `-c http.extraheader` 内联注入（不写 config、不入日志）；push 用 GIT_ASKPASS
  辅助脚本读已注入的 `$BOT_GITCODE_TOKEN`（不落盘、随目录清理）。
"""
import base64, os, shutil, subprocess, threading
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
                r = _run_git(["init", "--bare", base], token=token)
                if r is None or r.returncode != 0:
                    log.warning(f"repogit init failed {repo_key}: {(r.stderr if r else 'git exec failed')[:300]}")
                    return None
                _run_git(["-C", base, "remote", "add", "origin", url], token=token)
            r = _run_git(["-C", base, "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"], token=token)
            if r is None or r.returncode != 0:
                log.warning(f"repogit fetch failed {repo_key}: {(r.stderr if r else 'git exec failed')[:300]}")
                return None
            return base
        except Exception as e:
            log.warning(f"repogit ensure_repo_base failed {repo_key}: {e}")
            return None


def ensure_repo_base_multi(linked_repos: list, token: str = "") -> dict:
    """批量初始化多个关联仓库的 bare mirror（Issue #242）。

    Args:
        linked_repos: [{repo_key, branch}] 列表
        token: git token（共用主仓库 token；关联仓库可能需要不同 token）
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
        base = ensure_repo_base(rk, token=token)
        result[rk] = base
    return result


def ensure_linked_repo_worktrees(linked_repos: list, agent_type: str, issue_num,
                                  token: str = "") -> list:
    """为关联仓库列表创建 Issue 级 worktree（Issue #242）。

    遍历 linked_repos，为每个 repo_key 调用 ensure_issue_worktree。
    每个仓库使用各自的 branch 配置（linked_repos 中的 branch 字段）。

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
        wt = ensure_issue_worktree(rk, agent_type, issue_num, token=token, work_branch=branch)
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

    根据 repo_key 前缀选择正确的 token 环境变量：
    - github: 优先 BOT_GITHUB_TOKEN，兜底 BOT_GITCODE_TOKEN
    - gitcode: BOT_GITCODE_TOKEN
    - 通用：BOT_TOKEN（executor.py 注入的别名）

    chmod 后检查执行权限，失败则用 sh wrapper 兜底（不依赖 +x）。
    返回实际可执行的脚本路径（原脚本或 wrapper）。
    """
    # 按 provider 选择 token 环境变量，fallback 链保证兼容
    if repo_key.startswith("github:"):
        token_var = "${BOT_GITHUB_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}"
    else:
        token_var = "${BOT_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITHUB_TOKEN}}"
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
    if os.path.isdir(issue_dir) and (os.path.isdir(os.path.join(issue_dir, ".git"))
                                     or os.path.isfile(os.path.join(issue_dir, ".git"))):
        _write_askpass(issue_dir, repo_key)
        return issue_dir
    branch = f"feat/issue-{issue_num}"
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
        remote = _run_git(["ls-remote", clone_url_for(repo_key), f"refs/heads/{branch}"], token=token)
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
