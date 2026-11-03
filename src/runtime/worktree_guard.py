"""Issue #752（M2）：worktree 冲突标记推送守卫 + 非产物功能文件指纹保真校验。

背景（PR #352 / #778 实证）：服务端自动提交/推送流程对 worktree 执行
`git add -A → commit → push` 盲目整树提交——worktree 中未解决的 merge 冲突
文本会被原样写入分支，覆盖已合入的功能修改（"自动复测产物清理损坏功能文件"
的具体通道）。

设计（docs/issue-752/plan.md §1.2）：
- 推送守卫：commit 前扫描暂存文件，硬命中 `<<<<<<< `/`>>>>>>> ` → 阻断提交/推送；
- 保真校验（AC2）：任务开始（worktree 建/复用时）保存非产物功能文件 sha256 基线，
  任务结束（清理/推送前）比对指纹——modified/removed 非空 → 告警（不阻断，人工裁决）；
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


def scan_staged_files(wt_dir: str, token: str = "") -> dict:
    """扫描暂存区（git diff --cached）文件内容的冲突标记。

    返回 {"hard": {file: n}, "soft": {file: n}}；硬命中调用方应阻断 commit/push
    （防冲突残留写入分支，AC1 的自动提交路径）。
    """
    hard: dict = {}
    soft: dict = {}
    try:
        from src.runtime.repogit import _run_git
        r = _run_git(["-C", wt_dir, "diff", "--cached", "--name-only", "-z"], token=token)
        if r is None or r.returncode != 0:
            return {"hard": hard, "soft": soft}
        names = [n for n in (r.stdout or "").split("\0") if n]
        for name in names:
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
