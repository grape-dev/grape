"""Issue #752（M2）：worktree 冲突标记推送守卫 + 非产物功能文件指纹保真校验。

背景（PR #352 / #778 实证）：服务端自动提交/推送流程对 worktree 执行
`git add -A → commit → push` 盲目整树提交——worktree 中未解决的 merge 冲突
文本会被原样写入分支，覆盖已合入的功能修改（"自动复测产物清理损坏功能文件"
的具体通道）。

设计（docs/issue-752/plan.md §1.2）：
- 推送守卫：commit 前扫描暂存文件，硬命中 `<<<<<<< `/`>>>>>>> ` → 阻断提交/推送；
- 保真校验（AC2）：任务开始（worktree 建/复用时）保存非产物功能文件 sha256 基线，
  清理前比对指纹——modified/removed 非空 → 告警（不阻断，人工裁决）；
  推送口不做保真告警（PR #874 审查修正：正常编码必然修改功能文件，推送口告警
  全是噪声；仅保留清理口信号）；
- 基线文件存 worktree 外（`<wt_dir>.fidelity.json` 兄弟文件），避免被 agent `add -A`
  误提交；
- 产物路径白名单（dist/build/out/node_modules 等）不参与指纹——它们是可清理产物，
  非保真对象。
"""
from __future__ import annotations

import hashlib
import json
import os

from src.config import log

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
        from src.runtime.repogit import _run_git
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
        from src.runtime.repogit import _run_git
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
    from src.runtime.repogit import _run_git
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
        from src.runtime.repogit import _run_git
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
        from src.runtime.repogit import _run_git
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
