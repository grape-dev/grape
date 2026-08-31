#!/usr/bin/env python3
"""
generate_summary.py - 评测汇总报告生成主脚本

功能:
1. 生成单个 skill 的汇总报告
2. 生成全部 skill 的总览汇总报告
3. 更新已有报告（增量模式）

用法:
  # 生成全部 skill 汇总
  python3 generate_summary.py --eval-dir /path/to/evaluation-results --output-dir /path/to/output

  # 生成单个 skill 汇总
  python3 generate_summary.py --eval-dir /path/to/evaluation-results --output-dir /path/to/output --skill huawei-cloud-swr-image-automation

  # 更新模式（只更新有变化的 skill）
  python3 generate_summary.py --eval-dir /path/to/evaluation-results --output-dir /path/to/output --update
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 将 scripts 目录加入 path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from parse_eval_results import parse_all_skills, parse_case_run, parse_tester, find_issue_reports
from fetch_issues import (
    fetch_issues, normalize_issue, filter_issues_by_skill,
    group_issues_by_skill, extract_skill_from_title, format_issue_row
)


# ============================================================
# Skill → 云服务映射
# ============================================================
SKILL_SERVICE_MAP = {
    "huawei-cloud-obs-bucket-create": "OBS 对象存储",
    "huawei-cloud-obs-upload": "OBS 对象存储",
    "huawei-cloud-obs-stats": "OBS 对象存储",
    "huawei-cloud-obs-website-host": "OBS 对象存储 + DNS",
    "huawei-cloud-swr-image-management": "SWR（容器镜像仓库）",
    "huawei-cloud-swr-image-automation": "SWR（容器镜像仓库）",
    "huawei-cloud-swr-image-governance": "SWR（容器镜像仓库）",
    "huawei-cloud-swr-enterprise-instance": "SWR（容器镜像仓库）",
    "huawei-cloud-ecs-passwordless-login": "ECS 弹性云服务器 + COC",
    "huawei-cloud-computing-query": "ECS/BMS/IMS/AS 计算",
    "huawei-cloud-network-query": "VPC/EIP/ELB/NAT/VPN/DNS 网络",
    "huawei-cloud-storage-query": "EVS/OBS/SFS/CBR 存储",
    "huawei-cloud-iam-query": "IAM 身份认证",
    "huawei-cloud-monitoring-query": "CES/EPS 监控",
    "huawei-cloud-billing-scout": "BSS 计费",
    "huawei-cloud-business-support-query": "BSS 计费",
    "huawei-cloud-business-tf-support": "BSS 计费 + Terraform",
    "huawei-cloud-cci-instance-management": "CCI 云容器实例",
    "huawei-cloud-codearts-code-gen": "CodeArts 代码生成",
    "huawei-cloud-functiongraph-function-create": "FunctionGraph 函数",
    "huawei-cloud-functiongraph-trigger-create": "FunctionGraph 函数",
    "huawei-cloud-ges-graph": "GES 图数据库",
    "huawei-cloud-skill-audit": "Skill 开发工具",
    "huawei-cloud-skill-creator": "Skill 创建工具",
    "huawei-cloud-skill-tester": "Skill 测试工具",
    "huawei-cloud-terraform-generator": "Terraform IaC",
    "huawei-cloud-terraform-installer": "Terraform 安装",
    "huawei-cloud-ucs-cluster-onboarding-manager": "UCS 集群纳管",
    "huawei-cloud-ucs-policy-governor": "UCS 策略治理",
    "huawei-cloud-vod-collector": "VOC 体验反馈",
    "huawei-cloud-find-skills": "Skill 发现工具",
    "huawei-cloud-flexus-l-deploy-jiuwenswarm": "Flexus L 部署",
    "huawei-cloud-rds-list": "RDS 云数据库",
}


def get_service_name(skill_name: str) -> str:
    """获取 skill 对应的云服务名称。"""
    return SKILL_SERVICE_MAP.get(skill_name, "—")


def pass_rate_color(rate: float) -> str:
    """根据通过率返回颜色标记。"""
    if rate >= 95:
        return "🟢"
    elif rate >= 80:
        return "🟡"
    else:
        return "🔴"


def format_rate(rate: float) -> str:
    """格式化通过率。"""
    icon = pass_rate_color(rate)
    return f"{icon} {rate:.1f}%"


def sort_issues(issues: list) -> list:
    """排序问题单：open 在前、closed 在后，各自按创建时间倒序。"""
    open_issues = sorted(
        [i for i in issues if i.get("state") == "open"],
        key=lambda i: i.get("created_at", ""),
        reverse=True,
    )
    closed_issues = sorted(
        [i for i in issues if i.get("state") != "open"],
        key=lambda i: i.get("created_at", ""),
        reverse=True,
    )
    return open_issues + closed_issues


# ============================================================
# 单个 Skill 汇总报告生成
# ============================================================

def generate_skill_summary(
    skill_name: str,
    eval_data: dict,
    issues: list,
    output_dir: Path,
) -> Path:
    """生成单个 skill 的汇总报告。

    Args:
        skill_name: skill 名称
        eval_data: 解析后的评测数据
        issues: 该 skill 相关的 issue 列表
        output_dir: 输出目录

    Returns:
        Path: 生成的报告文件路径
    """
    service = get_service_name(skill_name)
    today = datetime.now().strftime("%Y-%m-%d")

    lines = []
    lines.append(f"# {skill_name} 汇总清单")
    lines.append("")
    lines.append(f"> 生成日期：{today}")
    lines.append("> 数据来源：本地 case-run 测试报告 + tester 测试报告 + GitCode Issue API")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- 一、测试统计 ----
    lines.append("## 一、测试统计")
    lines.append("")

    case_run = eval_data.get("case_run", [])
    tester = eval_data.get("tester", [])

    table_header = "| 序号 | Skill | 归属服务 | 报告生成时间 | 总用例数 | 通过 | 告警 | 失败 | 通过率 | 测试报告 |"
    table_sep = "|------|-------|---------|-------------|---------|------|------|------|--------|---------|"

    # case-run 表
    lines.append("### case-run")
    lines.append("")
    lines.append(table_header)
    lines.append(table_sep)
    if case_run:
        for idx, rec in enumerate(case_run, 1):
            rate = rec.get("pass_rate", 0)
            date_dir_name = rec.get("date_dir_name", "")
            if rec.get("report_file"):
                report_link = f"[整合测试报告](../evaluation-results/{skill_name}/case-run/{date_dir_name}/{rec['report_file']})"
            else:
                report_link = "—"
            lines.append(
                f"| {idx} | {skill_name} | {service} | "
                f"{rec.get('report_date', '—')} | "
                f"{rec.get('total', 0)} | "
                f"{rec.get('pass', 0)} | "
                f"{rec.get('warning', 0)} | "
                f"{rec.get('fail', 0)} | "
                f"{rate:.1f}% | {report_link} |"
            )
    else:
        lines.append(f"| — | {skill_name} | {service} | — | — | — | — | — | — | — |")
    lines.append("")

    # tester 表
    lines.append("### tester")
    lines.append("")
    lines.append(table_header)
    lines.append(table_sep)
    if tester:
        for idx, rec in enumerate(tester, 1):
            rate = rec.get("pass_rate", 0)
            date_dir_name = rec.get("date_dir_name", "")
            if rec.get("report_file"):
                report_link = f"[test-report](../evaluation-results/{skill_name}/tester/{date_dir_name}/{rec['report_file']})"
            else:
                report_link = "—"
            lines.append(
                f"| {idx} | {skill_name} | {service} | "
                f"{rec.get('report_date', '—')} | "
                f"{rec.get('total', 0)} | "
                f"{rec.get('pass', 0)} | "
                f"{rec.get('warning', 0)} | "
                f"{rec.get('fail', 0)} | "
                f"{rate:.1f}% | {report_link} |"
            )
    else:
        lines.append(f"| — | {skill_name} | {service} | — | — | — | — | — | — | — |")
    lines.append("")

    lines.append("> 通过率 = (通过 + 告警) / 总用例数 × 100%")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- 二、GitCode 问题单清单 ----
    lines.append("## 二、GitCode 问题单清单")
    lines.append("")

    # 只保留 open 状态的 Issue
    open_issues = [i for i in issues if i.get("state") == "open"]

    if open_issues:
        # 按创建时间倒序排列
        sorted_issue_list = sorted(
            open_issues,
            key=lambda i: i.get("created_at", ""),
            reverse=True,
        )

        lines.append(f"共 {len(open_issues)} 个 open Issue。")
        lines.append("")

        lines.append("| Issue | 状态 | 创建时间 | 责任人 | 标签 | 标题 |")
        lines.append("|-------|------|---------|--------|------|------|")
        for issue in sorted_issue_list:
            lines.append(format_issue_row(issue))
    else:
        lines.append("暂无 open 状态的 Issue。")

    lines.append("")
    lines.append("---")

    # 写入文件
    report_path = output_dir / f"{skill_name}_summary.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")

    return report_path


# ============================================================
# 总览汇总报告生成
# ============================================================

def generate_overview_summary(
    all_eval_data: dict,
    all_issues: list,
    output_dir: Path,
) -> Path:
    """生成全部 skill 的总览汇总报告。

    Args:
        all_eval_data: {skill_name: eval_data}
        all_issues: 全部 issue 列表
        output_dir: 输出目录

    Returns:
        Path: 生成的报告文件路径
    """
    today = datetime.now().strftime("%Y-%m-%d")
    skill_count = len(all_eval_data)
    lines = []

    lines.append("# 评测结果总览")
    lines.append("")
    lines.append(f"> 生成日期：{today}")
    lines.append("> 数据来源：本地 case-run / tester 测试报告 + GitCode Issue API")
    lines.append(f"> 覆盖范围：evaluation-results/ 下 {skill_count} 个 Skill 目录")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- 一、测试统计总览 ----
    lines.append("## 一、测试统计总览")
    lines.append("")

    table_header = "| 序号 | Skill | 归属服务 | 报告生成时间 | 总用例数 | 通过 | 告警 | 失败 | 通过率 | 测试报告 |"
    table_sep = "|------|-------|---------|-------------|---------|------|------|------|--------|---------|"

    issue_groups = group_issues_by_skill(all_issues)
    sorted_skills = sorted(all_eval_data.keys())

    for tool_key, tool_name in [("case_run", "case-run"), ("tester", "tester")]:
        lines.append(f"### {tool_name}")
        lines.append("")
        lines.append(table_header)
        lines.append(table_sep)

        seq = 0
        sum_total = 0
        sum_pass = 0
        sum_warn = 0
        sum_fail = 0

        for skill_name in sorted_skills:
            eval_data = all_eval_data[skill_name]
            service = get_service_name(skill_name)
            tool_records = eval_data.get(tool_key, [])

            if tool_records:
                for rec in tool_records:
                    seq += 1
                    total = rec.get("total", 0)
                    p = rec.get("pass", 0)
                    w = rec.get("warning", 0)
                    f = rec.get("fail", 0)
                    rate = rec.get("pass_rate", 0)
                    date_dir_name = rec.get("date_dir_name", "")
                    if rec.get("report_file"):
                        if tool_key == "case_run":
                            report_link = f"[整合测试报告](../evaluation-results/{skill_name}/case-run/{date_dir_name}/{rec['report_file']})"
                        else:
                            report_link = f"[test-report](../evaluation-results/{skill_name}/tester/{date_dir_name}/{rec['report_file']})"
                    else:
                        report_link = "—"
                    lines.append(
                        f"| {seq} | {skill_name} | {service} | "
                        f"{rec.get('report_date', '—')} | "
                        f"{total} | {p} | {w} | {f} | {rate:.1f}% | {report_link} |"
                    )
                    sum_total += total
                    sum_pass += p
                    sum_warn += w
                    sum_fail += f
            else:
                lines.append(
                    f"| — | {skill_name} | {service} | — | — | — | — | — | — | — |"
                )

        # 合计行
        if sum_total > 0:
            sum_rate = round((sum_pass + sum_warn) / sum_total * 100, 2)
            lines.append(
                f"| | **合计** | | | **{sum_total}** | **{sum_pass}** | "
                f"**{sum_warn}** | **{sum_fail}** | **{sum_rate}%** | |"
            )
        lines.append("")

    lines.append("> 通过率 = (通过 + 告警) / 总用例数 × 100%")

    # 额外注释：无数据 skill
    no_data_skills = []
    for skill_name in sorted_skills:
        eval_data = all_eval_data[skill_name]
        ai = eval_data.get("case_run", [])
        te = eval_data.get("tester", [])
        if not ai and not te:
            no_data_skills.append(skill_name)
    for skill in no_data_skills:
        lines.append(f"> {skill} 暂无本地测试目录，数据待补充。")

    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- 二、GitCode 问题单清单 ----
    lines.append("## 二、GitCode 问题单清单")
    lines.append("")

    # 只保留 open 状态的 Issue
    open_issues = [i for i in all_issues if i.get("state") == "open"]

    if open_issues:
        lines.append(f"共 {len(open_issues)} 个 open Issue。")
        lines.append("")
        lines.append("| Issue | 状态 | 创建时间 | 责任人 | 标签 | 标题 |")
        lines.append("|-------|------|---------|--------|------|------|")

        # 按创建时间倒序排列
        sorted_open_issues = sorted(
            open_issues,
            key=lambda i: i.get("created_at", ""),
            reverse=True,
        )
        for issue in sorted_open_issues:
            lines.append(format_issue_row(issue))
    else:
        lines.append("暂无 open 状态的 Issue。")

    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- 三、各 Skill 汇总文件 ----
    lines.append("## 三、各 Skill 汇总文件")
    lines.append("")
    lines.append("| Skill | 汇总文件 | Issue 数 | 有分析报告 | case-run 通过率 | tester 通过率 |")
    lines.append("|-------|---------|---------|-----------|----------------|--------------|")

    total_issues = 0
    total_reports = 0

    for skill_name in sorted_skills:
        eval_data = all_eval_data[skill_name]
        case_run = eval_data.get("case_run", [])
        tester = eval_data.get("tester", [])

        ai_rate = f"{case_run[0]['pass_rate']:.1f}%" if case_run and case_run[0].get("total", 0) > 0 else "—"
        te_rate = f"{tester[0]['pass_rate']:.1f}%" if tester and tester[0].get("total", 0) > 0 else "—"

        skill_issues = issue_groups.get(skill_name, [])
        issue_count = len(skill_issues)
        total_issues += issue_count

        # 有分析报告
        issue_reports = eval_data.get("issue_reports", [])
        report_count = len(issue_reports)
        total_reports += report_count
        if report_count > 0:
            # 提取 Issue 编号
            report_nums = []
            for rf in issue_reports:
                m = re.match(r"Issue-(\d+)-", rf)
                if m:
                    report_nums.append(f"#{m.group(1)}")
            report_str = f"{report_count}（{'、'.join(report_nums)}）"
        else:
            report_str = "0"

        lines.append(
            f"| {skill_name} | [summary]({skill_name}_summary.md) | "
            f"{issue_count} | {report_str} | {ai_rate} | {te_rate} |"
        )

    # 合计行
    # 计算加权平均通过率
    all_ai = [rec for d in all_eval_data.values() for rec in d.get("case_run", [])]
    all_te = [rec for d in all_eval_data.values() for rec in d.get("tester", [])]
    ai_total = sum(d.get("total", 0) for d in all_ai)
    ai_pass = sum(d.get("pass", 0) + d.get("warning", 0) for d in all_ai)
    te_total = sum(d.get("total", 0) for d in all_te)
    te_pass = sum(d.get("pass", 0) + d.get("warning", 0) for d in all_te)
    ai_avg = f"{round(ai_pass / ai_total * 100, 2)}%" if ai_total > 0 else "—"
    te_avg = f"{round(te_pass / te_total * 100, 2)}%" if te_total > 0 else "—"

    lines.append(
        f"| | **合计** | **{total_issues}** | **{total_reports}** | "
        f"**{ai_avg}** | **{te_avg}** |"
    )
    lines.append("")
    lines.append("---")

    # 写入文件
    report_path = output_dir / "总览_summary.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")

    return report_path


# ============================================================
# 仓库更新
# ============================================================

def update_repo(repo_dir: Path) -> bool:
    """拉取仓库最新代码。

    Args:
        repo_dir: 仓库目录

    Returns:
        bool: 是否成功更新
    """
    if not (repo_dir / ".git").exists():
        print(f"⚠️  {repo_dir} 不是 git 仓库，跳过更新", file=sys.stderr)
        return False

    print(f"📥 更新仓库: {repo_dir}")
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"⚠️  git pull 失败: {result.stderr}", file=sys.stderr)
        return False
    print(f"✅ 仓库已更新")
    return True


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="评测汇总报告生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 生成全部 skill 汇总
  %(prog)s --eval-dir /path/to/evaluation-results --output-dir ./reports

  # 生成单个 skill 汇总
  %(prog)s --eval-dir /path/to/evaluation-results --output-dir ./reports \\
      --skill huawei-cloud-swr-image-automation

  # 更新仓库并生成报告
  %(prog)s --eval-dir /path/to/evaluation-results --output-dir ./reports \\
      --repo-dir /path/to/developer_ljf

  # 仅生成报告不获取 Issue
  %(prog)s --eval-dir /path/to/evaluation-results --output-dir ./reports --no-issues
        """,
    )
    parser.add_argument("--eval-dir", required=True, help="evaluation-results 目录路径")
    parser.add_argument("--output-dir", required=True, help="报告输出目录")
    parser.add_argument("--skill", help="只生成指定 skill 的汇总报告")
    parser.add_argument("--repo-dir", help="仓库目录（用于 git pull 更新）")
    parser.add_argument("--no-issues", action="store_true", help="不获取 GitCode Issue")
    parser.add_argument("--no-overview", action="store_true", help="不生成总览报告")
    parser.add_argument("--issue-owner", help="Issue 仓库 owner（获取 Issue 时必填）")
    parser.add_argument("--issue-repo", help="Issue 仓库名（获取 Issue 时必填）")
    parser.add_argument("--issue-token", help="GitCode API token")
    parser.add_argument("--update", action="store_true", help="增量更新模式（只更新有变化的 skill）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式结果摘要")

    args = parser.parse_args()

    eval_dir = Path(args.eval_dir)
    output_dir = Path(args.output_dir)

    if not eval_dir.exists():
        print(f"❌ 评测目录不存在: {eval_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. 更新仓库
    if args.repo_dir:
        update_repo(Path(args.repo_dir))

    # 2. 发现所有 skill 并解析
    print(f"🔍 扫描评测目录: {eval_dir}")
    all_eval_data = parse_all_skills(eval_dir)
    skill_names = list(all_eval_data.keys())
    print(f"   发现 {len(skill_names)} 个 skill: {', '.join(sorted(skill_names))}")

    if args.skill:
        if args.skill not in skill_names:
            print(f"⚠️  未找到 skill: {args.skill}", file=sys.stderr)
            sys.exit(1)
        skill_names = [args.skill]

    # 3. 获取 Issue
    all_issues = []
    if not args.no_issues:
        if not args.issue_owner or not args.issue_repo:
            print(
                "❌ 获取 Issue 需要提供 --issue-owner 和 --issue-repo，"
                "或使用 --no-issues 跳过 Issue 获取",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"📋 获取 GitCode Issue...")
        raw_issues = fetch_issues(
            owner=args.issue_owner,
            repo=args.issue_repo,
            token=args.issue_token,
            repo_dir=args.repo_dir,
        )
        all_issues = [normalize_issue(i) for i in raw_issues]
        print(f"   获取到 {len(all_issues)} 个 Issue")

    # 4. 生成报告
    generated_reports = []

    if args.skill:
        # 单个 skill 模式
        skill_name = args.skill
        eval_data = all_eval_data[skill_name]
        skill_issues = []
        if all_issues:
            from fetch_issues import filter_issues_by_skill
            skill_issues = filter_issues_by_skill(all_issues, skill_name)

        report_path = generate_skill_summary(
            skill_name, eval_data, skill_issues, output_dir
        )
        generated_reports.append(str(report_path))
        print(f"✅ 生成报告: {report_path}")
    else:
        # 全量模式
        issue_groups = group_issues_by_skill(all_issues) if all_issues else {}

        for skill_name in sorted(skill_names):
            eval_data = all_eval_data[skill_name]
            skill_issues = issue_groups.get(skill_name, [])

            report_path = generate_skill_summary(
                skill_name, eval_data, skill_issues, output_dir
            )
            generated_reports.append(str(report_path))
            print(f"✅ 生成报告: {report_path}")

        # 生成总览
        if not args.no_overview:
            overview_path = generate_overview_summary(
                all_eval_data, all_issues, output_dir
            )
            generated_reports.append(str(overview_path))
            print(f"✅ 生成总览: {overview_path}")

    # 5. 输出摘要
    print(f"\n{'='*50}")
    print(f"  生成完成！共 {len(generated_reports)} 个报告")
    print(f"  输出目录: {output_dir}")
    print(f"{'='*50}")

    if args.json:
        summary = {
            "generated_at": datetime.now().isoformat(),
            "output_dir": str(output_dir),
            "reports": generated_reports,
            "skill_count": len(skill_names),
            "issue_count": len(all_issues),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
