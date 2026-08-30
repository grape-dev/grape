#!/usr/bin/env python3
"""
解析 evaluation-results 目录，提取每个 skill 的 case-run 和 tester 测试统计。

输出数据结构:
{
  "skill_name": {
    "case_run": [
      {
        "report_date": "2026-07-30",
        "total": 6,
        "pass": 1,
        "warning": 2,
        "fail": 3,
        "skip": 1,
        "pass_rate": 50.0
      },
      ...
    ],
    "tester": [
      {
        "report_date": "2026-07-22",
        "total": 36,
        "pass": 36,
        "warning": 0,
        "fail": 0,
        "skip": 0,
        "pass_rate": 100.0
      },
      ...
    ],
    "has_issue_report": true,
    "issue_reports": ["Issue-120-分析与实测报告.md"]
  }
}

case_run / tester 均为列表，按 report_date 倒序排列（最新在前）。
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime


def _empty_record() -> dict:
    """返回空的测试统计记录。"""
    return {
        "report_date": None,
        "date_dir_name": None,
        "report_file": None,
        "total": 0,
        "pass": 0,
        "warning": 0,
        "fail": 0,
        "skip": 0,
        "pass_rate": 0.0,
    }


def _parse_case_run_dir(date_dir: Path) -> dict:
    """解析单个日期目录下的 case-run 整合测试报告。

    策略:
    1. 优先解析 整合测试报告.md 中的用例结果表格
    2. 回退: 按目录名后缀统计（通过/告警/失败/跳过）
    """
    result = _empty_record()
    dir_date = f"{date_dir.name[:4]}-{date_dir.name[4:6]}-{date_dir.name[6:8]}"
    result["date_dir_name"] = date_dir.name

    # 尝试解析整合测试报告.md
    report_file = date_dir / "整合测试报告.md"
    if report_file.exists():
        content = report_file.read_text(encoding="utf-8")
        result["report_file"] = "整合测试报告.md"

        # 提取报告日期（支持多种格式）
        for date_pattern in [
            r"整合时间[:\s]*(\d{4}-\d{2}-\d{2})",
            r"测试日期[:\s]*(\d{4}-\d{2}-\d{2})",
            r"评测日期[:\s]*(\d{4}-\d{2}-\d{2})",
        ]:
            date_match = re.search(date_pattern, content)
            if date_match:
                result["report_date"] = date_match.group(1)
                break
        if not result["report_date"]:
            result["report_date"] = dir_date

        # 策略 A: 解析"实测结果概览"表格（格式: | 类型 | 总数 | 通过 | 告警 | 失败 |）
        # 找合计行
        summary_match = re.search(
            r"\|\s*合计\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
            content,
        )
        if summary_match:
            result["total"] = int(summary_match.group(1))
            result["pass"] = int(summary_match.group(2))
            result["warning"] = int(summary_match.group(3))
            result["fail"] = int(summary_match.group(4))
            if result["total"] > 0:
                result["pass_rate"] = round(
                    (result["pass"] + result["warning"]) / result["total"] * 100, 1
                )
            return result

        # 策略 B: 解析用例明细表格（格式: | 顺序 | 用例 | 类型 | 结果 | ... |）
        table_rows = re.findall(
            r"\|\s*\d+\s*\|[^|]+\|[^|]+\|\s*(通过|告警|失败|跳过|warn|pass|fail|skip)\s*\|",
            content,
        )
        if table_rows:
            for row in table_rows:
                r = row.strip().lower()
                if r in ("通过", "pass"):
                    result["pass"] += 1
                elif r in ("告警", "warn"):
                    result["warning"] += 1
                elif r in ("失败", "fail"):
                    result["fail"] += 1
                elif r in ("跳过", "skip"):
                    result["skip"] += 1
            result["total"] = result["pass"] + result["warning"] + result["fail"]
            if result["total"] > 0:
                result["pass_rate"] = round(
                    (result["pass"] + result["warning"]) / result["total"] * 100, 1
                )
            return result

    # 回退: 按目录名后缀统计
    result["report_date"] = dir_date

    for case_dir in date_dir.iterdir():
        if not case_dir.is_dir():
            continue
        # 跳过非用例目录
        name = case_dir.name
        if name.startswith("issue_") or name in ("phase-summaries", "reports"):
            continue

        # 目录名格式: {prefix}_{description}_{result}
        # 结果在最后一个 _ 之后
        parts = name.rsplit("_", 1)
        if len(parts) == 2:
            status = parts[1]
            if status == "通过":
                result["pass"] += 1
            elif status == "告警":
                result["warning"] += 1
            elif status == "失败":
                result["fail"] += 1
            elif status == "跳过":
                result["skip"] += 1

    result["total"] = result["pass"] + result["warning"] + result["fail"]
    if result["total"] > 0:
        result["pass_rate"] = round(
            (result["pass"] + result["warning"]) / result["total"] * 100, 1
        )

    return result


def parse_case_run(skill_dir: Path) -> list:
    """解析 case-run 测试统计，返回全部历史记录列表。

    Returns:
        list[dict]: 按报告日期倒序排列（最新在前），每条记录包含
                    report_date / total / pass / warning / fail / skip / pass_rate。
    """
    case_run_dir = skill_dir / "case-run"
    if not case_run_dir.exists():
        return []

    # 全部日期目录，按日期倒序
    date_dirs = sorted(
        [d for d in case_run_dir.iterdir() if d.is_dir() and d.name.isdigit()],
        reverse=True,
    )
    if not date_dirs:
        return []

    records = []
    for date_dir in date_dirs:
        rec = _parse_case_run_dir(date_dir)
        if rec["total"] > 0 or rec["report_date"]:
            records.append(rec)
    return records


def _parse_tester_dir(date_dir: Path) -> dict:
    """解析单个日期目录下的 tester 测试报告。

    策略:
    1. 优先解析 test-report.json 的 overall_statistics
    2. 回退: 解析 test-report.md 的统计表格
    """
    result = _empty_record()
    dir_date = f"{date_dir.name[:4]}-{date_dir.name[4:6]}-{date_dir.name[6:8]}"
    result["date_dir_name"] = date_dir.name

    # 检查 test-report.md 位置（用于生成报告链接）
    for md_path in [date_dir / "reports" / "test-report.md", date_dir / "test-report.md"]:
        if md_path.exists():
            result["report_file"] = str(md_path.relative_to(date_dir))
            break

    # 尝试解析 test-report.json
    json_paths = [
        date_dir / "reports" / "test-report.json",
        date_dir / "test-report.json",
    ]
    for json_path in json_paths:
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                stats = data.get("overall_statistics", {})

                result["total"] = stats.get("test_cases_total", 0)
                result["pass"] = stats.get("test_cases_pass", 0)
                result["fail"] = stats.get("test_cases_fail", 0)
                result["skip"] = stats.get("test_cases_skip", 0)
                result["warning"] = 0  # tester 无告警分类
                result["pass_rate"] = stats.get("pass_rate", 0.0)

                # 提取日期
                test_id = data.get("test_id", "")
                id_date_match = re.match(r"test-(\d{8})-", test_id)
                if id_date_match:
                    d = id_date_match.group(1)
                    result["report_date"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                else:
                    result["report_date"] = dir_date

                return result
            except (json.JSONDecodeError, KeyError):
                pass

    # 回退: 解析 test-report.md
    md_paths = [
        date_dir / "reports" / "test-report.md",
        date_dir / "test-report.md",
    ]
    for md_path in md_paths:
        if md_path.exists():
            content = md_path.read_text(encoding="utf-8")
            result["report_date"] = dir_date

            # 解析统计表格
            total_match = re.search(r"测试用例总数\s*\|\s*(\d+)", content)
            pass_match = re.search(r"用例通过\s*\|\s*(\d+)", content)
            fail_match = re.search(r"用例失败\s*\|\s*(\d+)", content)
            skip_match = re.search(r"用例跳过\s*\|\s*(\d+)", content)
            rate_match = re.search(r"通过率\s*\|\s*([\d.]+)%", content)

            if total_match:
                result["total"] = int(total_match.group(1))
            if pass_match:
                result["pass"] = int(pass_match.group(1))
            if fail_match:
                result["fail"] = int(fail_match.group(1))
            if skip_match:
                result["skip"] = int(skip_match.group(1))
            if rate_match:
                result["pass_rate"] = float(rate_match.group(1))
            else:
                if result["total"] > 0:
                    result["pass_rate"] = round(
                        (result["pass"] + result["warning"]) / result["total"] * 100, 1
                    )
            return result

    result["report_date"] = dir_date
    return result


def parse_tester(skill_dir: Path) -> list:
    """解析 tester 测试统计，返回全部历史记录列表。

    Returns:
        list[dict]: 按报告日期倒序排列（最新在前）。
    """
    tester_dir = skill_dir / "tester"
    if not tester_dir.exists():
        return []

    # 全部日期目录，按日期倒序
    date_dirs = sorted(
        [d for d in tester_dir.iterdir() if d.is_dir() and d.name.isdigit()],
        reverse=True,
    )
    if not date_dirs:
        return []

    records = []
    for date_dir in date_dirs:
        rec = _parse_tester_dir(date_dir)
        if rec["total"] > 0 or rec["report_date"]:
            records.append(rec)
    return records


def find_issue_reports(skill_dir: Path) -> list:
    """查找 问题单/ 目录下的分析报告文件。"""
    issue_dir = skill_dir / "问题单"
    if not issue_dir.exists():
        return []

    reports = []
    for f in sorted(issue_dir.iterdir()):
        if f.is_file() and f.suffix == ".md" and f.name.startswith("Issue-"):
            reports.append(f.name)
    return reports


def parse_all_skills(eval_results_dir: Path) -> dict:
    """解析 evaluation-results 目录下所有 skill 的测试统计。

    Args:
        eval_results_dir: evaluation-results 目录路径

    Returns:
        dict: {skill_name: {case_run: [...], tester: [...], issue_reports: [...]}}
    """
    results = {}

    if not eval_results_dir.exists():
        print(f"⚠️  evaluation-results 目录不存在: {eval_results_dir}", file=sys.stderr)
        return results

    for skill_dir in sorted(eval_results_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith("."):
            continue

        skill_name = skill_dir.name
        results[skill_name] = {
            "case_run": parse_case_run(skill_dir),
            "tester": parse_tester(skill_dir),
            "issue_reports": find_issue_reports(skill_dir),
        }

    return results


def main():
    """命令行入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="解析 evaluation-results 目录")
    parser.add_argument("eval_results_dir", help="evaluation-results 目录路径")
    parser.add_argument("--skill", help="仅解析指定 skill")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    eval_dir = Path(args.eval_results_dir)
    results = parse_all_skills(eval_dir)

    if args.skill:
        results = {k: v for k, v in results.items() if k == args.skill}

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for skill_name, data in results.items():
            print(f"\n{'='*60}")
            print(f"  Skill: {skill_name}")
            print(f"{'='*60}")

            print(f"\n  case-run ({len(data['case_run'])} 次测试):")
            for ai in data["case_run"]:
                print(f"    [{ai['report_date']}] 总用例: {ai['total']}  "
                      f"通过: {ai['pass']}  告警: {ai['warning']}  "
                      f"失败: {ai['fail']}  跳过: {ai['skip']}  "
                      f"通过率: {ai['pass_rate']}%")

            print(f"\n  tester ({len(data['tester'])} 次测试):")
            for te in data["tester"]:
                print(f"    [{te['report_date']}] 总用例: {te['total']}  "
                      f"通过: {te['pass']}  告警: {te['warning']}  "
                      f"失败: {te['fail']}  跳过: {te['skip']}  "
                      f"通过率: {te['pass_rate']}%")

            if data["issue_reports"]:
                print(f"\n  问题单分析报告: {', '.join(data['issue_reports'])}")
            else:
                print(f"\n  问题单分析报告: 无")


if __name__ == "__main__":
    main()
