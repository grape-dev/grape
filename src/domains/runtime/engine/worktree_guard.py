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
- `collect_reset_risk`：未推送提交（本地 ref 链 `@{upstream}`→基线 ref→`--not
  --remotes` 先判，命中疑似未推送时用远端分支 SHA 复核——平台 push 用
  `HEAD:refs/heads/<branch>` 不带 `-u`，`@{upstream}` 仍指向基线分支，只靠本地
  判定会把已推送的干净 worktree 误判为有未推送内容）+ 白名单外的未提交/未跟踪改动；
  HEAD 不可解析（unborn/元数据损坏）不再早退放行，仍按工作区扫描判风险；扫描本身
  失败 → scan_failed=true 且一律判风险，不区分 HEAD 是否可解析（扫描失败不能
  用来证明「无内容可丢」）；
- `archive_worktree_before_reset`：命中风险 → `git bundle create HEAD`（只打 HEAD
  链，避免 `--all` 把整仓 refs 打包）+ `git bundle verify` + `git diff HEAD --binary`
  补丁（dirty 非空但 diff 失败/空输出 = tracked 改动未保全 → ok=False）+
  untracked 文件快照（跳过/拷贝失败 = 未保全 → ok=False）+ manifest/record
  （任一步失败 → ok=False，调用方拒绝重置）；scan_failed → dirty/untracked 清单
  不可枚举、归档无从保全 → 直接拒绝删除；归档树新建各级目录 0700、产物 0600；
- `inject_reset_context`：下一轮任务派发时把 record（worktree_reset/lost_commits/
  bundle_path/archived_at/reset_reason）注入 env + prompt，禁止「实现已在本地」
  的过时自述被无校验继承；注入带「仅当本轮工作区无改动才 reset」前置条件，并
  立即标记 consumed（同一记录只告知一次，避免多轮重复注入与误 reset）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

from src.kernel.config import log

BASELINE_SUFFIX = ".fidelity.json"

# 产物路径白名单：这些前缀下的文件是可清理产物（复测清理对象），不做指纹保真
_ARTIFACT_DIR_PREFIXES = (
    "dist/", "build/", "out/", "node_modules/", "__pycache__/",
    ".skills/", ".project-info/", ".knowledge/", ".org-context/",
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
_MAX_UNTRACKED_FILES = 2000      # untracked 快照文件数上限（超出 → 归档不完整 → 拒绝删除）
_MAX_UNTRACKED_FILE_BYTES = 10 * 1024 * 1024  # 单文件快照上限（超大 → 归档不完整 → 拒绝删除）

# 2026-11 事故复盘：未跟踪快照把 .venv 一起拷走（单份约 57MB），单个 issue 的
# 归档目录累积数百份快照把工作区盘打满 → 任务技能物化失败（ENOSPC），流水线 fail-fast。
# 可重建的大目录永不进快照（它们不是「待保全改动」，删了能重装）。
_SNAPSHOT_EXCLUDE_DIRS = frozenset({
    ".venv", "venv", "env", ".env", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "site-packages", ".tox", ".git", ".gradle",
    "target", "dist", "build",
})
_SNAPSHOT_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".so", ".o", ".class")
# 每个 issue 归档目录保留的快照份数（时间戳目录，按名字/时间淘汰旧档）。
# 五源 = 项目实测（2026-11-24 事故复盘）：排除可重建目录前单份归档 57MB，
# 排除后单份实测 ~1MB；即便回落到 57MB 量级，10 份 ≈ 0.6GB，相对工作区
# 可用空间（事故处置后 23GB）留足两个数量级余量；同时覆盖「最近 10 次
# 重置」的回滚排查窗口。调整请走 GRAPE_WORKTREE_ARCHIVE_KEEP（env 覆盖优先）。
_ARCHIVE_KEEP_DEFAULT = 10
_SNAPSHOT_NAME_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]+$")


def _is_snapshot_excluded(rel: str) -> bool:
    """未跟踪路径是否属于「可重建的大目录」（不进快照，也不计为 skipped）。"""
    parts = [p for p in re.split(r"[\\/]+", str(rel or "")) if p and p not in (".", "..")]
    if not parts:
        return True
    if any(seg in _SNAPSHOT_EXCLUDE_DIRS for seg in parts[:-1]):
        return True
    return parts[-1].lower().endswith(_SNAPSHOT_EXCLUDE_SUFFIXES)


def _archive_keep_limit() -> int:
    raw = (os.environ.get("GRAPE_WORKTREE_ARCHIVE_KEEP") or "").strip()
    try:
        return max(1, int(raw)) if raw else _ARCHIVE_KEEP_DEFAULT
    except ValueError:
        return _ARCHIVE_KEEP_DEFAULT

_REMOTE_PROBE_TIMEOUT = 60       # `ls-remote` 远端事实探测上限（网络抖动不影响保守判定）


def _is_reset_ignored_path(rel_path: str) -> bool:
    """相对路径是否命中重置保全白名单（平台轻文件不触发归档）。"""
    p = (rel_path or "").strip().strip('"')
    if not p:
        return True
    if p in _RESET_IGNORE_NAMES:
        return True
    return any(p.startswith(prefix) for prefix in _RESET_IGNORE_PREFIXES)


def _porcelain_entries_all(wt_dir: str, token: str = "") -> list | None:
    """`git status --porcelain=v1 -z --untracked-files=all` → [(xy, path)]。

    -uall 展开未跟踪目录到文件级（新增包目录要逐文件保全）；rename/copy 的原路径
    是紧随的独立字段，需跳过——索引侧（`X`）与工作区侧（`Y`）都可能出现 R/C
    （` R new\\0old\\0` 为未暂存 rename），两列都要判，否则后续条目错位。

    执行失败 → None（调用方据此区分「工作区无改动」与「扫描本身失败」，
    后者不可用来证明无内容可丢）。
    """
    from src.domains.runtime.engine.repogit import _run_git
    try:
        r = _run_git(["-C", wt_dir, "status", "--porcelain=v1", "-z",
                      "--untracked-files=all"], token=token)
        if r is None or r.returncode != 0:
            return None
        fields = (r.stdout or "").split("\0")
        out, i = [], 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if not entry or len(entry) < 4:
                continue
            xy, path = entry[:2], entry[3:]
            if (xy[0] in "RC" or xy[1] in "RC") and i < len(fields):
                i += 1
            if path:
                out.append((xy, path))
        return out
    except Exception:
        return None


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


def _remote_branch_head(wt_dir: str, branch: str, token: str = "",
                        repo_key: str = "") -> tuple:
    """远端事实：`git ls-remote --heads origin refs/heads/<branch>` → (probed, sha)。

    probed=False 表示远端不可达/探测失败（无网络、无 origin、鉴权失败），调用方
    必须回退本地 ref 链判定，不能把「探测失败」当成「分支未推送」。
    """
    from src.domains.runtime.engine.repogit import _run_git
    if not branch:
        return False, ""
    r = _run_git(["-C", wt_dir, "ls-remote", "--heads", "origin",
                  f"refs/heads/{branch}"], token=token, timeout=_REMOTE_PROBE_TIMEOUT,
                  repo_key=repo_key)
    if r is None or r.returncode != 0:
        return False, ""
    out = (r.stdout or "").strip()
    return True, (out.split()[0] if out else "")


def _collect_unpushed(out: dict, wt_dir: str, token: str = "",
                      base_branch: str = "", repo_key: str = "") -> None:
    """填充 out 的 unpushed_count / lost_commits / compare_ref / remote_head。

    两步判定：
    1. 本地 ref 链（零网络）：`@{upstream}`（worktree 分支由 `worktree add -b` 创建
       时指向基线 `origin/<base>`）→ 基线 ref → `--not --remotes`。计数为 0 即
       「相对基线无新增提交」→ 无可丢内容，直接返回；
    2. 本地判定「疑似有未推送」→ 远端事实复核：`ls-remote --heads origin
       refs/heads/<branch>`（平台 push 用 `HEAD:refs/heads/<branch>` 不带 `-u`，
       upstream 仍是基线，只靠第 1 步会把「已推送且干净」误判为有未推送内容）。
       远端 SHA==HEAD → unpushed=0；远端落后 → 以远端 SHA 为基点重算；
       远端无该分支 / 探测失败 → 保留第 1 步的保守结论。
    """
    from src.domains.runtime.engine.repogit import _run_git
    ref = _resolve_compare_ref(wt_dir, token=token, base_branch=base_branch)
    out["compare_ref"] = ref
    range_arg = f"{ref}..HEAD" if ref else "HEAD --not --remotes"
    cnt = _run_git(["-C", wt_dir, "rev-list", "--count"] + range_arg.split(), token=token,
                   repo_key=repo_key)
    if cnt is None or cnt.returncode != 0:
        # 无法计数 → 保守视为有未推送内容（宁可多归档，不放过丢失）
        out["unpushed_count"] = -1
        return
    try:
        out["unpushed_count"] = int((cnt.stdout or "0").strip() or 0)
    except ValueError:
        out["unpushed_count"] = -1
    if out["unpushed_count"] == 0:
        return
    if out["unpushed_count"] > 0:
        lst = _run_git(["-C", wt_dir, "rev-list"] + range_arg.split(), token=token)
        if lst is not None and lst.returncode == 0:
            out["lost_commits"] = [s for s in (lst.stdout or "").split()
                                   if s][:_MAX_LOST_COMMITS]

    probed, remote_sha = _remote_branch_head(wt_dir, out["branch"], token=token,
                                             repo_key=repo_key)
    if not (probed and remote_sha):
        return  # 探测失败 / 远端无该分支 → 保留本地保守结论
    out["remote_head"] = remote_sha
    out["compare_ref"] = f"ls-remote:origin/{out['branch']}"
    if remote_sha == out["head"]:
        out["unpushed_count"] = 0
        out["lost_commits"] = []
        return
    cnt = _run_git(["-C", wt_dir, "rev-list", "--count",
                    f"{remote_sha}..HEAD"], token=token)
    if cnt is None or cnt.returncode != 0:
        out["unpushed_count"] = -1
        return
    try:
        out["unpushed_count"] = int((cnt.stdout or "0").strip() or 0)
    except ValueError:
        out["unpushed_count"] = -1
        return
    if out["unpushed_count"] > 0:
        lst = _run_git(["-C", wt_dir, "rev-list", f"{remote_sha}..HEAD"], token=token)
        if lst is not None and lst.returncode == 0:
            out["lost_commits"] = [s for s in (lst.stdout or "").split()
                                   if s][:_MAX_LOST_COMMITS]
    else:
        out["lost_commits"] = []


def collect_reset_risk(wt_dir: str, token: str = "", base_branch: str = "",
                       repo_key: str = "") -> dict:
    """重置前置检查：未推送提交 + 白名单外的未提交/未跟踪改动。

    返回 {"risk", "dirty", "untracked", "unpushed_count", "lost_commits",
          "compare_ref", "branch", "head", "head_missing", "remote_head",
          "dirty_count", "untracked_count", "untracked_overflow", "scan_failed"}：
    - 未推送：本地 ref 链优先（零网络），疑似未推送时远端 `ls-remote` 复核
      （见 _collect_unpushed）；计数失败保守视为有风险（unpushed_count=-1）；
    - HEAD 不可解析（unborn / git 元数据损坏）不再早退放行：仍执行
      porcelain 扫描，按 dirty/untracked 判风险；
    - 工作区扫描失败（index.lock/索引损坏/超时/权限异常）→ scan_failed=true
      且一律判风险，不区分 HEAD 是否可解析（第 2 轮审查 Required#1：扫描失败
      不能用来证明「无内容可丢」，归档侧据此拒绝删除）；
    - dirty/untracked 为白名单外路径清单（各带上限，count 为真实总数）。
    """
    out = {"risk": False, "dirty": [], "untracked": [], "unpushed_count": 0,
           "lost_commits": [], "compare_ref": "", "branch": "", "head": "",
           "head_missing": False, "remote_head": "", "dirty_count": 0,
           "untracked_count": 0, "untracked_overflow": 0, "scan_failed": False,
           "untracked_excluded_count": 0}
    from src.domains.runtime.engine.repogit import _run_git
    try:
        head = _run_git(["-C", wt_dir, "rev-parse", "HEAD"], token=token)
        head_sha = ((head.stdout or "").strip()
                    if head is not None and head.returncode == 0 else "")
        out["head"] = head_sha
        if not head_sha:
            out["head_missing"] = True
            log.warning(f"worktree reset guard: HEAD unresolvable in {wt_dir}, "
                        f"fallback to worktree scan (no early pass)")
        br = _run_git(["-C", wt_dir, "rev-parse", "--abbrev-ref", "HEAD"], token=token)
        if br is not None and br.returncode == 0:
            name = (br.stdout or "").strip()
            out["branch"] = "" if name in ("", "HEAD") else name

        if head_sha:
            _collect_unpushed(out, wt_dir, token=token, base_branch=base_branch,
                              repo_key=repo_key)

        entries = _porcelain_entries_all(wt_dir, token=token)
        if entries is None:
            # 工作区扫描失败：无法证明「无内容可丢」（index.lock/索引损坏/
            # 超时/权限异常）→ 一律判风险并标记 scan_failed，不区分 HEAD 是否
            # 可解析（第 2 轮审查 Required#1：HEAD 可解析时曾直接放行=fail-open）
            out["risk"] = True
            out["scan_failed"] = True
            return out
        dirty, untracked = [], []
        excluded_untracked = 0
        for xy, path in entries:
            if _is_reset_ignored_path(path):
                continue
            if xy == "??" and _is_snapshot_excluded(path):
                # QA F1：可重建目录（.venv/node_modules/__pycache__ 等）必须在
                # 计数与 _MAX_UNTRACKED_FILES 截断**之前**剔除，否则它们既占 2000
                # 配额、又推高 untracked_overflow → 归档被判不完整 → 仍然拒绝重置
                # （2026-11-24 事故在 node_modules 仓库上会原样复发）。
                excluded_untracked += 1
                continue
            (untracked if xy == "??" else dirty).append(path)
        out["untracked_excluded_count"] = excluded_untracked
        out["dirty_count"] = len(dirty)
        out["untracked_count"] = len(untracked)
        out["dirty"] = dirty[:_MAX_DIRTY_LIST]
        out["untracked"] = untracked[:_MAX_UNTRACKED_FILES]
        out["untracked_overflow"] = max(0, len(untracked) - _MAX_UNTRACKED_FILES)
        out["risk"] = bool(out["dirty"] or out["untracked"]
                           or out["untracked_overflow"]
                           or out["unpushed_count"] != 0)
        return out
    except Exception as e:
        # 守卫自身异常 → 视为有风险且无法枚举内容（scan_failed），
        # 归档侧据此拒绝删除（宁可残留，不可丢改动）
        log.warning(f"worktree reset guard: risk collect failed {wt_dir}: {e}")
        out["risk"] = True
        out["scan_failed"] = True
        return out


def reset_archive_root(repo_key: str) -> str:
    """归档根目录：workspace repo 目录下 .grape/archive（worktree 删除路径之外）。"""
    from src.kernel.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(repo_key), *RESET_ARCHIVE_DIRNAME.split("/"))


def reset_record_path(repo_key: str, issue_num, agent_type: str) -> str:
    """机器可读最新记录：{archive}/{issue}/{agent}-reset.json（原子覆盖写）。"""
    return os.path.join(reset_archive_root(repo_key), str(issue_num or "0"),
                       f"{agent_type or 'agent'}-reset.json")


def _ensure_private_dir(path: str) -> None:
    """归档树目录 0700（含凭据类未跟踪文件时，同机其它任务不可读）。

    新建的每一级（.grape / archive / {issue} / 叶子）都收紧 0700，不再只对
    叶子目录 chmod（第 2 轮审查建议项：父目录按 umask 0755 会暴露归档清单
    元数据）；已存在的目录不回写（不动仓库工作区等外部属主目录）。
    """
    full = os.path.abspath(path)
    missing, cur = [], full
    while cur and not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    os.makedirs(path, exist_ok=True)
    for d in missing + [full]:
        try:
            os.chmod(d, 0o700)
        except Exception:
            pass


def _chmod_private(path: str) -> None:
    """归档产物 0600（best effort；权限位失败不影响保全语义）。"""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _atomic_json_write(path: str, data: dict) -> bool:
    """原子写 JSON（tmp + rename；借用 checkpoint 的防半截策略），产物 0600。"""
    try:
        _ensure_private_dir(os.path.dirname(path))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        _chmod_private(tmp)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.warning(f"worktree reset guard: record write failed {path}: {e}")
        return False


def _prune_archive_snapshots(issue_archive_dir: str) -> int:
    """按时间戳目录名保留最新 N 份快照，返回删除份数（best-effort，不影响归档结论）。

    快照目录名形如 ``20261124T045905Z-1b625ca2``，字典序即时间序；只处理本 issue
    归档目录下一层，不动 bundle/patch/manifest 文件。
    """
    try:
        names = sorted(
            d for d in os.listdir(issue_archive_dir)
            if _SNAPSHOT_NAME_RE.match(d)
            and os.path.isdir(os.path.join(issue_archive_dir, d))
        )
    except Exception:
        return 0
    keep = _archive_keep_limit()
    removed = 0
    for name in names[:-keep]:
        try:
            shutil.rmtree(os.path.join(issue_archive_dir, name), ignore_errors=True)
            removed += 1
        except Exception as e:  # noqa: BLE001 - 清理失败不影响归档主流程
            log.warning(f"worktree reset guard: prune snapshot failed {name}: {e}")
    if removed:
        log.info(f"worktree reset guard: pruned {removed} old snapshot(s) under "
                 f"{issue_archive_dir} (keep={keep})")
    return removed


def _snapshot_untracked(wt_dir: str, archive_dir: str, untracked: list,
                        overflow: int = 0) -> tuple:
    """未跟踪文件内容快照（bundle 只含已提交对象，新增文件必须逐个拷贝）。

    bundle/补丁都保不住未跟踪文件，因此「跳过」= 内容无法保全：单文件超过
    _MAX_UNTRACKED_FILE_BYTES、文件数超 _MAX_UNTRACKED_FILES（overflow）、
    非法类型、copy 抛异常，全部计入 skipped（reason + path），由调用方置
    ok=False 拒绝删除（AC-a：归档成功才放行）。

    返回 (copied, skipped)；skipped 元素形如
    {"path": "<rel>", "reason": "too_large|not_regular_file|copy_error|file_count_cap"}。

    第三返回值 excluded = 命中可重建大目录（.venv/node_modules/__pycache__ 等）被
    有意跳过的路径，仅作记录，**不**影响 archive_complete（见 _SNAPSHOT_EXCLUDE_DIRS）。
    """
    copied, skipped, excluded = [], [], []
    dst_root = os.path.join(archive_dir, UNTRACKED_SNAPSHOT_DIR)
    if overflow > 0:
        skipped.append({"path": "", "reason": "file_count_cap",
                        "skipped_count": int(overflow)})
    for rel in untracked[:_MAX_UNTRACKED_FILES]:
        if _is_snapshot_excluded(rel):
            # 可重建目录/产物：不进快照也不计入「不完整」，避免把 venv 拷进归档
            excluded.append(rel)
            continue
        src = os.path.join(wt_dir, rel)
        if not os.path.isfile(src):
            skipped.append({"path": rel, "reason": "not_regular_file"})
            continue
        try:
            size = os.path.getsize(src)
        except Exception as e:
            skipped.append({"path": rel, "reason": "copy_error", "error": str(e)[:200]})
            continue
        if size > _MAX_UNTRACKED_FILE_BYTES:
            skipped.append({"path": rel, "reason": "too_large", "size": size})
            continue
        try:
            dst = os.path.join(dst_root, rel)
            _ensure_private_dir(os.path.dirname(dst))
            shutil.copy2(src, dst)
            _chmod_private(dst)
            copied.append(rel)
        except Exception as e:
            log.warning(f"worktree reset guard: untracked snapshot failed {rel}: {e}")
            skipped.append({"path": rel, "reason": "copy_error", "error": str(e)[:200]})
    return copied, skipped, excluded


def _build_reset_record(risk: dict, repo_key: str, issue_num, agent_type: str,
                        bundle_path: str, dirty_patch: str,
                        snap_files: list, snap_skipped: list, archived_at: str,
                        reason: str, task_id: str,
                        snap_excluded: "list | None" = None) -> dict:
    """归档 manifest / 机器可读记录（同一份内容，落两处）。"""
    try:
        bundle_size = os.path.getsize(bundle_path) if bundle_path else 0
    except Exception:
        bundle_size = 0
    return {
        "worktree_reset": True,
        "archive_complete": not snap_skipped,
        "lost_commits": risk["lost_commits"],
        "unpushed_count": risk["unpushed_count"],
        "dirty": risk["dirty"],
        "untracked": risk["untracked"],
        "untracked_snapshot": snap_files,
        "untracked_skipped": snap_skipped,
        "untracked_excluded": list(snap_excluded or []),
        "untracked_overflow": risk.get("untracked_overflow", 0),
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
        "head_missing": bool(risk.get("head_missing")),
        "compare_ref": risk["compare_ref"],
        "remote_head": risk.get("remote_head", ""),
        "task_id": task_id or "",
        "worker": os.environ.get("HOSTNAME", ""),
        "recovery": f"git fetch {bundle_path} && git reset --hard FETCH_HEAD",
    }


def _archive_worktree_before_reset_impl(wt_dir: str, repo_key: str, issue_num,
                                  agent_type: str, token: str = "",
                                  base_branch: str = "", reason: str = "",
                                  task_id: str = "") -> dict:
    """重置/删除前的归档保全（Issue #2041 核心）。

    流程：collect_reset_risk → 无风险原样返回（零副作用，AC-c/AC-d）；
    scan_failed（工作区扫描失败/守卫异常）→ dirty/untracked 清单不可枚举、
    归档无从保全未提交内容 → 直接拒绝删除（不产「空归档」假保全）；有风险：
    bundle create（仅 HEAD 链）→ bundle verify → dirty.patch（dirty 非空但
    diff 失败/空输出 = tracked 改动未保全 → ok=False）→ untracked 快照
    （逐文件，跳过/失败即「未保全」）→ manifest.json + {agent}-reset.json
    机器可读记录。任一步失败或存在未保全的未跟踪文件 → ok=False（调用方必须
    拒绝重置，保留现场）。

    返回 {"risk", "archived", "ok", "error", "archive_dir", "bundle_path",
          "record_path", "record", "skipped"}。
    """
    from datetime import datetime, timezone
    import uuid
    from src.domains.runtime.engine.repogit import _run_git, bundle_create, bundle_verify

    res = {"risk": False, "archived": False, "ok": True, "error": "",
           "archive_dir": "", "bundle_path": "", "record_path": "", "record": {},
           "skipped": []}
    if not wt_dir or not os.path.isdir(wt_dir):
        return res
    risk = collect_reset_risk(wt_dir, token=token, base_branch=base_branch,
                              repo_key=repo_key)
    res["risk"] = risk["risk"]
    if not risk["risk"]:
        return res
    if risk.get("scan_failed"):
        # 工作区扫描失败（index.lock/索引损坏/超时/权限/守卫异常）：dirty/
        # untracked 清单不可枚举，bundle 只含已提交对象、归档无从保全未提交
        # 与未跟踪内容 → 拒绝删除保留现场（第 2 轮审查 Required#1，fail-closed：
        # 不产「空归档」假保全后放行删除）
        res["ok"] = False
        res["error"] = ("worktree scan failed: cannot enumerate "
                        "uncommitted/untracked content to archive")
        log.warning(f"worktree reset guard: refuse reset, worktree scan failed "
                    f"{repo_key} issue={issue_num} agent={agent_type}")
        return res

    archived_at = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = os.path.join(reset_archive_root(repo_key), str(issue_num or "0"),
                              f"{stamp}-{uuid.uuid4().hex[:8]}")
    bundle_path = os.path.join(archive_dir, BUNDLE_NAME)
    try:
        _ensure_private_dir(archive_dir)
    except Exception as e:
        res["ok"] = False
        res["error"] = f"archive mkdir failed: {e}"
        return res

    ok, err = bundle_create(wt_dir, bundle_path, token=token)
    if not ok:
        res["ok"] = False
        res["error"] = f"bundle create failed: {err}"
        return res
    _chmod_private(bundle_path)
    if not bundle_verify(wt_dir, bundle_path, token=token):
        res["ok"] = False
        res["error"] = "bundle verify failed"
        return res

    # 未提交改动：bundle 只含已提交对象，`git diff HEAD --binary` 单独保全。
    # dirty 非空说明存在 tracked 未提交改动：diff 执行失败（None/非零返回码）
    # 或输出为空 = 补丁未保全 → 拒绝删除（第 2 轮审查 Required#2：不留空的
    # dirty_patch 后 ok=True 删除 = 归档链 fail-open）。
    dirty_patch = ""
    if risk["dirty"]:
        r = _run_git(["-C", wt_dir, "diff", "HEAD", "--binary"], token=token)
        if r is None or r.returncode != 0 or not (r.stdout or "").strip():
            res["ok"] = False
            res["error"] = (f"dirty diff failed: {len(risk['dirty'])} tracked "
                            f"path(s) with uncommitted changes not archived")
            log.warning(f"worktree reset guard: dirty diff failed "
                        f"{repo_key} issue={issue_num} agent={agent_type} "
                        f"-> refuse reset")
            return res
        dirty_patch = os.path.join(archive_dir, DIRTY_PATCH_NAME)
        try:
            with open(dirty_patch, "w", encoding="utf-8") as f:
                f.write(r.stdout)
            _chmod_private(dirty_patch)
        except Exception as e:
            res["ok"] = False
            res["error"] = f"dirty patch write failed: {e}"
            return res

    snap_files, snap_skipped, snap_excluded = _snapshot_untracked(
        wt_dir, archive_dir, risk["untracked"],
        overflow=risk.get("untracked_overflow") or 0)
    record = _build_reset_record(risk, repo_key, issue_num, agent_type,
                                 bundle_path, dirty_patch, snap_files, snap_skipped,
                                 archived_at, reason, task_id,
                                 snap_excluded=snap_excluded)
    manifest_path = os.path.join(archive_dir, MANIFEST_NAME)
    if not _atomic_json_write(manifest_path, record):
        res["ok"] = False
        res["error"] = "manifest write failed"
        return res
    if snap_skipped:
        # 未跟踪内容无法完整保全（超限/拷贝失败）→ 拒绝删除，保留现场（AC-a）
        res.update({"ok": False, "skipped": snap_skipped,
                    "archive_dir": archive_dir, "bundle_path": bundle_path,
                    "record": record,
                    "error": (f"untracked snapshot incomplete: "
                              f"{len(snap_skipped)} item(s) not archived")})
        log.warning(f"worktree reset guard: archive incomplete {repo_key} "
                    f"issue={issue_num} agent={agent_type} "
                    f"untracked={risk['untracked_count']} "
                    f"skipped={len(snap_skipped)} -> refuse reset")
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


def archive_worktree_before_reset(wt_dir: str, repo_key: str, issue_num,
                                  agent_type: str, token: str = "",
                                  base_branch: str = "", reason: str = "",
                                  task_id: str = "") -> dict:
    """重置前归档（公开入口）：在 impl 之上统一做「失败不留残档 + 保留裁剪」。

    QA #2098 F2：裁剪原先只在归档成功路径执行，快照不完整（超大文件/拷贝失败）时
    直接 return，反复重试会每轮新建一份永不裁剪的归档目录 —— 正是 2026-11-24
    事故的形态。这里改为所有出口统一收口：

    - 失败（ok=False）：删除本次新建的 archive_dir（残档无保全价值，manifest 已
      记录失败原因），再执行该 issue 的保留裁剪；
    - 成功：保留最新 N 份（GRAPE_WORKTREE_ARCHIVE_KEEP 可调）；
    - 裁剪异常只告警，不影响归档结论（保全语义优先）。
    """
    res = _archive_worktree_before_reset_impl(
        wt_dir, repo_key, issue_num, agent_type, token=token,
        base_branch=base_branch, reason=reason, task_id=task_id)
    _archive_dir = str(res.get("archive_dir") or "")
    if _archive_dir:
        try:
            if not res.get("ok"):
                shutil.rmtree(_archive_dir, ignore_errors=True)
                res["discarded_archive_dir"] = _archive_dir
                res["archive_dir"] = ""
            res["pruned"] = _prune_archive_snapshots(os.path.dirname(_archive_dir))
        except Exception as e:  # noqa: BLE001 - 清理失败不影响归档结论
            log.warning(f"worktree reset guard: archive cleanup failed {_archive_dir}: {e}")
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


def mark_reset_record_consumed(repo_key: str, issue_num, agent_type: str,
                               task_id: str = "") -> bool:
    """标记记录已被某轮任务消费（`consumed_at` / `consumed_by_task`）。

    记录只用于「告知下一轮」，告知过即失效：否则同一 (repo, issue, agent) 之后
    每一轮都会注入「worktree 已被重置 + git reset --hard FETCH_HEAD」提示与
    env，多轮任务 prompt 持续膨胀，且 Agent 照抄恢复命令会丢弃当轮未提交改动。
    归档目录内 manifest 不受影响（永久归档证据保留）。
    """
    from datetime import datetime, timezone
    rec = load_reset_record(repo_key, issue_num, agent_type)
    if not rec:
        return False
    rec["consumed_at"] = datetime.now(timezone.utc).isoformat()
    rec["consumed_by_task"] = task_id or ""
    path = reset_record_path(repo_key, issue_num, agent_type)
    if not _atomic_json_write(path, rec):
        log.warning(f"worktree reset guard: mark record consumed failed {path}")
        return False
    log.info(f"worktree reset guard: record consumed "
             f"(repo={repo_key} issue={issue_num} agent={agent_type} task={task_id})")
    return True


def _parse_issue_segment(parts: list) -> tuple:
    """按尾部布局锚定解析 `issues/<segment>`（Issue #3441）。

    返回 (issue_segment, identity_parts, legacy)：
    - 同 Issue 多 PR `{...}/issues/{segment}/pr-<p>/{agent}`：
      `parts[-4] == "issues"` 且 `parts[-2].startswith("pr-")`（Issue #3375）；
    - 新 `{...}/issues/{segment}/{agent}`：`parts[-3] == "issues"`；
    - 过渡期 `{...}/issues/{segment}`：`parts[-2] == "issues"`。

    仅从路径尾部锚定，避免路径中更早出现的名为 `issues` 的目录/仓库段被误取；
    `pr-<p>` 分支须先于通用 `issues/{segment}/{agent}` 判定，否则会把 PR 维度
    目录名误当作 Issue 标识。不匹配 → (None, [], False)。`identity_parts` 为
    `issues` 之前的部分。
    """
    if (len(parts) >= 4 and parts[-4] == "issues"
            and parts[-2].startswith("pr-")):
        return parts[-3], parts[:-4], False
    if len(parts) >= 3 and parts[-3] == "issues":
        return parts[-2], parts[:-3], False
    if len(parts) >= 3 and parts[-2] == "issues":
        return parts[-1], parts[:-3], True
    return None, [], False


def parse_issue_workdir(path: str, workspace_dir: str = "") -> tuple:
    """从 issue workdir 绝对路径解析 (repo_key, issue_num, agent_type)。

    支持结构：
    - 新 `{provider}/{owner}/{repo}/issues/{n}/{agent}`；
    - 同 Issue 多 PR `{provider}/{owner}/{repo}/issues/{n}/pr-<p>/{agent}`（Issue #3375）；
    - issue-less PR `{provider}/{owner}/{repo}/prs/<p>/{agent}`（Issue #3375）；
    - 旧 `{owner}/{repo}/issues/{n}/{agent}`（provider 回退 gitcode）；
- 过渡期旧 `{repo}/{agent}/issues/{n}`（cleanup._zombie_issue_dirs 仍扫描该布局，
      不解析会绕开保全闸直接 rmtree）。
    非该形态 / 非 git agent / 过渡期旧布局与同 Issue 多 PR 布局的非数字 issue 号
    → ("", "", "")；新 `issues/{segment}/{agent}` 布局允许字符串标识（PR 订阅伪
    id、Gitee 字符串 Issue 标识），与 `extract_workdir_issue_num` 同口径。
    """
    from src.domains.runtime.engine.repogit import is_git_agent
    try:
        from src.kernel.config import WORKSPACE_DIR
        ws = os.path.abspath(workspace_dir or WORKSPACE_DIR)
        rel = os.path.relpath(os.path.abspath(path), ws)
        if rel.startswith(".."):
            return "", "", ""
        parts = rel.split(os.sep)
        if (len(parts) >= 6 and parts[-4] == "issues"
                and parts[-2].startswith("pr-")):
            # {..}/issues/{n}/pr-<p>/{agent}
            agent, issue_part = parts[-1], parts[-3]
            identity = parts[:-4]
            if not issue_part.isdigit():
                return "", "", ""
        elif len(parts) >= 4 and parts[-3] == "prs":
            # issue-less PR {..}/prs/<p>/{agent}
            agent, issue_part = parts[-1], parts[-2]
            identity = parts[:-3]
            if not issue_part.isdigit():
                return "", "", ""
        else:
            issue_part, identity, legacy = _parse_issue_segment(parts)
            if issue_part is None:
                return "", "", ""
            # 新布局 {..}/issues/{n}/{agent}；过渡期旧布局 {repo}/{agent}/issues/{n}
            agent = parts[-3] if legacy else parts[-1]
            if legacy and not issue_part.isdigit():
                return "", "", ""
        if not is_git_agent(agent):
            return "", "", ""
        if len(identity) == 3:
            provider, owner, repo = identity
        elif len(identity) == 2:
            provider, owner, repo = "gitcode", identity[0], identity[1]
        else:
            return "", "", ""
        if not provider or not owner or not repo or not issue_part:
            return "", "", ""
        return f"{provider}:{owner}/{repo}", issue_part, agent
    except Exception:
        return "", "", ""


# Issue #3441：合成 Issue 标识前缀（autopilot 远端派发用仓库级 cwd，无 issues 段）
_SYNTHETIC_ISSUE_PREFIXES = ("autopilot-",)


def extract_workdir_issue_num(path: str) -> str:
    """从 workdir 路径提取 `issues/<segment>` 段中的 Issue 标识（Issue #3441）。

    兼容新布局 {provider}/{owner}/{repo}/issues/{segment}/{agent}、同 Issue 多 PR
    布局 {provider}/{owner}/{repo}/issues/{segment}/pr-<p>/{agent}（Issue #3375）
    与过渡期 {repo}/{agent}/issues/{segment}，与 parse_issue_workdir 复用同一
    `_parse_issue_segment`（尾部锚定，避免更早出现的 issues 段被误取）。
    issue-less PR 布局 {provider}/{owner}/{repo}/prs/<p>/{agent} 不含 Issue 标识
    → 返回 ''（该类任务无关联 Issue，不会进入本校验）。
    segment 取目录名原样字符串（非纯数字，与 get_agent_workdir 派生口径一致：
    PR 订阅伪 id `pr_257_default`、Gitee 字符串 Issue 标识等均可解析）。
    无法解析出标识 → 返回 ''。
    """
    try:
        raw = str(path or "").strip()
        if not raw:
            return ""
        parts = [p for p in raw.replace("\\", "/").split("/") if p]
        issue_part, _, _ = _parse_issue_segment(parts)
        return issue_part or ""
    except Exception:
        return ""


def validate_workdir_issue_consistency(cwd: str, issue_id: str,
                                       agent_type: str = "") -> tuple:
    """任务启动 fail-fast 校验：workdir 中的 Issue 标识必须与任务 Issue 标识一致。

    Issue #3441：并发 coding 任务曾共用同一 workdir 导致 HEAD/分支/提交互相污染
    （#3383 的提交父链混入 #3350 内容）。本校验在任务启动/就绪入口拦截
    「workdir 与任务 Issue 不一致」的错配派发，不静默继续。

    比较口径（与 event_pipeline._issue_num_from/get_agent_workdir 一致）：
    - 任务标识 = issue_id 去掉前导 `#` 后的字符串（不提取数字，兼容
      PR 订阅伪 id `pr_257_default` 与字符串 Issue 标识）；
    - workdir 标识 = `issues/<segment>` 目录名原样字符串；
    - 二者按字符串相等比较。

    返回 (ok: bool, message: str)：
    - 任务无 Issue 标识（push/#0 等非 Issue 任务）→ (True, "") 不校验；
    - 任务标识为合成键（`autopilot-<letters>`，autopilot 远端派发用仓库级 cwd）→
      (True, "") 不校验（该链路 cwd 从来不含 issues/<segment>）；
    - workdir 根本不是 issues 布局（VOD `vod/<provider>/<repo>`、autopilot
      `issue-agent` 等既有非 Issue 目录）→ (True, "") 不校验（本闸只负责
      「同为 issues 布局但标识不同」的错配）；
    - issues 布局下解析不出 <segment> 或标识不一致 → (False, <显式错误>)。
    """
    # 与 event_pipeline._issue_num_from 同口径：仅去前导 #，按字符串比较；
    # #0/0/空 视为无关联 Issue 任务，不校验
    task_issue = str(issue_id or "").strip().lstrip("#").strip()
    if not task_issue or task_issue == "0":
        return True, ""
    # Issue #3441 复审：合成 Issue 键（autopilot 远端/广播派发）不走 issues 布局，
    # 直接跳过，避免对既有链路 fail-fast
    if task_issue.startswith(_SYNTHETIC_ISSUE_PREFIXES):
        return True, ""
    # 只校验「issues 布局」的 workdir：VOD `vod/<provider>/<repo_>`、autopilot
    # `issue-agent` 等既有非 Issue 目录不属于本闸范围（否则会误伤既有派发）
    _parts = [p for p in str(cwd or "").replace("\\", "/").split("/") if p]
    if "issues" not in _parts:
        return True, ""
    workdir_issue = extract_workdir_issue_num(cwd)
    if not workdir_issue:
        return False, (
            f"workdir issue mismatch: cannot parse issue number from workdir "
            f"'{cwd}' (expected issues/{task_issue}/... layout) but task is for "
            f"issue #{task_issue} (agent={agent_type or 'unknown'}); aborting "
            f"task start to prevent cross-issue worktree pollution "
            f"(see docs/issue-3441/workdir-pollution-baseline-rebuild.md)"
        )
    if workdir_issue != task_issue:
        return False, (
            f"workdir issue mismatch: workdir '{cwd}' belongs to issue "
            f"#{workdir_issue} but task is for issue #{task_issue} "
            f"(agent={agent_type or 'unknown'}); aborting task start to prevent "
            f"cross-issue worktree pollution "
            f"(see docs/issue-3441/workdir-pollution-baseline-rebuild.md)"
        )
    return True, ""


def guard_issue_workdir_removal(path: str, reason: str = "zombie_ttl",
                                token: str = "") -> bool:
    """僵尸/兜底目录删除前的保全闸（cleanup._zombie_issue_dirs 调用）。

    - 非 git 工作树（无 .git）→ 直接放行（保持既有清理行为，不误伤垃圾残留）；
    - 含 .git 但无法解析归属（布局未知）→ 无法证明其无未推送内容，保守保留
      + log.warning（宁可残留，不可静默 rmtree 掉带 .git 的现场）；
    - 解析成功 → archive_worktree_before_reset（归档失败/不完整拒绝删除）；
    - 守卫自身异常 → 拒绝删除（保守保留现场）。
    返回 True=允许删除，False=保留。
    """
    try:
        if not (os.path.isfile(os.path.join(path, ".git"))
                or os.path.isdir(os.path.join(path, ".git"))):
            return True
        repo_key, issue_num, agent = parse_issue_workdir(path)
        if not repo_key or not issue_num or not agent:
            log.warning(f"worktree reset guard: unparsable workdir with .git, "
                        f"keep it for manual inspection: {path}")
            return False
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


def mark_worktree_restored(repo_key: str, issue_num, agent_type: str,
                           head: str = "", bundle_path: str = "",
                           source: str = "bundle") -> bool:
    """Issue #2281：记录「新回合已自动找回上一轮未推送 commit」。

    ensure_issue_worktree 重建成功恢复后调用：把 auto_restored/auto_restored_at/
    auto_restored_head/auto_restored_source 写回最新重置记录，使
    inject_reset_context 能区分「已找回（只需重试 push）」与「仍未找回（需手工
    fetch+reset）」，同时保留 lost_commits/bundle_path 作为审计依据。

    source: "bundle"（从归档 bundle fetch+reset）/ "branch"（分支 ref 复用）。
    """
    from datetime import datetime, timezone
    rec = load_reset_record(repo_key, issue_num, agent_type)
    if not rec:
        return False
    rec["auto_restored"] = True
    rec["auto_restored_at"] = datetime.now(timezone.utc).isoformat()
    rec["auto_restored_head"] = head or ""
    rec["auto_restored_source"] = source or "bundle"
    if bundle_path:
        rec["auto_restored_bundle"] = bundle_path
    path = reset_record_path(repo_key, issue_num, agent_type)
    if not _atomic_json_write(path, rec):
        log.warning(f"worktree reset guard: mark auto-restored failed {path}")
        return False
    log.info(f"worktree reset guard: worktree auto-restored ({rec['auto_restored_source']}) "
             f"(repo={repo_key} issue={issue_num} agent={agent_type} head={head[:12]})")
    return True


def inject_reset_context(task: dict, env: dict, prompt: str) -> tuple:
    """下一轮任务注入重置告知（worker _execute_task / direct_executor 调用）。

    读该 (repo_key, issue, agent) 的最新重置记录，命中且未被消费：
    - env 注入 `GRAPE_WORKTREE_RESET=true` + `GRAPE_WORKTREE_RESET_INFO=<record JSON>`
      （机器可读，AC-b）；
    - prompt 追加显式提示块：本地实现已归档、不可假定仍在 worktree、恢复命令
      与「仅当本轮工作区无改动时才执行恢复」的前置条件；
    - Issue #2281：记录带 auto_restored=true（ensure_issue_worktree 已从 bundle
      找回）时，提示块改为「已自动找回、只需重试 push」，不再引导手工 reset；
    - 注入后立即标记 consumed（同一记录只告知一次，避免每轮重复注入/reset 误用）。
    无记录 / 已消费 / 非 git agent → 原样返回（零副作用）。返回 (prompt, injected)。
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
        if record.get("consumed_at"):
            # 已告知过（同一记录只注入一次）→ 不再重复污染 prompt/env
            return prompt, False
        env["GRAPE_WORKTREE_RESET"] = "true"
        env["GRAPE_WORKTREE_RESET_INFO"] = json.dumps(record, ensure_ascii=False)
        fields = {k: record.get(k) for k in
                  ("worktree_reset", "lost_commits", "bundle_path", "archived_at",
                   "reset_reason", "unpushed_count", "auto_restored",
                   "auto_restored_head", "auto_restored_source")}
        _branch = record.get("branch") or f"feat/issue-{num}"
        _push_retry = (f"git push origin HEAD:refs/heads/{_branch}")
        if record.get("auto_restored"):
            _src = record.get("auto_restored_source") or "bundle"
            _how = ("the branch ref was kept and the worktree reused it"
                    if _src == "branch"
                    else "the worktree was restored from the archived bundle")
            block = (
                "\n\n[grape runtime] NOTE: the worktree for this issue was reset at "
                f"{record.get('archived_at', '?')} (reason: {record.get('reset_reason', '?')}), "
                f"but {_how} (HEAD {str(record.get('auto_restored_head') or '')[:12]}, "
                f"unpushed commits recovered: {record.get('unpushed_count', '?')}). "
                "The previous local commits are present again - verify with "
                f"`git log origin/<base_branch>..HEAD` / `git status`, do NOT re-implement. "
                "If the previous push was blocked, retry it first: "
                f"{_push_retry}. Recovery audit entry: "
                f"{record.get('bundle_path', '?')}. "
                "(env GRAPE_WORKTREE_RESET_INFO carries the full machine-readable record; "
                "this notice is delivered once per reset record.) "
                "Machine-readable context: "
                + json.dumps(fields, ensure_ascii=False)
                + "\n"
            )
        else:
            block = (
                "\n\n[grape runtime] NOTE: the worktree for this issue was reset at "
                f"{record.get('archived_at', '?')} (reason: {record.get('reset_reason', '?')}). "
                "Do NOT assume the previous local implementation is still in the worktree. "
                f"Unpushed work was archived to: {record.get('bundle_path', '?')} "
                f"(unpushed commits: {record.get('unpushed_count', '?')}). "
                "If the previous round committed locally and only the push was blocked, "
                f"retry the push first: {_push_retry}. "
                "Otherwise recover with: git fetch <bundle_path> && git reset --hard FETCH_HEAD "
                "ONLY IF the current worktree has no local changes of this run; never "
                "run reset --hard while this run has uncommitted or untracked work - "
                "keep the current implementation and rebuild on top of it instead. "
                "(env GRAPE_WORKTREE_RESET_INFO carries the full machine-readable record; "
                "this notice is delivered once per reset record.) "
                "Machine-readable context: "
                + json.dumps(fields, ensure_ascii=False)
                + "\n"
            )
        _task_id = str((task or {}).get("task_id") or (task or {}).get("id") or "")
        mark_reset_record_consumed(rk, num, ag, task_id=_task_id)
        return prompt + block, True
    except Exception as e:
        log.warning(f"worktree reset guard: context inject failed: {e}")
        return prompt, False
