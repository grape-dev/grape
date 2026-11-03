"""Issue #752：合入流程冲突标记扫描门禁（M1）。

背景（PR #352 实证）：含未解决 merge 冲突文本的 PR 分支被无条件合入基线，
runbook 364→1037 行、11 处 `>>>>>>>`/`=======` 残留覆盖已合入功能修改。

设计（docs/issue-752/plan.md §1.1）：
- 硬命中 = `<<<<<<< ` / `>>>>>>> ` 行首标记 → 合入前阻断（409）；
- 软命中 = 孤立 `=======` 行 → 仅告警放行（markdown 标题下划线是合法语法，防误杀）；
- 合入后再扫一次，命中 → 告警 + 合入日志（覆盖自动复测等旁路引入的残留）；
- 门禁 fail-open：扫描自身异常记 warning 放行，不阻断正常合入流水线；
- 每次决策（blocked/warned/alarmed/passed）落库 grape_t_merge_gate_log（AC3）。
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from src.config import log

# 行首锚定：仅识别冲突标记形态，避免正文提及 ">>>>>>>" 字样误报
_CONFLICT_START_RE = re.compile(r"^<{7}( .*)?$")   # <<<<<<< HEAD / <<<<<<< feat/issue-N
_CONFLICT_END_RE = re.compile(r"^>{7}( .*)?$")     # >>>>>>> dev / >>>>>>> origin/main
_CONFLICT_SEP_RE = re.compile(r"^={7}$")           # =======(孤立分隔行，markdown 下划线同形 → 软命中)

_MAX_SCAN_BYTES = 5 * 1024 * 1024  # 单文件扫描上限（超大文件截断扫描，防内存放大）


def scan_text(text: str) -> dict:
    """扫描文本行，返回 {"hard": n, "soft": n}（hard=开始/结束标记，soft=孤立分隔行）。"""
    hard = soft = 0
    for line in text.splitlines():
        if _CONFLICT_START_RE.match(line) or _CONFLICT_END_RE.match(line):
            hard += 1
        elif _CONFLICT_SEP_RE.match(line):
            soft += 1
    return {"hard": hard, "soft": soft}


def scan_patch(patch: str) -> dict:
    """扫描 unified diff：只看新增行（+ 前缀去掉后匹配），已删除的旧冲突标记不计。"""
    hard = soft = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            stripped = line[1:]
            if _CONFLICT_START_RE.match(stripped) or _CONFLICT_END_RE.match(stripped):
                hard += 1
            elif _CONFLICT_SEP_RE.match(stripped):
                soft += 1
    return {"hard": hard, "soft": soft}


def scan_pull_files(files) -> dict:
    """扫描 PR files 响应（[{filename, patch}, ...]），返回命中明细。"""
    hard_hits: dict = {}
    soft_hits: dict = {}
    if not isinstance(files, list):
        return {"hard_hits": hard_hits, "soft_hits": soft_hits}
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename") or item.get("path") or ""
        patch = item.get("patch") or ""
        if not filename or not isinstance(patch, str) or not patch:
            continue
        r = scan_patch(patch)
        if r["hard"]:
            hard_hits[filename] = r["hard"]
        if r["soft"]:
            soft_hits[filename] = r["soft"]
    return {"hard_hits": hard_hits, "soft_hits": soft_hits}


def run_merge_gate(owner: str, repo: str, number: int, p: str, token: str) -> dict:
    """取 PR 变更文件并扫描，返回 {"decision", "hard_hits", "soft_hits"}。

    decision: blocked（硬命中，合入前阻断）/ warned（仅软命中，告警放行）/ passed。
    任何异常 fail-open 返回 passed（记 warning），门禁故障不阻断合入。
    """
    empty = {"decision": "passed", "hard_hits": {}, "soft_hits": {}}
    try:
        # 延迟导入避免与 git_repo_api 循环依赖
        from src.api.git_repo_api import _vcs_req, _vcs_url
        files = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/files",
                                            {"per_page": 100}, p), token)
        hits = scan_pull_files(files)
        if hits["hard_hits"]:
            return {"decision": "blocked", **hits}
        if hits["soft_hits"]:
            return {"decision": "warned", **hits}
        return {"decision": "passed", **hits}
    except Exception as e:
        log.warning(f"merge gate scan failed (fail-open) {owner}/{repo}#{number}: {e}")
        return {**empty, "decision": "error"}


def record_gate_log(repo_key: str, pr_number: int, phase: str, decision: str,
                    hits: dict | None = None) -> None:
    """合入日志落库（AC3）。best-effort：异常仅告警，绝不影响合入主流程。"""
    try:
        import json

        from src.session_manager import db, db_lock
        with db_lock:
            db.execute(
                "INSERT INTO grape_t_merge_gate_log "
                "(id, repo_key, pr_number, phase, decision, hits_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), repo_key or "", int(pr_number or 0), phase or "",
                 decision or "", json.dumps(hits or {}, ensure_ascii=False),
                 datetime.now(UTC).isoformat()))
            db.commit()
    except Exception as e:
        log.warning(f"merge gate log write failed {repo_key}#{pr_number}: {e}")
