"""Issue #752：合入流程冲突标记扫描门禁（M1）。

背景（PR #352 实证）：含未解决 merge 冲突文本的 PR 分支被无条件合入基线，
runbook 364→1037 行、11 处 `>>>>>>>`/`=======` 残留覆盖已合入功能修改。

设计（docs/issue-752/plan.md §1.1）：
- 硬命中 = `<<<<<<< ` / `>>>>>>> ` 行首标记 → 合入前阻断（409）；
- 软命中 = 孤立 `=======` 行 → 仅告警放行（markdown 标题下划线是合法语法，防误杀）；
- 合入后再扫 base 分支合入结果（pre-merge base sha → 合入后 tip 的 compare diff），
  命中 → 告警 + 合入日志（覆盖自动复测等旁路写入 base 分支的残留，PR #874 审查修正）；
- 门禁 fail-open：扫描自身异常记 warning 放行，不阻断正常合入流水线；
- 每次决策（blocked/warned/alarmed/passed）落库 grape_t_merge_gate_log（AC3）。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from src.config import log

# 行首锚定：仅识别冲突标记形态，避免正文提及 ">>>>>>>" 字样误报
_CONFLICT_START_RE = re.compile(r"^<{7}( .*)?$")   # <<<<<<< HEAD / <<<<<<< feat/issue-N
_CONFLICT_END_RE = re.compile(r"^>{7}( .*)?$")     # >>>>>>> dev / >>>>>>> origin/main
_CONFLICT_SEP_RE = re.compile(r"^={7}$")           # =======(孤立分隔行，markdown 下划线同形 → 软命中)

_MAX_SCAN_BYTES = 5 * 1024 * 1024  # 单文件扫描上限（超大文件截断扫描，防内存放大）
_MAX_FILES_PAGES = 20             # PR files 翻页上限（20 页 × 100 = 2000 文件，防无限循环）

# Issue #993（M3）：重建文档结构校验。
# - pr_number: 0 / 元数据口径矛盾 → 硬阻断（修复丢失在基线之外的文档痕迹）；
# - 代码围栏（``` / ~~~）不配平 → 软告警（合法 Markdown 示例/片段可能误报）。
# 仅扫描 docs/ 下的 Markdown 重建文档（Spec 范围），且只认行首 YAML 式元数据
# （``key: value``），避免把正文引用/Python 注解/赋值误判为元数据矛盾。
_REBUILT_DOC_PREFIX = "docs/"
_PR_NUMBER_ZERO_RE = re.compile(
    r"^\s*(?:[-*]\s*)?[\"']?pr_number[\"']?\s*:\s*0[\"']?\s*$", re.IGNORECASE)
_META_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?[\"']?(pr_number|issue_number|status|state|merged)[\"']?"
    r"\s*:\s*(.+?)\s*$",
    re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def is_rebuilt_doc(filename: str) -> bool:
    """重建文档判定：``docs/`` 下的 Markdown 文件（Spec M3 扫描范围）。"""
    return bool(filename) and filename.startswith(_REBUILT_DOC_PREFIX) \
        and filename.lower().endswith(".md")


def _normalize_patch(patch) -> str:
    # Issue #754 归一：patch 可能是 str（GitHub）或 {"diff": str}（GitCode）。
    if isinstance(patch, dict):
        patch = patch.get("diff")
    return patch if isinstance(patch, str) else ""


def _scan_structure_lines(lines) -> tuple[dict, dict]:
    """扫描行序列，返回 (结构结果, 元数据 key→values)。

    代码围栏内只统计配平、不做元数据判定：文档里的 YAML/JSON 示例（如
    ``status: open``、``pr_number: 0``）不是真实元数据，不能误阻断合法文档 PR。
    """
    fence_open = ""
    pr_zero = 0
    meta_values: dict = {}
    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not fence_open:
                fence_open = marker
            elif marker == fence_open:
                fence_open = ""
            continue
        if fence_open:
            continue
        if _PR_NUMBER_ZERO_RE.search(line):
            pr_zero += 1
        m = _META_LINE_RE.match(line)
        if m:
            key = m.group(1).lower()
            value = m.group(2).strip().strip('"').strip("'")
            meta_values.setdefault(key, set()).add(value)
    conflicts = {k: sorted(v) for k, v in meta_values.items() if len(v) > 1}
    result = {
        "fence_unbalanced": bool(fence_open),
        "pr_number_zero": pr_zero,
        "metadata_conflicts": conflicts,
    }
    return result, meta_values


def scan_structure_text(text: str) -> dict:
    """扫描重建文档结构：围栏配平 / pr_number:0 / 元数据口径矛盾。"""
    return _scan_structure_lines((text or "").splitlines())[0]


def scan_structure_patch(patch: str) -> dict:
    """基于 unified diff 扫描：新增行判硬缺陷，变更后的完整 hunk 判元数据矛盾。

    - ``pr_number: 0`` 只看新增行（PR 自己引入的残留才阻断）；
    - 元数据矛盾看「新增值 + 既有值」：未修改的 context 行也计入结果态，避免
      新增值与文件既有值矛盾时漏检；仅当该 key 在新增行出现时才报告，避免误伤
      只是路过已有矛盾文档的 PR；
    - 围栏配平按变更后的结果态判断（context + 新增，删除行不计）。
    """
    added: list = []
    resulting: list = []
    for line in (patch or "").splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+") or line.startswith(" "):
            body = line[1:]
            resulting.append(body)
            if line.startswith("+"):
                added.append(body)
        elif line.startswith("-"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            resulting.append(line)
    added_res, added_meta = _scan_structure_lines(added)
    result_res, result_meta = _scan_structure_lines(resulting)
    conflicts = {
        k: sorted(v) for k, v in result_meta.items()
        if len(v) > 1 and k in added_meta
    }
    return {
        "fence_unbalanced": result_res["fence_unbalanced"],
        "pr_number_zero": added_res["pr_number_zero"],
        "metadata_conflicts": conflicts,
    }


def scan_structure_files(files) -> dict:
    """扫描 PR files 响应，返回结构缺陷的硬/软命中明细。

    hard_hits: {filename: n}（pr_number:0 / 元数据矛盾）
    soft_hits: {filename: n}（围栏不配平）
    details: 原始缺陷描述，供合入日志排查。
    """
    hard_hits: dict = {}
    soft_hits: dict = {}
    details: dict = {}
    if not isinstance(files, list):
        return {"hard_hits": hard_hits, "soft_hits": soft_hits, "details": details}
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename") or item.get("path") or ""
        patch = _normalize_patch(item.get("patch"))
        if not filename or not patch or not is_rebuilt_doc(filename):
            continue
        got = scan_structure_patch(patch)
        hard_n = int(got.get("pr_number_zero") or 0) + len(got.get("metadata_conflicts") or {})
        if hard_n:
            hard_hits[filename] = hard_n
        if got.get("fence_unbalanced"):
            soft_hits[filename] = 1
        if hard_n or got.get("fence_unbalanced"):
            details[filename] = got
    return {"hard_hits": hard_hits, "soft_hits": soft_hits, "details": details}


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
        patch = item.get("patch")
        # Issue #754 归一：patch 可能是 str（GitHub）或 {"diff": str}（GitCode，Issue #731/#750
        # 归一字段 .patch.diff），两种形态都扫描，避免 GitCode 响应漏检。
        if isinstance(patch, dict):
            patch = patch.get("diff")
        if not filename or not isinstance(patch, str) or not patch:
            continue
        r = scan_patch(patch)
        if r["hard"]:
            hard_hits[filename] = r["hard"]
        if r["soft"]:
            soft_hits[filename] = r["soft"]
    return {"hard_hits": hard_hits, "soft_hits": soft_hits}


def _pull_files_all_pages(owner: str, repo: str, number: int, p: str, token: str) -> list:
    """取 PR 变更文件全量（PR #874 审查修正：按 page 循环翻页，>100 文件 PR 不漏扫）。

    per_page=100，页结果 <100 或空即止；上限 _MAX_FILES_PAGES 页防异常无限循环。
    """
    from src.api.git_repo_api import _vcs_req, _vcs_url
    files: list = []
    for page in range(1, _MAX_FILES_PAGES + 1):
        part = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/files",
                                           {"per_page": 100, "page": page}, p), token)
        if not isinstance(part, list) or not part:
            break
        files.extend(part)
        if len(part) < 100:
            break
    return files


def get_pull_base(owner: str, repo: str, number: int, p: str, token: str) -> dict:
    """取 PR base 分支信息 {"ref", "sha"}（合入前快照，供合入后 compare 扫描起点）。

    fail-open：任何异常返回空值（post-merge 扫描降级到 merge commit 路径）。
    """
    try:
        from src.api.git_repo_api import _vcs_req, _vcs_url
        pr = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}", None, p), token)
        base = (pr or {}).get("base") or {} if isinstance(pr, dict) else {}
        return {"ref": base.get("ref") or "", "sha": base.get("sha") or ""}
    except Exception as e:
        log.warning(f"merge gate get pull base failed (fail-open) {owner}/{repo}#{number}: {e}")
        return {"ref": "", "sha": ""}


def run_merge_gate(owner: str, repo: str, number: int, p: str, token: str) -> dict:
    """取 PR 变更文件（全量翻页）并扫描，返回 {"decision", "hard_hits", "soft_hits"}。

    decision: blocked（硬命中，合入前阻断）/ warned（仅软命中，告警放行）/ passed。
    任何异常 fail-open 返回 passed（记 warning），门禁故障不阻断合入。
    """
    empty = {"decision": "passed", "hard_hits": {}, "soft_hits": {},
             "conflict_hard_hits": {}, "conflict_soft_hits": {},
             "structure_hard_hits": {}, "structure_soft_hits": {}}
    try:
        files = _pull_files_all_pages(owner, repo, number, p, token)
        hits = scan_pull_files(files)
        structure = scan_structure_files(files)
        hard_hits = {**hits["hard_hits"], **structure["hard_hits"]}
        soft_hits = {**hits["soft_hits"], **structure["soft_hits"]}
        result = {"hard_hits": hard_hits, "soft_hits": soft_hits,
                  "conflict_hard_hits": hits["hard_hits"],
                  "conflict_soft_hits": hits["soft_hits"],
                  "structure_hard_hits": structure["hard_hits"],
                  "structure_soft_hits": structure["soft_hits"],
                  "structure_details": structure["details"]}
        if hard_hits:
            return {"decision": "blocked", **result}
        if soft_hits:
            return {"decision": "warned", **result}
        return {"decision": "passed", **result}
    except Exception as e:
        log.warning(f"merge gate scan failed (fail-open) {owner}/{repo}#{number}: {e}")
        return {**empty, "decision": "error"}


def run_post_merge_gate(owner: str, repo: str, number: int, p: str, token: str,
                        base_ref: str = "", base_sha: str = "", merge_sha: str = "") -> dict:
    """合入后扫描 base 分支合入结果（PR #874 审查修正，替代复扫 PR files diff）。

    扫描对象（覆盖旁路写入 base 分支的残留，PR diff 复扫无法检出）：
    1. 首选：合入前 base sha → 合入后 base tip 的 compare diff 文件（含 merge 提交
       及合入后旁路直接落 base 的提交）；
    2. 降级：merge commit 详情（/commits/{merge_sha}）的变更文件。
    任何异常 fail-open 返回 passed（记 warning），不影响合入返回。
    """
    empty = {"decision": "passed", "hard_hits": {}, "soft_hits": {},
             "conflict_hard_hits": {}, "conflict_soft_hits": {},
             "structure_hard_hits": {}, "structure_soft_hits": {}}
    try:
        from src.api.git_repo_api import _vcs_req, _vcs_url
        files = None
        if base_ref:
            commits = _vcs_req(p, "GET", _vcs_url(owner, repo, "/commits",
                                                  {"sha": base_ref, "per_page": 1}, p), token)
            tip = ""
            if isinstance(commits, list) and commits and isinstance(commits[0], dict):
                tip = commits[0].get("sha") or ""
            if base_sha and tip and tip != base_sha:
                cmp = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/compare/{base_sha}...{tip}",
                                                  None, p), token)
                if isinstance(cmp, dict):
                    files = cmp.get("files")
        if not files and merge_sha:
            detail = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/commits/{merge_sha}", None, p),
                              token)
            if isinstance(detail, dict):
                files = detail.get("files")
        if not files:
            return empty
        hits = scan_pull_files(files)
        structure = scan_structure_files(files)
        hard_hits = {**hits["hard_hits"], **structure["hard_hits"]}
        soft_hits = {**hits["soft_hits"], **structure["soft_hits"]}
        result = {"hard_hits": hard_hits, "soft_hits": soft_hits,
                  "conflict_hard_hits": hits["hard_hits"],
                  "conflict_soft_hits": hits["soft_hits"],
                  "structure_hard_hits": structure["hard_hits"],
                  "structure_soft_hits": structure["soft_hits"],
                  "structure_details": structure["details"]}
        if hard_hits:
            return {"decision": "blocked", **result}
        if soft_hits:
            return {"decision": "warned", **result}
        return {"decision": "passed", **result}
    except Exception as e:
        log.warning(f"merge gate post-merge scan failed (fail-open) {owner}/{repo}#{number}: {e}")
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
                 datetime.now(timezone.utc).isoformat()))
            db.commit()
    except Exception as e:
        log.warning(f"merge gate log write failed {repo_key}#{pr_number}: {e}")
