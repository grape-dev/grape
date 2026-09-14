#!/usr/bin/env python3
"""任务分析报告脚本（Issue #134 / #133 Spec）。

一键产出最近一周任务分析报告：任务量、成功率、失败分布、失败原因归类、
per-machine 健康度排名。

用法：
    python3 scripts/analyze_task_report.py [--days 7] [--output report.md]

输出：Markdown 格式报告（默认打印到 stdout，--output 写文件）。
"""
import argparse
import os
import sys
import json
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

# 确保能 import src
_sys_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _sys_dir not in sys.path:
    sys.path.insert(0, _sys_dir)


def _query_task_stats(days: int = 7):
    """查询近 N 天的任务统计数据。"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone, timedelta

    window = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    stats = {
        "total": 0,
        "by_status": Counter(),
        "by_agent_type": Counter(),
        "by_runtime": defaultdict(lambda: {"total": 0, "failed": 0, "completed": 0}),
        "failure_errors": Counter(),
        "daily": defaultdict(lambda: {"total": 0, "failed": 0, "completed": 0}),
    }

    try:
        with db_lock:
            c = db.cursor()
            # 总量 + 状态分布
            c.execute(
                "SELECT status, agent_type, runtime_id, errors, created_at "
                "FROM agent_tasks WHERE created_at >= ? "
                "ORDER BY created_at ASC",
                (window,))
            rows = c.fetchall()
    except Exception as e:
        print(f"查询失败: {e}", file=sys.stderr)
        return stats

    for status, agent_type, runtime_id, errors, created_at in rows:
        stats["total"] += 1
        stats["by_status"][status] += 1
        stats["by_agent_type"][agent_type or "unknown"] += 1

        # per-runtime 统计
        rt_key = runtime_id or 0
        stats["by_runtime"][rt_key]["total"] += 1
        if status == "failed":
            stats["by_runtime"][rt_key]["failed"] += 1
        elif status == "completed":
            stats["by_runtime"][rt_key]["completed"] += 1

        # 失败原因归类
        if status == "failed" and errors:
            err_str = str(errors)[:200]
            # 简单归类：取第一行或前 100 字符
            first_line = err_str.split("\n")[0][:100]
            stats["failure_errors"][first_line] += 1

        # 按天统计
        try:
            day = (created_at or "")[:10]  # YYYY-MM-DD
            if day:
                stats["daily"][day]["total"] += 1
                if status == "failed":
                    stats["daily"][day]["failed"] += 1
                elif status == "completed":
                    stats["daily"][day]["completed"] += 1
        except Exception:
            pass

    return stats


def _format_report(stats: dict, days: int) -> str:
    """格式化为 Markdown 报告。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total = stats["total"]
    completed = stats["by_status"].get("completed", 0)
    failed = stats["by_status"].get("failed", 0)
    success_rate = round(completed / total * 100, 1) if total > 0 else 0
    fail_rate = round(failed / total * 100, 1) if total > 0 else 0

    lines = [
        f"# 任务分析报告（近 {days} 天）",
        f"",
        f"> 生成时间：{now}",
        f"",
        f"## 总览",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 总任务数 | {total} |",
        f"| 完成 | {completed} ({success_rate}%) |",
        f"| 失败 | {failed} ({fail_rate}%) |",
        f"| 其他 | {total - completed - failed} |",
        f"",
        f"## 按状态分布",
        f"",
        f"| 状态 | 数量 | 占比 |",
        f"|------|------|------|",
    ]
    for status, count in stats["by_status"].most_common():
        pct = round(count / total * 100, 1) if total > 0 else 0
        lines.append(f"| {status} | {count} | {pct}% |")

    lines.extend([
        f"",
        f"## 按 Agent 类型分布",
        f"",
        f"| Agent 类型 | 数量 | 占比 |",
        f"|-----------|------|------|",
    ])
    for atype, count in stats["by_agent_type"].most_common():
        pct = round(count / total * 100, 1) if total > 0 else 0
        lines.append(f"| {atype} | {count} | {pct}% |")

    lines.extend([
        f"",
        f"## 每日趋势",
        f"",
        f"| 日期 | 总数 | 完成 | 失败 | 成功率 |",
        f"|------|------|------|------|--------|",
    ])
    for day in sorted(stats["daily"].keys()):
        d = stats["daily"][day]
        sr = round(d["completed"] / d["total"] * 100, 1) if d["total"] > 0 else 0
        lines.append(f"| {day} | {d['total']} | {d['completed']} | {d['failed']} | {sr}% |")

    lines.extend([
        f"",
        f"## Per-Machine 健康度排名",
        f"",
        f"| Runtime ID | 总任务 | 完成 | 失败 | 失败率 |",
        f"|-----------|--------|------|------|--------|",
    ])
    # 按失败率降序排列
    rt_sorted = sorted(stats["by_runtime"].items(),
                       key=lambda x: x[1]["failed"] / max(x[1]["total"], 1),
                       reverse=True)
    for rid, d in rt_sorted:
        fr = round(d["failed"] / d["total"] * 100, 1) if d["total"] > 0 else 0
        lines.append(f"| {rid} | {d['total']} | {d['completed']} | {d['failed']} | {fr}% |")

    lines.extend([
        f"",
        f"## 失败原因 TOP 10",
        f"",
        f"| 失败原因 | 次数 |",
        f"|---------|------|",
    ])
    for err, count in stats["failure_errors"].most_common(10):
        lines.append(f"| {err} | {count} |")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="任务分析报告（近 N 天）")
    parser.add_argument("--days", type=int, default=7, help="分析窗口天数（默认 7）")
    parser.add_argument("--output", type=str, default="", help="输出文件路径（默认 stdout）")
    args = parser.parse_args()

    stats = _query_task_stats(days=args.days)
    report = _format_report(stats, days=args.days)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"报告已写入: {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
