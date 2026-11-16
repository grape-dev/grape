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

from src.kernel.config import log

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
    r"(?:无|非|不|未|没有|没|不存在|未见|未发现|无需|不需要|no|not|without|zero|none|(?<![\d.])0(?![\d]))\s*"
    r"(?:任何|的|个|条|项)?\s*"
    r"(?:(?:阻塞|阻断|冲突|critical|required|硬违规|失败|不通过|未通过|未达标|不达标|"
    r"问题|缺陷|违规|违反|越界|超出范围|最差|驳回|打回|"
    r"failures?|fails?|issues?|problems?|bugs?|blockers?|blocking|fixes?|"
    r"整改|需修复|需要修复|待修复|修复)[\s、,，/和及:：]*)+",
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
    r"\s*[:：=-]?\s*(?:none|zero|(?<![\d.])0(?![\d])|no)\b"
    # 英文后置定量否定：0/no tests failed / 0 failures（实际为全部通过，不能判失败）
    r"|\b(?:no|zero|(?<![\d.])0(?![\d]))\s+(?:\w+\s+){0,3}?(?:failures?|fails?|failed|errors?)\b",
    re.IGNORECASE,
)
# Issue #2429：量词结构 + 已闭环上下文豁免。"上轮 2 条 Required 均已闭环" /
# "N 条 Critical 已修复" / "Critical 1 项，Required 2 项，均已在上一轮闭环" 指代
# 已处理项，英文词不得计入失败词。允许量词在名词前后，并允许闭环短语共享。
_QA_CLOSED_CTX_RE = re.compile(
    r"(?:(?:上轮|上一轮|此前|本轮|之前|前轮|历史)\s*)?"
    r"(?:(?:\d+|若干|[Nn]|几)\s*(?:条|项|个)\s*)?"
    r"\b(?:critical|required)\b"
    r"(?:\s*(?:问题|项|缺陷|items?|issues?|fix(?:es)?))?"
    r"(?:\s*(?:\d+|若干|[Nn]|几)\s*(?:条|项|个))?"
    # 共享闭环短语的间隔只允许列表连接词等中性字符；出现失败信号（需/未/待/违/缺/失/不）
    # 即视为该段包含真实失败项，不得整体豁免。
    r"[^。;；\n需未待违缺失不]{0,20}?"
    r"(?:均已|均已经|已经|已)\s*(?:全部)?\s*"
    r"(?:(?:在|于)\s*(?:上一轮|上轮|此前|之前|本轮|历史)?)?\s*"
    r"(?:全部)?\s*(?:闭环|修复|处理|解决|关闭|完成)"
    # QA 第 3 轮：进行时尾缀（修复中/处理中/修复阶段/repairing）不算闭环。这条
    # 前括号必须挡住，否则无数量表述（"Required 多项已在修复中"）会在规则 5 整体
    # 豁免——规则 4c 只对带数量的句子生效，管不到这种形态。
    r"(?![^\S\n]*(?:中|之中|阶段|stage|ing\b))",
    re.IGNORECASE,
)
# Issue #2429：闭环词在名词之前（"已修复 2 条 Critical"）同样属已处理项豁免。
_QA_CLOSED_LEAD_RE = re.compile(
    r"(?:均已|均已经|已经|已)\s*(?:全部)?\s*(?:闭环|修复|处理|解决|关闭|完成)"
    r"(?![^\S\n]*(?:中|之中|阶段|stage|ing\b))\s*"
    r"(?:\d+|若干|[Nn]|几)?\s*(?:条|项|个)?\s*"
    r"\b(?:critical|required)\b"
    r"(?:\s*(?:问题|项|缺陷|items?|issues?|fix(?:es)?))*",
    re.IGNORECASE,
)
# Issue #2429：显式“不存在未闭环项”是双重否定（无 + 未修复），须先剔除再做
# 未闭环判定，否则“无未处理项，结论通过”会被误判 fail。
_QA_NEGATED_UNRESOLVED_RE = re.compile(
    r"(?:无|没有|没|不存在|未见|未发现|无需|不需要|零|(?<![\d.])0(?![\d]))\s*"
    r"(?:任何|的|个|条|项|遗留|剩余)?\s*"
    # Issue #2429：否定与“未+闭环”之间可夹英文名词（无 Critical 未闭环 /
    # 没有 Required 未处理 / 未发现 Required 未修复）。只允许名词及其
    # 问题/项修饰与中性连接符，禁止数字和逗号——“无 Critical，2 条
    # Required 未修复”仍须命中未闭环判 fail。
    r"(?:(?:critical|required)\b"
    r"(?:\s*(?:问题|项|缺陷|items?|issues?|fix(?:es)?))?"
    r"\s*(?:[/、和及与]|and)?\s*)*"
    r"未(?:被|全部|完全|能|曾|再)?\s*"
    r"(?:修复|闭环|处理|解决|关闭|完成)",
    re.IGNORECASE,
)
# Issue #2429：反向未闭环结构必须优先判 fail（未修复/未闭环/未处理…），
# 不能被上下文豁免或否定剔除吞掉。
_QA_UNRESOLVED_RE = re.compile(
    r"(?:尚未|未|没有|没)\s*(?:被|全部|完全|能)?\s*"
    r"(?:修复|闭环|处理|解决|关闭|完成)",
)
# Issue #2429：显式“不存在失败项”也属通过语义（无 Critical/Required、
# no blocking issues、0 failures），在失败语义之后放行。数字 0 需词边界，
# 避免“20 failures”里的 0 被当成“0 failures”放行。
_QA_ABSENCE_RE = re.compile(
    r"(?:无|没有|不存在|未见|未发现|无需|不需要|\bno\b|\bnot\b|\bwithout\b|"
    r"\bzero\b|\bnone\b|(?<![\d.])0(?![\d]))\s*"
    r"(?:任何|的|个|条|项)?\s*"
    r"(?:critical|required|阻塞|阻断|冲突|硬违规|失败|不通过|未通过|"
    r"failures?|fails?|issues?|problems?|bugs?|blockers?|blocking)",
    re.IGNORECASE,
)
# 中文后置否定（“阻塞为零 / 阻塞 0 / 阻断: 0 / 未通过项 0”）；动词须含闭环族，
# 否则“未处理 0 项 / 未闭环 0 项”无法被剔除，会落到规则 4b 误拦（QA 第 2 轮）。
_QA_NEG_CN_TRAILING_RE = re.compile(
    r"(?:阻塞|阻断|冲突|硬违规|失败|不通过|未通过|问题|缺陷|违规|整改|修复|闭环|处理|解决|关闭|完成)"
    r"\s*(?:的)?\s*(?:项|个|条|例|处|次|数量|数)?\s*(?:[:：=]|为|是)?\s*"
    r"(?:零|(?<![\d.])0(?![\d]))\s*(?:项|个|条|例|处|次)?",
)
# 中文前置定量否定（“0 项未修复 / 零项待处理”）：数量在动词之前，上面的后置式匹配不到。
_QA_NEG_CN_LEADING_RE = re.compile(
    r"(?:零|(?<![\d.])0(?![\d]))\s*(?:项|个|条|例|处|次)?\s*(?:的)?\s*"
    r"(?:尚未|未|没有|没)\s*(?:被|全部|完全|能)?\s*(?:修复|闭环|处理|解决|关闭|完成)",
)

# QA 第 2 轮：闭环豁免必须按项数对账，不能“命中即通过”。只对显式带正数量的
# Critical/Required 项计账（“required fixes: none” 这类无数量表述交给既有否定规则）：
#   - 闭环动词后跟 中/阶段/ing → 在修复中，不算闭环；
#   - 句内出现 只有/仅/部分/前者/后者/其中 → 无法证明全部闭环；
#   - “已修复 N 项” 少于声明总数 → 差额计入未闭环。
_QA_ITEM_QTY_RE = re.compile(
    r"(?:(?P<n>\d+)\s*(?:条|项|个)\s*(?:的)?\s*)?"
    r"\b(?:critical|required)\b"
    r"(?:\s*(?:问题|项|缺陷|items?|issues?|fix(?:es)?))?"
    r"(?:\s*(?P<m>\d+)\s*(?:条|项|个))?",
    re.IGNORECASE)
_QA_CLOSED_VERB_RE = re.compile(
    r"(?:均已|均已经|已经|已)\s*(?:全部)?\s*"
    r"(?:(?:在|于)\s*(?:上一轮|上轮|此前|之前|本轮|历史)?)?\s*"
    r"(?:全部)?\s*(?:闭环|修复|处理|解决|关闭|完成)")
_QA_FIXED_QTY_RE = re.compile(
    r"(?:已|均已|已经)\s*(?:全部)?\s*(?:闭环|修复|处理|解决|关闭|完成)\s*(?:了)?\s*"
    r"(\d+)\s*(?:条|项|个)")
_QA_IN_PROGRESS_TAIL_RE = re.compile(r"(?:中|之中|阶段|stage|ing\b)")
_QA_PARTIAL_SCOPE_RE = re.compile(r"(?:只有|仅有|仅|部分|其余|前者|后者|其中)")


def _qa_unresolved_items(blob: str) -> int:
    """按句统计仍未闭环的 Critical/Required 项数（0 = 无残留项）。"""
    _unresolved = 0
    for _sent in re.split(r"[。;；\n]", blob or ""):
        _total = 0
        for _m in _QA_ITEM_QTY_RE.finditer(_sent):
            _qty = int(_m.group("n") or _m.group("m") or 0)
            if _qty > 0:
                _total += _qty
        if not _total:
            continue
        _closed = list(_QA_CLOSED_VERB_RE.finditer(_sent))
        if any(_QA_IN_PROGRESS_TAIL_RE.match(_sent[m.end():]) for m in _closed):
            _unresolved += _total
            continue
        if not _closed or _QA_PARTIAL_SCOPE_RE.search(_sent):
            _unresolved += _total
            continue
        _fixed = sum(int(_x) for _x in _QA_FIXED_QTY_RE.findall(_sent))
        if _fixed and _fixed < _total:
            _unresolved += _total - _fixed
    return _unresolved

# 英文失败/通过按词边界识别（避免 "ok" 命中 "broken"、"fail" 命中 "failsafe" 之类子串误判）
_QA_FAIL_EN_RE = re.compile(
    r"\b(?:fail|failed|fails|failure|failures|failing|errors?)\b", re.IGNORECASE)
_QA_PASS_EN_RE = re.compile(r"\b(?:pass|passed|ok)\b", re.IGNORECASE)

# 否定式“通过”= 实际未通过（“无法通过/没有通过/not passed”不能被下面的通过语义吞掉）。
# 否定词与通过词之间允许“全部/完全/达到/测试”等修饰语（未全部通过 / 未达到通过标准 /
# not all tests passed / not fully passed），但不能跨越无关子句（not required, tests
# passed 中的 not 不得命中后面的 passed）。
_QA_NEG_PASS_RE = re.compile(
    r"(?:无法|不能|不可|未能|未|没有|没|不会|no\s+way\s+to|"
    r"not\s+(?:be\s+)?able\s+to|unable\s+to|cannot|can't|"
    r"did\s+not|does\s+not|didn't|doesn't|never|not|"
    # 英文量词否定：none of the tests passed / no tests passed / 0 tests passed
    r"none|no|zero|(?<![\d.])0(?![\d]))\s*"
    r"(?:(?:全部|所有|全|完全|百分百|100%|能够|能|均|都|达到|达|满足|"
    r"all|every|any|fully|completely|entirely|be|been|"
    r"have|has|had|are|is|were|was|of|the|them|these|those|do|does|did)\s*)*"
    r"(?:(?:测试|用例|检查|项|tests?|checks?|cases?)\s*)*"
    # 测试名后允许助动词（not all tests have passed / tests are not passing）
    r"(?:(?:have|has|had|are|is|were|was|been|be|do|does|did)\s*)*"
    r"(?:通过|pass(?:ed)?|ok|succeed|approved|合并|合入)",
    re.IGNORECASE,
)
# 未达标语义（否定词可能在通过词之后）：通过率未达 100% / 未达到通过标准 /
# not reach the passing standard——这些都表示未通过，不能被“通过”一词吞成 pass。
_QA_UNMET_STANDARD_RE = re.compile(
    r"(?:通过率|合格率|达标率|pass\s+rate)\s*[^。;\n]{0,10}?"
    r"(?:未|没有|没|不)\s*(?:达到|达|到|满足|达标|符合)\s*"
    r"(?:100\s*%|百分之百|标准|要求|门槛|预期|目标)?"
    # 通过率 + 不是/不足/未满/低于/仅/只 + 数字%（通过率不是100% / 不足100% / 仅80%）
    r"|(?:通过率|合格率|达标率|pass\s+rate)\s*[^。;\n]{0,10}?"
    r"(?:不是|不足|不满|未满|低于|少于|不够|仅为?|只有?|仅|只)\s*"
    r"(?:\d+\s*%|百分之百|标准|要求|门槛|预期|目标)"
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

# 真实 qa 结论常见形态是“项数汇总”而非“不通过/失败”字样
# （Standards: 3 项…；Spec: 4 项需修复…）。Spec 轴报告的正向项数即代表未通过，
# 不能依赖“失败/不通过”字面，也不能被缺陷描述里出现的英文 “pass” 吞成通过。
_QA_SUMMARY_COUNT_RE = re.compile(
    r"(?<![A-Za-z])(?:Standards|Spec|规范|标准|规格|需求(?:评审|规格)?|验收(?:评审|标准)?)"
    r"(?![A-Za-z])\s*[:：]?\s*(?:发现|存在|共|有)?\s*([0-9]+)\s*(?:项|个|条)"
    r"|[1-9]\d*\s*(?:项|个|条)\s*(?:需|需要|待)\s*修复",
    re.IGNORECASE,
)
# 汇总中的硬失败语义（出现即按未通过处理，显式“结论：通过”也不能覆盖）。
_QA_SUMMARY_HARD_RE = re.compile(
    r"需修复|需要修复|待修复|硬违规|违反|越界|超出范围|最差|"
    r"不通过|未通过|未达标|不达标|阻塞|阻断|失败|漏判|误判|绕过|失效|缺陷|违规|"
    r"(?:中高|高)\s*[:：]"
)
# 仅属建议/低判断项的汇总项：项数 > 0 也不应视为失败（需结合显式通过结论）。
_QA_SUMMARY_SUGGEST_RE = re.compile(
    r"建议|判断项|可选|优化|低\s*[:：]|低\s*(?:严重|风险|优先)"
)
# 显式通过结论：用于放行“仅含建议/低判断项”的项数汇总。
_QA_EXPLICIT_PASS_RE = re.compile(
    r"结论\s*[:：]?\s*(?:为|是)?\s*(?:通过|pass(?:ed)?\b|✅)",
    re.IGNORECASE,
)
# 无轴标签的“N 项需修复”汇总（N > 0 即视为未通过）。
_QA_SUMMARY_FIX_RE = re.compile(
    r"[1-9]\d*\s*(?:项|个|条)\s*(?:需|需要|待)\s*修复"
)
# 汇总语义里的失败信号（在剔除否定语境后判定，避免“无违反/无需修复”误判）。
_QA_SUMMARY_KEYWORD_RE = re.compile(
    r"最差|违反|越界|超出范围|需修复|需要修复|待修复|未达(?!标)"
)
# 零失败率表示全量通过（失败率为 0 / 失败数为 0 / 0% 的失败率），
# 需在“失败”字面判定前归一为通过，且不影响同段其他真实失败信号。
_QA_ZERO_FAIL_RATE_RE = re.compile(
    r"(?:失败率|失败数|失败次数|未通过率|错误率|错误数)\s*"
    r"(?:[:：=]|为|是)?\s*(?:零|0)\s*(?:%|％|项|个|条|次)?"
    r"|(?:零|0)\s*(?:%|％)?\s*(?:的)?\s*(?:失败率|失败数|失败次数)"
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
    # 仓库默认/未绑定场景：历史事件的 scene_id 可能为空串或 "default"
    _default_scene = _scene_id == "default"
    try:
        from src.domains.runtime import public as _runtime_public

        _row = _runtime_public.qa_gate_latest_event_any(
            repo_key, _tags, _scene_id, default_scene=_default_scene)
        if not _row:
            return None
        _structured = None
        if len(_row) > 4 and _row[4]:
            try:
                _parsed = json.loads(_row[4])
                _structured = _parsed if isinstance(_parsed, dict) else None
            except Exception:
                _structured = None
        return {"seq": int(_row[0] or 0), "status": (_row[1] or "").strip(),
                "result": _row[2] or "", "summary": _row[3] or "",
                "structured": _structured}
    except Exception as _e:
        log.warning(f"qa gate: latest qa event query failed {repo_key}/#{issue_number}: {_e}")
        return None


def _qa_fail_by_terms(text: str) -> bool:
    return (any(_t in text for _t in _QA_FAIL_TERMS)
            or any(_w in text for _w in _QA_FAIL_WORDS)
            or bool(_QA_FAIL_EN_WORD_RE.search(text))
            or bool(_QA_FAIL_EN_RE.search(text)))


def _qa_pass_by_terms(text: str) -> bool:
    return (any(_t in text for _t in _QA_PASS_TERMS)
            or bool(_QA_PASS_EN_RE.search(text)))


def _qa_summary_verdict(blob: str) -> str | None:
    """项数汇总判定：区分硬失败项与建议/低判断项。

    - 任一轴存在硬失败项（需修复/违反/最差/阻塞/失败等）→ ``fail``；
    - 正项数未标注为建议/低判断项 → ``fail``（对齐历史“N 项即未通过”语义）；
    - 全部正项数均为建议/低判断项且含显式通过结论 → ``pass``；
    - 无正项数 / 无汇总标签 → ``None``（交由后续规则判定）。
    """
    _matches = list(_QA_SUMMARY_COUNT_RE.finditer(blob))
    _has_positive = False
    _suggestion_only = False
    for _i, _m in enumerate(_matches):
        if _m.group(1) is None:
            # 无轴标签的“N 项需修复”汇总是明确失败项。
            return "fail"
        _count = int(_m.group(1))
        if _count <= 0:
            continue
        _end = _matches[_i + 1].start() if _i + 1 < len(_matches) else len(blob)
        _seg = blob[_m.end():_end]
        if _QA_SUMMARY_HARD_RE.search(_seg):
            return "fail"
        _has_positive = True
        if not _QA_SUMMARY_SUGGEST_RE.search(_seg):
            # 未标注为建议/低判断项的正项数 → 视为失败项
            return "fail"
        _suggestion_only = True
    if not _has_positive:
        return "fail" if _QA_SUMMARY_FIX_RE.search(blob) else None
    # 全部正项数均为建议/低判断项：仅显式通过结论时才放行。
    if _suggestion_only and _QA_EXPLICIT_PASS_RE.search(blob):
        return "pass"
    return None


def _classify_qa_text(blob: str) -> str:
    """结论文本的有序规则判定。

    规则表按顺序命中即返回，显式表达优先级（复审：避免多组正则隐式依赖命中顺序）：
    1. 项数汇总（Standards/Spec N 项 / N 项需修复）→ 硬失败项 fail；仅建议/低判断项
       且显式“结论：通过”→ pass；
    2. 零失败率归一为“通过”（失败率为 0）；
    3. 剔除后置定量否定（0 tests failed / 未通过项 0）；
    4. 否定式通过 / 未达标 / 部分通过 / 反向未闭环 → fail；
    5. 剔除“量词 + 已闭环”上下文与否定语境（无违反 / 无阻塞 / no failures）；
    6. 汇总失败词 → fail；
    7. 失败语义 → fail，其次显式无失败/通过语义 → pass；
    8. 不可判定 → unknown（fail-open）。
    """
    # 规则 1：真实 qa 失败结论多为项数汇总，不能依赖“失败/不通过”字面；
    # 但“仅建议/低判断项 + 显式通过”不应拦截正常关闭链路。
    _summary_verdict = _qa_summary_verdict(blob)
    if _summary_verdict is not None:
        return _summary_verdict
    # 规则 2：失败率/失败数为 0 表示全量通过。
    _normalized = _QA_ZERO_FAIL_RATE_RE.sub("通过", blob)
    # 规则 3：先剔除后置定量否定，否则“未通过 0 项”会被否定式通过误判。
    # 命中后置定量“零失败”表述即属显式无失败项。
    _before_trailing = _normalized
    _normalized = _QA_NEG_TRAILING_RE.sub("", _normalized)
    _normalized = _QA_NEG_CN_TRAILING_RE.sub("", _normalized)
    # QA 第 2 轮：前置定量否定（“0 项未修复”）与后置式同口径剔除，否则规则 4b 会误拦。
    _normalized = _QA_NEG_CN_LEADING_RE.sub("", _normalized)
    _zero_by_trailing = _normalized != _before_trailing
    # 规则 4：否定式通过/未达标/部分通过优先于泛化的“通过”词。
    if _QA_NEG_PASS_RE.search(_normalized):
        return "fail"
    if _QA_UNMET_STANDARD_RE.search(_normalized):
        return "fail"
    if _QA_PARTIAL_PASS_RE.search(_normalized):
        return "fail"
    # 规则 4b：先在剔除“无/没有 + 未修复/未闭环”双重否定后判定，避免
    # “无未处理项，结论通过”被反向未闭环规则误杀。QA 第 2 轮：改用
    # `_normalized`（已剔除后置/前置定量否定），否则“0 项未修复”会按原文命中。
    if _QA_UNRESOLVED_RE.search(_QA_NEGATED_UNRESOLVED_RE.sub("", _normalized)):
        return "fail"
    # 规则 4c：闭环豁免按项数对账——句内仍有未闭环的 Critical/Required 项即 fail。
    # 覆盖三类误放：在修复中（不是闭环）、只有后者已闭环（共享短语吞掉前项）、
    # 已修复 1 项 / 声明 2 项（部分修复）。
    if _qa_unresolved_items(blob) > 0:
        return "fail"
    # 规则 5：先剔除“量词/闭环 + 已闭环”豁免，再剔除否定语境，最后做失败/通过判定。
    _closed_context = bool(_QA_CLOSED_CTX_RE.search(_normalized)
                           or _QA_CLOSED_LEAD_RE.search(_normalized))
    _normalized = _QA_CLOSED_CTX_RE.sub("", _normalized)
    _normalized = _QA_CLOSED_LEAD_RE.sub("", _normalized)
    _normalized = _QA_NEGATION_RE.sub("", _normalized)
    # 规则 6：汇总失败词（最差/违反/越界/需修复/未达）→ fail。
    if _QA_SUMMARY_KEYWORD_RE.search(_normalized):
        return "fail"
    # 规则 7：失败语义优先于通过语义；已闭环上下文/显式无失败项也视为通过。
    if _qa_fail_by_terms(_normalized):
        return "fail"
    if _closed_context or _zero_by_trailing or _QA_ABSENCE_RE.search(blob):
        return "pass"
    if _qa_pass_by_terms(_normalized):
        return "pass"
    return "unknown"


def classify_qa_conclusion(status: str, text: str,
                           structured: dict | None = None) -> str:
    """判定 QA 结论：``fail`` / ``pass`` / ``unknown``。

    - status=failed/cancelled/error/failure → fail（Spec：取消同样视为未通过）；
    - running/dispatched/pending/queued → unknown（尚无结论）；
    - completed 时优先使用结构化产出 ``archive.output``：
      ``conclusion`` 作为结论文本，``next_step`` 指示需 coding 修复时兜底判 fail；
    - 无结构化时解析 result 文本（先剔除否定语境，失败语义优先，其次通过语义）；
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
    _next_step = ""
    if isinstance(structured, dict):
        _structured_conclusion = str(structured.get("conclusion") or "").strip()
        if _structured_conclusion:
            _blob = _structured_conclusion
        _next_step = str(structured.get("next_step") or "").strip().lower()
    if not _blob:
        # 结构化产出无 conclusion，但 next_step 明确交办 coding 修复 → 未通过。
        if _next_step.startswith("a2a_coding"):
            return "fail"
        return "unknown"
    _verdict = _classify_qa_text(_blob)
    if _verdict == "unknown" and _next_step.startswith("a2a_coding"):
        return "fail"
    return _verdict


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
        _verdict = classify_qa_conclusion(_result["status"], _evt.get("result") or "",
                                          structured=_evt.get("structured"))
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
        from src.domains.runtime import public as _runtime_public

        _payload = _runtime_public.qa_gate_latest_pr_payload_any(repo_key, _tags)
        if not _payload:
            return 0
        _p = json.loads(_payload) if isinstance(_payload, str) else (_payload or {})
        _oa = (_p.get("object_attributes") or {}) if isinstance(_p, dict) else {}
        _pr = _p.get("pull_request") or {} if isinstance(_p, dict) else {}
        for _v in (_oa.get("iid"), _oa.get("number"), _pr.get("number"), _pr.get("iid")):
            _s = str(_v or "").strip()
            if _s.isdigit() and int(_s) > 0:
                return int(_s)
    except Exception:
        return 0
    return 0


# PR/MR 网页路径段：provider 模块能力位 PR_WEB_PATH（gitcode=merge_requests /
# gitee=pulls / github=pull / gitlab=merge_requests），业务侧零平台判断。
def _pr_web_path(provider: str) -> str:
    from src.domains.runtime.vcs.providers import _MODULES
    _m = _MODULES.get(provider or "")
    return getattr(_m, "PR_WEB_PATH", "pull") or "pull"


def _pr_link(repo_key: str, pr_number: int) -> str:
    if not pr_number:
        return ""
    from src.kernel.repo_key import parse_repo_key

    _provider, _owner, _repo = parse_repo_key(repo_key)
    if not _owner or not _repo:
        return ""
    # Issue #1739：host 与 PR 路径均按 repo_key 平台派生（此前非 github 一律拼
    # gitcode.com/merge_requests —— gitee/gitlab 的 host+路径双错）。
    # 平台卡 host 优先（无卡回退 repo_key 的存量映射），PR 路径见 _PR_WEB_PATH。
    try:
        from src.domains.runtime.vcs.platform_card import host_for_repo_key
        _host = host_for_repo_key(repo_key)
    except Exception:
        _host = ""
    if not _host:
        return ""
    return f"https://{_host}/{_owner}/{_repo}/{_pr_web_path(_provider)}/{pr_number}"


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
        from src.kernel.repo_key import parse_repo_key
        from src.domains.runtime.vcs.api_client import VcsApiClient

        _provider, _owner, _repo = parse_repo_key(repo_key)
        if not _owner or not _repo:
            return "skipped"
        # 2026-11-13 if/else 收口：统一入口按 repo_key 前缀派发（github=
        # 策略链+D32-补丁回落，其余=TokenResolver 统一链），零平台分叉。
        from src.domains.mgmt.user.db import get_repo_token_for as _get_token
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
