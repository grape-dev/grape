"""Issue #2429: QA gate 英文失败词上下文豁免 + 未闭环 fail 优先 + 交叉引用隔离。

复现：seq=71902 的归档结论语义为通过，但裸词 Required 命中失败词规则导致
grape-test#183 合入被 409 阻断。
"""

SEQ_71902 = (
    "复审通过，无 Critical/Required，PR #181 已自动合入 arch-weekly-11"
    "（merge sha 16ca6a73c）；上轮 2 条 Required 均已闭环，"
    "真跑 4 passed / 0 skipped，违规样本 2/2+2/2 会红。"
)


def _classify(text: str) -> str:
    from src.api.qa_gate import classify_qa_conclusion
    return classify_qa_conclusion("completed", text)


def test_seq_71902_is_pass():
    assert _classify(SEQ_71902) == "pass"


def test_closed_quantifier_context_is_pass():
    assert _classify("N 条 Required 均已闭环") == "pass"
    assert _classify("上轮 2 条 Critical 已全部修复") == "pass"
    assert _classify("本轮 1 项 Required 已处理") == "pass"
    # 量词在名词之后 / 闭环短语共享 / 闭环词在名词之前
    assert _classify("Critical 1 项，Required 2 项，均已在上一轮闭环") == "pass"
    assert _classify("已修复 2 条 Critical") == "pass"


def test_negated_unresolved_is_not_fail():
    """无/没有 + 未闭环类是双重否定，等价于已闭环，不能按未处理判 fail。"""
    assert _classify("无未处理项，结论通过") == "pass"
    assert _classify("无未闭环项") != "fail"
    assert _classify("没有未修复的问题") != "fail"
    assert _classify("均已修复，无未修复") != "fail"


def test_negated_unresolved_with_en_noun_is_not_fail():
    """否定结构 + 英文名词 + 未闭环=等价已闭环，不能判 fail（复审残留误判）。"""
    assert _classify("无 Critical 未闭环") == "pass"
    assert _classify("无 Critical 未修复") == "pass"
    assert _classify("无任何 Critical 未闭环") == "pass"
    assert _classify("没有 Required 未处理") == "pass"
    assert _classify("未发现 Required 未修复") == "pass"
    assert _classify("无 Critical 未闭环项") == "pass"
    # 否定只覆盖紧邻名词；带入真实失败项（数字/逗号分隔）仍须 fail
    assert _classify("无 Critical，2 条 Required 未修复") == "fail"
    assert _classify("无未修复的问题，但有 1 条 Critical 未处理") == "fail"


def test_unresolved_quantifier_context_is_fail():
    assert _classify("上轮 2 条 Required 未修复") == "fail"
    assert _classify("有 1 条 Critical 未闭环") == "fail"
    assert _classify("复审通过，但仍有 1 条 Critical 未处理") == "fail"


def test_negation_at_all_positions_is_pass():
    assert _classify("无 Critical/Required") == "pass"
    assert _classify("无 Critical，也无 Required") == "pass"
    assert _classify("no Critical, no Required") == "pass"


def test_explicit_pass_does_not_hide_real_failure():
    assert _classify("Critical: 1 个未修复") == "fail"
    assert _classify("required fixes: 3") == "fail"
    assert _classify("复审通过，required fixes: 2") == "fail"
    # 共享闭环短语不能吞掉同段真实失败项
    assert _classify("Critical 1 项需修复，Required 2 项均已闭环") == "fail"
    assert _classify("已修复 2 条 Critical，但 3 条 Required 未修复") == "fail"


def test_numeric_zero_has_word_boundary():
    """“20 failures”里的 0 不能被当作“0 failures”放行。"""
    assert _classify("20 failures") == "fail"
    assert _classify("10 problems") != "pass"
    assert _classify("10 critical issues remain") == "fail"
    # 真正的零失败仍为通过
    assert _classify("0 failures") == "pass"
    assert _classify("未通过 0 项") == "pass"


def test_gate_prefers_explicit_issue_chain(monkeypatch):
    """PR 详情已有显式 issues 关联时，body 里的交叉引用 #214 不参与合门禁。"""
    from src.api import git_repo_api, qa_gate

    captured = {}

    def _fake_collect(repo_key, nums):
        captured["nums"] = list(nums)
        return []

    monkeypatch.setattr(qa_gate, "collect_qa_failures", _fake_collect)
    detail = {
        "number": 183,
        "body": "口径同 Issue #214 + PR #178，本 PR 不实现 #214。",
        "issues": [{"number": 1739}],
    }
    git_repo_api._pre_merge_qa_gate(
        "grape-dev", "grape-test", 183, "gitcode", "tok", "gitcode:grape-dev/grape-test",
        pr_detail=detail, detail_loaded=True)
    assert captured["nums"] == [1739]
