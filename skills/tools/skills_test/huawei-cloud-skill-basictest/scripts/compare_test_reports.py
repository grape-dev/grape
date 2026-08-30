#!/usr/bin/env python3
"""compare_test_reports.py — 比对两份测试报告（skills_test vs skills_test2）

从两份《总测试报告.md》解析统计与问题数，输出双智能体对比表格文本，
供 leader 在测试结论中展示。

用法:
  python3 compare_test_reports.py <report1.md> <report2.md> --name1 <名称> --name2 <名称>

输出（stdout）: Markdown 对比表格
"""
import argparse
import re


def parse_report(report_text):
    """从总测试报告解析 {总用例, 通过, 失败, 问题数, 结论}。解析不到返回 None。"""
    stats = {}
    m = re.search(r"\|\s*总用例数\s*\|\s*(\d+)\s*\|", report_text)
    if m:
        stats["total"] = int(m.group(1))
    m = re.search(r"\|\s*通过\s*\|\s*(\d+)\s*\|", report_text)
    if m:
        stats["passed"] = int(m.group(1))
    m = re.search(r"\|\s*失败\s*\|\s*(\d+)\s*\|", report_text)
    if m:
        stats["failed"] = int(m.group(1))
    # 核心统计缺失 → 无法比对, 返回 None
    if not stats:
        return None
    # 问题数：测试总结里"发现 N 个问题" 或 问题清单行数
    m = re.search(r"测试发现\s*(\d+)\s*个问题", report_text)
    if m:
        stats["issues"] = int(m.group(1))
    else:
        stats["issues"] = 0
    # 结论：测试总结中的 ✅ 通过 / ❌ 不通过
    if "**测试结论：✅ 通过**" in report_text:
        stats["verdict"] = "通过"
    elif "**测试结论：❌ 不通过**" in report_text:
        stats["verdict"] = "不通过"
    else:
        stats["verdict"] = "?"
    return stats


def render_comparison(r1, r2, name1="Hermes", name2="opencode"):
    """渲染双智能体结果对比表（横版：维度行 × Agent列）。"""
    def cell(r, k, default="-"):
        return str(r[k]) if r and r.get(k) is not None else default
    lines = [
        "### 双智能体测试结果对比",
        "",
        f"| 维度 | {name1} | {name2} |",
        "|------|--------|----------|",
        f"| 总用例 | {cell(r1,'total')} | {cell(r2,'total')} |",
        f"| 通过 | {cell(r1,'passed')} | {cell(r2,'passed')} |",
        f"| 失败 | {cell(r1,'failed')} | {cell(r2,'failed')} |",
        f"| 问题数 | {cell(r1,'issues')} | {cell(r2,'issues')} |",
        f"| 测试结论 | {cell(r1,'verdict')} | {cell(r2,'verdict')} |",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="比对两份测试报告")
    parser.add_argument("report1", help="报告1 路径")
    parser.add_argument("report2", help="报告2 路径")
    parser.add_argument("--name1", default="Hermes")
    parser.add_argument("--name2", default="opencode")
    args = parser.parse_args()

    def load(p):
        try:
            return open(p, encoding="utf-8").read()
        except OSError as e:
            print(f"错误: 无法读取 {p}: {e}", file=__import__("sys").stderr)
            return None

    t1, t2 = load(args.report1), load(args.report2)
    if t1 is None or t2 is None:
        __import__("sys").exit(1)
    r1, r2 = parse_report(t1), parse_report(t2)
    if not r1 and not r2:
        print("错误: 两份报告均无法解析统计", file=__import__("sys").stderr)
        __import__("sys").exit(1)
    print(render_comparison(r1, r2, args.name1, args.name2))


if __name__ == "__main__":
    main()
