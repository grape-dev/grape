#!/usr/bin/env python3
"""generate_total_report.py 的测试总结判定单测。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from generate_total_report import compute_verdict, render_summary

PASS_STATS = (10, 10, 0, 0, 100.0)
FAIL_STATS = (10, 8, 0, 2, 80.0)
NO_ISSUES = []
ISSUES = [
    {"title": "SKILL.md 文档 --domain 参数不可用", "severity": "MEDIUM", "improvement": "改用 --domains.N"},
    {"title": "ShowEvent 无效 eventid 静默空", "severity": "LOW", "improvement": "明确报错"},
]


def test_pass_no_issues():
    v, reason = compute_verdict(PASS_STATS, NO_ISSUES)
    assert v == "pass", f"expected pass, got {v}"
    assert "通过" in reason


def test_fail_when_issues():
    # 核心规则: 只要测试发现问题(BUG), 就不通过
    v, reason = compute_verdict(PASS_STATS, ISSUES)
    assert v == "fail", f"有问题(即使用例全过)也应不通过, got {v}"
    assert "问题" in reason


def test_fail_when_failed_cases():
    # 有失败用例但没有问题单条目 → 也不通过
    v, reason = compute_verdict(FAIL_STATS, NO_ISSUES)
    assert v == "fail", f"有失败用例应不通过, got {v}"
    assert "失败" in reason


def test_fail_when_both():
    v, reason = compute_verdict(FAIL_STATS, ISSUES)
    assert v == "fail"
    assert "问题" in reason


def test_render_pass():
    out = render_summary("pass", "测试未发现问题，全部用例通过", NO_ISSUES)
    text = "\n".join(out)
    assert "✅ 通过" in text
    assert "问题清单" not in text  # 通过时不列问题清单


def test_render_fail_lists_issues_as_evidence():
    out = render_summary("fail", "测试发现 2 个问题（BUG），测试不通过", ISSUES)
    text = "\n".join(out)
    assert "❌ 不通过" in text
    assert "问题清单" in text
    assert "证据" in text
    assert "SKILL.md 文档 --domain 参数不可用" in text
    assert "ShowEvent 无效 eventid 静默空" in text
    assert "MEDIUM" in text and "LOW" in text


def test_verdict_is_stable_function():
    # compute_verdict 应是无副作用纯函数
    before = list(ISSUES)
    compute_verdict(PASS_STATS, ISSUES)
    assert before == ISSUES


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ✅ {fn.__name__}")
    print(f"\n全部 {passed} 个测试通过")
