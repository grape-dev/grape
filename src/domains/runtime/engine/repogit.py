"""git 基建：共享 bare mirror 基线 + Issue 级 git worktree（多 Issue 并行）。

见 multi-issue-isolation-design.md §3.4.1。X 工作模型（用户拍板）：coding/qa/sre 使用本地 git 命令，
跨平台复用（GitHub 只需在 _HOSTS 加映射）。

- 基线：{WORKSPACE_DIR}/{owner}/{repo}/codebase/ 的 bare mirror（每机懒建立，fetch --prune 刷新）
- worktree：{WORKSPACE_DIR}/{owner}/{repo}/issues/{issue_num}/{agent_type}/（任务 cwd，按 Issue 统一隔离）
- 鉴权安全：clone 用 `-c http.extraheader` 内联注入（不写 config、不入日志）；push 用 GIT_ASKPASS
  辅助脚本读已注入的 `$BOT_GITCODE_TOKEN`（不落盘、随目录清理）。
"""

import base64, json, os, re, shutil, subprocess, threading, time

from src.kernel import config

from src.kernel.config import log

_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}

_GIT_AGENTS = ("coding", "qa", "sre")

# ---------------------------------------------------------------------------
# Issue #2514：coding 工作区与「当前 Issue + 当前基线」严格绑定
#
# 背景（#2506 实证，issue 6417）：注入的 `issues/2506/coding` 检出在 `feat/issue-1588`，
# 携带 3 个 commit 与 49 个未提交改动，Agent 只能现场另建 linked worktree 规避。
# 修复 = 复用前「归属标记 → 分支 → 脏树」三道闸门 + 基线分叉硬失败 +
# 任务终态清理 + 派发前巡检（详见 docs/issue-2514/plan.md）。
#
# 实现蓝本 PR #1176（#1939，closed 未合入）。其遗留 Critical（基线闸门 expected 被注入的
# 环境基线覆盖、回退失效，与 #2449 同源）在本版修正为：基线**只解析一次**并锁定，
# 闸门 expected 只用「已核实在远端存在」的 resolved 值，注入名仅作 requested 溯源字段。
# ---------------------------------------------------------------------------

# 归属标记文件（issue_dir 内）：缺失/不匹配 = 历史 Issue 残留，不参与复用
WORKSPACE_MARKER = ".grape-workspace.json"

# 脏检测白名单：平台物化的轻文件与凭据辅助脚本不是「他人改动」，不计入脏判定
#（口径与 worktree_guard._ARTIFACT_DIR_PREFIXES 一致；.git-askpass* 由 _write_askpass 写入）
_DIRT_IGNORE_NAMES = (WORKSPACE_MARKER, ".git-askpass.sh", ".git-askpass-wrapper.sh")
_DIRT_IGNORE_PREFIXES = (".skills/", ".project-info/", ".knowledge/")

# GRAPE_WORKSPACE_TTL_HOURS：巡检回收阈值（默认 24h）
# GRAPE_WORKSPACE_STRICT：基线分叉是否阻断任务（默认 1=开；0=降级 warn 继续复用）
# GRAPE_WORKSPACE_PATROL_INTERVAL：派发前巡检节流秒数（默认 3600，下限 60）

# 巡检/终结清理时不算「Issue 工作区」的兄弟目录（bare mirror / 归档产物）
_NON_WORKSPACE_DIRS = ("codebase", "artifacts")


def _workspace_ttl_hours() -> float:
    """巡检回收阈值（小时）：GRAPE_WORKSPACE_TTL_HOURS，缺省/非法 → 24。"""
    try:
        return float(os.environ.get("GRAPE_WORKSPACE_TTL_HOURS", "") or 24)
    except Exception:
        return 24.0


def _workspace_strict(override=None) -> bool:
    """STRICT 开关：任务 env 显式值 > worker 级 os.environ > 默认开。"""
    raw = override if override is not None else os.environ.get("GRAPE_WORKSPACE_STRICT", "")
    raw = str(raw).strip().lower()
    if not raw:
        return True  # 未配置 = 默认开
    return raw not in ("0", "false", "off", "no")


class WorkspaceBaselineError(Exception):
    """Issue #2514：HEAD 与基线分叉/不可达——预置阶段显式失败，阻断任务进入 Agent 执行。

    错误文本六要素可照抄定位：issue / path / expected_baseline / actual_head / ahead / behind
    （#1841 回退教训 3e71b256：绝不静默回退到别的基线，失败必须可见）。

    `expected_baseline` 恒为**已核实在远端存在**的解析后基线；`requested_baseline` 保留
    场景注入名，二者不等即说明本轮发生过基线回退（#2449 同源问题的可溯源位）。
    """

    def __init__(self, issue_number, path, expected_baseline, actual_head,
                 ahead, behind, reason="diverged", requested_baseline=""):
        self.issue_number = issue_number
        self.path = path
        self.expected_baseline = expected_baseline
        self.actual_head = actual_head
        self.ahead = ahead
        self.behind = behind
        self.reason = reason
        self.requested_baseline = requested_baseline or ""
        ahead_s = "unknown" if ahead is None else ahead
        behind_s = "unknown" if behind is None else behind
        msg = (f"workspace baseline mismatch: issue=#{issue_number} path={path} "
               f"expected_baseline={expected_baseline} actual_head={actual_head} "
               f"ahead={ahead_s} behind={behind_s} reason={reason}")
        if self.requested_baseline and self.requested_baseline != expected_baseline:
            msg += f" requested_baseline={self.requested_baseline}"
        super().__init__(msg)

    def to_dict(self) -> dict:
        """结构化字段（供事件/任务错误上报，字段名与 Spec 验收一致）。"""
        return {
            "issue": str(self.issue_number),
            "path": self.path,
            "expected_baseline": self.expected_baseline,
            "requested_baseline": self.requested_baseline,
            "actual_head": self.actual_head,
            "ahead": self.ahead,
            "behind": self.behind,
            "reason": self.reason,
        }


def _is_ignored_dirt(rel_path: str) -> bool:
    """路径是否命中脏检测白名单（平台轻文件 / askpass / 归属标记）。"""
    p = (rel_path or "").strip().strip('"')
    if not p:
        return True
    if p in _DIRT_IGNORE_NAMES:
        return True
    return any(p.startswith(prefix) for prefix in _DIRT_IGNORE_PREFIXES)


def _porcelain_entries(issue_dir: str, token: str = "", untracked_all: bool = False):
    """解析 `git status --porcelain=v1 -z` → [(状态字符, 路径)]；git 不可用 → None。

    -z 下 rename/copy 的「原路径」为紧随的独立字段，需跳过；untracked_all=True 时
    未跟踪目录展开为具体文件（证据存档需要逐文件内容）。
    """
    args = ["-C", issue_dir, "status", "--porcelain=v1", "-z"]
    if untracked_all:
        args.append("--untracked-files=all")
    r = _run_git(args, token=token)
    if r is None or r.returncode != 0:
        return None
    fields = (r.stdout or "").split("\0")
    entries = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry or len(entry) < 4:
            continue
        entries.append((entry[0], entry[3:]))
        if entry[0] in "RC" and i < len(fields):
            i += 1
    return entries


def _worktree_state(issue_dir: str, token: str = "") -> tuple:
    """(脏文件数, HEAD 分支, HEAD sha12)；git 不可用 → (-1, '', '')。"""
    entries = _porcelain_entries(issue_dir, token=token)
    if entries is not None:
        dirty = sum(1 for _flag, path in entries if not _is_ignored_dirt(path))
        branch, head = "", ""
        br = _run_git(["-C", issue_dir, "rev-parse", "--abbrev-ref", "HEAD"], token=token)
        if br is not None and br.returncode == 0:
            branch = (br.stdout or "").strip()
        sha = _run_git(["-C", issue_dir, "rev-parse", "HEAD"], token=token)
        if sha is not None and sha.returncode == 0:
            head = (sha.stdout or "").strip()[:12]
        return dirty, branch, head
    return -1, "", ""


def _marker_path(issue_dir: str) -> str:
    return os.path.join(issue_dir, WORKSPACE_MARKER)


def _read_workspace_marker(issue_dir: str) -> dict:
    """读归属标记；缺失/损坏 → {}（视为非本 Issue 工作区）。"""
    try:
        with open(_marker_path(issue_dir), "r", encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _write_workspace_marker(issue_dir: str, issue_num, agent_type: str,
                            baseline: str = "", task_id: str = "") -> None:
    """写入/刷新归属标记（issue_number/agent/baseline/task_id/created_at）。"""
    from datetime import datetime, timezone
    try:
        marker = _read_workspace_marker(issue_dir)
        marker.update({
            "issue_number": str(issue_num),
            "agent": agent_type,
            "baseline": baseline or "",
            "task_id": task_id or marker.get("task_id", ""),
            "created_at": marker.get("created_at") or datetime.now(timezone.utc).isoformat(),
        })
        with open(_marker_path(issue_dir), "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.warning(f"workspace marker write failed {issue_dir}: {e}")


def _exclude_marker_in_git(issue_dir: str, token: str = "") -> None:
    """把归属标记写进 per-worktree info/exclude——防 agent `add -A` 误提交、防 status 误报脏。"""
    try:
        r = _run_git(["-C", issue_dir, "rev-parse", "--git-path", "info/exclude"], token=token)
        if r is None or r.returncode != 0:
            return
        path = (r.stdout or "").strip()
        if not path:
            return
        if not os.path.isabs(path):
            path = os.path.join(issue_dir, path)
        raw = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        if WORKSPACE_MARKER in raw.splitlines():
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prefix = "\n" if (raw and not raw.endswith("\n")) else ""
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{prefix}{WORKSPACE_MARKER}\n")
    except Exception as e:
        # 只读 gitdir（#2245/#1891）→ 只记 warning，脏检测另有白名单兜底
        log.warning(f"workspace marker exclude failed {issue_dir}: {e}")


def _untracked_files(issue_dir: str, token: str = "", expand_dirs: bool = False) -> list:
    """白名单外的未跟踪文件清单（证据存档用）。

    expand_dirs=True 时未跟踪目录展开为具体文件（-uall），供逐文件存档内容。
    """
    entries = _porcelain_entries(issue_dir, token=token, untracked_all=expand_dirs) or []
    return [path for flag, path in entries
            if flag == "?" and not _is_ignored_dirt(path)]


def _archive_workspace_patch(issue_dir: str, repo_key: str, task_id: str = "",
                             token: str = "", suffix: str = "",
                             log_range: tuple | None = None) -> str:
    """工作区证据存档：脏树 diff + 未跟踪文件内容（+可选未推提交区间）→ artifacts/*.patch。

    移除/重建前保留证据（绝不丢改动）：`git diff HEAD --binary`（工作树+暂存 vs HEAD）
    + 白名单外 untracked 的**完整内容**（逐文件 `git diff --no-index --binary`，可
    `git apply` 还原）；log_range=(from, to) 时追加 `git log -p --binary` 区间
    （未 push 提交的完整补丁，含提交信息）。

    返回存档路径；**任一证据无法落盘 → 返回 ""**，调用方必须保留 worktree（不得在
    证据链不完整时删除工作区）。
    """
    from src.kernel.repo_key import workspace_repo_dir
    try:
        art_dir = os.path.join(workspace_repo_dir(repo_key), "artifacts")
        os.makedirs(art_dir, exist_ok=True)
        name = f"workspace-{task_id or 'unknown'}{suffix}.patch"
        out = os.path.join(art_dir, name)
        parts = []
        dirty, branch, head = _worktree_state(issue_dir, token)
        parts.append(f"# grape workspace archive\n# path={issue_dir}\n"
                     f"# branch={branch} head={head} dirty_files={max(dirty, 0)}\n\n")
        r = _run_git(["-C", issue_dir, "diff", "HEAD", "--binary"], token=token)
        if r is None:
            raise RuntimeError("git diff HEAD failed")
        if (r.stdout or "").strip():
            parts.append("# --- git diff HEAD (tracked/staged) ---\n" + r.stdout + "\n")
        if log_range:
            rl = _run_git(["-C", issue_dir, "log", "-p", "--binary",
                           f"{log_range[0]}..{log_range[1]}"], token=token)
            if rl is None or rl.returncode != 0:
                raise RuntimeError("git log -p failed")
            if (rl.stdout or "").strip():
                parts.append(f"# --- unpushed commits {log_range[0]}..{log_range[1]} ---\n"
                             + rl.stdout + "\n")
        untracked = _untracked_files(issue_dir, token=token, expand_dirs=True)
        if untracked:
            chunks, failed = [], []
            for rel in untracked:
                d = _run_git(["-C", issue_dir, "diff", "--no-index", "--binary",
                              "--", "/dev/null", rel], token=token)
                # --no-index 有差异时 returncode=1，属正常；其余视为存档失败
                if d is None or d.returncode not in (0, 1):
                    failed.append(rel)
                    continue
                if (d.stdout or "").strip():
                    chunks.append(d.stdout if d.stdout.endswith("\n") else d.stdout + "\n")
            if failed:
                raise RuntimeError(f"untracked content archive failed: {failed[:3]}")
            parts.append("# --- untracked file contents (git diff --no-index --binary) ---\n"
                         + "".join(chunks))
        with open(out, "w", encoding="utf-8") as f:
            f.write("".join(parts))
        log.warning(f"workspace archive written: {out} (dirty={max(dirty, 0)}, "
                    f"untracked={len(untracked)}, log_range={log_range})")
        return out
    except Exception as e:
        log.warning(f"workspace archive failed {issue_dir}: {e}")
        return ""


def _drop_fidelity_baseline(agent_dir: str) -> None:
    """删除 worktree 外的兄弟保真基线（#752 `<dir>.fidelity.json`）；失败只记 warning。"""
    p = f"{agent_dir.rstrip('/')}.fidelity.json"
    try:
        if os.path.isfile(p):
            os.remove(p)
    except Exception as e:
        log.warning(f"workspace fidelity baseline remove failed {p}: {e}")


def _refuse_reuse(issue_dir, base, repo_key, issue_num, agent_type, reason,
                  dirty_count, token: str = "", task_id: str = "") -> bool:
    """拒绝复用处置：存档 → worktree remove --force → prune。

    返回 True=已清除可重建；False=移除失败（#1891 gitdir 只读卷等），
    调用方降级为「保留原目录复用 + 显式 warn」。不做重试（只读卷重试无效）。
    调用方须已持有 `_repo_lock(base)`（threading.Lock 不可重入）。
    """
    log.warning(f"workspace.reuse_refused issue={issue_num} agent={agent_type} "
                f"path={issue_dir} reason={reason} dirty_files={max(dirty_count, 0)}")
    if not _archive_workspace_patch(issue_dir, repo_key, task_id=task_id, token=token,
                                    suffix="-reuse-refused"):
        log.warning(f"workspace.reuse_refused archive_failed keep worktree "
                    f"issue={issue_num} path={issue_dir}")
        return False
    _run_git(["-C", base, "worktree", "remove", "--force", issue_dir], token=token)
    _run_git(["-C", base, "worktree", "prune"], token=token)
    if os.path.isdir(issue_dir):
        shutil.rmtree(issue_dir, ignore_errors=True)
    if os.path.isdir(issue_dir):
        log.warning(f"workspace reuse refused but dir kept (remove failed, "
                    f"#1891 read-only gitdir?): {issue_dir}")
        return False
    return True


def _resolve_base_branch(base: str, token: str = "", repo_key: str = "",
                         env_work_branch: str = "") -> tuple:
    """解析并**锁定**本次工作区基线：#2514 / #2449 的核心口径。

    返回 (resolved, requested, fallback_reason)：
    - requested = 场景注入值（env `GRAPE_WORK_BRANCH` → 场景 DB），可能为空；
    - resolved = **已核实在 bare mirror 中存在 `refs/remotes/origin/<resolved>`** 的基线，
      requested 不可用（不存在/未配置）时回退默认分支（main → master → main 兜底），
      回退为显式可见事件 `workspace.baseline_fallback`；
    - fallback_reason 非空即说明发生过回退，闸门与错误文本据此可溯源。

    PR #1176 的 Critical（expected 被注入值直接覆盖、回退结果不参与判定）由此消除：
    调用方一律用 resolved 建树、写标记、做基线校验。
    """
    requested = (env_work_branch or "").strip()
    if not requested and repo_key:
        try:
            requested = (_read_scene_work_branch(repo_key) or "").strip()
        except Exception:
            requested = ""
    reason = ""
    if requested:
        r = _run_git(["-C", base, "show-ref", "--verify", "--quiet",
                      f"refs/remotes/origin/{requested}"], token=token)
        if r is not None and r.returncode == 0:
            return requested, requested, ""
        reason = "not_in_remote"
    else:
        reason = "no_requested_baseline"
    resolved = _default_base_branch(base, token, "", "")
    if requested:
        # 只有「注入了基线但不可用」才需要显式回退事件；未注入基线属正常默认选择（不刷日志）
        log.warning(f"workspace.baseline_fallback requested={requested} "
                    f"resolved={resolved} reason={reason}")
    return resolved, requested, reason


def _baseline_fail_or_warn(issue_dir, issue_num, agent_type, expected, head,
                           ahead, behind, reason, strict_override,
                           requested: str = "") -> None:
    """STRICT=on → 抛 WorkspaceBaselineError 阻断；off → 降级 warning 继续复用（错误仍可见）。"""
    err = WorkspaceBaselineError(issue_num, issue_dir, expected, head, ahead, behind,
                                 reason, requested_baseline=requested)
    if _workspace_strict(strict_override):
        log.error(f"workspace baseline blocked: {err}")
        raise err
    log.warning(f"workspace baseline mismatch (strict=off, reuse anyway): {err}")


def _validate_issue_baseline(issue_dir, issue_num, agent_type, expected,
                             token: str = "", requested: str = "",
                             strict_override=None) -> None:
    """#2514 闸门三：HEAD 与 origin/<expected> 的一致性显式校验。

    `expected` 必须是 `_resolve_base_branch` 返回的 resolved 基线（已在远端核实）。
    - ahead=0, behind=0 → 直通；
    - ahead=0, behind>0 → fast-forward 并记 `workspace.baseline_fast_forward`；
    - ahead>0 且 behind>0（真分叉）或 rev-list 失败（HEAD 不可达基线）
      → 结构化失败（STRICT=0 降级 warn 继续复用）。
    inspector（qa/sre 交付对齐）工作区 HEAD 会被对齐到交付 sha，不走基线闸门。
    """
    if not expected:
        return
    if is_inspector_agent(agent_type):
        log.info(f"workspace baseline gate skipped for inspector agent={agent_type} "
                 f"issue={issue_num} (head aligned to delivery)")
        return
    # 显式 fetch 刷新该基线 ref（失败仅告警：只读 gitdir #2245/#1891，用 mirror 已有 refs 判）
    _run_git_retry(["-C", issue_dir, "fetch", "origin", expected], token=token, timeout=120)
    r = _run_git(["-C", issue_dir, "rev-list", "--left-right", "--count",
                  f"HEAD...origin/{expected}"], token=token)
    head = "unknown"
    sha = _run_git(["-C", issue_dir, "rev-parse", "HEAD"], token=token)
    if sha is not None and sha.returncode == 0:
        head = (sha.stdout or "").strip()[:12]
    br = _run_git(["-C", issue_dir, "rev-parse", "--abbrev-ref", "HEAD"], token=token)
    if br is not None and br.returncode == 0:
        head = f"{(br.stdout or '').strip()}@{head}"
    if r is None or r.returncode != 0:
        _baseline_fail_or_warn(issue_dir, issue_num, agent_type, expected, head,
                               None, None, "head_unreachable", strict_override, requested)
        return
    try:
        counts = (r.stdout or "").split()
        ahead, behind = int(counts[0]), int(counts[1])
    except Exception:
        _baseline_fail_or_warn(issue_dir, issue_num, agent_type, expected, head,
                               None, None, "count_parse_failed", strict_override, requested)
        return
    if ahead == 0 and behind == 0:
        return
    if ahead == 0 and behind > 0:
        ff = _run_git(["-C", issue_dir, "merge", "--ff-only", f"origin/{expected}"],
                      token=token)
        if ff is None or ff.returncode != 0:
            _baseline_fail_or_warn(issue_dir, issue_num, agent_type, expected, head,
                                   ahead, behind, "ff_failed", strict_override, requested)
            return
        log.info(f"workspace.baseline_fast_forward issue={issue_num} agent={agent_type} "
                 f"path={issue_dir} baseline={expected} head={head} behind={behind}")
        return
    if ahead > 0 and behind > 0:
        _baseline_fail_or_warn(issue_dir, issue_num, agent_type, expected, head,
                               ahead, behind, "diverged", strict_override, requested)

_lock_guard = threading.Lock()

_repo_locks = {}

def _lock_for(repo_key: str) -> threading.Lock:
    with _lock_guard:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


def _repo_lock(base: str) -> threading.Lock:
    """仓库级互斥锁：统一以 bare mirror 物理路径为键。

    Issue #2514：建树/重建/终态清理/巡检必须互斥，键若一半用 repo_key 一半用目录
    路径就会各锁各的。这里一律用 `realpath(repo_base_dir)`，调用方（ensure/cleanup/
    patrol）取同一把锁。
    """
    return _lock_for(os.path.realpath(base))

def issue_num_from(issue_id: str) -> str:
    """"#5" / "5" → "5"；#0/空 → ''。"""
    s = (issue_id or "").strip().lstrip("#")
    return s if s and s != "0" else ""

def is_git_agent(agent_type: str) -> bool:
    """仅 coding/qa/sre 需要本地 git 工作树。"""
    return agent_type in _GIT_AGENTS

def repo_base_dir(repo_key: str) -> str:
    from src.kernel.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), "codebase")

def issue_dir_for(repo_key: str, agent_type: str, issue_num) -> str:
    from src.kernel.repo_key import workspace_repo_dir
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
        logger: 可选 logger（默认用 src.kernel.config.log）

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
    """按仓库平台卡解析 host 构造 clone URL（D4/G1 修复）。

    未知 provider（带前缀但无平台卡）fail-fast 抛错——替代旧版静默回退
    gitcode.com 的错误地址（S7 验收）。无前缀 repo_key 由 parse_repo_key
    默认 gitcode，行为不变。
    """
    from src.kernel.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    host = _host_for_provider(provider)
    return f"https://{host}/{owner}/{repo}.git"

def _basic_auth(token: str, scheme: str = "x_access_token") -> str:
    user = _AUTH_USER_BY_SCHEME.get(scheme, "x-access-token")
    return base64.b64encode(f"{user}:{token}".encode()).decode()

def _run_git(args: list, token: str = "", timeout: int = 900, scheme: str = "x_access_token"):
    """执行 git；token 经 `-c http.extraheader` 注入（不落盘、不进日志）。

    scheme 由平台卡 clone_auth 决定（gitcode/github=x-access-token 现状，
    gitlab=oauth2，gitee=basic_password）。
    """
    cmd = ["git"]
    if token:
        cmd += ["-c", f"http.extraheader=Authorization: Basic {_basic_auth(token, scheme)}"]
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
                   retries: int = 3, base_delay: float = 1.0, scheme: str = "x_access_token"):
    """执行 git；网络类错误有限次退避重试（默认 3 次：1s/2s/4s）。

    - 成功（returncode==0）立即返回；
    - 仅网络类错误重试；4xx（401/403/404）与非网络类立即返回；
    - 全部重试失败返回最后一次结果（调用方按原有 None/非 0 逻辑降级）。
    """
    delay = base_delay
    last = None
    for attempt in range(retries):
        last = _run_git(args, token=token, timeout=timeout, scheme=scheme)
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
    _scheme = _clone_auth_scheme_for(repo_key)
    with _repo_lock(base):
        try:
            if not os.path.isdir(base):
                os.makedirs(os.path.dirname(base), exist_ok=True)
                r = _run_git_retry(["init", "--bare", base], token=token, scheme=_scheme)
                if r is None or r.returncode != 0:
                    log.warning(f"repogit init failed {repo_key}: {(r.stderr if r else 'git exec failed')[:300]}")
                    return None
                _run_git_retry(["-C", base, "remote", "add", "origin", url], token=token, scheme=_scheme)
            r = _run_git_retry(["-C", base, "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"], token=token, scheme=_scheme)
            if r is None or r.returncode != 0:
                log.warning(f"repogit fetch failed {repo_key}: {(r.stderr if r else 'git exec failed')[:300]}")
                return None
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
        from src.kernel.repo import Repo
        return Repo(repo_key).bot_token
    except Exception as e:
        log.warning(f"linked repo token resolve failed for {repo_key}: {e}")
        return ""

def ensure_linked_repo_worktrees(linked_repos: list, agent_type: str, issue_num,
                                  token: str = "", env: dict = None,
                                  task_id: str = "") -> list:
    """为关联仓库列表创建 Issue 级 worktree（Issue #242）。

    遍历 linked_repos，为每个 repo_key 调用 ensure_issue_worktree。
    每个仓库使用各自的 branch 配置（linked_repos 中的 branch 字段）。

    R20.26.1: 每个关联仓库独立获取 token。
    Issue #466 断点二: token 解析 env（GRAPE_LINKED_REPO_TOKENS）优先，
    本地 Repo.bot_token 回退（env 由调用方传任务 env，缺省读 os.environ）。
    Issue #2514: 关联仓基线分叉按仓隔离降级——单仓 WorkspaceBaselineError 记
    `workspace.baseline_blocked_linked` 并跳过该仓，不连坐其它关联仓与主仓任务。

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
        try:
            wt = ensure_issue_worktree(rk, agent_type, issue_num, token=_repo_token,
                                       work_branch=branch, task_id=task_id)
        except WorkspaceBaselineError as _wb_err:
            log.error(f"workspace.baseline_blocked_linked repo={rk} issue={issue_num} "
                      f"agent={agent_type}: {_wb_err}")
            continue
        if wt:
            worktrees.append({"repo_key": rk, "branch": branch, "worktree_dir": wt})
        else:
            log.warning(f"repogit: linked repo worktree failed for {rk} issue={issue_num}")
    return worktrees

def _read_scene_work_branch(repo_key: str, issue_id: str = "") -> str:
    """读场景高级选项的 work_branch。

    优先读 issue_state.scene_id（Issue 级快照），空则回退 managed_repos.dev_scene →
    devops_pipeline_scene.advanced_options。这样流水线跑到一半时仓库场景切换不影响已在跑的 Issue。
    公共读取逻辑见 session_store.get_scene_work_branch（统一三处查询口径）。
    """
    from src.api.session_store import get_scene_work_branch
    return get_scene_work_branch(repo_key, issue_id=issue_id)


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
    # 基线口径要求 expected 已核实；此处两端均缺失时仅兜底（保留既有行为），显式告警
    log.warning(f"repogit: no verified default branch in {base} "
                f"(origin/main, origin/master missing), fallback unverified 'main'")
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
    # D4/G3：新平台（gitee 等）按平台卡 token_env 派生；存量 github/gitcode
    # 二分硬编码保持原样（S8 回归锁）。
    from src.kernel.repo_key import parse_repo_key
    _provider, _, _ = parse_repo_key(repo_key) if ":" in (repo_key or "") else ("gitcode", "", "")
    if repo_key.startswith("github:"):
        token_var = "${BOT_GITHUB_TOKEN:-${USER_GITHUB_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}}"
    elif repo_key.startswith("gitcode:") or ":" not in (repo_key or ""):
        token_var = "${BOT_GITCODE_TOKEN:-${USER_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITHUB_TOKEN}}}"
    else:
        # 平台卡驱动：本平台 BOT → USER → BOT_TOKEN 兜底
        _bot = f"BOT_{_provider.upper()}_TOKEN"
        _user = f"USER_{_provider.upper()}_TOKEN"
        token_var = "${" + _bot + ":-${" + _user + ":-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}}"
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
        from src.domains.runtime.engine.branch_guard import install_pre_push_hook
        if not install_pre_push_hook(issue_dir, repo_key):
            log.warning(f"repogit branch guard hook install failed {repo_key}:{branch}")
    except Exception as e:
        log.warning(f"repogit branch guard hook install error {repo_key}:{branch}: {e}")

def _align_inspector_delivery(issue_dir: str, base: str, issue_num, delivery_branch: str,
                              delivery_info=None, token: str = "") -> None:
    """#1954/#2514：检测方（qa）工作区按被检交付 sha 对齐（失败只告警，不阻断建树）。"""
    if not (delivery_branch or delivery_info is not None):
        return
    try:
        sha, _src = resolve_delivery_sha(base, delivery_branch, token=token)
        if not sha:
            log.warning(f"repogit delivery sha unresolved {issue_dir} "
                        f"delivery_branch={delivery_branch} (reuse as-is)")
            _record_delivery_info(delivery_info, _delivery_info(reason="no_delivery_sha"))
            return
        align = align_worktree_to_delivery(issue_dir, sha, token=token)
        _record_delivery_info(delivery_info, _delivery_info(
            sha=sha, aligned=bool(align.get("aligned")),
            old_sha=align.get("old_sha") or "", new_sha=align.get("new_sha") or "",
            reason=align.get("reason") or ""))
    except Exception as e:
        log.warning(f"repogit delivery align failed {issue_dir}: {e}")


def ensure_issue_worktree(repo_key: str, agent_type: str, issue_num, token: str = "",
                          work_branch: str = "", task_id: str = "",
                          strict_override=None, delivery_branch: str = "",
                          delivery_info=None) -> str | None:
    """确保共享基线 + Issue 级 worktree（任务 cwd）。已存在则**过闸门后**复用。

    work_branch 由调用方从 env_extra["GRAPE_WORK_BRANCH"] 传入（API 服务器从 MySQL 读取后注入）。

    Issue #2514 复用三道闸门（标记 → 分支 → 脏），任一命中拒绝复用并隔离重建：
    - stale_marker：归属标记缺失/与本 Issue 不符（历史 Issue 残留，#2506/#1931 的形态）；
    - wrong_branch：HEAD 不在 feat/issue-{N} 上（inspector 的 detached HEAD 放行）；
    - dirty：git status --porcelain 非空（平台轻文件/askpass 白名单除外）。
    拒绝处置 = 证据存档 patch → worktree remove --force → prune → 重建干净工作区
    （日志 `workspace.reuse_refused reason=... dirty_files=N`）。

    随后闸门三（基线校验）：`_resolve_base_branch` 锁定的 resolved 基线与 HEAD 比对，
    分叉/不可达抛 WorkspaceBaselineError（STRICT=0 降级 warn）。基线错误**不**在此吞掉。
    """
    if not is_git_agent(agent_type):
        return None
    base = ensure_repo_base(repo_key, token)
    if not base:
        return None
    issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
    os.makedirs(os.path.dirname(issue_dir), exist_ok=True)
    branch = f"feat/issue-{issue_num}"
    # #2514：基线只解析一次并贯穿全函数（标记 baseline / 新建起点 / 闸门同一口径）
    base_branch, requested_branch, _fb_reason = _resolve_base_branch(
        base, token, repo_key, env_work_branch=work_branch)
    with _repo_lock(base):
        try:
            _has_git = os.path.isdir(issue_dir) and (
                os.path.isdir(os.path.join(issue_dir, ".git"))
                or os.path.isfile(os.path.join(issue_dir, ".git")))
            reuse = False
            # 拒绝复用但移除失败 → 保留的是「外来脏树」，不得重写归属标记（跳过标记落盘）
            foreign_kept = False
            if _has_git:
                dirty, cur_branch, _cur_head = _worktree_state(issue_dir, token=token)
                marker = _read_workspace_marker(issue_dir)
                reason = ""
                if (not marker
                        or str(marker.get("issue_number", "")) != str(issue_num)
                        or str(marker.get("agent", "")) != str(agent_type)):
                    reason = "stale_marker"
                elif cur_branch and cur_branch != branch and not (
                        is_inspector_agent(agent_type) and cur_branch == "HEAD"):
                    reason = "wrong_branch"
                elif dirty > 0:
                    reason = "dirty"
                elif dirty < 0:
                    # git 状态不可读（#2245/#1891 只读 gitdir）→ 不据此拒绝，
                    # 复用并由后续基线闸门兜底
                    log.warning(f"workspace.reuse_status_unreadable issue={issue_num} "
                                f"agent={agent_type} path={issue_dir} (git status failed)")
                if reason:
                    # 拒绝复用 = 存档 + 移除 + 重建；移除失败（#1891 只读 gitdir）才降级为复用
                    if not _refuse_reuse(issue_dir, base, repo_key, issue_num,
                                         agent_type, reason, dirty,
                                         token=token, task_id=task_id):
                        reuse = True
                        foreign_kept = True
                else:
                    log.info(f"workspace.reuse_ok issue={issue_num} agent={agent_type} "
                             f"path={issue_dir} baseline={base_branch}")
                    reuse = True
            if not reuse:
                br = _run_git(["-C", base, "show-ref", "--verify", "--quiet",
                               f"refs/heads/{branch}"], token=token)
                if br is not None and br.returncode == 0:
                    r = _run_git(["-C", base, "worktree", "add", "--force", issue_dir,
                                  branch], token=token)
                else:
                    r = _run_git(["-C", base, "worktree", "add", issue_dir, "-b", branch,
                                  f"origin/{base_branch}"], token=token)
                if r is None or r.returncode != 0:
                    log.warning(f"repogit worktree add failed {repo_key}: "
                                f"{(r.stderr if r else 'git exec failed')[:300]}")
                    return None
            _write_askpass(issue_dir, repo_key)
            # Issue #752（M2）：新建/复用 worktree 都重置保真基线（任务结束时比对）
            try:
                from src.domains.runtime.engine.worktree_guard import save_baseline
                save_baseline(issue_dir, token=token)
            except Exception as _fg_e:
                log.warning(f"repogit fidelity baseline failed {repo_key}: {_fg_e}")
            # Issue #993：worktree 建好后安装 pre-push 分支守卫（租约/merged/漂移）
            _install_branch_guard_hook(issue_dir, repo_key, branch)
            # #2514：归属标记 + git exclude（防 add -A 误提交 / status 误报脏）
            if foreign_kept:
                # 外来脏树（拒绝复用但移除失败）保持无归属状态，避免重标为本 Issue
                log.warning(f"workspace.reuse_refused keep_foreign_unmarked "
                            f"issue={issue_num} agent={agent_type} path={issue_dir}")
            else:
                _write_workspace_marker(issue_dir, issue_num, agent_type,
                                        baseline=base_branch, task_id=task_id)
                _exclude_marker_in_git(issue_dir, token=token)
            # #1954：inspector 工作区按交付提交对齐（detached），对齐后不走分支基线闸门
            _align_inspector_delivery(issue_dir, base, issue_num, delivery_branch,
                                      delivery_info=delivery_info, token=token)
            # #2514 闸门三：HEAD 与 resolved 基线显式校验（分叉抛错阻断）
            _validate_issue_baseline(issue_dir, issue_num, agent_type, base_branch,
                                     token=token, requested=requested_branch,
                                     strict_override=strict_override)
            return issue_dir
        except WorkspaceBaselineError:
            raise
        except Exception as e:
            log.warning(f"repogit worktree failed {repo_key}: {e}")
            return None


# Issue #2041：归档产物超时（具名常量，rationale 见 docs/issue-2041/plan.md「归档 bundle 取值」）。
# bundle 只打包 HEAD 链（不含 --all 的其它分支/基线），体积≈本分支历史；取默认 900s 的
# 2 倍覆盖大仓冷缓存打包，同时不超过 sweeper 15min 周期的 2 倍，避免单 issue 长期
# 占用串行 sweep 线程（超时 → 归档失败 → 保留现场，下轮重试）。
_BUNDLE_CREATE_TIMEOUT = 1800
# 从本地 bundle fetch 无网络依赖，600s 足够；超出即视为异常（失败 → 拒绝删除）。
_BUNDLE_FETCH_TIMEOUT = 600


def bundle_create(wt_dir: str, bundle_path: str, token: str = "") -> tuple:
    """Issue #2041：`git bundle create <path> HEAD` 封装（提交链归档）。

    只打包 HEAD 可达提交：linked worktree 与主仓共享 refs，`--all` 会把其它 issue
    分支与基线一并打包（归档体积≈整仓、sweeper 长时间阻塞），而恢复路径只需要
    `git fetch <bundle> && git reset --hard FETCH_HEAD` 对应的 HEAD 链。

    返回 (ok, err)；token 经 `-c http.extraheader` 内联注入（不落盘、不进日志）。
    """
    r = _run_git(["-C", wt_dir, "bundle", "create", bundle_path, "HEAD"],
                 token=token, timeout=_BUNDLE_CREATE_TIMEOUT)
    if r is None:
        return False, "git exec failed"
    if r.returncode != 0:
        return False, (r.stderr or "")[:300]
    return True, ""


def bundle_verify(wt_dir: str, bundle_path: str, token: str = "") -> bool:
    """Issue #2041：`git bundle verify` 封装——在 worktree 上下文校验 bundle
    可用（含 prerequisites 满足），归档成功与否以此为准。"""
    r = _run_git(["-C", wt_dir, "bundle", "verify", bundle_path], token=token)
    return r is not None and r.returncode == 0


def fetch_from_bundle(wt_dir: str, bundle_path: str, token: str = "") -> tuple:
    """Issue #2041：从 bundle 恢复——`git fetch <bundle>`（FETCH_HEAD 指向 bundle
    的 HEAD，随后可 `git reset --hard FETCH_HEAD` 还原提交内容）。"""
    r = _run_git(["-C", wt_dir, "fetch", bundle_path], token=token,
                 timeout=_BUNDLE_FETCH_TIMEOUT)
    if r is None:
        return False, "git exec failed"
    if r.returncode != 0:
        return False, (r.stderr or "")[:300]
    return True, ""


def cleanup_issue_worktree(repo_key: str, agent_type: str, issue_num,
                           token: str = "", reason: str = "",
                           task_id: str = "") -> bool:
    """移除 Issue 级 worktree + 删除目录（sweeper 用）。

    Issue #2041：删除/重置前过保全守卫——存在未推送提交或白名单外的未提交/
    未跟踪改动时先归档（bundle + 补丁 + untracked 快照 + 机器可读记录，
    见 worktree_guard.archive_worktree_before_reset），归档成功才放行删除；
    归档失败或守卫异常拒绝删除（宁可残留，不可丢改动）。

    QA N1：非 git 残留目录（无 .git，如 `git worktree add` 失败/中断留下的普通
    目录）直接跳过归档守卫——其中没有版本库内容可丢，而 `git status` 在非仓库上
    必然失败（exit 128）→ scan_failed/risk → 归档 ok=False → 恒拒绝删除，
    使 done-issue 清扫路径对这类目录永不回收、只能等僵尸 TTL 兜底。
    判据与 worktree_guard.guard_issue_workdir_removal 一致（无 .git 直接放行）。

    Issue #2514：另一条证据链（workspace-<task_id>.patch）由调用方
    （remove_issue_worktree_if_pushed）先行落盘；本函数保留「移除目录」语义并
    顺带清掉 worktree 外的保真基线兄弟文件。`token` 只用于本地 remove，
    `reason`/`task_id` 进结构化日志便于追溯清理来源。

    返回 True=已删除；False=保留（守卫拒绝/未删除）。
    """
    """
    issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
    _git_marker = os.path.join(issue_dir, ".git")
    _has_git = os.path.isfile(_git_marker) or os.path.isdir(_git_marker)
    if os.path.isdir(issue_dir) and not _has_git and is_git_agent(agent_type):
        # N1（QA 第2轮）：非 git 残留（无 .git，worktree add 失败/中断的残片）——
        # 没有提交可丢，跳过归档守卫直接回收（口径与 guard_issue_workdir_removal
        # 一致）。单条 info 日志保留删除可观测性（QA 第5/6轮 I-i）。
        log.info(f"repogit: non-git leftover, remove directly "
                 f"{repo_key} issue={issue_num} agent={agent_type} reason={reason or 'cleanup'}")
    elif _has_git and is_git_agent(agent_type):
        try:
            from src.domains.runtime.engine.worktree_guard import archive_worktree_before_reset
            _res = archive_worktree_before_reset(
                issue_dir, repo_key, issue_num, agent_type,
                token=token, base_branch=base_branch,
                reason=reason or "cleanup", task_id=task_id)
            if _res.get("risk") and not _res.get("ok"):
                log.warning(f"repogit: worktree cleanup refused (archive failed) "
                            f"{repo_key} issue={issue_num} agent={agent_type}: "
                            f"{_res.get('error', '')}")
                return False
        except Exception as _ge:
            log.warning(f"repogit: reset guard failed, refuse cleanup "
                        f"{repo_key} issue={issue_num}: {_ge}")
            return False
    base = repo_base_dir(repo_key)
    log.info(f"workspace.cleanup issue={issue_num} agent={agent_type} path={issue_dir} "
             f"reason={reason or 'unspecified'} task_id={task_id or '-'}")
    if os.path.isdir(base):
        with _repo_lock(base):
            try:
                _run_git(["-C", base, "worktree", "remove", "--force", issue_dir],
                         token=token)
            except Exception:
                pass
    shutil.rmtree(issue_dir, ignore_errors=True)
    _drop_fidelity_baseline(issue_dir)
    return True

def remove_issue_worktree_if_pushed(repo_key: str, agent_type: str, issue_num, token: str = "",
                                    task_id: str = "") -> bool:
    """任务终态清理 Issue 级 worktree（Issue #2514 收紧语义）。

    旧语义 = 仅当本地 HEAD 已与远程分支一致（已 push）才删，导致失败任务/未 push 轮次的
    工作区永久残留、跨 Issue 累积。新语义 = 终态必清，但**证据先存档**：

    用 `git ls-remote` 直连远程核对（不依赖 bare mirror 的 fetch，避免 fetch 滞后误判）。
    终态必清，但证据先落：
    - 脏树 → `<repo>/artifacts/workspace-<task_id>.patch`（`git diff HEAD --binary`
      + 白名单外 untracked 文件完整内容）；
    - 未推送提交 → 追加 `log -p --binary <remote_head>..<HEAD>` 区间存档（含提交信息）；
    - 删除动作本身仍过 Issue #2041 保全守卫（cleanup 内部归档），守卫拒绝则保留现场；
    - 清理失败只记日志，不阻断终态上报（调用方 _cleanup_task_workdir / _cleanup_direct_workdir）。

    返回 True=已删除；False=保留（网络不可核对 / 守卫拒绝 / 异常）。
    """
    if not is_git_agent(agent_type):
        return False
    try:
        issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
        if not os.path.isdir(issue_dir):
            return False
        branch = f"feat/issue-{issue_num}"
        # Issue #2514：脏树先存档（证据一次落全）；存档失败 → 保留工作区，不删
        dirty, _cur_branch, _cur_head = _worktree_state(issue_dir, token=token)
        if dirty > 0:
            if not _archive_workspace_patch(issue_dir, repo_key, task_id=task_id,
                                            token=token):
                log.warning(f"workspace terminal cleanup deferred (archive failed) "
                            f"{repo_key} issue={issue_num} path={issue_dir}")
                return False
        head = _run_git(["-C", issue_dir, "rev-parse", "HEAD"], token=token)
        if head is None or not (head.stdout or "").strip():
            return False
        local_head = head.stdout.strip()
        remote = _run_git_retry(["ls-remote", clone_url_for(repo_key), f"refs/heads/{branch}"], token=token)
        if remote is None or remote.returncode != 0:
            log.warning(f"workspace terminal cleanup deferred (remote unreachable) "
                        f"{repo_key} issue={issue_num} path={issue_dir} "
                        f"err={(remote.stderr or '')[:200] if remote is not None else 'git exec failed'}")
            return False  # 网络不可核对：保留（存档已落），待派发前巡检/下轮回收
        remote_out = (remote.stdout or "").strip()
        if remote_out:
            remote_head = remote_out.split()[0]
            if local_head != remote_head:
                # 未推送提交：区间 log -p 存档（含提交信息）后再移除，不丢码
            if local_head != remote_head:
                # 未推送提交：区间 log -p 存档（含提交信息）后再移除，不丢码
                if not _archive_workspace_patch(
                        issue_dir, repo_key, task_id=task_id, token=token,
                        suffix="-unpushed", log_range=(remote_head, local_head)):
                    log.warning(f"workspace terminal cleanup deferred (unpushed archive "
                                f"failed) {repo_key} issue={issue_num} path={issue_dir}")
                    return False
        # Issue #2041：删除动作过保全守卫（cleanup 内部归档），拒绝则保留
        return cleanup_issue_worktree(repo_key, agent_type, issue_num, token=token,
                                      reason="task_terminal", task_id=task_id)
    except Exception as e:
        log.warning(f"repogit remove_issue_worktree_if_pushed failed {repo_key}: {e}")
        return False


def patrol_workspaces(active_dirs=None, ttl_hours=None, now=None) -> dict:
    """Issue #2514：workspace 巡检——worktree 对账 + prune + 跨 Issue 残留回收。

    worker 启动时与派发前（节流）调用：
    - 逐 repo `git worktree list --porcelain` 对账 + `git worktree prune` 失效条目；
    - 回收「无活跃任务 + 目录超 GRAPE_WORKSPACE_TTL_HOURS（默认 24）」的带标记 Issue 目录
      （只回收带 .grape-workspace.json 标记的目录，防误删人工 worktree）；
    - 无 .git 且为空的目录（跨 Issue 空残留形态）归类 orphan 一并回收；
    - 无标记的非空目录只计 orphan、保留（宁可残留，不可误删）；
    - 在跑任务的 cwd 通过 active_dirs 白名单保护（in-flight 工作树不误删）。
    返回 {"removed": N, "kept": N, "orphan": N}；日志 `workspace.patrol removed= kept= orphan=`。
    """
    from src.kernel.config import WORKSPACE_DIR
    active = set(active_dirs or [])
    _active_real = {os.path.realpath(a) for a in active if a}
    ttl = float(ttl_hours if ttl_hours is not None else _workspace_ttl_hours()) * 3600.0
    now = time.time() if now is None else float(now)
    stats = {"removed": 0, "kept": 0, "orphan": 0}
    ws = os.path.abspath(WORKSPACE_DIR or "")
    if not ws or not os.path.isdir(ws):
        return stats

    def _scan_repo(repo_dir: str, base: str | None, repo_key: str = "") -> None:
        issues_root = os.path.join(repo_dir, "issues")
        # Issue #2514：与 ensure/cleanup 同一把仓库锁（键 = bare mirror 物理路径），
        # 避免巡检的 remove/prune 与建树/重建各锁各的、无互斥。
        _lock_base = base or repo_dir
        if base and os.path.isdir(base):
            with _repo_lock(_lock_base):
                _run_git(["-C", base, "worktree", "prune"])
        if not os.path.isdir(issues_root):
            return
        for num in os.listdir(issues_root):
            issue_dir = os.path.join(issues_root, num)
            if not os.path.isdir(issue_dir):
                continue
            for agent in os.listdir(issue_dir):
                if agent not in _GIT_AGENTS:
                    continue  # 非 git agent 目录（无工作树语义）不动
                agent_dir = os.path.join(issue_dir, agent)
                if not os.path.isdir(agent_dir):
                    continue
                if os.path.realpath(agent_dir) in _active_real:
                    stats["kept"] += 1
                    continue
                try:
                    age = now - os.path.getmtime(agent_dir)
                except Exception:
                    age = 0
                if age <= ttl:
                    stats["kept"] += 1
                    continue
                marker = _read_workspace_marker(agent_dir)
                has_git = os.path.exists(os.path.join(agent_dir, ".git"))
                if marker:
                    # 带归属标记 → 超期且无活跃任务 → 回收（脏树先存档再删，证据不丢）
                    dirty, _b, _h = _worktree_state(agent_dir)
                    if dirty < 0:
                        # 状态不可读 → 无法确认可安全删除，保留（宁可残留）
                        stats["kept"] += 1
                        continue
                    if dirty > 0 and not _archive_workspace_patch(
                            agent_dir, repo_key, token="", suffix="-patrol"):
                        # 证据存档失败 → 不删（保留 worktree 待人工处理）
                        stats["kept"] += 1
                        continue
                    if base and has_git:
                        with _repo_lock(_lock_base):
                            _run_git(["-C", base, "worktree", "remove", "--force", agent_dir])
                    shutil.rmtree(agent_dir, ignore_errors=True)
                    _drop_fidelity_baseline(agent_dir)
                    if os.path.isdir(agent_dir):
                        stats["kept"] += 1  # 移除失败（#1891 只读卷）→ 保留计数
                    else:
                        stats["removed"] += 1
                elif not has_git and not os.listdir(agent_dir):
                    # 跨 Issue 空目录残留（无 .git、无内容、超 TTL）→ orphan 回收
                    shutil.rmtree(agent_dir, ignore_errors=True)
                    stats["orphan"] += 1
                else:
                    stats["orphan"] += 1  # 无标记非空目录：计 orphan、保留（人工 worktree 防误删）
        # 清理空的 issue 目录壳（worktree 回收后的空壳，避免目录树无限增长）
        try:
            for num in os.listdir(issues_root):
                issue_dir = os.path.join(issues_root, num)
                if os.path.isdir(issue_dir) and not os.listdir(issue_dir):
                    shutil.rmtree(issue_dir, ignore_errors=True)
        except Exception as e:
            log.warning(f"workspace patrol issue-dir sweep failed {issues_root}: {e}")

    try:
        providers = ("gitcode", "github")
        for d1 in os.listdir(ws):
            p1 = os.path.join(ws, d1)
            if not os.path.isdir(p1):
                continue
            for d2 in os.listdir(p1):
                p2 = os.path.join(p1, d2)
                if not os.path.isdir(p2):
                    continue
                if os.path.isdir(os.path.join(p2, "issues")):
                    # 旧结构 {WS}/{owner}/{repo}/issues/
                    _scan_repo(p2, os.path.join(p2, "codebase"), f"gitcode:{d1}/{d2}")
                elif d1 in providers:
                    # 新结构 {WS}/{provider}/{owner}/{repo}/issues/
                    for d3 in os.listdir(p2):
                        p3 = os.path.join(p2, d3)
                        if os.path.isdir(p3) and os.path.isdir(os.path.join(p3, "issues")):
                            _scan_repo(p3, os.path.join(p3, "codebase"), f"{d1}:{d2}/{d3}")
    except Exception as e:
        log.warning(f"workspace patrol scan failed: {e}")
    log.info(f"workspace.patrol removed={stats['removed']} kept={stats['kept']} "
             f"orphan={stats['orphan']}")
    return stats

from src import config

def _host_for_provider(provider: str) -> str:
    host = _HOSTS.get(provider)
    if host:
        return host
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider)
    except Exception:
        card = None
    if card is not None:
        return card.host
    raise ValueError(f"unknown vcs provider {provider!r}: no platform card "
                     f"(clone URL 拒绝静默回退，S7 fail-fast)")

def _clone_auth_scheme_for(repo_key: str) -> str:
    """按平台卡取 git 传输鉴权 scheme（D4/G2）。默认 x-access-token（现状）。"""
    from src.kernel.repo_key import parse_repo_key
    provider, _, _ = parse_repo_key(repo_key)
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider)
        if card is not None:
            return card.clone_auth
    except Exception:
        pass
    return "x_access_token"

# scheme → Basic auth 的用户名段（G2：gitlab=oauth2:，gitee=空用户名+token 作密码，
# gitcode/github=x-access-token 现状不变）
_AUTH_USER_BY_SCHEME = {
    "x_access_token": "x-access-token",
    "oauth2": "oauth2",
    "basic_password": "",
}


# --- Issue #1954（arch 基线移植）：检测方交付对齐与取证上下文解析 ---
_INSPECTOR_AGENTS = ("qa",)

def is_inspector_agent(agent_type: str) -> bool:
    """检测方（inspector）角色：需要按被检交付提交对齐工作区。"""
    return agent_type in _INSPECTOR_AGENTS


def worktree_head_sha(wt_dir: str, token: str = "") -> str:
    """读 worktree 当前 HEAD sha；不可读 → 空串（调用方降级）。"""
    if not wt_dir:
        return ""
    r = _run_git(["-C", wt_dir, "rev-parse", "--verify", "--quiet", "HEAD"], token=token)
    if r is None or r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def resolve_delivery_sha(base: str, branch: str, token: str = "",
                         delivery_ref: str = "", delivery_sha: str = "") -> tuple[str, str]:
    """解析被检交付提交 sha（Issue #1954）。

    优先级：`origin/{branch}` 远端最新 → 任务上下文显式 delivery_sha → delivery_ref。
    返回 (sha, source)；无法解析 → ("", "")。只读操作，失败不抛异常。
    """
    candidates = []
    if branch:
        candidates.append((f"origin/{branch}", f"origin/{branch}"))
    if delivery_sha:
        candidates.append((delivery_sha, "delivery_sha"))
    if delivery_ref:
        candidates.append((delivery_ref, "delivery_ref"))
    for rev, label in candidates:
        r = _run_git(["-C", base, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                     token=token)
        if r is not None and r.returncode == 0:
            sha = (r.stdout or "").strip()
            if sha:
                return sha, label
    return "", ""


def _delivery_info(sha: str = "", aligned: bool = False, old_sha: str = "",
                   new_sha: str = "", reason: str = "") -> dict:
    """单点装配交付对齐结果（保证 delivery_info 写回协议一致）。"""
    return {"sha": sha, "aligned": aligned, "old_sha": old_sha,
            "new_sha": new_sha, "reason": reason}


def _record_delivery_info(target: dict | None, info: dict) -> None:
    """把对齐结果写回调用方传入的 delivery_info（可选）。"""
    if isinstance(target, dict):
        target.update(info)


def align_worktree_to_delivery(wt_dir: str, delivery_sha: str, token: str = "") -> dict:
    """复用 worktree 时受控对齐 HEAD 到被检交付 sha（Issue #1954）。

    先查 dirty（含未跟踪文件）：有未提交/未跟踪残留仅告警跳过（不覆盖检测方内容）；
    干净则以 `checkout --detach` 对齐（detach 不动其它 worktree 共享的分支 ref）。
    返回 {"aligned": bool, "old_sha": str, "new_sha": str, "reason": str}。
    """
    out = {"aligned": False, "old_sha": "", "new_sha": "", "reason": ""}
    if not wt_dir or not delivery_sha:
        out["reason"] = "no_delivery_sha"
        return out
    old_sha = worktree_head_sha(wt_dir, token=token)
    out["old_sha"] = old_sha
    if old_sha == delivery_sha:
        out["aligned"] = True
        out["new_sha"] = old_sha
        out["reason"] = "already_aligned"
        return out
    st = _run_git(["-C", wt_dir, "status", "--porcelain", "--untracked-files=normal", "--",
                   ".", ":(exclude).git-askpass.sh",
                   ":(exclude).git-askpass-wrapper.sh"], token=token)
    if st is None or st.returncode != 0:
        out["reason"] = "status_failed"
        return out
    if (st.stdout or "").strip():
        out["reason"] = "dirty_skip"
        log.warning(
            f"repogit: worktree {wt_dir} dirty (tracked/untracked), skip delivery alignment "
            f"old={old_sha[:12] if old_sha else '?'} target={delivery_sha[:12]}")
        return out
    r = _run_git(["-C", wt_dir, "checkout", "--detach", delivery_sha], token=token)
    if r is None or r.returncode != 0:
        out["reason"] = "checkout_failed"
        return out
    out["aligned"] = True
    out["new_sha"] = worktree_head_sha(wt_dir, token=token)
    out["reason"] = "aligned"
    log.info(
        f"repogit: worktree {wt_dir} aligned to delivery "
        f"old={(old_sha or '?')[:12]} new={(out['new_sha'] or '?')[:12]}")
    return out


