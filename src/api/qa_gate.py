"""Issue #1739：Issue 关闭/完成前的 QA 结论门禁。

背景：流水线 qa 阶段失败后，coding 提交/合入 PR 仍会把 Issue 置为 completed 并关闭
VCS Issue，未经验证的需求被误判为已解决。

设计：
- 统一从 events 表读取该 Issue 在同一流水线（同一 scene_id）中的最新 qa 结论；
- qa 事件 status=failed/cancelled 直接判失败；status=completed 时解析 result 结论文本，
  兼容 qa Agent 以 completed + “不通过/阻塞” 自报失败的实际形态；
- 流水线无 qa 节点、无 qa 事件、结论不可判定、查询异常 → 放行（fail-open，
  绝不误杀正常关闭链路）；
- 命中失败时在 Issue 发布可追溯评论（QA 结论 + 事件 seq + 关联 PR），best-effort。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict

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

# 单独词项需先剔除否定语境（无阻塞 / 非阻塞 / 无 Critical / 0 Required / no failure）
_QA_FAIL_WORDS = ("阻塞", "阻断")
# 英文词按词边界匹配（避免 "criticality" 之类子串误命中）
_QA_FAIL_EN_WORD_RE = re.compile(r"\b(?:critical|required)\b", re.IGNORECASE)
_QA_NEGATION_RE = re.compile(
    r"(?:无|非|不|未|没有|没|不存在|未见|未发现|无需|不需要|no|not|without|zero|none|0)\s*"
    r"(?:任何|的|个|条|项)?\s*"
    r"(?:(?:阻塞|阻断|冲突|critical|required|硬违规|失败|不通过|未通过|未达标|不达标|"
    r"问题|缺陷|违规|驳回|打回|"
    r"failures?|fails?|issues?|problems?|bugs?|blockers?|blocking|fixes?|"
    r"整改|修复)[\s、,，/和及:：]*)+",
    re.IGNORECASE,
)
# “critical: none / required: 0 / required fixes: none / blocking issues: none”
# 等否定收尾（否定词在后）。前置修饰词（required fixes / blocking issues）必须
# 连同量词一起剔除，否则残留的 required/critical 会命中英文失败词误判 fail。
_QA_NEG_TRAILING_RE = re.compile(
    r"\b(?:(?:critical|required|blocking|blocker|major|minor|any|all|"
    r"remaining|open|known)\s+)*"
    r"(?:critical|required|blockers?|blocking|failures?|fails?|issues?|fixes?|"
    r"problems?|bugs?|findings?|changes?)"
    r"\s*[:：=-]?\s*(?:none|zero|0|no)\b"
    # 英文后置定量否定：0/no tests failed / 0 failures（实际为全部通过，不能判失败）
    r"|\b(?:no|zero|0)\s+(?:\w+\s+){0,3}?(?:failures?|fails?|failed|errors?)\b",
    re.IGNORECASE,
)
# 中文后置否定（“阻塞为零 / 阻塞 0 / 阻断: 0 / 未通过项 0”）——中文无词边界，单独处理
_QA_NEG_CN_TRAILING_RE = re.compile(
    r"(?:阻塞|阻断|冲突|硬违规|失败|不通过|未通过|问题|缺陷|违规|整改|修复)"
    r"\s*(?:的)?\s*(?:项|个|条|例|处|次|数量|数)?\s*(?:[:：=]|为|是)?\s*"
    r"(?:零|0)\s*(?:项|个|条|例|处|次)?",
)

# 英文失败/通过按词边界识别（避免 "ok" 命中 "broken"、"fail" 命中 "failsafe" 之类子串误判）
_QA_FAIL_EN_RE = re.compile(r"\b(?:fail|failed|failure|fails)\b", re.IGNORECASE)
_QA_PASS_EN_RE = re.compile(r"\b(?:pass|passed|ok)\b", re.IGNORECASE)

# 否定式“通过”= 实际未通过（“无法通过/没有通过/not passed”不能被下面的通过语义吞掉）。
# 否定词与通过词之间允许“全部/完全/达到/测试”等修饰语（未全部通过 / 未达到通过标准 /
# not all tests passed / not fully passed），但不能跨越无关子句（not required, tests
# passed 中的 not 不得命中后面的 passed）。
_QA_NEG_PASS_RE = re.compile(
    r"(?:无法|不能|不可|未能|未|没有|没|不会|no\s+way\s+to|"
    r"not\s+(?:be\s+)?able\s+to|unable\s+to|cannot|can't|"
    r"did\s+not|does\s+not|didn't|doesn't|never|not)\s*"
    r"(?:(?:全部|所有|全|完全|百分百|100%|能够|能|均|都|达到|达|满足|"
    r"all|every|any|fully|completely|entirely|be|been)\s*)*"
    r"(?:(?:测试|用例|检查|项|tests?|checks?|cases?)\s*)*"
    r"(?:通过|pass(?:ed)?|ok|succeed|approved|合并|合入)",
    re.IGNORECASE,
)
# 未达标语义（否定词可能在通过词之后）：通过率未达 100% / 未达到通过标准 /
# not reach the passing standard——这些都表示未通过，不能被“通过”一词吞成 pass。
_QA_UNMET_STANDARD_RE = re.compile(
    r"(?:通过率|合格率|达标率|pass\s+rate)\s*[^。;\n]{0,10}?"
    r"(?:未|没有|没|不)\s*(?:达到|达|到|满足|达标|符合)\s*"
    r"(?:100\s*%|百分之百|标准|要求|门槛|预期|目标)?"
    r"|(?:未|没有|没|不|无法|未能)\s*(?:能)?\s*(?:达到|达|满足|符合|实现|到)\s*"
    r"[^，,。;；\n]{0,16}?(?:通过率|通过标准|达标率|合格率|验收标准|标准|要求|合格|达标)"
    r"|(?:未|没有|没|不)\s*(?:达到|达|满足|符合)\s*(?:通过|验收)?\s*(?:标准|要求|门槛)"
    r"|(?:not|did\s+not|does\s+not|failed\s+to)\s+(?:reach|meet|achieve|satisfy)\s+"
    r"(?:the\s+)?(?:pass(?:ing)?\s+)?(?:standard|criteria|threshold|requirement)",
    re.IGNORECASE,
)
# 部分通过 = 未全部通过（部分用例通过 / tests partially passed）。
_QA_PARTIAL_PASS_RE = re.compile(
    r"(?:partially|partly)\s*(?:pass(?:ed)?|succeed(?:ed)?|ok)"
    r"|(?:部分|部份)\s*(?:用例|测试|检查|项)?\s*(?:通过|合格)"
    r"|(?:some|only\s+some)\s+(?:tests?|checks?|cases?)\s+(?:pass(?:ed)?|succeed(?:ed)?)",
    re.IGNORECASE,
)

# 通过语义（在失败语义之后判定）
_QA_PASS_TERMS = ("通过", "✅")

_QA_ANNOTATION_MARKER = "<!-- grape-qa-gate"
_ANNOTATED_MAX = 2048
# 进程内幂等缓存：仅作远程查询的快速前置（跨进程去重仍以远端评论标记为准）。
# 用 OrderedDict 实现 LRU，超限淘汰最久未命中项；并发 worker 共享同进程缓存，需加锁。
_annotated: OrderedDict[tuple[str, int, str], None] = OrderedDict()
_annotated_lock = threading.Lock()


def _remember_annotation(key: tuple[str, int, str]) -> None:
    """记录已标注键（LRU 淘汰，线程安全）。"""
    with _annotated_lock:
        _annotated[key] = None
        _annotated.move_to_end(key)
        while len(_annotated) > _ANNOTATED_MAX:
            _annotated.popitem(last=False)


def _is_annotated(key: tuple[str, int, str]) -> bool:
    """命中的键刷新 LRU 顺序，返回是否已标注。"""
    with _annotated_lock:
        if key not in _annotated:
            return False
        _annotated.move_to_end(key)
        return True


def _issue_tags(issue_number) -> tuple[str, str] | None:
    """归一 Issue 编号为 (#N, N) 两种历史 issue_id 形态。非法编号返回 None。"""
    _s = str(issue_number or "").strip().lstrip("#")
    if not _s.isdigit() or _s == "0":
        return None
    return f"#{_s}", _s


def _issue_number_int(issue_number) -> int:
    _tags = _issue_tags(issue_number)
    return int(_tags[1]) if _tags else 0


def _issue_scene_key(repo_key: str, issue_number) -> str | None:
    """该 Issue 当前流水线场景标识（与 events.scene_id 对齐；空场景归一 ``default``）。

    与 ``pipeline_has_qa_node`` 使用同一解析口径，保证“同一流水线”判定一致。
    非法编号或查询异常返回 ``None``——调用方须按“查询异常均放行”处理，
    不得退化为 ``default`` 流水线判定。
    """
    _tags = _issue_tags(issue_number)
    if not _tags:
        return None
    try:
        from src.api.session_store import get_issue_scene

        _scene = get_issue_scene(repo_key, _tags[0])
        return (_scene[0] if _scene and _scene[0] else "") or "default"
    except Exception as _e:
        log.warning(f"qa gate: issue scene resolve failed {repo_key}/#{issue_number}: {_e}")
        return None


def pipeline_has_qa_node(repo_key: str, issue_number) -> bool:
    """该 Issue 所属流水线是否存在启用中的 qa 节点（无 qa 阶段 → 不启用门禁）。

    场景解析异常（含非法编号）→ False（放行，不按 default 流水线判定）。
    """
    try:
        from src.api.pipeline_config import get_pipeline_config

        _scene_key = _issue_scene_key(repo_key, issue_number)
        if _scene_key is None:
            log.info(f"qa gate: scene unresolved, skip gate {repo_key}/#{issue_number}")
            return False
        _nodes = (get_pipeline_config(_scene_key, repo_key=repo_key) or {}).get("nodes") or []
        for _n in _nodes:
            _nid = str(_n.get("id") or _n.get("tag") or "").strip().lower()
            if _nid == "qa" and _n.get("enabled", True):
                return True
        return False
    except Exception as _e:
        log.warning(f"qa gate: pipeline qa node check failed {repo_key}/#{issue_number}: {_e}")
        return False


def latest_qa_event(repo_key: str, issue_number) -> dict | None:
    """该 Issue 同一流水线最新一条 qa 事件（按 seq 最大）。无记录/异常返回 None。"""
    _tags = _issue_tags(issue_number)
    if not _tags:
        return None
    _scene_id = _issue_scene_key(repo_key, issue_number)
    if _scene_id is None:
        # 场景解析异常 → 放行，不按 default 流水线匹配历史事件
        log.info(f"qa gate: scene unresolved, skip latest event {repo_key}/#{issue_number}")
        return None
    if _scene_id == "default":
        # 仓库默认/未绑定场景：历史事件的 scene_id 可能为空串或 "default"
        _scene_clause = "(scene_id=? OR scene_id='' OR scene_id IS NULL)"
        _scene_arg = "default"
    else:
        _scene_clause = "scene_id=?"
        _scene_arg = _scene_id
    try:
        from src.session_manager import db, db_lock

        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, status, result, summary FROM events "
                "WHERE repo_key=? AND issue_id IN (?, ?) AND agent_type='qa' "
                f"AND {_scene_clause} "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, _tags[0], _tags[1], _scene_arg))
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

    - status=failed/cancelled/error/failure → fail（Spec：取消同样视为未通过）；
    - running/dispatched/pending/queued → unknown（尚无结论）；
    - completed 时解析 result 文本：先剔除否定语境，失败语义优先，其次通过语义；
      其中“无法通过/not passed”等否定式通过判 fail。
    """
    _st = (status or "").strip().lower()
    if _st in ("failed", "failure", "error", "cancelled", "canceled"):
        return "fail"
    if _st in ("running", "dispatched", "pending", "queued"):
        return "unknown"
    if _st not in ("", "completed", "success", "succeeded"):
        return "unknown"

    _blob = (text or "").strip()
    if not _blob:
        return "unknown"
    # 先剔除后置定量否定（0 tests failed / 未通过项 0），否则会被误判为失败或否定式通过
    _normalized = _QA_NEG_TRAILING_RE.sub("", _blob)
    _normalized = _QA_NEG_CN_TRAILING_RE.sub("", _normalized)
    # 否定式通过/未达标/部分通过优先于泛化的“通过”词（未全通过 / 未达到 100% 通过率）
    if _QA_NEG_PASS_RE.search(_normalized):
        return "fail"
    if _QA_UNMET_STANDARD_RE.search(_normalized):
        return "fail"
    if _QA_PARTIAL_PASS_RE.search(_normalized):
        return "fail"
    _normalized = _QA_NEGATION_RE.sub("", _normalized)

    if any(_t in _normalized for _t in _QA_FAIL_TERMS):
        return "fail"
    if any(_w in _normalized for _w in _QA_FAIL_WORDS):
        return "fail"
    if _QA_FAIL_EN_WORD_RE.search(_normalized):
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


def gate_or_annotate(repo_key: str, issue_number, pr_number: int = 0,
                     node_failure: dict | None = None) -> dict:
    """统一门禁入口：校验 QA 结论，命中 fail 时写入 Issue 标注，返回校验结果。

    各关闭/合入判定点复用此函数，避免 `qa_gate_check → annotate_qa_failure`
    样板散落多处（Issue #1739 审查：门禁策略变更需多点同步）。自身不抛异常。

    ``node_failure``：流水线节点失败分支（如 qa 节点 status=failed）使用的
    节点失败语义。即使结论文本不可判定，也按节点失败标注，避免该分支自行手写
    门禁样板。
    """
    _info = qa_gate_check(repo_key, issue_number)
    _annotate_info = None
    if node_failure:
        _annotate_info = dict(_info or {})
        if not _annotate_info.get("reason"):
            _annotate_info["reason"] = (node_failure.get("reason")
                                        or "qa 节点失败")
        if not _annotate_info.get("status"):
            _annotate_info["status"] = node_failure.get("status") or ""
    elif _info.get("blocked"):
        _annotate_info = _info
    if _annotate_info is not None:
        try:
            annotate_qa_failure(repo_key, issue_number, _annotate_info,
                                pr_number=pr_number)
        except Exception as _e:
            log.warning(f"qa gate: annotate via guard failed "
                        f"{repo_key}/#{issue_number}: {_e}")
    return _info


def collect_qa_failures(repo_key: str, issue_numbers) -> list[dict]:
    """批量校验多个关联 Issue，返回全部 qa fail 项（``[{issue, info}]``）。

    PR 建单/合入都是“多 Issue 一并判定”，此处统一收集逻辑，调用方不再各自
    手写 check→收集循环（Issue #1739 审查：门禁样板重复）。
    """
    _failures: list[dict] = []
    for _num in issue_numbers or []:
        try:
            _info = qa_gate_check(repo_key, _num)
        except Exception as _e:
            log.warning(f"qa gate: collect check failed (fail-open) "
                        f"{repo_key}/#{_num}: {_e}")
            continue
        if _info.get("blocked"):
            _failures.append({"issue": _num, "info": _info})
    return _failures


def annotate_qa_failures(repo_key: str, failures, pr_number: int = 0) -> None:
    """批量标注 qa fail 的 Issue（best-effort，不抛异常，不因首个失败中断）。"""
    for _f in failures or []:
        try:
            annotate_qa_failure(repo_key, _f.get("issue"), _f.get("info") or {},
                                pr_number=pr_number)
        except Exception as _e:
            log.warning(f"qa gate: annotate batch failed "
                        f"{repo_key}/#{_f.get('issue')}: {_e}")


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
    # Issue #1739：GitHub 为 /pull/{n}，GitCode（及 GitLab 语义 provider）为
    # /merge_requests/{n}；统一拼 /pull 会让 GitCode 关联 PR 链接 404。
    if _provider == "github":
        return f"https://github.com/{_owner}/{_repo}/pull/{pr_number}"
    return f"https://gitcode.com/{_owner}/{_repo}/merge_requests/{pr_number}"


def _annotation_token(seq: int, status: str, reason: str) -> str:
    """幂等标记：seq + 结论指纹，避免 seq=0 时不同失败被折叠成一条评论。"""
    _h = hashlib.md5(f"{status}|{reason}".encode("utf-8")).hexdigest()[:8]
    return f"{_QA_ANNOTATION_MARKER} seq={seq} hash={_h} -->"


def _annotation_exists(client, issue_number: int, token: str) -> bool:
    """best-effort：查询 Issue 已有评论是否包含相同幂等标记（跨进程/重启去重）。

    查询失败或返回结构异常 → False（宁可重复一次，也不漏标注）。
    """
    try:
        _comments = client.list_issue_comments(issue_number)
    except Exception as _e:
        log.info(f"qa gate: list comments failed, skip remote dedupe: {_e}")
        return False
    if not isinstance(_comments, list):
        return False
    for _c in _comments:
        _body = _c.get("body") if isinstance(_c, dict) else ""
        if _body and token in str(_body):
            return True
    return False


def annotate_qa_failure(repo_key: str, issue_number, qa_info: dict,
                        pr_number: int = 0) -> str:
    """在 Issue 上标注 QA 失败原因与关联 PR（best-effort，返回 success/skipped/failed）。"""
    try:
        _num = _issue_number_int(issue_number)
        _seq = int((qa_info or {}).get("event_seq") or 0)
        if not _num:
            return "skipped"
        _reason = (qa_info or {}).get("reason") or "qa 未通过"
        _status = (qa_info or {}).get("status") or ""
        _token = _annotation_token(_seq, _status, _reason)
        _key = (repo_key, _num, _token)
        if _is_annotated(_key):
            return "skipped"
        _pr = int(pr_number or 0) or _latest_pr_number(repo_key, _num)
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
        _lines.append(_token)
        _body = "\n".join(_lines)
        return _post_issue_comment(repo_key, _num, _body, _key)
    except Exception as _e:
        log.warning(f"qa gate: annotate failed {repo_key}/#{issue_number}: {_e}")
        return "failed"


def _post_issue_comment(repo_key: str, issue_number: int, body: str,
                        key: tuple[str, int, str]) -> str:
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
        if _annotation_exists(_client, issue_number, key[2]):
            _remember_annotation(key)
            log.info(f"qa gate: annotation already present {repo_key}/#{issue_number}")
            return "skipped"
        _resp = _client.post_issue_comment(issue_number, body)
        if isinstance(_resp, dict) and "error" not in _resp:
            _remember_annotation(key)
            log.info(f"qa gate: annotated {repo_key}/#{issue_number}")
            return "success"
        log.warning(f"qa gate: annotate {repo_key}/#{issue_number} -> {_resp}")
        return "failed"
    except Exception as _e:
        log.warning(f"qa gate: post annotation failed {repo_key}/#{issue_number}: {_e}")
        return "failed"
