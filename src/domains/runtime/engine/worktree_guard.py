"""Issue #752（M2）：worktree 冲突标记推送守卫 + 非产物功能文件指纹保真校验。
+ Issue #2041：worktree 重置前置保全（reset archive guard）。

背景（PR #352 / #778 实证）：服务端自动提交/推送流程对 worktree 执行
`git add -A → commit → push` 盲目整树提交——worktree 中未解决的 merge 冲突
文本会被原样写入分支，覆盖已合入的功能修改（"自动复测产物清理损坏功能文件"
的具体通道）。

设计（docs/issue-752/plan.md §1.2）：
- 推送守卫：commit 前扫描暂存文件，硬命中 `<<<<<<< `/`- 保真校验（AC2）：任务开始（worktree 建/复用时）保存非产物功能文件 sha256 基线，
  清理前比对指纹——modified/removed 非空 → 告警（不阻断，人工裁决）；
  推送口不做保真告警（PR #874 审查修正：正常编码必然修改功能文件，推送口告警
  全是噪声；仅保留清理口信号）；
- 基线文件存 worktree 外（`<wt_dir>.fidelity.json` 兄弟文件），避免被 agent `add -A`
  误提交；
- 产物路径白名单（dist/build/out/node_modules 等）不参与指纹——它们是可清理产物，
  非保真对象。

Issue #2041（docs/issue-2041/plan.md）：worktree 删除/重置入口曾无条件销毁
`worktree remove --force` + `rmtree`——push 被阻断后未推送 commit、未提交改动、
未跟踪文件全部静默丢失且无告知（Issue #11 任务 t_3700b87233c0 实证整体返工）。
本文件提供「前置检查 → 归档保全 → 机器可读告知」三段式守卫：
- `collect_reset_risk`：未推送提交（`rev-list @{upstream}..HEAD`，无 upstream 回退
  基线分支）+ 白名单外的未提交/未跟踪改动；
- `archive_worktree_before_reset`：命中风险 → `git bundle create --all` +
  `git bundle verify` + `git diff HEAD --binary` 补丁 + untracked 文件快照 +
  manifest/record（任一步失败 → ok=False，调用方拒绝重置）；
- `inject_reset_context`：下一轮任务派发时把 record（worktree_reset/lost_commits/
  bundle_path/archived_at/reset_reason）注入 env + prompt，禁止「实现已在本地」
  的过时自述被无校验继承。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil

from src.kernel.config import log

BASELINE_SUFFIX = ".fidelity.json"

# 产物路径白名单：这些前缀下的文件是可清理产物（复测清理对象），不做指纹保真
_ARTIFACT_DIR_PREFIXES = (
    "dist/", "build/", "out/", "node_modules/", "__pycache__/",
    ".skills/", ".project-info/", ".knowledge/",
)

_MAX_FILE_BYTES = 5 * 1024 * 1024  # 指纹/扫描单文件上限（超大文件跳过，防内存放大）


def is_artifact_path(rel_path: str) -> bool:
    """相对路径是否命中产物白名单（产物=可清理对象，非保真对象）。"""
    normalized = rel_path or ""
    normalized = normalized.removeprefix("./")
    return any(normalized.startswith(prefix) for prefix in _ARTIFACT_DIR_PREFIXES)


def _sha256_file(path: str) -> str | None:
    try:
        size = os.path.getsize(path)
        if size > _MAX_FILE_BYTES:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _scan_file_text(path: str) -> dict:
    """读文件文本并扫描冲突标记（复用 merge_gate 的行首锚定口径）。"""
    try:
        from src.api.merge_gate import scan_text
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            return {"hard": 0, "soft": 0}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return scan_text(f.read())
    except Exception:
        return {"hard": 0, "soft": 0}


def _scan_staged_blob(wt_dir: str, name: str, token: str) -> dict | None:
    """读暂存区 blob（git show :name）并扫描——保证扫描对象=实际将提交的内容。

    PR #874 审查修正：文件暂存后又被修改时，磁盘内容 ≠ 提交内容，须扫 blob。
    `git show :name` 失败（如 unmerged 路径多 stage）→ 返回 None，调用方降级扫磁盘。
    """
    try:
        from src.domains.runtime.engine.repogit import _run_git
        b = _run_git(["-C", wt_dir, "show", f":{name}"], token=token)
        if b is None or b.returncode != 0:
            return None
        text = b.stdout or ""
        if len(text) > _MAX_FILE_BYTES:
            return {"hard": 0, "soft": 0}
        from src.api.merge_gate import scan_text
        return scan_text(text)
    except Exception:
        return None


def scan_staged_files(wt_dir: str, token: str = "") -> dict:
    """扫描暂存区文件内容的冲突标记（批量实现）。

    扫描对象仍是暂存 blob（index 内容，PR #874 审查修正语义不变）：文件暂存后
    又被修改时，与实际提交内容一致。

    实现（Issue #752 性能优化）：用 `git grep --cached -c` 以**单进程**扫描 index，
    替代原「逐文件 `git show :name`」的 N 次进程派生——300 文件暂存从 ~220ms 降到
    ~10ms，且文件数不再线性放大进程开销。unmerged 路径（index 无 stage-0 blob）
    仍降级扫工作区文件；git grep 异常时回退旧的逐文件实现（保证行为不丢）。

    返回 {"hard": {file: n}, "soft": {file: n}}；硬命中调用方应阻断 commit/push。
    """
    hard: dict = {}
    soft: dict = {}
    try:
        unmerged, changed = _staged_and_unmerged(wt_dir, token)
        if changed is None:
            # 批量扫描不可用（老 git / 异常）→ 回退旧的逐文件实现
            _legacy = _scan_staged_files_legacy(wt_dir, token=token)
            hard, soft = _legacy["hard"], _legacy["soft"]
        elif changed:
            # 口径与旧实现一致：只判本次暂存变更的文件，但扫描内容是整块 blob；
            # 单进程 git grep（小变更走 pathspec，大变更扫全 index 后取交集）。
            _hit = _grep_cached_markers(wt_dir, changed, token)
            if _hit is None:
                _legacy = _scan_staged_files_legacy(wt_dir, token=token)
                hard, soft = _legacy["hard"], _legacy["soft"]
            else:
                hard, soft = _hit
        # unmerged：index 无 stage-0（git grep / diff --cached 都不覆盖）→ 扫工作区
        for name in unmerged:
            path = os.path.join(wt_dir, name)
            if not os.path.isfile(path):
                continue
            got = _scan_file_text(path)
            if got["hard"]:
                hard[name] = got["hard"]
            if got["soft"]:
                soft[name] = got["soft"]
    except Exception as e:
        log.warning(f"worktree guard staged scan failed {wt_dir}: {e}")
    return {"hard": hard, "soft": soft}


def _staged_and_unmerged(wt_dir: str, token: str = "") -> tuple[list, set | None]:
    """一次 `git status` 同时取「本次暂存变更路径」与「unmerged 路径」。

    返回 (unmerged_list, changed_set)；执行失败 → ([], None)（调用方回退旧实现）。
    - 只看索引列（暂存），不看工作区列：git status --porcelain=v1 -z 的 `XY path`，
      X 为索引状态；unmerged 组合（DD/AU/UD/UA/DU/AA/UU）单列返回。
    - `--untracked-files=no`：避免大仓未跟踪文件拖慢（守卫不关心未跟踪）。
    """
    try:
        from src.domains.runtime.engine.repogit import _run_git
        r = _run_git(["-C", wt_dir, "status", "--porcelain=v1", "-z", "--untracked-files=no"],
                     token=token)
        if r is None or r.returncode != 0:
            return [], None
        fields = (r.stdout or "").split("\0")
        changed: set = set()
        unmerged: list = []
        i = 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if not entry or len(entry) < 3:
                continue
            xy, path = entry[:2], entry[3:]
            if xy in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
                if path:
                    unmerged.append(path)
                continue
            if xy[0] in "MADRC":  # 索引有变更（含 rename/copy 的新路径）
                if path:
                    changed.add(path)
                # -z 下 rename/copy 的「原路径」为紧随的独立字段，需跳过
                if xy[0] in "RC" and i < len(fields):
                    i += 1
        return sorted(set(unmerged)), changed
    except Exception as e:
        log.warning(f"worktree guard git status failed: {e}")
        return [], None


# 冲突标记的 ERE 形态（git grep -E）：等价的 Python 口径见 merge_gate.scan_text。
# 行首锚定 + \r? 兼容 CRLF；硬=<<<<<<</>>>>>>>，软=孤立 =======。
_MARKERS_ERE = r"^(<{7}|>{7})( .*)?\r?$|^={7}\r?$"
_MAX_PATHSPEC = 400  # pathspec 上限：超过则扫全 index 后按变更集合取交集（防命令行过长）


def _classify_marker_line(text: str) -> str:
    """单行归类：'hard' / 'soft' / ''（口径对齐 merge_gate.scan_text）。"""
    line = text.rstrip("\r")
    core = line.rstrip()
    if (core.startswith("<" * 7) or core.startswith(">" * 7)) and (len(core) == 7 or core[7] == " "):
        return "hard"
    if line == "=======":
        return "soft"
    return ""


def _grep_cached_markers(wt_dir: str, changed: set, token: str = "") -> tuple | None:
    """**单进程**扫描 index 中变更文件的冲突标记 → ({hard: n}, {soft: n})。

    `git grep --cached -n -I -E <组合模式>`：一次进程同时拿到两类标记，按行归类计数。
    变更文件数 ≤ _MAX_PATHSPEC 时用 pathspec 只扫这些文件（小推送更快）；超过则扫
    全 index 后按变更集合取交集。执行失败/异常 → None（调用方回退旧实现）。
    """
    from src.domains.runtime.engine.repogit import _run_git
    whole_index = len(changed) > _MAX_PATHSPEC
    args = ["-C", wt_dir, "grep", "--cached", "-n", "-I", "-E", _MARKERS_ERE, "--"]
    if not whole_index:
        args.extend(f":(literal){p}" for p in sorted(changed))
    try:
        r = _run_git(args, token=token)
        if r is None:
            return None
        if r.returncode == 1:  # 无命中
            return {}, {}
        if r.returncode != 0:
            log.warning(f"worktree guard git grep rc={r.returncode}: {(r.stderr or '')[:200]}")
            return None
        hard: dict = {}
        soft: dict = {}
        for line in (r.stdout or "").splitlines():
            # 输出格式：<path>:<lineno>:<content>
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            name, _lineno, content = parts[0], parts[1], parts[2]
            if whole_index and name not in changed:
                continue
            kind = _classify_marker_line(content)
            if kind == "hard":
                hard[name] = hard.get(name, 0) + 1
            elif kind == "soft":
                soft[name] = soft.get(name, 0) + 1
        return hard, soft
    except Exception as e:
        log.warning(f"worktree guard git grep failed: {e}")
        return None


def _scan_staged_files_legacy(wt_dir: str, token: str = "") -> dict:
    """旧实现（逐文件 `git show :name`），仅在批量扫描不可用时回退。"""
    hard: dict = {}
    soft: dict = {}
    try:
        from src.domains.runtime.engine.repogit import _run_git
        r = _run_git(["-C", wt_dir, "diff", "--cached", "--name-only", "-z"], token=token)
        if r is None or r.returncode != 0:
            return {"hard": hard, "soft": soft}
        names = [n for n in (r.stdout or "").split("\0") if n]
        for name in names:
            got = _scan_staged_blob(wt_dir, name, token)
            if got is None:
                # 降级：unmerged 路径无单 stage blob，扫工作区文件（git 冲突标记在工作区）
                path = os.path.join(wt_dir, name)
                if not os.path.isfile(path):
                    continue
                got = _scan_file_text(path)
            if got["hard"]:
                hard[name] = got["hard"]
            if got["soft"]:
                soft[name] = got["soft"]
    except Exception as e:
        log.warning(f"worktree guard staged scan failed {wt_dir}: {e}")
    return {"hard": hard, "soft": soft}


def snapshot_functional_files(wt_dir: str, token: str = "") -> dict:
    """非产物功能文件指纹快照：git ls-files 列 tracked 文件，排除产物白名单。"""
    out: dict = {}
    try:
        from src.domains.runtime.engine.repogit import _run_git
        r = _run_git(["-C", wt_dir, "ls-files", "-z"], token=token)
        if r is None or r.returncode != 0:
            return out
        for name in [n for n in (r.stdout or "").split("\0") if n]:
            if is_artifact_path(name):
                continue
            digest = _sha256_file(os.path.join(wt_dir, name))
            if digest:
                out[name] = digest
    except Exception as e:
        log.warning(f"worktree guard snapshot failed {wt_dir}: {e}")
    return out


def _baseline_path(wt_dir: str) -> str:
    # worktree 外兄弟文件（wt_dir 本身就是 issue 级 agent 目录），不会被 add -A 提交
    return f"{wt_dir.rstrip('/')}{BASELINE_SUFFIX}"


def save_baseline(wt_dir: str, token: str = "") -> dict:
    """任务开始时保存功能文件指纹基线（=保真校验的"清理前"快照）。"""
    snap = snapshot_functional_files(wt_dir, token=token)
    try:
        with open(_baseline_path(wt_dir), "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"worktree guard baseline write failed {wt_dir}: {e}")
    return snap


def load_baseline(wt_dir: str) -> dict:
    try:
        with open(_baseline_path(wt_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def check_delivery_alignment(wt_dir: str, delivery_sha: str, token: str = "") -> dict:
    """QA 前置自检（Issue #1954）：工作区检出 sha 与被检交付 sha 一致性门禁。

    返回 {"ok", "head_sha", "delivery_sha", "reason", "message"}：
    - delivery_sha 缺失 / HEAD 不可读 → 降级放行（reason 显式标注，调用方告警）；
    - HEAD == delivery_sha → 放行；
    - 不一致 → 阻断（ok=False），文案为「基线不一致」而非「输入缺失」。
    """
    delivery = (delivery_sha or "").strip()
    out = {"ok": True, "head_sha": "", "delivery_sha": delivery, "reason": "", "message": ""}
    if not delivery:
        out["reason"] = "delivery_sha_missing"
        out["message"] = "未提供被检交付 sha，跳过基线一致性自检（降级放行）"
        return out
    try:
        from src.domains.runtime.engine.repogit import worktree_head_sha
        head = worktree_head_sha(wt_dir, token=token)
    except Exception as e:
        out["reason"] = "head_unreadable"
        out["message"] = f"无法读取工作区检出 sha，跳过自检（降级放行）：{e}"
        return out
    out["head_sha"] = head
    if not head:
        out["reason"] = "head_unreadable"
        out["message"] = "无法读取工作区检出 sha，跳过自检（降级放行）"
        return out
    if head != delivery:
        out["ok"] = False
        out["reason"] = "baseline_mismatch"
        out["message"] = (
            f"基线不一致：QA 工作区检出 sha={head[:12]} 与被检交付 sha={delivery[:12]} 不一致，"
            f"阻断执行以避免检测错误产物。")
        log.warning(f"worktree guard delivery mismatch {wt_dir}: head={head} delivery={delivery}")
        return out
    out["reason"] = "aligned"
    return out


def verify_fidelity(wt_dir: str, token: str = "") -> dict:
    """保真校验（AC2）：当前功能文件指纹 vs 基线。

    返回 {"modified": [...], "removed": [...], "baseline_missing": bool}。
    modified/removed 非空 = 非产物功能文件被改动/删除（复测清理损坏功能文件的证据），
    调用方告警（不阻断，人工裁决）。
    """
    baseline = load_baseline(wt_dir)
    if not baseline:
        return {"modified": [], "removed": [], "baseline_missing": True}
    current = snapshot_functional_files(wt_dir, token=token)
    modified = sorted(k for k, v in baseline.items() if current.get(k) not in (v, None))
    removed = sorted(k for k in baseline if k not in current)
    return {"modified": modified, "removed": removed, "baseline_missing": False}


# ===========================================================================
# Issue #2041：worktree 重置前置保全（reset archive guard）
# ===========================================================================

# 归档目录：{WORKSPACE_DIR}/{provider}/{owner}/{repo}/.grape/archive/{issue}/
# （worktree 之外的仓库级持久目录，删除/重建 worktree 不会波及；Spec 兜底保留策略）
RESET_ARCHIVE_DIRNAME = os.path.join(".grape", "archive")
BUNDLE_NAME = "worktree.bundle"
MANIFEST_NAME = "manifest.json"
UNTRACKED_SNAPSHOT_DIR = "untracked"
DIRTY_PATCH_NAME = "dirty.patch"

# 脏检测白名单：平台物化的轻文件与凭据辅助脚本不是「待保全改动」，不触发归档
#（口径对齐 repogit 复用物化产物：.skills/.project-info/.knowledge；askpass 由
# repogit._write_askpass 写入；.grape-workspace.json 为 #1939 归属标记，前向兼容）
_RESET_IGNORE_NAMES = (".git-askpass.sh", ".git-askpass-wrapper.sh", ".grape-workspace.json")
_RESET_IGNORE_PREFIXES = (".skills/", ".project-info/", ".knowledge/")

_MAX_LOST_COMMITS = 200          # manifest 记录的未推送提交上限
_MAX_DIRTY_LIST = 200            # manifest 记录的脏文件清单上限
_MAX_UNTRACKED_FILES = 2000      # untracked 快照文件数上限（超出记清单截断）
_MAX_UNTRACKED_FILE_BYTES = 10 * 1024 * 1024  # 单文件快照上限（超大文件跳过）


def _is_reset_ignored_path(rel_path: str) -> bool:
    """相对路径是否命中重置保全白名单（平台轻文件不触发归档）。"""
    p = (rel_path or "").strip().strip('"')
    if not p:
        return True
    if p in _RESET_IGNORE_NAMES:
        return True
    return any(p.startswith(prefix) for prefix in _RESET_IGNORE_PREFIXES)


def _porcelain_entries_all(wt_dir: str, token: str = "") -> list:
    """`git status --porcelain=v1 -z --untracked-files=all` → [(xy, path)]。

    -uall 展开未跟踪目录到文件级（新增包目录要逐文件保全）；rename/copy 的
    原路径是紧随的独立字段，需跳过。执行失败 → []。
    """
    from src.domains.runtime.engine.repogit import _run_git
    try:
        r = _run_git(["-C", wt_dir, "status", "--porcelain=v1", "-z",
                      "--untracked-files=all"], token=token)
        if r is None or r.returncode != 0:
            return []
        fields = (r.stdout or "").split("\0")
        out, i = [], 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if not entry or len(entry) < 4:
                continue
            xy, path = entry[:2], entry[3:]
            if entry[0] in "RC" and i < len(fields):
                i += 1
            if path:
                out.append((xy, path))
        return out
    except Exception:
        return []


def _resolve_compare_ref(wt_dir: str, token: str = "", base_branch: str = "") -> str:
    """未推送判定的对比 ref（Spec：`@{upstream}` 优先，无 upstream 对比基线分支）。

    候选链：@{upstream} → origin/<base_branch> → origin/main → origin/master →
    本地 <base_branch>/main/master。全部不可解析 → ""（调用方走无 ref 兜底）。
    """
    from src.domains.runtime.engine.repogit import _run_git
    candidates = ["@{upstream}"]
    if base_branch:
        candidates.append(f"refs/remotes/origin/{base_branch}")
        candidates.append(f"refs/heads/{base_branch}")
    candidates += ["refs/remotes/origin/main", "refs/remotes/origin/master",
                   "refs/heads/main", "refs/heads/master"]
    for ref in candidates:
        r = _run_git(["-C", wt_dir, "rev-parse", "--verify", "--quiet",
                      f"{ref}^{{commit}}"], token=token)
        if r is not None and r.returncode == 0:
            return ref
    return ""


def collect_reset_risk(wt_dir: str, token: str = "", base_branch: str = "") -> dict:
    """重置前置检查：未推送提交 + 白名单外的未提交/未跟踪改动。

    返回 {"risk", "dirty", "untracked", "unpushed_count", "lost_commits",
          "compare_ref", "branch", "head"}：
    - 未推送：`rev-list --count <ref>..HEAD`；无对比 ref 时以
      `rev-list HEAD --not --remotes` 兜底（HEAD 未被任一 remote-tracking ref
      包含即视为未推送），计数失败保守视为有风险（unpushed_count=-1）；
    - dirty/untracked 为白名单外路径清单（各带上限）。
    """
    out = {"risk": False, "dirty": [], "untracked": [], "unpushed_count": 0,
           "lost_commits": [], "compare_ref": "", "branch": "", "head": ""}
    from src.domains.runtime.engine.repogit import _run_git
    try:
        head = _run_git(["-C", wt_dir, "rev-parse", "HEAD"], token=token)
        if head is None or head.returncode != 0:
            return out  # 空仓（无任何 commit）→ 无可丢内容
        out["head"] = (head.stdout or "").strip()
        br = _run_git(["-C", wt_dir, "rev-parse", "--abbrev-ref", "HEAD"], token=token)
        if br is not None and br.returncode == 0:
            out["branch"] = (br.stdout or "").strip()

        ref = _resolve_compare_ref(wt_dir, token=token, base_branch=base_branch)
        out["compare_ref"] = ref
        range_arg = f"{ref}..HEAD" if ref else "HEAD --not --remotes"
        cnt = _run_git(["-C", wt_dir, "rev-list", "--count"] + range_arg.split(),
                       token=token)
        if cnt is None or cnt.returncode != 0:
            # 无法计数 → 保守视为有未推送内容（宁可多归档，不放过丢失）
            out["unpushed_count"] = -1
        else:
            try:
                out["unpushed_count"] = int((cnt.stdout or "0").strip() or 0)
            except ValueError:
                out["unpushed_count"] = -1
        if out["unpushed_count"] > 0:
            lst = _run_git(["-C", wt_dir, "rev-list"] + range_arg.split(),
                           token=token)
            if lst is not None and lst.returncode == 0:
                out["lost_commits"] = [s for s in (lst.stdout or "").split()
                                       if s][:_MAX_LOST_COMMITS]

        for xy, path in _porcelain_entries_all(wt_dir, token=token):
            if _is_reset_ignored_path(path):
                continue
            if xy == "??":
                out["untracked"].append(path)
            else:
                out["dirty"].append(path)
        out["dirty"] = out["dirty"][:_MAX_DIRTY_LIST]
        out["untracked"] = out["untracked"][:_MAX_UNTRACKED_FILES]
        out["risk"] = bool(out["dirty"] or out["untracked"] or out["unpushed_count"] != 0)
        return out
    except Exception as e:
        # 守卫自身异常 → 视为有风险（归档时 bundle 会再失败 → 调用方拒绝重置）
        log.warning(f"worktree reset guard: risk collect failed {wt_dir}: {e}")
        out["risk"] = True
        return out


def reset_archive_root(repo_key: str) -> str:
    """归档根目录：workspace repo 目录下 .grape/archive（worktree 删除路径之外）。"""
    from src.kernel.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), *RESET_ARCHIVE_DIRNAME.split("/"))


def reset_record_path(repo_key: str, issue_num, agent_type: str) -> str:
    """机器可读最新记录：{archive}/{issue}/{agent}-reset.json（原子覆盖写）。"""
    return os.path.join(reset_archive_root(repo_key), str(issue_num or "0"),
                       f"{agent_type or 'agent'}-reset.json")


def _atomic_json_write(path: str, data: dict) -> bool:
    """原子写 JSON（tmp + rename；借用 checkpoint 的防半截策略）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.warning(f"worktree reset guard: record write failed {path}: {e}")
        return False


def _snapshot_untracked(wt_dir: str, archive_dir: str, untracked: list) -> list:
    """未跟踪文件内容快照（bundle 只含已提交对象，新增文件必须逐个拷贝）。

    单文件超过 _MAX_UNTRACKED_FILE_BYTES / 总数超过 _MAX_UNTRACKED_FILES 跳过
    （清单仍完整记录在 manifest）。目录按相对路径重建。
    """
    copied = []
    dst_root = os.path.join(archive_dir, UNTRACKED_SNAPSHOT_DIR)
    for rel in untracked[:_MAX_UNTRACKED_FILES]:
        src = os.path.join(wt_dir, rel)
        if not os.path.isfile(src):
            continue
        try:
            if os.path.getsize(src) > _MAX_UNTRACKED_FILE_BYTES:
                continue
            dst = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
        except Exception as e:
            log.warning(f"worktree reset guard: untracked snapshot skip {rel}: {e}")
    return copied


def archive_worktree_before_reset(wt_dir: str, repo_key: str, issue_num,
                                  agent_type: str, token: str = "",
                                  base_branch: str = "", reason: str = "",
                                  task_id: str = "") -> dict:
    """重置/删除前的归档保全（Issue #2041 核心）。

    流程：collect_reset_risk → 无风险原样返回（零副作用，AC-c/AC-d）；有风险：
    bundle create --all → bundle verify → dirty.patch（未提交改动）→ untracked
    快照 → manifest.json + {agent}-reset.json 机器可读记录。任一步失败 →
    ok=False（调用方必须拒绝重置）。

    返回 {"risk", "archived", "ok", "error", "archive_dir", "bundle_path",
          "record_path", "record"}。
    """
    from datetime import datetime, timezone
    import uuid
    from src.domains.runtime.engine.repogit import _run_git, bundle_create, bundle_verify

    res = {"risk": False, "archived": False, "ok": True, "error": "",
           "archive_dir": "", "bundle_path": "", "record_path": "", "record": {}}
    if not wt_dir or not os.path.isdir(wt_dir):
        return res
    risk = collect_reset_risk(wt_dir, token=token, base_branch=base_branch)
    res["risk"] = risk["risk"]
    if not risk["risk"]:
        return res

    archived_at = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = os.path.join(reset_archive_root(repo_key), str(issue_num or "0"),
                              f"{stamp}-{uuid.uuid4().hex[:8]}")
    bundle_path = os.path.join(archive_dir, BUNDLE_NAME)
    try:
        os.makedirs(archive_dir, exist_ok=True)
    except Exception as e:
        res["ok"] = False
        res["error"] = f"archive mkdir failed: {e}"
        return res

    ok, err = bundle_create(wt_dir, bundle_path, token=token)
    if not ok:
        res["ok"] = False
        res["error"] = f"bundle create failed: {err}"
        return res
    if not bundle_verify(wt_dir, bundle_path, token=token):
        res["ok"] = False
        res["error"] = "bundle verify failed"
        return res

    # 未提交改动：bundle 只含已提交对象，`git diff HEAD --binary` 单独保全
    dirty_patch = ""
    if risk["dirty"]:
        r = _run_git(["-C", wt_dir, "diff", "HEAD", "--binary"], token=token)
        if r is not None and (r.stdout or "").strip():
            dirty_patch = os.path.join(archive_dir, DIRTY_PATCH_NAME)
            try:
                with open(dirty_patch, "w", encoding="utf-8") as f:
                    f.write(r.stdout)
            except Exception as e:
                res["ok"] = False
                res["error"] = f"dirty patch write failed: {e}"
                return res

    snap_files = _snapshot_untracked(wt_dir, archive_dir, risk["untracked"])

    try:
        bundle_size = os.path.getsize(bundle_path)
    except Exception:
        bundle_size = 0
    record = {
        "worktree_reset": True,
        "lost_commits": risk["lost_commits"],
        "unpushed_count": risk["unpushed_count"],
        "dirty": risk["dirty"],
        "untracked": risk["untracked"],
        "untracked_snapshot": snap_files,
        "bundle_path": bundle_path,
        "bundle_size": bundle_size,
        "dirty_patch": dirty_patch,
        "archived_at": archived_at,
        "reset_reason": reason or "reset",
        "repo_key": repo_key,
        "issue_number": str(issue_num or ""),
        "agent": agent_type or "",
        "branch": risk["branch"],
        "head": risk["head"],
        "compare_ref": risk["compare_ref"],
        "task_id": task_id or "",
        "worker": os.environ.get("HOSTNAME", ""),
        "recovery": f"git fetch {bundle_path} && git reset --hard FETCH_HEAD",
    }
    if not _atomic_json_write(os.path.join(archive_dir, MANIFEST_NAME), record):
        res["ok"] = False
        res["error"] = "manifest write failed"
        return res
    # 机器可读最新记录（下一轮任务派发读取注入）
    record_path = reset_record_path(repo_key, issue_num, agent_type)
    if not _atomic_json_write(record_path, record):
        res["ok"] = False
        res["error"] = "reset record write failed"
        return res

    res.update({"archived": True, "archive_dir": archive_dir,
                "bundle_path": bundle_path, "record_path": record_path,
                "record": record})
    log.warning(
        f"worktree reset guard: archived {repo_key} issue={issue_num} "
        f"agent={agent_type} unpushed={risk['unpushed_count']} "
        f"dirty={len(risk['dirty'])} untracked={len(risk['untracked'])} "
        f"reason={record['reset_reason']} -> {bundle_path}")
    return res


def load_reset_record(repo_key: str, issue_num, agent_type: str) -> dict | None:
    """读取该 (repo, issue, agent) 的最新重置记录；缺失/损坏返回 None。"""
    try:
        with open(reset_record_path(repo_key, issue_num, agent_type),
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def parse_issue_workdir(path: str, workspace_dir: str = "") -> tuple:
    """从 issue workdir 绝对路径解析 (repo_key, issue_num, agent_type)。

    支持新结构 {provider}/{owner}/{repo}/issues/{n}/{agent} 与旧结构
    {owner}/{repo}/issues/{n}/{agent}；非该形态或非 git agent → ("", "", "")。
    """
    from src.domains.runtime.engine.repogit import is_git_agent
    try:
        from src.kernel.config import WORKSPACE_DIR
        ws = os.path.abspath(workspace_dir or WORKSPACE_DIR)
        rel = os.path.relpath(os.path.abspath(path), ws)
        if rel.startswith(".."):
            return "", "", ""
        parts = rel.split(os.sep)
        if len(parts) < 5 or parts[-3] != "issues":
            return "", "", ""
        agent = parts[-1]
        if not is_git_agent(agent):
            return "", "", ""
        identity = parts[:-3]
        if len(identity) == 3:
            provider, owner, repo = identity
        elif len(identity) == 2:
            provider, owner, repo = "gitcode", identity[0], identity[1]
        else:
            return "", "", ""
        if not provider or not owner or not repo:
            return "", "", ""
        return f"{provider}:{owner}/{repo}", parts[-2], agent
    except Exception:
        return "", "", ""


def guard_issue_workdir_removal(path: str, reason: str = "zombie_ttl",
                                token: str = "") -> bool:
    """僵尸/兜底目录删除前的保全闸（cleanup._zombie_issue_dirs 调用）。

    - 非 issue workdir 形态 / 非 git agent / 非 git 工作树 → 直接放行（保持既有
      清理行为，不误伤垃圾残留）；
    - 解析成功 → archive_worktree_before_reset（归档失败拒绝删除）；
    - 守卫自身异常 → 拒绝删除（保守保留现场）。
    返回 True=允许删除，False=保留。
    """
    try:
        if not (os.path.isfile(os.path.join(path, ".git"))
                or os.path.isdir(os.path.join(path, ".git"))):
            return True
        repo_key, issue_num, agent = parse_issue_workdir(path)
        if not repo_key or not issue_num or not agent:
            return True
        res = archive_worktree_before_reset(path, repo_key, issue_num, agent,
                                            token=token, reason=reason)
        if res.get("risk") and not res.get("ok"):
            log.warning(f"worktree reset guard: zombie removal refused {path}: "
                        f"{res.get('error', '')}")
            return False
        return True
    except Exception as e:
        log.warning(f"worktree reset guard: zombie guard failed {path}: {e}")
        return False


def inject_reset_context(task: dict, env: dict, prompt: str) -> tuple:
    """下一轮任务注入重置告知（worker _execute_task / direct_executor 调用）。

    读该 (repo_key, issue, agent) 的最新重置记录，命中：
    - env 注入 `GRAPE_WORKTREE_RESET=true` + `GRAPE_WORKTREE_RESET_INFO=<record JSON>`
      （机器可读，AC-b）；
    - prompt 追加显式提示块：本地实现已归档、不可假定仍在 worktree、恢复命令。
    无记录 / 非 git agent → 原样返回（零副作用）。返回 (prompt, injected)。
    """
    try:
        from src.domains.runtime.engine.repogit import issue_num_from, is_git_agent
        rk = (task or {}).get("repo_key") or ""
        ag = (task or {}).get("agent_type") or ""
        num = issue_num_from((task or {}).get("issue_id") or "")
        if not rk or not num or not is_git_agent(ag):
            return prompt, False
        record = load_reset_record(rk, num, ag)
        if not record or not record.get("worktree_reset"):
            return prompt, False
        env["GRAPE_WORKTREE_RESET"] = "true"
        env["GRAPE_WORKTREE_RESET_INFO"] = json.dumps(record, ensure_ascii=False)
        fields = {k: record.get(k) for k in
                  ("worktree_reset", "lost_commits", "bundle_path", "archived_at",
                   "reset_reason", "unpushed_count")}
        block = (
            "\n\n[grape runtime] NOTE: the worktree for this issue was reset at "
            f"{record.get('archived_at', '?')} (reason: {record.get('reset_reason', '?')}). "
            "Do NOT assume the previous local implementation is still in the worktree. "
            f"Unpushed work was archived to: {record.get('bundle_path', '?')} "
            f"(unpushed commits: {record.get('unpushed_count', '?')}). "
            "Recover with: git fetch <bundle_path> && git reset --hard FETCH_HEAD "
            "(env GRAPE_WORKTREE_RESET_INFO carries the full machine-readable record). "
            "Machine-readable context: "
            + json.dumps(fields, ensure_ascii=False)
            + "\n"
        )
        return prompt + block, True
    except Exception as e:
        log.warning(f"worktree reset guard: context inject failed: {e}")
        return prompt, False
