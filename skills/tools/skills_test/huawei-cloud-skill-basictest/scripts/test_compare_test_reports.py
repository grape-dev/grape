#!/usr/bin/env python3
"""compare_test_reports.py 单测。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from compare_test_reports import parse_report, render_comparison

REPORT_PASS = """# 总测试报告: demo
## 测试总结
**测试结论：✅ 通过**
## 一、测试统计
| 指标 | 数值 |
|------|------|
| 总用例数 | 18 |
| 通过 | 18 |
| 告警 | 0 |
| 失败 | 0 |
"""

REPORT_FAIL = """# 总测试报告: demo
## 测试总结
**测试结论：❌ 不通过**
> 测试发现 2 个问题（BUG），测试不通过。
## 一、测试统计
| 指标 | 数值 |
|------|------|
| 总用例数 | 18 |
| 通过 | 16 |
| 告警 | 0 |
| 失败 | 2 |
"""


def test_parse_pass_report():
    r = parse_report(REPORT_PASS)
    assert r["total"] == 18 and r["passed"] == 18 and r["failed"] == 0
    assert r["issues"] == 0 and r["verdict"] == "通过"


def test_parse_fail_report():
    r = parse_report(REPORT_FAIL)
    assert r["total"] == 18 and r["passed"] == 16 and r["failed"] == 2
    assert r["issues"] == 2 and r["verdict"] == "不通过"


def test_render_contains_both_agents():
    r1 = parse_report(REPORT_PASS)
    r2 = parse_report(REPORT_FAIL)
    out = render_comparison(r1, r2, "Hermes", "opencode")
    assert "Hermes" in out and "opencode" in out
    assert "双智能体测试结果对比" in out


def test_render_transposed_contains_both():
    r1 = parse_report(REPORT_PASS)
    r2 = parse_report(REPORT_FAIL)
    out = render_comparison(r1, r2)
    assert "| 总用例 | 18 | 18 |" in out
    assert "| 通过 | 18 | 16 |" in out
    assert "| 失败 | 0 | 2 |" in out
    assert "| 测试结论 | 通过 | 不通过 |" in out


def test_parse_none_for_garbage():
    assert parse_report("nothing useful") is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n全部 {len(fns)} 个测试通过")
