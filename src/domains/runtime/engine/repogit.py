"""git 基建：共享 bare mirror 基线 + Issue 级 git worktree（多 Issue 并行）。

见 multi-issue-isolation-design.md §3.4.1。X 工作模型（用户拍板）：coding/qa/sre 使用本地 git 命令，
跨平台复用（GitHub 只需在 _HOSTS 加映射）。

- 基线：{WORKSPACE_DIR}/{owner}/{repo}/codebase/ 的 bare mirror（每机懒建立，fetch --prune 刷新）
- worktree：{WORKSPACE_DIR}/{owner}/{repo}/issues/{issue_num}/{agent_type}/（任务 cwd，按 Issue 统一隔离）
- 鉴权安全：clone 用 `-c http.extraheader` 内联注入（不写 config、不入日志）；push 用 GIT_ASKPASS
  辅助脚本读已注入的 `$BOT_GITCODE_TOKEN`（不落盘、随目录清理）。
"""

import base64, json, os, re, shutil, subprocess, sys, threading, time

from src.kernel import config

from src.kernel.config import log

_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}

_GIT_AGENTS = ("coding", "qa", "sre")

# 检测/发布类环节：其工作区的职责是核对或上传既有交付，不需要继承 coding 本地
# 未合并提交。本地 `feat/issue-N` 既 ahead 又 behind 时不再直接复用，先按基线
# 对齐（旧 tip 存 refs/grape-preserved/），避免闸门判 diverged 把整条流水线卡死。
_REUSE_CLEAN_AGENTS = ("qa", "sre")

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

    def to_log_fields(self) -> dict:
        """Issue #4023：日志单行字段（与 to_dict() 同源，ahead/behind 归一 unknown）。

        日志与结构化落库共用同一份字段值，避免两处格式漂移；None 的分叉计数在
        日志侧显示 unknown（与错误文本历史口径一致）。
        """
        d = self.to_dict()
        d["ahead"] = "unknown" if d.get("ahead") is None else d["ahead"]
        d["behind"] = "unknown" if d.get("behind") is None else d["behind"]
        return d

    def format_one_line(self) -> str:
        """Issue #4023：失败日志专用单行（固定顺序七要素，**不做截断**）。

        字段顺序与 to_dict() 同源：issue / path / expected_baseline / actual_head /
        ahead / behind / reason；requested_baseline 与 expected_baseline 不同
        （发生过基线回退）时追加，保证 #2514「错误文本六要素可照抄定位」在唯一的
        运行失败日志出口不被 `[:120]` 切掉。
        """
        d = self.to_log_fields()
        issue = str(d.get("issue") or "")
        if not issue.startswith("#"):
            issue = f"#{issue}"
        line = (f"workspace baseline mismatch: issue={issue} path={d.get('path')} "
                f"expected_baseline={d.get('expected_baseline')} "
                f"actual_head={d.get('actual_head')} ahead={d.get('ahead')} "
                f"behind={d.get('behind')} reason={d.get('reason')}")
        rb = d.get("requested_baseline") or ""
        if rb and rb != (d.get("expected_baseline") or ""):
            line += f" requested_baseline={rb}"
        return line


# Issue #4023：worker 只把 str(error) 回传服务端（client.fail body.error），fail_task
# 收到的基线错误是字符串而非对象；按固定 `key=value` 词法还原结构化字段，使日志
# 单行与 JSON 落库在「对象来源 / 字符串来源」两条路径上同构。
_BASELINE_ERROR_FIELDS = ("issue", "path", "expected_baseline", "actual_head",
                          "ahead", "behind", "reason", "requested_baseline")


def parse_baseline_error_text(text) -> dict:
    """从失败上报文本还原 WorkspaceBaselineError 结构化字段；非基线文本 → {}。

    仅含 `workspace baseline mismatch:` 前缀但解析不出 issue 与 reason 的文本
    （半截截断 / 仿写）不视为基线错误，返回 {}，避免落入
    WORKSPACE_BASELINE_MISMATCH 且打点 key 归 '-'。
    """
    s = str(text or "")
    if "workspace baseline mismatch:" not in s:
        return {}
    fields = {}
    for key in _BASELINE_ERROR_FIELDS:
        m = re.search(rf"(?:^|\s){re.escape(key)}=(\S+)", s)
        if m:
            fields[key] = m.group(1)
    if not (fields.get("issue") and fields.get("reason")):
        return {}
    if fields.get("issue"):
        fields["issue"] = fields["issue"].lstrip("#")
    # requested_baseline 仅在发生基线回退时才出现在文本里；与 to_dict() 恒含该 key
    # 对齐，避免 errors JSON 在 requested == expected 时缺字段。
    fields.setdefault("requested_baseline", "")
    for key in ("ahead", "behind"):
        val = fields.get(key)
        if val == "unknown":
            fields[key] = None
        elif val is not None:
            try:
                fields[key] = int(val)
            except (TypeError, ValueError):
                pass
    return fields


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
        # rename/copy 的原路径字段紧随其后；状态位可能在 XY 任一位（如工作树侧 " R"）
        if ("R" in entry[:2] or "C" in entry[:2]) and i < len(fields):
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


def _archive_workspace_label(issue_num, agent_type, task_id: str = "") -> str:
    """证据存档文件名标签：优先 task_id，缺省用 issue+agent 保证跨目录唯一。

    同一仓库多份超期脏工作区在同一轮巡检/清理时若都落到 `workspace-unknown*.patch`，
    会互相覆盖、仅最后一份留存；这里以 issue/agent 兜底，杜绝同名覆盖。
    """
    tid = str(task_id or "").strip()
    return tid or f"issue{issue_num}-{agent_type}"


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
    if not _archive_workspace_patch(
            issue_dir, repo_key,
            task_id=_archive_workspace_label(issue_num, agent_type, task_id),
            token=token, suffix="-reuse-refused"):
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
                             token: str = "", requested: str = "", repo_key: str = "",
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
    _run_git_retry(["-C", issue_dir, "fetch", "origin", expected], token=token, timeout=120,
                   repo_key=repo_key)
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

    实现已收口到 `vcs.clone_endpoint.clone_url`（单一派生点，四处 clone 入口共用）：
    沙箱 executor / sandbox_context / takeover 原先各自硬编码 `https://gitcode.com/...`，
    gitee/github/gitlab 仓在那些链路上会被当成 gitcode 仓去 clone。本函数保留原名与
    异常语义（`PlatformCardMissingError`，带 code=platform_not_registered），因为
    agent 侧按该类型显式阻断任务——异常类型一变，又会落回"一条 WARNING 就放行"的旧链。

    未知 provider（带前缀但无平台卡）fail-fast——替代旧版静默回退 gitcode.com 的错误
    地址（S7 验收）。无前缀 repo_key 由 parse_repo_key 默认 gitcode，行为不变。
    """
    from src.domains.runtime.vcs.clone_endpoint import CloneEndpointError, clone_url
    from src.domains.runtime.vcs.platform_card import PlatformCardMissingError

    try:
        return clone_url(repo_key)
    except CloneEndpointError as e:
        # 透传 code：取卡通道不可用（platform_card_contract_unavailable）与
        # 平台未注册（platform_not_registered）在任务侧要能分开处置。
        raise PlatformCardMissingError(e.provider, detail=str(e),
                                       code=getattr(e, "code", "") or None) from e

def _basic_auth(token: str, scheme: str = "x_access_token", provider: str = "",
                repo_key: str = "", username: str = "") -> str:
    """Basic 凭据（用户名按平台卡 clone_auth 派生，Issue #3632 后继）。

    改前用户名查本模块常量表且 `basic_password` → 空串。真机实测（gitee 私有仓）：
    `x-access-token:<token>` 与 `:<token>` 均返回 `The token username invalid`，
    只有"令牌所属账号名:<token>"能过——所以用户名必须按卡派生，且派生要有 repo_key
    （`basic_password` 的兜底是仓库 owner，缺 repo_key 会退化成空用户名而必然被拒）。

    Args:
        username: 已派生好的用户名（调用方一次算好、多处复用，避免每处再解析）；
        repo_key: 未给 username 时的派生输入（owner 兜底需要它）。
    """
    user = username or _AUTH_USER_BY_SCHEME.get(scheme, "x-access-token")
    if provider and not username:
        try:
            from src.domains.runtime.vcs.clone_endpoint import clone_username

            user = clone_username(provider, repo_key) or user
        except Exception as e:  # noqa: BLE001 - 派生失败回落旧表，不阻断 git
            log.warning(f"repogit clone username derive failed ({provider}): {e}")
    return base64.b64encode(f"{user}:{token}".encode()).decode()

def _transport_config_for(repo_key: str) -> "tuple[str, str]":
    """repo_key → (clone_auth scheme, ca_bundle)。无卡/未知 provider → 现状默认。

    Issue #3580/R4/R9：自建实例的 TLS CA 由平台卡 `ca_bundle` 提供，只在 git
    传输层注入，改卡即生效，不依赖环境变量。
    """
    if not repo_key:
        return "x_access_token", ""
    from src.kernel.repo_key import parse_repo_key
    provider, _, _ = parse_repo_key(repo_key)
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider)
    except Exception:
        card = None
    if card is None:
        return _clone_auth_scheme_for(repo_key), ""
    return (card.clone_auth or "x_access_token"), (getattr(card, "ca_bundle", "") or "")


def _run_git(args: list, token: str = "", timeout: int = 900, scheme: str = "",
             provider: str = "", username: str = "", repo_key: str = "",
             ca_bundle: str = ""):
    """执行 git；凭据/TLS 经环境（GIT_CONFIG_COUNT）注入，不进 argv、不进日志。

    鉴权来源沿用 arch-weekly-11 的派生链（clone_params / clone_identity），
    注入机制承接 Issue #3580/R9：token 不落进程参数（`ps`/错误输出均不可见）；
    R4：自建实例 CA 以 `http.sslCAInfo` 注入，`http.followRedirects=false`
    防止凭据随重定向外泄。

    scheme 由平台卡 clone_auth 决定（gitcode/github=x-access-token 现状，
    gitlab=oauth2，gitee=basic_password）；传 repo_key 时自动按卡解析 scheme 与 CA。
    """
    if token and not scheme:
        # 传输凭据必须与 clone 同源：gitee=basic_password（用户名=令牌账号）、
        # gitlab=oauth2、github/gitcode=x-access-token。缺 repo_key 才回落旧默认。
        scheme = "x_access_token"
        if repo_key:
            try:
                from src.domains.runtime.vcs.clone_endpoint import clone_params

                _p = clone_params(repo_key)
                scheme = _p.get("clone_auth") or scheme
                provider = provider or _p.get("provider", "")
                username = username or _p.get("username", "")
            except Exception as e:  # noqa: BLE001 - 派生失败回落旧默认，不阻断 git
                log.warning(f"transport auth derive failed ({repo_key}): {e}")
    if repo_key:
        # 无显式 scheme 时按卡补；CA 一律按卡取（显式传入优先）
        _card_scheme, _card_ca = _transport_config_for(repo_key)
        scheme = scheme or _card_scheme
        ca_bundle = ca_bundle or _card_ca
    cmd = ["git"] + list(args)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    _pairs: list[tuple[str, str]] = []
    if token:
        _pairs.append(("http.extraHeader",
                       f"Authorization: Basic {_basic_auth(token, scheme, provider, repo_key=repo_key, username=username)}"))
    if ca_bundle:
        _pairs.append(("http.sslCAInfo", str(ca_bundle)))
    # 传输安全：禁止 HTTP 重定向携带鉴权头到其他主机（默认 true 会跨主机转发）
    _pairs.append(("http.followRedirects", "false"))
    # Issue #3580 复审：累加调用方已有的 GIT_CONFIG_* 项，不覆盖
    _offset = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for _i, (_k, _v) in enumerate(_pairs, _offset):
        env[f"GIT_CONFIG_KEY_{_i}"] = _k
        env[f"GIT_CONFIG_VALUE_{_i}"] = _v
    env["GIT_CONFIG_COUNT"] = str(_offset + len(_pairs))
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
                       retries: int = 3, base_delay: float = 1.0, scheme: str = "",
                       provider: str = "", username: str = "", repo_key: str = "",
                       ca_bundle: str = ""):
    """执行 git；网络类错误有限次退避重试（默认 3 次：1s/2s/4s）。

    - 成功（returncode==0）立即返回；
    - 仅网络类错误重试；4xx（401/403/404）与非网络类立即返回；
    - 全部重试失败返回最后一次结果（调用方按原有 None/非 0 逻辑降级）。
    Issue #3580：repo_key 透传，保证每次重试都用同一实例的 CA/鉴权方式。
    """
    delay = base_delay
    last = None
    for attempt in range(retries):
        last = _run_git(args, token=token, timeout=timeout, scheme=scheme,
                            provider=provider, username=username, repo_key=repo_key,
                            ca_bundle=ca_bundle)
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

def ensure_repo_base(repo_key: str, token: str = "", issue_num=None,
                     agent_type: str = "") -> str | None:
    """确保本机 bare 基线存在并刷新（每机懒建立）。

    用 `git init --bare` + refspec 拉取到 `refs/remotes/origin/*`，而非 `--mirror`：
    mirror 会把 per-Issue 本地分支 refs/heads/feat/issue-* 误当远程跟踪引用，被 `fetch --prune` 误删。

    Issue #3923：``issue_num``/``agent_type`` 下传后，init/remote add/fetch/异常各失败
    路径直接把现场写到 (repo, issue, agent) 三元组键——并发任务各自持有自己的 git
    原文，互不覆盖；不传时回落仓级键（历史行为）。
    """
    base = repo_base_dir(repo_key)
    url = clone_url_for(repo_key)
    _scheme = _clone_auth_scheme_for(repo_key)
    # 用户名按平台卡 clone_auth 派生（gitee=真实账号名），与地址同一个派生点
    from src.kernel.repo_key import parse_repo_key as _parse_rk

    _provider = _parse_rk(repo_key)[0]
    # 用户名只解析一次（basic_password 需要 repo_key 才能落 owner 兜底；缺它会变空用户名）
    try:
        from src.domains.runtime.vcs.clone_identity import (
            username_for_token as _user_for_token,
        )

        # 有令牌时优先「令牌→账号 login」动态解析（带本地缓存），失败回落卡/owner 派生。
        # 卡上 bot_username 人工填错（组织名 / grape 账号名）时，这里不会再发出错用户名。
        _clone_user, _clone_user_src = _user_for_token(_provider, token, repo_key)
        # 来源可观测（2026-11-21）：basic_password 平台只接受**令牌所属账号名**；
        # owner_fallback 只在「个人仓 owner 恰等于令牌账号」时正确，组织仓/他人仓
        # 会被上游拒（The token username invalid / 403）。此前只有 clone_endpoint
        # 内部一条一次性 WARNING，调用侧看不到，排障会误查凭据。
        if _clone_user_src in ("owner_fallback", "missing"):
            log.warning(f"repogit clone username source={_clone_user_src} provider={_provider} "
                        f"repo_key={repo_key} username={_clone_user or '-'} —— "
                        f"该平台要求令牌账号名，组织仓/他人仓会被上游拒；请在平台卡填 bot_username")
        else:
            log.info(f"repogit clone username source={_clone_user_src} provider={_provider}")
    except Exception as e:  # noqa: BLE001
        log.warning(f"repogit clone username resolve failed ({repo_key}): {e}")
        _clone_user = ""
    with _repo_lock(base):
        try:
            if not os.path.isdir(base):
                os.makedirs(os.path.dirname(base), exist_ok=True)
                r = _run_git_retry(["init", "--bare", base], token=token, scheme=_scheme,
                              provider=_provider, username=_clone_user)
                if r is None or r.returncode != 0:
                    _stderr = (r.stderr if r is not None else "git exec failed") or ""
                    log.warning(f"repogit init failed {repo_key}: {_stderr[:300]}")
                    # Issue #3923：init 失败同样写失败现场——不写会导致
                    # worktree_failure_error 为空、任务不阻断而在无工作区状态继续执行
                    _record_worktree_failure(repo_key, WORKTREE_STAGE_BASE, _stderr,
                                             issue_num=issue_num, agent_type=agent_type)
                    return None
                r = _run_git_retry(["-C", base, "remote", "add", "origin", url], token=token, scheme=_scheme,
                                provider=_provider, username=_clone_user)
                if r is None or r.returncode != 0:
                    _stderr = (r.stderr if r is not None else "git exec failed") or ""
                    log.warning(f"repogit remote add failed {repo_key}: {_stderr[:300]}")
                    _record_worktree_failure(repo_key, WORKTREE_STAGE_BASE, _stderr,
                                             issue_num=issue_num, agent_type=agent_type)
                    return None
            r = _run_git_retry(["-C", base, "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*",
                                "+refs/tags/*:refs/tags/*"], token=token, scheme=_scheme,
                               provider=_provider, username=_clone_user)
            if r is None or r.returncode != 0:
                _stderr = (r.stderr if r is not None else "git exec failed") or ""
                log.warning(f"repogit fetch failed {repo_key}: {_stderr[:300]}")
                # Issue #3923：建树失败要能透传 git 原文与失败阶段（不落 not_configured）
                _record_worktree_failure(repo_key, WORKTREE_STAGE_BASE, _stderr,
                                         issue_num=issue_num, agent_type=agent_type)
                return None
            # Issue #3923：裸仓 HEAD 必须在 refs/heads/* 下。历史现场里 HEAD 被写成
            # `ref: refs/remotes/origin/<baseline>`（远程跟踪引用），此后每次
            # `git worktree add` 都必然 `fatal: HEAD not found below refs/heads!`。
            # 建树前先校验并自愈，避免把 git 侧故障带进归因阶段。
            _head = ensure_bare_head(
                base, _default_base_branch(base, token, repo_key), token=token,
                repo_key=repo_key, stage="ensure_repo_base")
            if not _head.get("ok"):
                log.warning(f"repogit bare HEAD unusable {repo_key}: {_head.get('detail', '')}")
            return base
        except Exception as e:
            log.warning(f"repogit ensure_repo_base failed {repo_key}: {e}")
            # Issue #3923：异常兜底同样写失败现场，不得静默返回 None
            _record_worktree_failure(repo_key, WORKTREE_STAGE_BASE, f"ensure_repo_base exception: {e}",
                                     issue_num=issue_num, agent_type=agent_type)
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


# ---------------------------------------------------------------------------
# Issue #3758：#1841 的 work_branch 存在性校验在仓库重构为 src/domains/** 后丢失
# 公开符号 resolve_work_branch，webhook 调用点被 except Exception 吞成静默 WARNING，
# 未校验分支被标 target_branch_source=target_scene_work_branch 交给下游 coding。
# 这里恢复公开入口：基于 VCS 分支列表显式比对（交办时点无 worktree，不能照搬依赖
# base 目录 + token 的 _default_base_branch），显式确认不存在才回退默认分支并留痕，
# 分支列表不可核实（无 token/网络失败/分页截断）时保持原配置。
# ---------------------------------------------------------------------------
_BRANCH_LIST_TTL = 90.0
_BRANCH_LIST_FAIL_TTL = 30.0
_BRANCH_MIRROR_TTL = 120.0
_BRANCH_MIRROR_FETCH_TIMEOUT = 30
_MAX_BRANCH_PAGES = 50
_branch_list_cache = {}          # repo_key -> (ts, branches: set|None, default_branch)
_branch_list_force_remote = set()
_branch_mirror_fresh_at = {}     # repo_key -> 最近一次成功的 mirror 刷新时间
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


def _refresh_local_mirror(repo_key: str, token: str = "",
                          timeout: int = _BRANCH_MIRROR_FETCH_TIMEOUT) -> bool:
    """`fetch --prune` 刷新本地 bare mirror（含平台外新建/删除的分支）。

    timeout 默认短超时（20-30s）：本函数会被 A2A 交办链路调用，长超时 + 多次重试会
    长时间占用线程（#3758 R2），不再沿用 git 写操作默认的 900s。
    """
    base = repo_base_dir(repo_key)
    if not base or not os.path.isdir(base):
        return False
    with _repo_lock(base):
        r = _run_git_retry(
            ["-C", base, "fetch", "--prune", "origin",
             "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"],
            token=token, timeout=timeout)
        if r is None or r.returncode != 0:
            log.warning(f"work_branch: refresh mirror failed repo={repo_key}: "
                        f"{(r.stderr if r else 'git exec failed')[:200]}")
            return False
        _run_git(["-C", base, "remote", "set-head", "origin", "--auto"],
                 token=token, timeout=60)
    _mark_mirror_fresh(repo_key)
    return True


def _branch_list_cache_get(repo_key: str = "", refresh: bool = False):
    """读分支列表缓存；失败结果短 TTL，避免交办链路反复穿透外部 API。"""
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
    """失效分支列表缓存（建分支等写操作后调用；空 repo_key = 全清）。"""
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
    """分支查询 token：统一 TokenResolver → 本地 Repo.bot_token 兜底。"""
    try:
        from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key
        tok = TokenResolver(create_provider_from_repo_key(repo_key)).get_token(repo_key) or ""
    except Exception:
        tok = ""
    if tok:
        return tok
    try:
        from src.kernel.repo import Repo
        return Repo(repo_key).bot_token or ""
    except Exception:
        return ""


def fetch_repo_branch_records(repo_key: str, token: str = "",
                              max_pages: int = _MAX_BRANCH_PAGES):
    """经 VCS 公开分支列表 API 拉取分支记录。

    返回 (records: list[dict]|None, truncated: bool)。None 表示请求失败/无 token。
    只消费 ``VcsApiClient.list_branches`` 公开方法，私有路径/分页实现漂移由 API
    客户端自身兜底，不在此复制（#3758 R1）；异常收窄为预期运行期失败。
    """
    from src.kernel.repo_key import parse_repo_key
    provider, owner, name = parse_repo_key(repo_key)
    tok = token or _branch_lookup_token(repo_key)
    if not owner or not name or not tok:
        return None, False
    try:
        from src.domains.runtime.vcs.api_client import VcsApiClient
        client = VcsApiClient(provider, tok, owner, name)
        records, truncated = client.list_branches(
            max_pages=max_pages, per_page=100, with_truncation=True)
        return records, truncated
    except (OSError, RuntimeError, TimeoutError) as e:
        log.warning(f"work_branch: list branches failed repo={repo_key}: {e}")
        return None, False


def _fetch_remote_default_branch(repo_key: str, token: str = "") -> str:
    """经 VCS 方言正确的公开默认分支方法读取；失败返回空串。"""
    from src.kernel.repo_key import parse_repo_key
    provider, owner, name = parse_repo_key(repo_key)
    tok = token or _branch_lookup_token(repo_key)
    if not owner or not name or not tok:
        return ""
    try:
        from src.domains.runtime.vcs.api_client import VcsApiClient
        return VcsApiClient(provider, tok, owner, name).get_default_branch()
    except (OSError, RuntimeError, TimeoutError) as e:
        log.warning(f"work_branch: read default branch failed repo={repo_key}: {e}")
    return ""


def _fetch_remote_branch_names(repo_key: str, token: str = ""):
    """经 VCS 分支列表 API 拉取真实分支集合 + 默认分支；失败返回 (None, "")。

    分页上限 50 页（5000 分支）。超过上限仍满页时视为不可核实，避免把
    「列表被截断」误判为「分支确实不存在」。
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

    - 先读本地 bare mirror（无网络开销）；mirror 超 TTL 时先 `fetch --prune`
      刷新，确保平台外新建/删除的分支可见，避免陈旧 refs 双向误判。
    - 无镜像或 remote_only=True 时穿透 VCS 分支列表 API。
    - 结果按 repo 缓存（成功 90s / 失败 30s）。branches=None 表示无法核实。
    """
    forced = remote_only or _consume_force_remote(repo_key)
    cached = _branch_list_cache_get(repo_key, refresh=refresh or forced)
    if cached is not None:
        return cached
    branches, default = (None, "")
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


def pick_default_branch(branches: set, default_branch: str = "") -> str:
    """选仓库默认分支：优先 API/mirror 报告的默认分支，其次 main/master。

    取不到可信默认分支时返回空串——**不回退到 `sorted(branches)[0]` 任意分支**
    （#3758 R3：gitlab 方言默认分支读取曾为空，任意分支会被当作权威基线）。
    """
    if default_branch and (not branches or default_branch in branches):
        return default_branch
    for cand in ("main", "master"):
        if branches and cand in branches:
            return cand
    return ""


def _classify_work_branch(repo_key: str, branch: str, token: str = "",
                          authoritative: bool = False):
    """统一分支判定：返回 (exists: bool|None, default_branch, branches)。

    - authoritative=True（保存校验）：直接以 VCS 远端分支列表为准。
    - authoritative=False（注入/交办）：mirror 未命中目标分支时强制远端复核，
      避免平台外新建分支（如 weekly 发布流水线）被误判失效。
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


def resolve_work_branch(repo_key: str, configured: str, token: str = "") -> tuple:
    """交办/注入前校验 work_branch 是否存在，失效则回退仓库默认分支并留痕。

    Returns (effective_branch, fallback_note)。fallback_note 为空表示未回退。
    - 仅用「分支列表显式比对」，不依赖 GitCode ref/tree 查询（ref 不存在会静默回退）。
    - 无法拉取分支列表（无 token/网络失败/分页截断）时保持原配置，不误判回退。
    """
    cfg = (configured or "").strip()
    if not cfg:
        return "", ""
    exists, default, branches = _classify_work_branch(repo_key, cfg, token=token)
    if exists is None:
        log.warning(f"work_branch: cannot verify '{cfg}' for repo={repo_key} "
                    f"(branch list unavailable); keep configured value")
        return cfg, ""
    if exists:
        return cfg, ""
    fallback = pick_default_branch(branches, default)
    if not fallback:
        # 分支明确不存在，但无法确定可信默认分支：不选任意分支当权威基线，
        # 保持原配置并标注未确认（#3758 R3）。
        note = (f"configured work_branch '{cfg}' does not exist in repo {repo_key}; "
                f"default branch unknown, keep configured value (unconfirmed)")
        log.warning(note)
        return cfg, note
    note = (f"configured work_branch '{cfg}' does not exist in repo {repo_key}; "
            f"fallback to default branch '{fallback}'")
    log.warning(note)
    return fallback, note


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
            # Issue #2933：安装失败的可见告警（含真实 reason）由安装函数统一输出，
            # 这里只留调试线索，避免同一次失败重复告警、且不覆盖真实原因。
            log.debug(
                f"repogit branch guard hook install returned False "
                f"{repo_key}:{branch}")
    except Exception as e:
        log.warning(f"repogit branch guard hook install error {repo_key}:{branch}: {e}")

# --- Issue #2245：worktree git 元数据可写保障（M1）+ 只读降级自检（M2） ---
# 复现场景：预置 worktree 的 `.git` 指向只读共享基线卷（.../codebase/worktrees/{agent}），
# 导致 config/fetch/commit/push 全部 Read-only file system，会话只能整仓重新 clone。
# 策略：写权限实写探测（index.lock）→ 不可写时把 gitdir 迁移到会话可写卷
# （clone --shared + alternates 共享对象，不复制对象库）→ 仍不可写则标注只读并给出提示。
_SESSION_GITDIR_NAME = ".git-session"
_GIT_WRITE_PROBE_TIMEOUT = 60


def _is_git_dir(path: str) -> bool:
    """路径是否为可用的 git 目录（bare 或 linked worktree admin dir）。"""
    if not path or not os.path.isdir(path):
        return False
    return (os.path.isfile(os.path.join(path, "HEAD"))
            and os.path.isdir(os.path.join(path, "objects")))


def resolve_gitdir(wt_dir: str) -> str:
    """把 worktree 的 `.git`（目录或 gitdir 指针文件）解析为真实 git 目录。"""
    if not wt_dir:
        return ""
    dot = os.path.join(wt_dir, ".git")
    if os.path.isdir(dot):
        return os.path.abspath(dot)
    if not os.path.isfile(dot):
        return ""
    try:
        with open(dot, "r", encoding="utf-8") as f:
            first = (f.readline() or "").strip()
    except OSError:
        return ""
    if not first.lower().startswith("gitdir:"):
        return ""
    target = first.split(":", 1)[1].strip()
    if not target:
        return ""
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(wt_dir, target))
    return target


def git_common_dir(gitdir: str) -> str:
    """解析 linked worktree 的共享 git 目录（commondir）；独立仓库返回 gitdir 自身。"""
    if not gitdir:
        return ""
    pointer = os.path.join(gitdir, "commondir")
    if os.path.isfile(pointer):
        try:
            with open(pointer, "r", encoding="utf-8") as f:
                rel = (f.readline() or "").strip()
        except OSError:
            return ""
        if rel:
            if not os.path.isabs(rel):
                rel = os.path.normpath(os.path.join(gitdir, rel))
            return rel
    return gitdir


def probe_path_writable(path: str) -> tuple:
    """实写探测目录可写（root/只读挂载语义与 git 一致，不依赖 os.access）。"""
    if not path or not os.path.isdir(path):
        return False, f"not a directory: {path or '-'}"
    probe = os.path.join(path, f".grape-write-probe-{os.getpid()}-{threading.get_ident()}")
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.unlink(probe)
        return True, ""
    except OSError as e:
        return False, (e.strerror or str(e))


def probe_git_write(gitdir: str) -> tuple:
    """探测 git 元数据目录写权限：优先创建 `.git/index.lock`（与 git 写路径一致）。

    已有 index.lock（其它 git 进程持锁）时不删除他人锁，退化为临时文件探测。
    """
    if not gitdir or not os.path.isdir(gitdir):
        return False, f"git dir missing: {gitdir or '-'}"
    lock = os.path.join(gitdir, "index.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            os.unlink(lock)
        except OSError as e:
            log.warning(f"repogit write probe: failed to remove residual index.lock at {lock}: {e}")
        return True, ""
    except FileExistsError:
        return probe_path_writable(gitdir)
    except OSError as e:
        return False, (e.strerror or str(e))


def probe_common_write(common_dir: str) -> tuple:
    """探测 linked worktree 共享 git 目录的写权限（objects/refs 实际写入路径）。

    worktree 的 admin gitdir 可能可写，但 commit/fetch 仍需写入共享目录的
    objects 与 refs；只探测 admin gitdir 会在 common 只读时漏判。
    """
    if not common_dir or not os.path.isdir(common_dir):
        return False, f"common git dir missing: {common_dir or '-'}"
    checks = (
        ("common dir", common_dir),
        ("objects", os.path.join(common_dir, "objects")),
        ("refs", os.path.join(common_dir, "refs")),
    )
    for name, path in checks:
        ok, why = probe_path_writable(path)
        if not ok:
            return False, f"{name} not writable: {why}"
    return True, ""


def _is_readonly_git_failure(stderr: str) -> bool:
    """判断 git 失败是否由只读挂载/权限导致，避免对非只读错误盲目降级建树。"""
    text = (stderr or "").lower()
    if not text:
        return False
    markers = (
        "read-only file system",
        "operation not permitted",
        "permission denied",
    )
    return any(marker in text for marker in markers)


def _session_git_config_args(repo_key: str, session: str, worktree: str) -> list:
    """会话 gitdir 的必要配置（工作区指向 + origin 凭据无关 URL）。"""
    return [
        ["-C", session, "config", "core.bare", "false"],
        ["-C", session, "config", "core.worktree", worktree],
        ["-C", session, "config", "remote.origin.url", clone_url_for(repo_key)],
        ["-C", session, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
    ]


def _prepare_session_gitdir(repo_key: str, session: str, worktree: str, token: str = "") -> str:
    """配置会话 gitdir（core.worktree/origin）并写入 exclude；失败返回错误信息。"""
    for args in _session_git_config_args(repo_key, session, worktree):
        r = _run_git(args, token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if r is None or r.returncode != 0:
            return f"configure session git dir failed: {args[-2:]}"
    # 迁移前 worktree 已设置的 git 身份需随会话 gitdir 带走（clone --bare 不复制用户配置）
    for key in ("user.name", "user.email", "user.signingkey"):
        r = _run_git(["-C", worktree, "config", "--get", key],
                     token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        value = (r.stdout or "").strip() if r is not None and r.returncode == 0 else ""
        if not value:
            continue
        r = _run_git(["-C", session, "config", key, value],
                     token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if r is None or r.returncode != 0:
            return f"copy session git config {key} failed"
    try:
        info = os.path.join(session, "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "exclude"), "a", encoding="utf-8") as f:
            f.write("\n" + _SESSION_GITDIR_NAME + "/\n")
    except OSError as e:
        return f"prepare session git dir failed: {e}"
    return ""


def _relocate_gitdir_to_session(repo_key: str, wt_dir: str, gitdir: str,
                                common_dir: str, token: str = "") -> dict:
    """把只读 worktree git 元数据迁移到会话可写卷（`<wt>/.git-session`）。

    对象库通过 `git clone --shared` 的 alternates 共享只读基线，不复制对象；
    迁移只切换 gitdir 指针并 `reset --mixed` 重建索引，不触碰工作区文件。
    """
    dot = os.path.join(wt_dir, ".git")
    if not os.path.isfile(dot):
        return {"ok": False, "error": "worktree .git is not a gitdir pointer file"}
    if not common_dir or not os.path.isdir(os.path.join(common_dir, "objects")):
        return {"ok": False, "error": f"shared git dir invalid: {common_dir or '-'}"}
    ok, why = probe_path_writable(wt_dir)
    if not ok:
        return {"ok": False, "error": f"worktree is read-only: {why}"}
    head_sha = worktree_head_sha(wt_dir, token=token)
    if not head_sha:
        return {"ok": False, "error": "cannot resolve worktree HEAD"}
    head_ref = _run_git(["-C", wt_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                        token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
    branch = (head_ref.stdout or "").strip() if head_ref is not None and head_ref.returncode == 0 else ""
    try:
        with open(dot, "r", encoding="utf-8") as f:
            original_pointer = f.read()
    except OSError as e:
        return {"ok": False, "error": f"read .git pointer failed: {e}"}
    session = os.path.join(wt_dir, _SESSION_GITDIR_NAME)
    try:
        if os.path.isdir(session):
            shutil.rmtree(session, ignore_errors=True)
        # 本地 clone 不走网络传输，不注入 token（缩小凭据可见面）
        r = _run_git(["clone", "--shared", "--bare", common_dir, session],
                     timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if r is None or r.returncode != 0:
            return {"ok": False, "error": "clone --shared failed: "
                    + (((r.stderr if r is not None else "git exec failed") or "")[:200])}
        err = _prepare_session_gitdir(repo_key, session, wt_dir, token=token)
        if err:
            return {"ok": False, "error": err}
        # 新 session gitdir 的 HEAD 必须精确等于原 worktree HEAD：ref 分支时同步
        # refs/heads/<branch> 并让 HEAD 指向它；detached 时把 HEAD 直接固定到原 commit。
        # 否则 clone --shared 复制来的默认分支会让后续 reset --mixed 落到错误基线。
        if branch and branch != "HEAD":
            _run_git(["-C", session, "update-ref", f"refs/heads/{branch}", head_sha],
                     token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
            _run_git(["-C", session, "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
                     token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        else:
            _run_git(["-C", session, "update-ref", "--no-deref", "HEAD", head_sha],
                     token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        pinned = _run_git(["-C", session, "rev-parse", "HEAD"],
                          token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if (pinned is None or pinned.returncode != 0
                or (pinned.stdout or "").strip() != head_sha):
            return {"ok": False, "error": "session HEAD mismatch after relocate"}
        try:
            with open(os.path.join(session, "grape-origin-gitdir"), "w", encoding="utf-8") as f:
                f.write(gitdir + "\n")
        except OSError as e:
            log.warning(f"repogit relocate: failed to record origin gitdir in session {session}: {e}")
        tmp = dot + ".grape-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"gitdir: {session}\n")
        os.replace(tmp, dot)
        rr = _run_git(["-C", wt_dir, "reset", "--mixed", "-q", "HEAD"],
                      token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if rr is None or rr.returncode != 0:
            try:
                with open(dot, "w", encoding="utf-8") as f:
                    f.write(original_pointer)
            except OSError as e:
                log.warning(f"repogit relocate: failed to roll back .git pointer to '{original_pointer}' after index rebuild failure: {e}")
            return {"ok": False, "error": "rebuild index failed: "
                    + (((rr.stderr if rr is not None else "git exec failed") or "")[:200])}
        moved_head = _run_git(["-C", wt_dir, "rev-parse", "HEAD"],
                              token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if (moved_head is None or moved_head.returncode != 0
                or (moved_head.stdout or "").strip() != head_sha):
            try:
                with open(dot, "w", encoding="utf-8") as f:
                    f.write(original_pointer)
            except OSError as e:
                log.warning(f"repogit relocate: failed to roll back .git pointer to '{original_pointer}' after HEAD mismatch: {e}")
            got = (moved_head.stdout or "").strip() if moved_head is not None else "-"
            return {"ok": False,
                    "error": f"HEAD mismatch after relocate (expected {head_sha}, got {got})"}
    except Exception as e:
        return {"ok": False, "error": f"relocate failed: {e}"}
    ok2, why2 = probe_git_write(session)
    if not ok2:
        return {"ok": False, "error": f"session git dir not writable: {why2}"}
    return {"ok": True, "gitdir": session}


def ensure_worktree_git_writable(repo_key: str, wt_dir: str, token: str = "") -> dict:
    """Issue #2245（M1/M2）：确保 worktree git 元数据可写；不可写时迁移或降级标注。

    Returns:
        {writable, mode(native|relocated|readonly), gitdir, common_dir, reason, hint}
    """
    out = {"writable": False, "mode": "readonly", "gitdir": "", "common_dir": "",
           "reason": "", "hint": ""}
    gitdir = resolve_gitdir(wt_dir)
    out["gitdir"] = gitdir
    if not gitdir:
        out["reason"] = "git dir not found"
        out["hint"] = ("No .git directory or gitdir pointer found in the worktree; "
                       "git baseline sync/commit/push are unavailable. Actionable: provide a "
                       "preset worktree with a valid .git, or run a read-only session.")
        return out
    common = git_common_dir(gitdir)
    out["common_dir"] = common
    ok, why = probe_git_write(gitdir)
    if ok and common and os.path.abspath(common) != os.path.abspath(gitdir):
        # linked worktree 的 commit/fetch 还要写共享 common 的 objects/refs；
        # admin gitdir 可写不代表 common 可写，必须一并探测。
        ok, why = probe_common_write(common)
    if ok:
        out.update(writable=True, mode="native")
        return out
    moved = _relocate_gitdir_to_session(repo_key, wt_dir, gitdir, common, token=token)
    if moved.get("ok"):
        out.update(writable=True, mode="relocated", gitdir=moved.get("gitdir", ""),
                   reason=why)
        log.warning(f"repogit: worktree git metadata was read-only ({why}); "
                    f"relocated to session git dir: {moved.get('gitdir', '')}")
        return out
    out["reason"] = moved.get("error") or why
    out["hint"] = (
        f"Git metadata directory is read-only: {gitdir} ({out['reason']}). "
        "This session runs in READ-ONLY git mode: grape git config/fetch/commit/push are unavailable. "
        "Actionable: do not clone, commit or push inside the worktree; finish with a read-only "
        "conclusion and report it instead of retrying git writes. To enable write mode, remount the "
        "session git directory read-write or run the session on a writable workspace (Issue #2245).")
    log.warning(f"repogit: worktree git metadata is read-only, fallback to read-only mode: "
                f"{wt_dir}: {out['reason']}")
    return out


def _create_worktree_with_session_gitdir(repo_key: str, issue_dir: str, base: str,
                                         branch: str, base_branch: str,
                                         token: str = "") -> bool:
    """只读基线无法 `git worktree add` 时的降级建树：会话可写 gitdir + 共享对象。

    `clone --shared --bare` 只写 refs 与 alternates，不复制对象库；随后 checkout
    写入会话工作区（可写），避免整仓重新 clone。
    """
    try:
        ok, why = probe_path_writable(os.path.dirname(issue_dir) or issue_dir)
        if not ok:
            log.warning(f"repogit: session worktree parent is read-only: {why}")
            return False
        os.makedirs(issue_dir, exist_ok=True)
        session = os.path.join(issue_dir, _SESSION_GITDIR_NAME)
        if os.path.isdir(session):
            shutil.rmtree(session, ignore_errors=True)
        # 本地 clone 不走网络传输，不注入 token（缩小凭据可见面）
        r = _run_git(["clone", "--shared", "--bare", base, session],
                     timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if r is None or r.returncode != 0:
            log.warning(f"repogit: session worktree clone failed: "
                        f"{((r.stderr if r is not None else 'git exec failed') or '')[:200]}")
            return False
        err = _prepare_session_gitdir(repo_key, session, issue_dir, token=token)
        if err:
            log.warning(f"repogit: session worktree config failed: {err}")
            return False
        has = _run_git(["-C", session, "show-ref", "--verify", "--quiet",
                        f"refs/heads/{branch}"],
                       token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if has is None or has.returncode != 0:
            sha = ""
            for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{base_branch}"):
                rr = _run_git(["-C", base, "rev-parse", "--verify", "--quiet", ref],
                              token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
                if rr is not None and rr.returncode == 0 and (rr.stdout or "").strip():
                    sha = rr.stdout.strip()
                    break
            if not sha:
                log.warning(f"repogit: cannot resolve base sha for {branch} from {base_branch}")
                return False
            _run_git(["-C", session, "update-ref", f"refs/heads/{branch}", sha],
                     token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        _run_git(["-C", session, "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
                 token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        with open(os.path.join(issue_dir, ".git"), "w", encoding="utf-8") as f:
            f.write(f"gitdir: {session}\n")
        rr = _run_git(["-C", issue_dir, "reset", "--hard", "-q", "HEAD"],
                      token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if rr is None or rr.returncode != 0:
            log.warning(f"repogit: session worktree checkout failed: "
                        f"{((rr.stderr if rr is not None else 'git exec failed') or '')[:200]}")
            return False
        return probe_git_write(session)[0]
    except Exception as e:
        log.warning(f"repogit: session worktree create failed: {e}")
        return False


def inject_git_write_context(task: dict, env: dict, prompt: str) -> tuple:
    """Issue #2245（M2）：会话启动自检 git 写权限，注入只读标记与降级提示。

    - env 注入 `GRAPE_GIT_WRITABLE`（true/false）与 `GRAPE_GIT_DIR`；只读时追加
      `GRAPE_GIT_READONLY=true` / `GRAPE_GIT_READONLY_REASON`，为下游 runbook 提供分支判据。
    - 只读时向 prompt 追加可操作提示块（不重试 git 写、输出只读结论等）。
    - 非 git agent / worktree 不存在 → 原样返回（零副作用）。
    """
    try:
        rk = (task or {}).get("repo_key") or ""
        ag = (task or {}).get("agent_type") or ""
        if not rk or not is_git_agent(ag):
            return prompt, {}
        wt = (task or {}).get("worktree_dir") or ""
        if not wt:
            num = issue_num_from((task or {}).get("issue_id") or "")
            if num:
                wt = issue_dir_for(rk, ag, num)
        if not wt or not os.path.isdir(wt):
            return prompt, {}
        status = ensure_worktree_git_writable(rk, wt)
        env["GRAPE_GIT_WRITABLE"] = "true" if status.get("writable") else "false"
        env["GRAPE_GIT_DIR"] = status.get("gitdir") or ""
        if status.get("writable"):
            env.pop("GRAPE_GIT_READONLY", None)
            env.pop("GRAPE_GIT_READONLY_REASON", None)
            return prompt, status
        env["GRAPE_GIT_READONLY"] = "true"
        env["GRAPE_GIT_READONLY_REASON"] = status.get("reason") or ""
        block = (
            "\n\n[grape runtime] NOTE: READ-ONLY git session. "
            f"Git metadata directory {status.get('gitdir') or '?'} is not writable "
            f"(reason: {status.get('reason') or 'unknown'}). "
            "grape git config/fetch/commit/push are unavailable: do NOT retry git writes or "
            "re-clone the repository into another directory. "
            "Deliver a read-only conclusion (analysis only) and report this degradation; "
            "write-type work needs the session git directory remounted read-write "
            "(Issue #2245 M1) or a writable workspace. "
            "Machine-readable context: GRAPE_GIT_WRITABLE=false, GRAPE_GIT_READONLY=true.\n"
        )
        return prompt + block, status
    except Exception as e:
        log.warning(f"repogit: git write context inject failed: {e}")
        return prompt, {}


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


def _branch_vs_baseline(base: str, branch: str, base_branch: str,
                        token: str = "", repo_key: str = "") -> dict:
    """本地分支相对 origin/<base> 的分叉状态（复用判据用）。

    sre/qa 这类「检测-发布」环节不应继承 coding 未合并的本地提交：一旦本地
    `feat/issue-N` 既 ahead 又 behind，闸门必判 diverged，任务在 2 秒内失败且
    agent 从未启动（见 #92017 同类现场）。复用前先量化，再决定是否对齐基线。

    Returns:
        {exists, ahead, behind, pushed}；判不出时 ahead/behind 为 -1。
        - pushed: 本地 tip 已包含在 origin/<branch>（提交推过，重置不丢东西）。
    先 fetch origin <base> 刷新判据；fetch 失败（只读 gitdir/离线）沿用已有 refs。
    """
    st = {"exists": False, "ahead": -1, "behind": -1, "pushed": False}
    if not base or not branch or not base_branch:
        return st
    _run_git(["-C", base, "fetch", "origin", base_branch], token=token, timeout=30,
             repo_key=repo_key)
    r = _run_git(["-C", base, "show-ref", "--verify", "--quiet",
                  f"refs/heads/{branch}"], token=token)
    st["exists"] = r is not None and r.returncode == 0
    if not st["exists"]:
        return st
    rel = _run_git(["-C", base, "rev-list", "--left-right", "--count",
                    f"refs/heads/{branch}...origin/{base_branch}"], token=token)
    if rel is not None and rel.returncode == 0:
        try:
            _a, _b = (rel.stdout or "").split()[:2]
            st["ahead"], st["behind"] = int(_a), int(_b)
        except Exception:
            pass
    remote = _run_git(["-C", base, "rev-parse", "--verify", "--quiet",
                       f"refs/remotes/origin/{branch}"], token=token)
    if remote is not None and remote.returncode == 0 and (remote.stdout or "").strip():
        anc = _run_git(["-C", base, "merge-base", "--is-ancestor",
                        f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"], token=token)
        st["pushed"] = anc is not None and anc.returncode == 0
    return st


def _preserve_diverged_branch(base: str, branch: str, issue_num, token: str = "") -> str:
    """把分叉本地分支的 tip 存进 `refs/grape-preserved/issue-N-<sha12>`。

    重置分支前必须先落这个 ref，保证「不复用」不会变成「删工作」。
    """
    tip = _run_git(["-C", base, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}"], token=token)
    sha = (tip.stdout or "").strip() if tip is not None and tip.returncode == 0 else ""
    if not sha:
        return ""
    ref = f"refs/grape-preserved/issue-{issue_num}-{sha[:12]}"
    upd = _run_git(["-C", base, "update-ref", ref, sha], token=token)
    if upd is None or upd.returncode != 0:
        log.warning(f"workspace.preserve_branch_failed issue={issue_num} "
                    f"branch={branch} sha={sha[:12]}")
        return ""
    log.warning(f"workspace.branch_preserved issue={issue_num} agent_branch={branch} "
                f"sha={sha[:12]} ref={ref}")
    return ref


def _reset_branch_to_baseline(base: str, branch: str, base_branch: str,
                              issue_num, token: str = "") -> bool:
    """把本地分支重置到 origin/<base>（调用前须已 _preserve_diverged_branch）。"""
    r = _run_git(["-C", base, "branch", "-f", branch, f"origin/{base_branch}"], token=token)
    if r is None or r.returncode != 0:
        log.warning(f"workspace.branch_reset_failed issue={issue_num} branch={branch}: "
                    f"{((r.stderr if r is not None else '') or '')[:200]}")
        return False
    log.warning(f"workspace.branch_baseline_reset issue={issue_num} branch={branch} "
                f"baseline={base_branch} (diverged branch aligned before reuse)")
    return True


# ---------------------------------------------------------------------------
# Issue #3923：裸仓 HEAD 校验与自愈
#
# 裸仓 HEAD 必须是 `ref: refs/heads/*`。某台 worker 上曾被写成
# `ref: refs/remotes/origin/<baseline>`，此后**每次** `git worktree add` 都必然
# `fatal: HEAD not found below refs/heads!`；而建树失败后的归因又落在凭据上，
# 把 git 侧故障伪装成「仓库没配凭据」。这里在建树入口先校验、非法即自愈。
# ---------------------------------------------------------------------------

def bare_head_ref(base: str, token: str = "") -> str:
    """读取裸仓 HEAD 的符号引用目标（如 `refs/heads/main`）；读不到返回空串。"""
    r = _run_git(["-C", base, "symbolic-ref", "-q", "HEAD"],
                 token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
    if r is None or r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _repair_branch_candidates(base: str, preferred: str, token: str = "") -> list:
    """候选修复分支名：优先基线，其次任一远端分支，再任一本地分支（去重保序）。"""
    candidates: list = []

    def _add(name: str) -> None:
        name = (name or "").strip()
        if name and name not in candidates:
            candidates.append(name)

    _add(preferred)
    r = _run_git(["-C", base, "for-each-ref", "--format=%(refname:short)",
                  "refs/remotes/origin"], token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
    if r is not None and r.returncode == 0:
        for name in (r.stdout or "").splitlines():
            name = (name or "").strip()
            if not name or name.endswith("/HEAD"):
                continue
            _add(name.split("/", 1)[-1])
    r = _run_git(["-C", base, "for-each-ref", "--format=%(refname:short)",
                  "refs/heads"], token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
    if r is not None and r.returncode == 0:
        for name in (r.stdout or "").splitlines():
            _add(name)
    return candidates


def _ensure_local_branch_for_head(base: str, branch: str, token: str = "") -> str:
    """确保 `refs/heads/<branch>` 存在（缺则从 origin 建）；返回错误串，成功空串。"""
    head_ref = f"refs/heads/{branch}"
    has_local = _run_git(["-C", base, "show-ref", "--verify", "--quiet", head_ref],
                         token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
    if has_local is not None and has_local.returncode == 0:
        return ""
    remote_ref = f"refs/remotes/origin/{branch}"
    has_remote = _run_git(["-C", base, "show-ref", "--verify", "--quiet", remote_ref],
                          token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
    if has_remote is None or has_remote.returncode != 0:
        return f"分支 {branch} 本地与远端都不存在"
    cr = _run_git(["-C", base, "update-ref", head_ref, remote_ref],
                  token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
    if cr is None or cr.returncode != 0:
        return (f"从 {remote_ref} 建本地分支 {branch} 失败："
                f"{((cr.stderr if cr is not None else '') or 'git exec failed')[:200]}")
    return ""


def ensure_bare_head(base: str, preferred_branch: str = "", token: str = "",
                     repo_key: str = "", stage: str = "") -> dict:
    """校验裸仓 HEAD 位于 `refs/heads/*`，非法则修到可用分支（Issue #3923）。

    Returns:
        {"ok", "previous", "current", "repaired", "branch", "detail"}
        ok=False 只表示「修不了」（如仓库无任何分支），调用方据此决定是否继续建树。

    为什么要自愈而不是只报错：HEAD 指到 `refs/remotes/*` 是**本机裸仓的陈旧状态**，
    仓库本身完好（远端分支齐全），修一行即可恢复；不自愈就会让该机所有新建树任务
    持续失败，且失败被误归因成凭据问题（Issue #3923 的现场形态）。
    """
    out = {"ok": False, "previous": "", "current": "", "repaired": False,
           "branch": "", "detail": ""}
    if not base or not _is_git_dir(base):
        out["detail"] = "bare base not found"
        return out
    previous = bare_head_ref(base, token=token)
    out["previous"] = previous
    valid = previous.startswith("refs/heads/")
    if valid:
        chk = _run_git(["-C", base, "show-ref", "--verify", "--quiet", previous],
                       token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if chk is not None and chk.returncode == 0:
            out.update(ok=True, current=previous, branch=previous[len("refs/heads/"):])
            return out
        valid = False
    candidates = _repair_branch_candidates(base, preferred_branch, token=token)
    if not candidates:
        out["detail"] = (f"HEAD={previous or 'unreadable'} 不在 refs/heads/* 下，"
                         f"且仓库没有可用于修复的分支")
        log.warning(f"repogit head repair impossible repo={repo_key or '-'} "
                    f"stage={stage or '-'} {out['detail']}")
        return out
    details = []
    for branch in candidates:
        err = _ensure_local_branch_for_head(base, branch, token=token)
        if err:
            details.append(err)
            continue
        head_ref = f"refs/heads/{branch}"
        sr = _run_git(["-C", base, "symbolic-ref", "HEAD", head_ref],
                      token=token, timeout=_GIT_WRITE_PROBE_TIMEOUT)
        if sr is None or sr.returncode != 0:
            details.append(f"HEAD 指回 {head_ref} 失败："
                           f"{((sr.stderr if sr is not None else '') or 'git exec failed')[:200]}")
            continue
        out.update(ok=True, current=head_ref, repaired=True, branch=branch)
        log.warning(f"repogit head repaired repo={repo_key or '-'} stage={stage or '-'} "
                    f"previous={previous or 'unreadable'} current={head_ref} "
                    f"(bare HEAD must live under refs/heads/*, Issue #3923)")
        return out
    out["detail"] = ("HEAD " + (previous or "unreadable") + " 非法，修复失败："
                     + "；".join(details)[:400])
    log.warning(f"repogit head repair failed repo={repo_key or '-'} "
                f"stage={stage or '-'} {out['detail']}")
    return out


# 建树失败阶段（写进错误面的「已执行到哪一步」，只陈述事实）
WORKTREE_STAGE_ADD = "worktree_add"
WORKTREE_STAGE_HEAD_REPAIR = "worktree_add_head_repair"
WORKTREE_STAGE_BASE = "repo_base"
# 建树函数自身抛异常/目录建不出来（os.makedirs、_write_askpass 等）的出口：
# 这类失败没有 git stderr 可透传，但同样**必须**留下现场，否则任务不阻断。
WORKTREE_STAGE_BUILD_EXCEPTION = "worktree_build_exception"

# 阶段 → 事实措辞（Issue #3923 审查 Required）：阶段名不能一律渲染成
# 「git worktree add 失败」，否则 repo_base（clone/fetch 失败）会被误述。
_WORKTREE_STAGE_FAILURE_TEXT = {
    WORKTREE_STAGE_BASE: "基线 fetch/clone 失败",
    WORKTREE_STAGE_ADD: "git worktree add 失败",
    WORKTREE_STAGE_HEAD_REPAIR: "HEAD 自愈后重试仍失败",
    WORKTREE_STAGE_BUILD_EXCEPTION: "建树过程异常（目录/文件系统）",
}

# 阶段优先级（Issue #3923 复审 Required）：同一任务键**不全量覆盖**。
# base（init/fetch）失败先于 worktree add，后者覆盖会把 401/网络原文抹掉；
# 优先级低的新现场不得顶掉已记录的更高优先级现场（同等优先级允许后写覆盖，
# 例如同一次调用内 add → head_repair 的推进）。数值越大越接近根因。
WORKTREE_STAGE_PRIORITY = {
    WORKTREE_STAGE_BASE: 3,
    WORKTREE_STAGE_HEAD_REPAIR: 2,
    WORKTREE_STAGE_ADD: 1,
    WORKTREE_STAGE_BUILD_EXCEPTION: 1,
}

# 最近一次建树失败的现场（供失败后的错误面透传 git 原文与失败阶段）
# key = `<repo_key>|<issue>|<agent>`（无 issue/agent 时用 repo_key）；只保留最近一次，
# 避免无限增长。**不存 token/凭据**。
_LAST_WORKTREE_FAILURE: dict = {}
# 现场条数上限。来源（业务要求，Issue #3923）：单 worker 并发建树的 (repo, issue,
# agent) 三元组规模实测远小于该值，200 是「同机全仓失败现场各留一份」的安全上界，
# 只防内存无限增长，不参与任何判定语义；超限丢最旧（按写入时间）。
_LAST_WORKTREE_FAILURE_MAX = 200
# 仓级兜底记录（无 issue/agent 三元组时）的保留时长：过期记录不再当作当前失败
# 原因（见 worktree_last_failure 的 TTL 判定）。三元组记录不设 TTL，由建树成功/
# 入口清理；Issue #3923 复审后失败现场在 ensure_repo_base 内直接写三元组键，
# 不再经过「仓级 → 三元组」复制。
# 数值来源（业务要求，Issue #3923）：仓级键只在「建树入口未带 issue/agent」的
# 调用路径产生，读取方是紧接着同一仓的下一批任务；15 分钟覆盖「同仓连续派单的
# 常规间隔」的观察窗口，目标是避免上上轮的仓级失败被后来的任务误读为当前原因
# ——窗口应大于派单重试间隔、小于一次值班排查的会话时长。若需精确值，
# 需实测「仓级失败 → 下一任务读现场」的实际间隔分布再定（待决策项已登记）。
_LAST_WORKTREE_FAILURE_REPO_TTL = 900


def _record_worktree_failure(repo_key: str, stage: str, stderr: str,
                             head_repair: dict = None, issue_num=None,
                             agent_type: str = "") -> None:
    key = _failure_key(repo_key, issue_num, agent_type)
    prev = _LAST_WORKTREE_FAILURE.get(key) or {}
    prev_stage = prev.get("stage") or ""
    # Issue #3923 复审 Required：同一键按阶段优先级保留现场。一次建树里
    # `ensure_repo_base` 失败（401/网络原文）先写入，随后回落到既有裸仓再
    # `worktree add` 又失败——旧实现直接覆盖，把真正的 git 原文抹掉。
    if prev_stage and (WORKTREE_STAGE_PRIORITY.get(stage, 0)
                       < WORKTREE_STAGE_PRIORITY.get(prev_stage, 0)):
        log.info(f"repogit keep earlier failure scene repo={repo_key or '-'} "
                 f"issue={issue_num or '-'} agent={agent_type or '-'} "
                 f"kept_stage={prev_stage} dropped_stage={stage} "
                 f"dropped_stderr={(stderr or '')[:160]}")
        return
    _LAST_WORKTREE_FAILURE[key] = {
        "stage": stage,
        "stderr": (stderr or "").strip()[:600],
        "head_repair": dict(head_repair or {}),
        "issue_num": str(issue_num or ""),
        "agent_type": agent_type or "",
        "at": time.time(),
    }
    # 失败现场只是诊断信息，不随失败任务数无限增长：超上限丢最旧的（按写入时间）
    if len(_LAST_WORKTREE_FAILURE) > _LAST_WORKTREE_FAILURE_MAX:
        for _k, _ in sorted(_LAST_WORKTREE_FAILURE.items(),
                            key=lambda kv: kv[1].get("at", 0))[:len(_LAST_WORKTREE_FAILURE)
                                                          - _LAST_WORKTREE_FAILURE_MAX]:
            _LAST_WORKTREE_FAILURE.pop(_k, None)


def _failure_key(repo_key: str, issue_num=None, agent_type: str = "") -> str:
    """失败现场的键：有 Issue/Agent 时按三元组隔离，避免并发任务互相覆盖。"""
    if issue_num or agent_type:
        return f"{repo_key}|{issue_num or ''}|{agent_type or ''}"
    return repo_key


def worktree_last_failure(repo_key: str, issue_num=None, agent_type: str = "") -> dict:
    """取最近一次建树失败现场（按 (repo, issue, agent) 优先，回落 repo；无记录空 dict）。

    Issue #3923 审查：仓级兜底记录不 pop，同仓并发任务都要能读到同一份现场；
    这里用 TTL 收敛，避免很久以前的仓级失败被后来的任务当成当前原因。
    """
    rk = repo_key or ""
    if not rk:
        return {}
    if issue_num or agent_type:
        hit = _LAST_WORKTREE_FAILURE.get(_failure_key(rk, issue_num, agent_type))
        if hit:
            return dict(hit)
    repo_hit = _LAST_WORKTREE_FAILURE.get(rk, {}) or {}
    if not repo_hit:
        return {}
    if time.time() - float(repo_hit.get("at") or 0) > _LAST_WORKTREE_FAILURE_REPO_TTL:
        return {}
    return dict(repo_hit)


def clear_worktree_failure(repo_key: str = "", issue_num=None,
                           agent_type: str = "") -> None:
    """清除建树失败现场（建树入口/成功后调用）。

    传 issue_num/agent_type 时只清该三元组（并发任务互不影响）；
    只传 repo_key 时清该仓全部记录；都不传则全清。
    """
    if not repo_key:
        _LAST_WORKTREE_FAILURE.clear()
        return
    if issue_num or agent_type:
        _LAST_WORKTREE_FAILURE.pop(_failure_key(repo_key, issue_num, agent_type), None)
        return
    for key in [k for k in list(_LAST_WORKTREE_FAILURE)
                if k == repo_key or k.startswith(f"{repo_key}|")]:
        _LAST_WORKTREE_FAILURE.pop(key, None)


# 凭据不可用分类 → 任务面措辞（Issue #3713）。分类枚举由 kernel 选择器给出
# （`CREDENTIAL_STAGES`），这里只做「渲染」，不重复判定。
_CREDENTIAL_STAGE_HINTS = {
    "empty": "仓库 bot 凭据值为空或被撤销",
    "undecryptable": "仓库 bot 凭据密文无法解密（密钥不匹配或行被破坏）",
    "expired": "仓库 bot 凭据已标记过期",
    "not_configured": "该仓库未配置任何可用凭据（无 bot，也无归属者/会话用户凭据）",
}


def repo_base_has_refs(repo_key: str) -> "bool | None":
    """本地 bare 基线是否已有远端分支：True/False 可用，None = 基线不存在或探测失败。

    为什么需要：`worktree add origin/main` 失败有两类完全不同原因 ——
      · 空仓（远端 0 分支）：凭据再好也建不出树；
      · 凭据缺失：取不到 token，mirror refs=0。
    只看 token 是否为空会把前者也归因成凭据问题（Issue #21 第一层就是空仓）。
    """
    try:
        base = repo_base_dir(repo_key)
    except Exception:  # noqa: BLE001
        return None
    if not base or not os.path.isdir(base):
        return None
    r = _run_git(["-C", base, "for-each-ref", "--count=1", "--format=%(refname)",
                  "refs/remotes/origin"], token="")
    if r is None or r.returncode != 0:
        return None
    return bool((r.stdout or "").strip())


def worktree_credential_error(repo_key: str, token_result=None,
                              failure_scene=None) -> str:
    """建树失败后的凭据归因（Issue #3713）：返回 `<code>: <message>`，否则空串。

    为什么需要：token 取空 → mirror refs=0 → `git worktree add` 失败 → 平台只看得到
    「git dir not found」→ `GRAPE_GIT_WRITABLE=false` → agent 自述「git 元数据只读」、
    零交付仍 completed（与 #3711 同一条收口通道）。凭据才是真因时，必须把
    **分类（stage）** 带进任务错误，而不是渲染成 git 只读。

    判据（只做归因，不改既有语义）：
      · 凭据可用（token 非空）→ 返回空串：建树失败原因在 git 侧，走原告警路径；
      · 凭据不可用           → 返回 `credential_<stage>`，stage ∈ 选择器的分类枚举，
                              无 rc 行且全链为空时用 not_configured。

    Issue #3923 收敛：只有「归因**跑完了**且结论是没凭据」（attribution=no_credential）
    才允许落 `credential_not_configured`。worker 进程没有装配层（`vcs_provider`
    端口未绑），归因本身不成立（unavailable/unattributed）→ 返回空串，由
    `worktree_failure_error()` 如实透传 git 原文与失败阶段。这样「归因不可用」与
    「确实无凭据」在任务错误与日志中可区分。

    `token_result`：调用方已跑过一次 `resolve_exec_token` 时传入，避免单次阻断
    把同一条凭据链重复解析（Issue #3923 审查 Optional）。

    `failure_scene`：本任务已记录的失败现场（`worktree_last_failure` 的结果）。
    空仓断言只在「**没有** repo_base 阶段现场」时成立——fetch/init 失败本身就会
    让本地 `refs/remotes/origin` 为 0，此时空仓是失败的**结果**而非原因，再报
    `empty_repo` 会把 401/网络原文抹掉（Issue #3923 复审 Required）。
    """
    if not repo_key:
        return ""
    scene_stage = (failure_scene or {}).get("stage") or ""
    # 空仓优先：远端 0 分支时凭据再好也建不出树 —— 先把事实说清，不归因到凭据。
    # 但已有 repo_base 阶段现场时不得短路：那是 git 侧失败的后果，原文才是真因。
    if scene_stage != WORKTREE_STAGE_BASE and repo_base_has_refs(repo_key) is False:
        return ("empty_repo: 仓库还没有任何提交（远端 0 条分支）；"
                "请先在仓库放置一个初始提交后再派任务（这不是凭据问题）")
    try:
        from src.kernel.token_resolver import (
            CREDENTIAL_ATTRIBUTION_UNAVAILABLE, CREDENTIAL_ATTRIBUTION_UNATTRIBUTED,
            resolve_exec_token,
        )
        sel = token_result if token_result is not None else resolve_exec_token(repo_key)
    except Exception as e:  # noqa: BLE001 - 归因失败不得反过来打断任务
        log.warning(f"repogit: credential attribution failed {repo_key}: {e}")
        return ""
    if getattr(sel, "token", ""):
        return ""
    stage = getattr(sel, "stage", "") or ""
    attribution = getattr(sel, "attribution", "") or ""
    if not stage and attribution in (CREDENTIAL_ATTRIBUTION_UNAVAILABLE,
                                     CREDENTIAL_ATTRIBUTION_UNATTRIBUTED):
        # 归因不可用/未归因：这是「查不了」，不是「没有」。不得落 not_configured。
        log.warning(f"repogit: credential attribution {attribution} for {repo_key}: "
                    f"{getattr(sel, 'attribution_detail', '') or '-'}")
        return ""
    stage = stage or "not_configured"
    hint = _CREDENTIAL_STAGE_HINTS.get(stage, "凭据不可用")
    return (f"credential_{stage}: {hint}；建树取 token 为空，本轮无法 clone/fetch")


def worktree_failure_error(repo_key: str, issue_num=None, agent_type: str = "",
                           token_result=None, failure_scene=None) -> str:
    """建树失败的错误面（Issue #3923）：凭据归因成立才报凭据，否则透传 git 原文。

    worker 侧不做 repo token 归因（无装配层），因此这里的主路径是「如实上报」：

      `<stage>: <阶段事实措辞>: <git stderr 原文>`（含 HEAD 修复结果）

    只有 `worktree_credential_error()` 明确归因为凭据问题（或空仓）时才返回
    `credential_<stage>` / `empty_repo`。无失败现场返回空串（保持既有告警路径）。

    `token_result`：调用方已解析过凭据链时传入，凭据归因与归因备注复用同一结果，
    单次阻断只解析一次（Issue #3923 审查 Optional）。

    `failure_scene`：调用方已读过的失败现场，传入即不再重复读（也便于先读现场
    再问凭据的次序在单一入口处可见，Issue #3923 复审 Required）。
    """
    # 先读现场、再问凭据：空仓断言与凭据归因都要能看见「git 侧已经失败在哪一步」，
    # 否则 fetch 失败导致的本地 0 refs 会被渲染成 empty_repo / 凭据问题。
    fail = (failure_scene if failure_scene is not None
            else worktree_last_failure(repo_key, issue_num=issue_num,
                                       agent_type=agent_type))
    cred = worktree_credential_error(repo_key, token_result=token_result,
                                     failure_scene=fail)
    if cred:
        return cred
    if not fail:
        return ""
    stage = fail.get("stage") or WORKTREE_STAGE_ADD
    stderr = (fail.get("stderr") or "").strip()
    repair = fail.get("head_repair") or {}
    failure_text = _WORKTREE_STAGE_FAILURE_TEXT.get(stage, "git worktree add 失败")
    parts = [f"{stage}: {failure_text}"]
    if repair:
        prev = repair.get("previous") or "unreadable"
        cur = repair.get("current") or "-"
        if repair.get("repaired"):
            parts.append(f"已执行 HEAD 自愈：{prev} → {cur}")
        elif repair.get("detail"):
            parts.append(f"HEAD 自愈未成功：{repair.get('detail')}")
    if stderr:
        parts.append(f"git 原文：{stderr}")
    # Issue #3923：事件错误里也要能看出「归因不可用」≠「确实无凭据」
    try:
        from src.kernel.token_resolver import (
            CREDENTIAL_ATTRIBUTION_UNAVAILABLE, CREDENTIAL_ATTRIBUTION_UNATTRIBUTED,
            resolve_exec_token,
        )
        _sel = token_result if token_result is not None else resolve_exec_token(repo_key)
        if (getattr(_sel, "attribution", "") in (CREDENTIAL_ATTRIBUTION_UNAVAILABLE,
                                                 CREDENTIAL_ATTRIBUTION_UNATTRIBUTED)
                and not getattr(_sel, "token", "")):
            parts.append("凭据归因不可用（未据此判定为凭据问题）")
    except Exception as e:  # noqa: BLE001 - 注释性信息失败不得打断错误上报
        log.warning(f"repogit: worktree failure attribution note failed {repo_key}: {e}")
    return "；".join(parts)


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

    Issue #2245：基线/gitdir 只读时不再直接失败——只读基线跳过 fetch 复用，worktree 建/复用时
    自检 git 写权限，不可写则迁移到会话可写 gitdir（或降级标注只读）。
    Issue #2281：分支 ref 仍在时复用分支并标记「已找回」；ref 消失时从归档 bundle 恢复上一轮
    未推送 commit（仅在新建的基线 worktree 上执行，无归档/失败仅告警）。
    """
    if not is_git_agent(agent_type):
        return None
    # Issue #3923：本入口重试前先清掉该仓的旧失败现场，避免把上一轮的 git 原文
    # 当成这一轮的失败原因；新失败会在下面重新记录。按 (repo, issue, agent)
    # 精确清理，并发任务互不覆盖。
    clear_worktree_failure(repo_key, issue_num=issue_num, agent_type=agent_type)
    _base_dir = repo_base_dir(repo_key)
    if _is_git_dir(_base_dir) and not probe_path_writable(_base_dir)[0]:
        base = _base_dir
        log.warning(f"repogit: shared base is read-only, reuse as-is: {_base_dir}")
    else:
        # Issue #3923：issue/agent 下传 ensure_repo_base，init/remote add/fetch/异常
        # 失败直接写 (repo, issue, agent) 三元组键——并发任务各自持有自己的 git 原文，
        # 不再经过「仓级 → 三元组」复制（后写覆盖先写的竞态随之消失）。
        base = ensure_repo_base(repo_key, token, issue_num=issue_num,
                                agent_type=agent_type) or (_base_dir if _is_git_dir(_base_dir) else None)
    if not base:
        return None
    issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
    # Issue #3923 复审 Required：目录建不出来（父路径是普通文件/磁盘满/权限）
    # 必须在**本函数内**写失败现场。旧实现把 makedirs 放在 try 之外，异常一路
    # 冒到 agent 的宽 except 只 WARNING 一条 → 无现场 → 任务在无工作区状态下继续跑。
    try:
        os.makedirs(os.path.dirname(issue_dir), exist_ok=True)
    except Exception as e:  # noqa: BLE001 - 失败已记录，按不可建树处理
        log.warning(f"repogit worktree dir create failed {repo_key}: {e}")
        _record_worktree_failure(
            repo_key, WORKTREE_STAGE_BUILD_EXCEPTION,
            f"ensure_issue_worktree dir create failed: {e}",
            issue_num=issue_num, agent_type=agent_type)
        return None
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
                else:
                    # 已存在的 git 工作区若停在「既 ahead 又 behind」的本地 feat
                    # 分支上，复用必然被基线闸门拦下（sre 2 秒 failed，agent 从未
                    # 启动）。检测/发布类环境、或提交已推到远端的，按分叉拒绝复用，
                    # 走下面的重建对齐路径；coding 未推提交仍保留复用（稍后阻断）。
                    _st = _branch_vs_baseline(base, branch, base_branch, token=token, repo_key=repo_key)
                    if (_st["ahead"] > 0 and _st["behind"] > 0
                            and not is_inspector_agent(agent_type)
                            and (agent_type in _REUSE_CLEAN_AGENTS or _st["pushed"])):
                        reason = "diverged_ahead"
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
            _branch_reused = False
            if not reuse:
                br = _run_git(["-C", base, "show-ref", "--verify", "--quiet",
                               f"refs/heads/{branch}"], token=token)
                _branch_reused = br is not None and br.returncode == 0
                if _branch_reused:
                    # Issue #2281：分支 ref 仍在 → 上一轮未推送 commit 随分支保留。
                    # 但「本地 feat 分支既 ahead 又 behind」时不直接复用：sre 会把
                    # coding 未合并提交带进工作区，闸门随即判 diverged，任务 2 秒
                    # 就 failed（#92017）。检测/发布类环境或提交已推远端的，先把旧
                    # tip 存 refs/grape-preserved/ 再把分支对齐基线，用干净树建树；
                    # 作者类 coding 且提交未推 → 保持阻断（不允许静默丢工作）。
                    _st = _branch_vs_baseline(base, branch, base_branch, token=token, repo_key=repo_key)
                    if _st["ahead"] > 0 and _st["behind"] > 0:
                        if not is_inspector_agent(agent_type) and (
                                agent_type in _REUSE_CLEAN_AGENTS or _st["pushed"]):
                            _pres_ref = _preserve_diverged_branch(
                                base, branch, issue_num, token=token)
                            if _pres_ref and _reset_branch_to_baseline(
                                    base, branch, base_branch, issue_num, token=token):
                                _st = _branch_vs_baseline(base, branch, base_branch, token=token, repo_key=repo_key)
                            log.warning(
                                f"workspace.reuse_branch_aligned issue={issue_num} "
                                f"agent={agent_type} preserved={_pres_ref or '-'} "
                                f"ahead_after={_st['ahead']} behind_after={_st['behind']}")
                        else:
                            log.warning(
                                f"workspace.diverged_branch_kept issue={issue_num} "
                                f"agent={agent_type} ahead={_st['ahead']} behind={_st['behind']} "
                                f"(unpushed commits; baseline gate will block by design)")
                    r = _run_git(["-C", base, "worktree", "add", "--force", issue_dir,
                                  branch], token=token)
                else:
                    r = _run_git(["-C", base, "worktree", "add", issue_dir, "-b", branch,
                                  f"origin/{base_branch}"], token=token)
                stderr = ""
                if r is None or r.returncode != 0:
                    stderr = (r.stderr if r is not None else "git exec failed") or ""
                    log.warning(f"repogit worktree add failed {repo_key}: {stderr[:300]}")
                    # Issue #3923：HEAD 非法（如指到 refs/remotes/*）时先自愈再重试一次。
                    # 这是本机裸仓的陈旧状态，不重试就会让该机所有新建树任务持续失败，
                    # 且失败被归因到凭据。仅重试一次，失败即返回。
                    _repair = {"ok": False}
                    if "head not found below refs/heads" in stderr.lower():
                        _repair = ensure_bare_head(
                            base, base_branch, token=token, repo_key=repo_key,
                            stage=WORKTREE_STAGE_HEAD_REPAIR)
                    if _repair.get("ok"):
                        log.warning(f"repogit worktree add retrying after HEAD repair "
                                    f"{repo_key} issue={issue_num} agent={agent_type} "
                                    f"previous={_repair.get('previous') or '-'} "
                                    f"current={_repair.get('current')}")
                        if _branch_reused:
                            r = _run_git(["-C", base, "worktree", "add", "--force",
                                          issue_dir, branch], token=token)
                        else:
                            r = _run_git(["-C", base, "worktree", "add", issue_dir,
                                          "-b", branch, f"origin/{base_branch}"], token=token)
                        if r is not None and r.returncode == 0:
                            stderr = ""
                        else:
                            stderr = (r.stderr if r is not None else "git exec failed") or ""
                    if stderr:
                        _record_worktree_failure(
                            repo_key,
                            WORKTREE_STAGE_ADD if not _repair.get("ok")
                            else WORKTREE_STAGE_HEAD_REPAIR,
                            stderr, head_repair=_repair,
                            issue_num=issue_num, agent_type=agent_type)
                if stderr:
                    # stderr 非空 = 重试后仍失败（或本就不需要重试）
                    # Issue #2245：仅当失败源于只读挂载/权限时降级建树；其它失败
                    # （如分支已被其它 worktree 占用）降级会制造同分支双工作树，直接失败。
                    if not _is_readonly_git_failure(stderr):
                        log.warning(f"repogit worktree add non-readonly failure, refuse degrade {repo_key}")
                        return None
                    if not _create_worktree_with_session_gitdir(
                            repo_key, issue_dir, base, branch, base_branch, token=token):
                        return None
                else:
                    # Issue #2245：新建 worktree 后自检 git 写权限（admin dir 可能位于只读卷）
                    _git_status = ensure_worktree_git_writable(repo_key, issue_dir, token=token)
                    if not _git_status.get("writable"):
                        log.warning(f"repogit: new worktree in read-only git mode {repo_key}: "
                                    f"{_git_status.get('reason', '')}")
            else:
                # Issue #2245：复用预置 worktree 时先自检 git 写权限；只读则迁移到会话可写 gitdir
                _git_status = ensure_worktree_git_writable(repo_key, issue_dir, token=token)
                if not _git_status.get("writable"):
                    log.warning(f"repogit: reuse worktree in read-only git mode {repo_key}: "
                                f"{_git_status.get('reason', '')}")
            _write_askpass(issue_dir, repo_key)
            if not reuse and _branch_reused:
                # Issue #2281：分支 ref 仍在 → 上一轮 commit 随分支保留，标记
                # 「已自动找回（source=branch）」，下一轮提示先重试 push。
                _br_head = _run_git(["-C", issue_dir, "rev-parse", "HEAD"], token=token)
                _mark_auto_restored(
                    repo_key, issue_num, agent_type,
                    head=((_br_head.stdout or "").strip()
                          if _br_head is not None and _br_head.returncode == 0 else ""),
                    source="branch")
            elif not reuse:
                # Issue #2281：分支 ref 已从 bare mirror 消失（重克隆/被清理）→ 从
                # 最近归档 bundle 找回上一轮未推送 commit（fetch + reset --hard
                # FETCH_HEAD）。只在刚新建的基线 worktree 上执行，不覆盖当轮改动；
                # 无归档/恢复失败仅告警，仍返回可用的基线 worktree。
                _restore = restore_worktree_from_bundle(
                    issue_dir, repo_key, issue_num, agent_type, token=token)
                if _restore.get("ok"):
                    _mark_auto_restored(repo_key, issue_num, agent_type,
                                        head=_restore.get("head", ""),
                                        bundle_path_arg=_restore.get("bundle_path", ""))
                    log.warning(
                        f"repogit: worktree rebuilt from archived bundle "
                        f"{repo_key} issue={issue_num} agent={agent_type} "
                        f"head={_restore.get('head', '')[:12]} "
                        f"bundle={_restore.get('bundle_path', '')}")
                elif _restore.get("bundle_path"):
                    log.warning(
                        f"repogit: bundle recovery failed, keep baseline worktree "
                        f"{repo_key} issue={issue_num} agent={agent_type} "
                        f"reason={_restore.get('reason', '')} "
                        f"bundle={_restore.get('bundle_path', '')}")
                else:
                    log.info(f"repogit: no archived bundle to recover "
                             f"{repo_key} issue={issue_num} agent={agent_type}")
            # foreign_kept：保留的是拒绝复用的外来脏树，不得在其分支上落保真基线/装守卫
            if not foreign_kept:
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
            clear_worktree_failure(repo_key, issue_num=issue_num, agent_type=agent_type)
            return issue_dir
        except WorkspaceBaselineError:
            raise
        except Exception as e:
            log.warning(f"repogit worktree failed {repo_key}: {e}")
            # Issue #3923 复审 Required：建树自身的异常出口必须写现场——
            # 旧实现只 warn 后 return None，agent 侧宽 except 再吞一条 → 无现场 →
            # worktree_block_result 返回 None → 任务无工作区继续执行。
            _record_worktree_failure(
                repo_key, WORKTREE_STAGE_BUILD_EXCEPTION,
                f"ensure_issue_worktree exception: {e}",
                issue_num=issue_num, agent_type=agent_type)
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


# Issue #2281：重建找回（ensure_issue_worktree → bundle fetch+reset）。以下两个
# 辅助函数只服务于「worktree 已删除/重置后，新回合把上一轮未推送 commit 找回来」：
# 归档由 cleanup_issue_worktree / worktree_guard.archive_worktree_before_reset 完成，
# 本段只负责选择最近 bundle 并恢复，不新增删除判据（清理判据仍与
# remove_issue_worktree_if_pushed「宁可残留、不可误删」一致）。

def latest_archived_bundle(repo_key: str, issue_num, agent_type: str = "") -> str:
    """该 (repo, issue, agent) 最近一次归档的 bundle 路径；无归档返回 ""。

    优先读机器可读重置记录（worktree_guard.reset_record_path）的 bundle_path；
    记录缺失/已消费/路径被清理时，回退扫描 archive/{issue}/*/worktree.bundle，
    取 mtime 最新者（归档目录命名带时间戳，扫描兜底兼容记录被删的情况）。
    读取失败一律返回 ""（调用方继续按基线重建，不阻断建树）。
    """
    try:
        from src.domains.runtime.engine.worktree_guard import (
            BUNDLE_NAME, load_reset_record, reset_archive_root)
        cands = []
        rec = load_reset_record(repo_key, issue_num, agent_type) if agent_type else None
        if rec and rec.get("bundle_path"):
            p = rec["bundle_path"]
            if os.path.isfile(p):
                cands.append((os.path.getmtime(p), p))
        if agent_type:
            root = os.path.join(reset_archive_root(repo_key), str(issue_num or "0"))
            if os.path.isdir(root):
                for name in os.listdir(root):
                    p = os.path.join(root, name, BUNDLE_NAME)
                    if os.path.isfile(p):
                        cands.append((os.path.getmtime(p), p))
        return max(cands)[1] if cands else ""
    except Exception as e:
        log.warning(f"repogit: latest archived bundle lookup failed "
                    f"{repo_key} issue={issue_num} agent={agent_type}: {e}")
        return ""


def _mark_auto_restored(repo_key: str, issue_num, agent_type: str,
                        head: str = "", bundle_path_arg: str = "",
                        source: str = "bundle") -> None:
    """把恢复事实写回重置记录（供下一轮 inject_reset_context 区分「已自动找回」）。

    source="bundle" 表示从归档 bundle fetch+reset 找回；source="branch" 表示
    bare mirror 的 feat/issue-N 分支 ref 仍保留、新 worktree 直接复用。
    失败仅告警（恢复本身已成功，不因记录写失败回滚）。
    """
    try:
        from src.domains.runtime.engine.worktree_guard import mark_worktree_restored
        mark_worktree_restored(repo_key, issue_num, agent_type,
                               head=head, bundle_path=bundle_path_arg,
                               source=source)
    except Exception as e:
        log.warning(f"repogit: mark auto-restored failed "
                    f"{repo_key} issue={issue_num} agent={agent_type}: {e}")


def restore_worktree_from_bundle(wt_dir: str, repo_key: str, issue_num,
                                 agent_type: str = "", token: str = "") -> dict:
    """Issue #2281：从最近归档 bundle 找回上一轮未推送 commit。

    调用时机：ensure_issue_worktree 已按基线新建 worktree（分支 ref 不可复用）
    之后。流程 `bundle verify` → `git fetch <bundle>` → `git reset --hard
    FETCH_HEAD`；只在新建的干净基线 worktree 上执行，不会覆盖当轮已有改动。

    返回 {"ok", "reason", "bundle_path", "head"}。无 bundle/校验失败/fetch 失败/
    reset 失败 → ok=False 且不改变 worktree（保留基线现场），调用方仅告警，
    不阻断建树（找回是增强，不是前置条件）。
    """
    res = {"ok": False, "reason": "no_bundle", "bundle_path": "", "head": ""}
    bundle_path = latest_archived_bundle(repo_key, issue_num, agent_type)
    if not bundle_path:
        return res
    res["bundle_path"] = bundle_path
    if not bundle_verify(wt_dir, bundle_path, token=token):
        res["reason"] = "bundle_verify_failed"
        return res
    ok, err = fetch_from_bundle(wt_dir, bundle_path, token=token)
    if not ok:
        res["reason"] = f"fetch_failed: {err}"
        return res
    r = _run_git(["-C", wt_dir, "reset", "--hard", "FETCH_HEAD"], token=token)
    if r is None or r.returncode != 0:
        res["reason"] = f"reset_failed: {(r.stderr if r else 'git exec failed')[:300]}"
        return res
    head = _run_git(["-C", wt_dir, "rev-parse", "HEAD"], token=token)
    res["head"] = ((head.stdout or "").strip()
                   if head is not None and head.returncode == 0 else "")
    res["ok"] = True
    res["reason"] = "restored"
    return res


def cleanup_issue_worktree(repo_key: str, agent_type: str, issue_num,
                           token: str = "", base_branch: str = "",
                           reason: str = "", task_id: str = "") -> bool:
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
    removed = not os.path.isdir(issue_dir)
    if not removed:
        log.warning(f"workspace.cleanup dir kept (remove failed, #1891 read-only gitdir?): "
                    f"issue={issue_num} agent={agent_type} path={issue_dir}")
    return removed


def _unpushed_archive_start(issue_dir: str, token: str = "") -> str:
    """未推送提交存档起点：优先 marker.baseline，兜底 origin/main|master|HEAD 的 merge-base。

    远程分支不存在（从未 push）时，仍应以 **mirror 基线**为起点存档本地新提交，
    否则区间证据链缺失。返回空串表示无法确定起点（调用方跳过区间存档）。
    """
    marker = _read_workspace_marker(issue_dir)
    refs = []
    baseline = str(marker.get("baseline") or "").strip()
    if baseline:
        refs.append(f"origin/{baseline}")
    refs += ["origin/main", "origin/master", "origin/HEAD"]
    seen = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        r = _run_git(["-C", issue_dir, "merge-base", "HEAD", ref], token=token)
        if r is not None and r.returncode == 0 and (r.stdout or "").strip():
            return (r.stdout or "").strip()
    return ""


def remove_issue_worktree_if_pushed(repo_key: str, agent_type: str, issue_num, token: str = "",
                                    task_id: str = "") -> bool:
    """任务终态清理 Issue 级 worktree（Issue #2514 收紧语义）。

    旧语义 = 仅当本地 HEAD 已与远程分支一致（已 push）才删，导致失败任务/未 push 轮次的
    工作区永久残留、跨 Issue 累积。新语义 = 终态必清，但**证据先存档**：

    - 脏树 → 先落 `<repo>/artifacts/workspace-<task_id>.patch`（`git diff HEAD --binary`
      + 白名单外 untracked 文件完整内容；task_id 缺省用 issue+agent 防同名覆盖）；
    - 未推送提交 → 追加 `log -p --binary <remote_head>..<HEAD>` 区间存档（含提交信息）；
      远程分支不存在（从未 push）时以 marker 基线 / `merge-base` 为起点补档；
    - `ls-remote` 失败（网络不可核对）→ 保留现场不删（返回 False），存档已落；
    - 清理失败只记日志，不阻断终态上报（调用方 _cleanup_task_workdir / _cleanup_direct_workdir）。

    返回 True=已删除；False=保留（网络不可核对 / 存档失败 / 清理失败 / 异常）。
    """
    if not is_git_agent(agent_type):
        return False
    try:
        issue_dir = issue_dir_for(repo_key, agent_type, issue_num)
        if not os.path.isdir(issue_dir):
            return False
        branch = f"feat/issue-{issue_num}"
        archive_label = _archive_workspace_label(issue_num, agent_type, task_id)
        # Issue #2514：脏树先存档（证据一次落全）；存档失败 → 保留工作区，不删
        dirty, _cur_branch, _cur_head = _worktree_state(issue_dir, token=token)
        if dirty > 0:
            if not _archive_workspace_patch(issue_dir, repo_key, task_id=archive_label,
                                            token=token):
                log.warning(f"workspace terminal cleanup deferred (archive failed) "
                            f"{repo_key} issue={issue_num} path={issue_dir}")
                return False
        head = _run_git(["-C", issue_dir, "rev-parse", "HEAD"], token=token)
        if head is None or not (head.stdout or "").strip():
            return False
        local_head = head.stdout.strip()
        remote = _run_git_retry(["ls-remote", clone_url_for(repo_key), f"refs/heads/{branch}"],
                                token=token, repo_key=repo_key)
        if remote is None or remote.returncode != 0:
            log.warning(f"workspace terminal cleanup deferred (remote unreachable) "
                        f"{repo_key} issue={issue_num} path={issue_dir} "
                        f"err={(remote.stderr or '')[:200] if remote is not None else 'git exec failed'}")
            return False  # 网络不可核对：保留（存档已落），待派发前巡检/下轮回收
        remote_out = (remote.stdout or "").strip()
        if remote_out:
            start_head = remote_out.split()[0]
        else:
            # 从未 push（远程无分支）：以 mirror 基线 / merge-base 为起点补档，不直接丢证据
            start_head = _unpushed_archive_start(issue_dir, token=token)
            if not start_head:
                log.warning(f"workspace terminal cleanup unpushed archive start unresolved "
                            f"{repo_key} issue={issue_num} path={issue_dir} (skip commit archive)")
        if start_head and local_head != start_head:
            # 未推送提交：区间 log -p 存档（含提交信息）后再移除，不丢码
            if not _archive_workspace_patch(
                    issue_dir, repo_key, task_id=archive_label, token=token,
                    suffix="-unpushed", log_range=(start_head, local_head)):
                log.warning(f"workspace terminal cleanup deferred (unpushed archive "
                            f"failed) {repo_key} issue={issue_num} path={issue_dir}")
                return False
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
                    if dirty > 0:
                        # 存档标签优先 marker.task_id，缺省用 issue+agent：同一轮巡检多份
                        # 超期脏目录不得都落到 workspace-unknown-patrol.patch 互相覆盖
                        label = _archive_workspace_label(num, agent, marker.get("task_id"))
                        if not _archive_workspace_patch(
                                agent_dir, repo_key, task_id=label, token="",
                                suffix="-patrol"):
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
    """host 派生：平台卡优先，`_HOSTS` 仅无卡时兜底（D105：卡是唯一真源）。

    改前是 `_HOSTS` 常量优先——卡里配置的内网镜像 / 自建域名会被静默忽略，那个常量
    本身就是第二真源；现在卡在则用卡，卡缺（数据未就绪）才回落存量常量。
    仍然抛 `PlatformCardMissingError`（带 code），保持 agent 侧"平台未注册 → 显式阻断"
    的既有语义，不让它落回通用异常后的一条 WARNING 就放行。
    """
    from src.domains.runtime.vcs.clone_endpoint import CloneEndpointError, clone_host
    from src.domains.runtime.vcs.platform_card import PlatformCardMissingError

    try:
        return clone_host(provider)
    except CloneEndpointError as e:
        host = _HOSTS.get(provider)
        if host:
            return host
        raise PlatformCardMissingError(provider, detail=str(e)) from e

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


