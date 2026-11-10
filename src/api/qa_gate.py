"""Issue #1739：Issue 关闭/完成前的 QA 结论门禁。

背景：流水线 qa 阶段失败后，coding 提交/合入 PR 仍会把 Issue 置为 completed 并关闭
VCS Issue，未经验证的需求被误判为已解决。

设计：
- 统一从 events 表读取该 Issue 在同一流水线中的最新 qa 结论；
- qa 事件 status=failed 直接判失败；status=completed 时解析 result 结论文本，
  兼容 qa Agent 以 completed + “不通过/阻塞” 自报失败的实际形态；
- 流水线无 qa 节点、无 qa 事件、结论不可判定、查询异常 → 放行（fail-open，
  绝不误杀正常关闭链路）；
- 命中失败时在 Issue 发布可追溯评论（QA 结论 + 事件 seq + 关联 PR），best-effort。
"""
from __future__ import annotations

import json
import re

from src.config import log

# 失败语义（顺序：显式否定优先于泛化的“失败”）
_QA_FAIL_TERMS = (
    "不通过", "未通过", "未达标", "不达标",
    "驳回", "被驳回", "打回",
    "整改", "需整改", "需要整改",
    "禁止合入", "禁止合并", "不可合并", "不能合入", "无法合入",
    "需人工复核", "需要人工复核", "待人工复核",
    "失败", "❌", "✗",
)

# 单独词项需先剔除否定语境（无阻塞 / 非阻塞 / 无 Critical / 0 Required）
_QA_FAIL_WORDS = ("阻塞", "阻断", "critical", "required")
_QA_NEGATION_RE = re.compile(
    r"(?:无|非|不|未|没有|no|n/a|0)\s*(?:的)?\s*"
    r"(?:阻塞|阻断|冲突|critical|required|硬违规)"
    r"(?:\s*[/、,，和及]\s*(?:阻塞|阻断|冲突|critical|required|硬违规))*",
    re.IGNORECASE,
)

# 英文失败/通过按词边界识别（避免 "ok" 命中 "broken"、"fail" 命中 "failsafe" 之类子串误判）
_QA_FAIL_EN_RE = re.compile(r"\b(?:fail|failed|failure)\b", re.IGNORECASE)
_QA_PASS_EN_RE = re.compile(r"\b(?:pass|passed|ok)\b", re.IGNORECASE)

# 通过语义（在失败语义之后判定）
_QA_PASS_TERMS = ("通过", "✅")

_QA_ANNOTATION_MARKER = "<!-- grape-qa-gate -->"
_annotated: set[tuple[str, int, int]] = set()


def _issue_tags(issue_number) -> tuple[str, str] | None:
    """归一 Issue 编号为 (#N, N) 两种历史 issue_id 形态。非法编号返回 None。"""
    _s = str(issue_number or "").strip().lstrip("#")
    if not _s.isdigit() or _s == "0":
        return None
    return f"#{_s}", _s


def _issue_number_int(issue_number) -> int:
    _tags = _issue_tags(issue_number)
    return int(_tags[1]) if _tags else 0


def pipeline_has_qa_node(repo_key: str, issue_number) -> bool:
    """该 Issue 所属流水线是否存在启用中的 qa 节点（无 qa 阶段 → 不启用门禁）。"""
    try:
        from src.api.pipeline_config import get_pipeline_config
        from src.api.session_store import get_issue_scene

        _tags = _issue_tags(issue_number)
        if not _tags:
            return False
        _scene = get_issue_scene(repo_key, _tags[0])
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _nodes = (get_pipeline_config(_scene_id, repo_key=repo_key) or {}).get("nodes") or []
        for _n in _nodes:
            _nid = str(_n.get("id") or _n.get("tag") or "").strip().lower()
            if _nid == "qa" and _n.get("enabled", True):
                return True
        return False
    except Exception as _e:
        log.warning(f"qa gate: pipeline qa node check failed {repo_key}/#{issue_number}: {_e}")
        return False


def latest_qa_event(repo_key: str, issue_number) -> dict | None:
    """该 Issue 最新一条 qa 事件（按 seq 最大）。无记录/异常返回 None。"""
    _tags = _issue_tags(issue_number)
    if not _tags:
        return None
    try:
        from src.session_manager import db, db_lock

        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, status, result, summary FROM events "
                "WHERE repo_key=? AND issue_id IN (?, ?) AND agent_type='qa' "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, _tags[0], _tags[1]))
            _row = c.fetchone()
        if not _row:
            return None
        return {"seq": int(_row[0] or 0), "status": (_row[1] or "").strip(),
                "result": _row[2] or "", "summary": _row[3] or ""}
    except Exception as _e:
        log.warning(f"qa gate: latest qa event query failed {repo_key}/#{issue_number}: {_e}")
        return None


def classify_qa_conclusion(status: str, text: str) -> str:
    """判定 QA 结论：``fail`` / ``pass`` / ``unknown``。

    - status=failed/error/failure → fail；
    - running/dispatched/pending/queued → unknown（尚无结论）；
    - completed 时解析 result 文本：先剔除否定语境，失败语义优先，其次通过语义。
    """
    _st = (status or "").strip().lower()
    if _st in ("failed", "failure", "error"):
        return "fail"
    if _st in ("running", "dispatched", "pending", "queued", "cancelled"):
        return "unknown"
    if _st not in ("", "completed", "success", "succeeded"):
        return "unknown"

    _blob = (text or "").strip()
    if not _blob:
        return "unknown"
    _normalized = _QA_NEGATION_RE.sub("", _blob)
    _lower = _normalized.lower()

    if any(_t in _normalized for _t in _QA_FAIL_TERMS):
        return "fail"
    if any(_w in _lower for _w in _QA_FAIL_WORDS):
        return "fail"
    if _QA_FAIL_EN_RE.search(_normalized):
        return "fail"
    if any(_t in _normalized for _t in _QA_PASS_TERMS):
        return "pass"
    if _QA_PASS_EN_RE.search(_normalized):
        return "pass"
    return "unknown"


def qa_gate_check(repo_key: str, issue_number) -> dict:
    """关闭/完成前置校验。

    返回 ``{blocked, qa_present, event_seq, status, conclusion, reason}``。
    无 qa 节点 / 无 qa 事件 / 结论 unknown / 查询异常 → ``blocked=False``。
    """
    _result = {"blocked": False, "qa_present": False, "event_seq": 0,
               "status": "", "conclusion": "", "reason": ""}
    try:
        if not _issue_tags(issue_number):
            return _result
        _result["qa_present"] = pipeline_has_qa_node(repo_key, issue_number)
        if not _result["qa_present"]:
            return _result
        _evt = latest_qa_event(repo_key, issue_number)
        if not _evt:
            return _result
        _result.update(event_seq=_evt.get("seq") or 0, status=_evt.get("status") or "")
        _verdict = classify_qa_conclusion(_result["status"], _evt.get("result") or "")
        _result["conclusion"] = _verdict
        if _verdict == "fail":
            _reason = " ".join((_evt.get("result") or "").split())[:300]
            _result["blocked"] = True
            _result["reason"] = _reason or f"qa {_result['status']}"
            log.info(f"qa gate: blocked {repo_key}/#{issue_number} "
                     f"(seq={_result['event_seq']}, status={_result['status']})")
        return _result
    except Exception as _e:
        log.warning(f"qa gate: check failed (fail-open) {repo_key}/#{issue_number}: {_e}")
        return _result


def _latest_pr_number(repo_key: str, issue_number) -> int:
    """best-effort：从该 Issue 最近 PR 事件中取关联 PR 编号，取不到返回 0。"""
    _tags = _issue_tags(issue_number)
    if not _tags:
        return 0
    try:
        from src.session_manager import db, db_lock

        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT payload FROM events WHERE repo_key=? AND issue_id IN (?, ?) "
                "AND event_type IN ('note_hook_pr','pull_request_opened',"
                "'pull_request_updated','pull_request_merged') "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, _tags[0], _tags[1]))
            _row = c.fetchone()
        if not _row or not _row[0]:
            return 0
        _p = json.loads(_row[0]) if isinstance(_row[0], str) else (_row[0] or {})
        _oa = (_p.get("object_attributes") or {}) if isinstance(_p, dict) else {}
        _pr = _p.get("pull_request") or {} if isinstance(_p, dict) else {}
        for _v in (_oa.get("iid"), _oa.get("number"), _pr.get("number"), _pr.get("iid")):
            _s = str(_v or "").strip()
            if _s.isdigit() and int(_s) > 0:
                return int(_s)
    except Exception:
        return 0
    return 0


def _pr_link(repo_key: str, pr_number: int) -> str:
    if not pr_number:
        return ""
    from src.repo_key import parse_repo_key

    _provider, _owner, _repo = parse_repo_key(repo_key)
    if not _owner or not _repo:
        return ""
    _host = "github.com" if _provider == "github" else "gitcode.com"
    return f"https://{_host}/{_owner}/{_repo}/pull/{pr_number}"


def annotate_qa_failure(repo_key: str, issue_number, qa_info: dict,
                        pr_number: int = 0) -> str:
    """在 Issue 上标注 QA 失败原因与关联 PR（best-effort，返回 success/skipped/failed）。"""
    try:
        _num = _issue_number_int(issue_number)
        _seq = int((qa_info or {}).get("event_seq") or 0)
        if not _num:
            return "skipped"
        _key = (repo_key, _num, _seq)
        if _key in _annotated:
            return "skipped"
        _pr = int(pr_number or 0) or _latest_pr_number(repo_key, _num)
        _reason = (qa_info or {}).get("reason") or "qa 未通过"
        _status = (qa_info or {}).get("status") or ""
        _lines = [
            "[Grape QA Gate] ⚠️ QA 未通过，Issue 暂不自动关闭。",
            "",
            f"- QA 结论：{_reason}",
            f"- QA 事件：seq {_seq}（status={_status or 'unknown'}）",
        ]
        if _pr:
            _link = _pr_link(repo_key, _pr)
            _lines.append(f"- 关联 PR：#{_pr}" + (f"（{_link}）" if _link else ""))
        _lines.append("")
        _lines.append("请完成整改并重新通过 QA 验证；QA 通过后将按原逻辑自动关闭。")
        _lines.append(_QA_ANNOTATION_MARKER)
        _body = "\n".join(_lines)
        return _post_issue_comment(repo_key, _num, _body, _key)
    except Exception as _e:
        log.warning(f"qa gate: annotate failed {repo_key}/#{issue_number}: {_e}")
        return "failed"


def _post_issue_comment(repo_key: str, issue_number: int, body: str,
                        key: tuple[str, int, int]) -> str:
    try:
        from src.repo_key import parse_repo_key
        from src.vcs.api_client import VcsApiClient

        _provider, _owner, _repo = parse_repo_key(repo_key)
        if not _owner or not _repo:
            return "skipped"
        if _provider == "github":
            from src.user.db import get_github_token as _get_token
        else:
            from src.user.db import get_repo_bot_token as _get_token
        _token = _get_token(repo_key=repo_key) or ""
        if not _token:
            log.info(f"qa gate: no bot token for {repo_key}, skip annotation")
            return "skipped"
        _client = VcsApiClient(_provider, _token, _owner, _repo)
        _resp = _client.post_issue_comment(issue_number, body)
        if isinstance(_resp, dict) and "error" not in _resp:
            _annotated.add(key)
            log.info(f"qa gate: annotated {repo_key}/#{issue_number}")
            return "success"
        log.warning(f"qa gate: annotate {repo_key}/#{issue_number} -> {_resp}")
        return "failed"
    except Exception as _e:
        log.warning(f"qa gate: post annotation failed {repo_key}/#{issue_number}: {_e}")
        return "failed"