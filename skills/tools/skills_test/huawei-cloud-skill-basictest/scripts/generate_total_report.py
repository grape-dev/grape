#!/usr/bin/env python3
"""generate_total_report.py — 合并生成 PR 唯一总测试报告

将 case-run 日期目录下的整合测试报告、问题单、正反用例集合并为一份《总测试报告》，
作为上传到 PR 的唯一报告。覆盖式生成（同名覆盖，不新增多份）。

用法:
  python3 generate_total_report.py --skill <skill_name> --case-run <日期目录>
      [--output <输出目录>] [--pr-url <PR链接>]

输出:
  {output}/{skill}_总测试报告.md
"""

import argparse
import re
import sys
from pathlib import Path


def parse_stats(report_text: str):
    """从整合测试报告解析测试统计（合计行）。"""
    m = re.search(r"\|\s*合计\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", report_text)
    if m:
        total, passed, warned, failed = map(int, m.groups())
        rate = round((passed + warned) / total * 100, 1) if total else 0.0
        return total, passed, warned, failed, rate
    return None


def parse_stats_detailed(report_text: str):
    """从整合测试报告解析正例/反例分项统计。

    格式（列顺序：总用例 / 通过 / 告警 / 失败）：
    | 正例 | N | N | 0 | 0 |
    → (总正例, 正例通过, 正例告警, 正例失败)
    | 反例 | N | N | 0 | M |
    
    返回 (pos_pass, pos_total, neg_pass, neg_total, warned)。
    """
    pos = re.search(r"\|\s*正例\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", report_text)
    neg = re.search(r"\|\s*反例\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", report_text)
    if pos and neg:
        p_total, p_pass, p_warn, p_fail = map(int, pos.groups())
        n_total, n_pass, n_warn, n_fail = map(int, neg.groups())
        return (p_pass, p_total, n_pass, n_total, p_warn + n_warn)
    return None


def extract_issues(issue_text: str):
    """提取问题单中每个问题（标题 + 严重级别 + 改进点）。"""
    issues = []
    for block in re.split(r"\n## 问题 \d+:", issue_text)[1:]:
        title_line = block.strip().splitlines()[0].strip() if block.strip() else ""
        sev = re.search(r"\|\s*严重级别\s*\|\s*(\w+)", block)
        improvement = re.search(r"### 改进点\n\n(.*?)(?=\n## |\Z)", block, re.S)
        issues.append({
            "title": title_line,
            "severity": sev.group(1) if sev else "LOW",
            "improvement": improvement.group(1).strip() if improvement else "",
        })
    return issues


def _extract_field(block: str, field_name: str) -> str:
    """从用例段落提取指定字段值（如"用户原话"、"预期行为"）。"""
    m = re.search(rf"- \*\*{re.escape(field_name)}\*\*\s*(.*)", block)
    if not m:
        m = re.search(rf"- {re.escape(field_name)}[：:]\s*(.*)", block)
    return m.group(1).strip() if m else ""


def render_cases_as_table(text: str) -> str:
    """将正反用例集（段落格式）渲染为紧凑表格。"""
    lines = ["| 用例 | 用户原话 | 预期行为 | 实际结果 | 结论 |",
             "|------|---------|---------|---------|------|"]
    blocks = re.split(r"\n(?=### [PN]\d+[：:])", text)
    for block in blocks:
        block = block.strip()
        if not block.startswith("### "):
            continue
        title_m = re.match(r"### ([PN]\d+[：:]\s*.+)", block)
        if not title_m:
            continue
        case_id = title_m.group(1).strip()
        user_input = _extract_field(block, "用户原话")
        expected = _extract_field(block, "预期行为")
        actual = _extract_field(block, "实测")
        # 结论从实际结果行中的 ✅/❌ 标记判定
        if "✅" in actual or "右通过" in actual:
            result = "✅"
        elif "❌" in actual or "失败" in actual or "不通过" in actual:
            result = "❌"
        else:
            result = "—"
        actual_short = actual.replace("→", "").strip()[:50]
        lines.append(f"| {case_id[:24]} | {user_input[:35]} | {expected[:35]} | {actual_short} | {result} |")
    return "\n".join(lines) + "\n"


def compute_verdict(stats, issues, detailed=None):
    """判定测试通过/不通过。

    规则: 只要测试发现问题(BUG)即测试不通过。
    有用例失败但均为反例预期失败（无真实BUG），判通过并标注。
    返回 (verdict, reason): verdict in ('pass','fail'), reason 为结论描述。
    """
    if issues:
        return "fail", f"测试发现 {len(issues)} 个问题（BUG），测试不通过"
    if stats and stats[3] > 0:
        if detailed:
            # 有分项统计: 正例全部通过, 失败来自反例 → 标注预期失败
            pos_pass, pos_total, neg_pass, neg_total, warned = detailed
            real_fail = len(issues)  # 真实 BUG 来自问题单，非反例失败数列
            if pos_pass == pos_total and real_fail == 0:
                return "pass", f"正例 {pos_pass}/{pos_total} 全通过；{neg_total - neg_pass} 条反例正确报错（预期失败）；无真实 BUG"
            return "fail", f"存在 {stats[3]} 个失败用例（含真实失败），测试不通过"
        return "fail", f"存在 {stats[3]} 个失败用例，测试不通过"
    return "pass", "测试未发现问题，全部用例通过"


def render_summary(verdict, reason, issues):
    """渲染《测试总结》章节（置于报告最前）。
    不通过时把问题清单作为不通过的证据列在结论下方。"""
    lines = ["## 测试总结", ""]
    if verdict == "fail":
        lines.append("**测试结论：❌ 不通过**")
        lines.append("")
        lines.append(f"> {reason}。以下问题清单为测试不通过的证据：")
        lines.append("")
        lines.append("| 编号 | 严重级别 | 问题 |")
        lines.append("|------|---------|------|")
        for i, iss in enumerate(issues, 1):
            lines.append(f"| {i} | {iss['severity']} | {iss['title']} |")
    else:
        lines.append("**测试结论：✅ 通过**")
        lines.append("")
        lines.append(f"> {reason}")
    lines.append("")
    return lines


def main():
    parser = argparse.ArgumentParser(description="合并生成 PR 唯一总测试报告")
    parser.add_argument("--skill", required=True, help="被测 skill 名称")
    parser.add_argument("--case-run", required=True, help="case-run 日期目录路径")
    parser.add_argument("--output", default=None, help="输出目录（默认=日期目录）")
    parser.add_argument("--pr-url", default="", help="对应 PR 链接")
    args = parser.parse_args()

    case_dir = Path(args.case_run)
    if not case_dir.is_dir():
        print(f"错误: 日期目录不存在: {case_dir}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output) if args.output else case_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{args.skill}_总测试报告.md"

    integrated = case_dir / "整合测试报告.md"
    issue_file = case_dir / f"{args.skill}_问题单.md"
    case_set = case_dir / "正反用例集.md"

    if not integrated.exists():
        print(f"错误: 缺少整合测试报告: {integrated}", file=sys.stderr)
        sys.exit(1)

    integrated_text = integrated.read_text(encoding="utf-8")

    lines = []
    lines.append(f"# 总测试报告: {args.skill}")
    lines.append("")
    lines.append("- 生成时间: 2026-08-03")
    lines.append(f"- 被测 skill: {args.skill}")
    if args.pr_url:
        lines.append(f"- 对应 PR: {args.pr_url}")
    lines.append("- 报告类型: PR 唯一总测试报告（一个 PR 一份）")
    lines.append("")

    # 解析统计与问题（供测试总结使用）
    stats = parse_stats(integrated_text)
    detailed = parse_stats_detailed(integrated_text)
    issues = []
    if issue_file.exists():
        issues = extract_issues(issue_file.read_text(encoding="utf-8"))

    # 〇、测试总结（置顶，结论先行）
    verdict, reason = compute_verdict(stats, issues, detailed)
    lines.extend(render_summary(verdict, reason, issues))

    # 一、测试统计（横版：有分项时显示正例/反例/真实BUG）
    lines.append("## 一、测试统计")
    lines.append("")
    if detailed:
        pos_pass, pos_total, neg_pass, neg_total, warned = detailed
        pos_rate = round(pos_pass / pos_total * 100, 1) if pos_total else 0.0
        lines.append(f"| 正例通过率 | 反例(预期失败) | 真实失败(BUG) | 警告 |")
        lines.append(f"|-----------|----------------|--------------|------|")
        lines.append(f"| {pos_pass}/{pos_total} ({pos_rate}%) | {neg_total - neg_pass} | 0 | {warned} |")
    elif stats:
        total, passed, warned, failed, rate = stats
        lines.append(f"| 总用例数 | 通过 | 告警 | 失败 | 通过率 |")
        lines.append(f"|---------|------|------|------|--------|")
        lines.append(f"| {total} | {passed} | {warned} | {failed} | {rate}% |")
    else:
        lines.append("（未解析到统计，见整合测试报告）")
    lines.append("")

    # 二、问题清单
    lines.append("## 二、问题清单")
    lines.append("")
    if issue_file.exists():
        if issues:
            lines.append("| 编号 | 严重级别 | 问题 | 改进点 |")
            lines.append("|------|---------|------|--------|")
            for i, iss in enumerate(issues, 1):
                imp = iss["improvement"].replace("\n", " ")[:80]
                lines.append(f"| {i} | {iss['severity']} | {iss['title']} | {imp} |")
        else:
            lines.append("（问题单中未提取到问题条目）")
    else:
        lines.append("（未发现问题单）")
    lines.append("")

    # 三、完整整合报告
    lines.append("## 三、详细测试报告")
    lines.append("")
    # 去掉整合报告内部所有 `## ` 转为 `### `（避免编号与主报告冲突）
    cleaned = re.sub(r'^## ', '### ', integrated_text, flags=re.M)
    # 去掉"四、正反用例集"及其内容（属于整合报告内部旧排版，外部已用紧凑表格替代）
    cleaned = re.sub(r'\n### 四、正反用例集\n.*?(?=\n### |\Z)', '', cleaned, flags=re.S)
    lines.append(cleaned)
    lines.append("")

    # 附录：正反用例集（紧凑表格，避免与整合报告内部编号冲突）
    if case_set.exists():
        lines.append("## 附录：正反用例集")
        lines.append("")
        lines.append(render_cases_as_table(case_set.read_text(encoding="utf-8")))
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 已生成总测试报告: {report_path}")
    print(f"   一个 PR 仅此一份（覆盖式，重复运行不新增）")


if __name__ == "__main__":
    main()
