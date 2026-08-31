#!/usr/bin/env python3
import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional

import pymysql

from .config import DB_CONFIG
from .log_parser import RawMetrics
from .scorer import EvalResult


@dataclass
class SecurityIssue:
    rule_id: str
    severity: str
    category: str
    file_path: str
    line_number: Optional[int]
    snippet: str
    fix_strategy: str


def _get_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset=DB_CONFIG.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_skill_id(skill_name, category=None, sub_category=None):
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT skill_id FROM hd_eval_skill WHERE skill_name = %s",
                (skill_name,),
            )
            row = cur.fetchone()
            if row:
                return row["skill_id"]

            cur.execute("SELECT MAX(id) AS max_id FROM hd_eval_skill")
            max_row = cur.fetchone()
            next_id = (max_row["max_id"] or 0) + 1
            skill_id = f"skill_{next_id:03d}"

            try:
                cur.execute(
                    "INSERT INTO hd_eval_skill (skill_id, skill_name, category, sub_category) "
                    "VALUES (%s, %s, %s, %s)",
                    (skill_id, skill_name, category, sub_category),
                )
                conn.commit()
            except pymysql.err.IntegrityError:
                conn.rollback()
                cur.execute(
                    "SELECT skill_id FROM hd_eval_skill WHERE skill_name = %s",
                    (skill_name,),
                )
                row = cur.fetchone()
                if row:
                    return row["skill_id"]
            return skill_id
    finally:
        conn.close()


def get_baseline(skill_id):
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM hd_eval_report WHERE skill_id = %s "
                "ORDER BY eval_date DESC LIMIT 1",
                (skill_id,),
            )
            report = cur.fetchone()
            if not report:
                return None

            report_id = report["id"]
            cur.execute(
                "SELECT metric_name, metric_value FROM hd_eval_sub_metric "
                "WHERE report_id = %s",
                (report_id,),
            )
            metrics = {r["metric_name"]: r["metric_value"] for r in cur.fetchall()}

        name_map = {
            "完成耗时": ("duration_seconds", lambda v: float(v.rstrip("s").strip())),
            "工具失败次数": ("tool_fail_count", int),
            "重试次数": ("retry_count", int),
            "CLI调用次数": ("cli_call_count", int),
            "SDK调用次数": ("sdk_call_count", int),
            "API调用次数": ("api_call_count", int),
            "LLM调用次数": ("llm_call_count", int),
            "上下文是否压缩": (
                "context_compressed",
                lambda v: 1 if v.lower() in ("yes", "true", "1") else 0,
            ),
        }

        baseline = {"report_id": report_id}
        for metric_name, (key, converter) in name_map.items():
            if metric_name in metrics:
                baseline[key] = converter(metrics[metric_name])

        baseline["total_tokens"] = float(report.get("token_cost", 0) or 0)
        return baseline
    finally:
        conn.close()


def write_all(
    skill_name,
    skill_id,
    agent_name,
    session_id,
    metrics,
    eval_result,
    category=None,
    sub_category=None,
):
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hd_eval_report "
                "(agent_name, total_score, stability_score, cost_score, "
                "efficiency_score, security_score, eval_date, creator_id, "
                "skill_id, trace_id, token_cost) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    agent_name,
                    eval_result.total_score,
                    eval_result.stability_score,
                    eval_result.cost_score,
                    eval_result.efficiency_score,
                    eval_result.security_score,
                    datetime.date.today(),
                    1,
                    skill_id,
                    session_id,
                    metrics.total_tokens,
                ),
            )
            report_id = cur.lastrowid

            for item in eval_result.item_scores:
                cur.execute(
                    "INSERT INTO hd_eval_detail "
                    "(report_id, category, item_name, max_score, actual_score) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (report_id, item.category, item.item_name, item.max_score, item.actual_score),
                )

            sub_metrics: List[tuple] = [
                ("stability", "功能可用", "正常可用" if not metrics.has_bug else "存在BUG"),
                ("stability", "触发词充足度", str(metrics.prompt_match_count)),
                ("cost", "总消耗Token", str(metrics.total_tokens)),
                ("efficiency", "完成耗时", f"{metrics.duration_seconds}s"),
                ("efficiency", "工具失败次数", str(metrics.tool_fail_count)),
                ("efficiency", "重试次数", str(metrics.retry_count)),
                ("efficiency", "CLI调用次数", str(metrics.cli_call_count)),
                ("efficiency", "SDK调用次数", str(metrics.sdk_call_count)),
                ("efficiency", "API调用次数", str(metrics.api_call_count)),
                ("efficiency", "LLM调用次数", str(metrics.llm_call_count)),
                ("efficiency", "上下文是否压缩", "yes" if metrics.context_compressed else "no"),
                ("security", "安全等级", metrics.security_risk_level),
            ]
            for dimension, metric_name, metric_value in sub_metrics:
                cur.execute(
                    "INSERT INTO hd_eval_sub_metric "
                    "(report_id, dimension, metric_name, metric_value) "
                    "VALUES (%s, %s, %s, %s)",
                    (report_id, dimension, metric_name, metric_value),
                )

            for item in metrics.call_chain:
                cur.execute(
                    "INSERT INTO hd_eval_chain "
                    "(report_id, chain_type, description, status, exec_time, sort_order, timestamp) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (report_id, item.chain_type, item.description, item.status, item.exec_time, item.sort_order, getattr(item, "timestamp", "")),
                )

            conn.commit()
            return report_id
    finally:
        conn.close()


def write_security_issues(skill_id, skill_name, report_id, risk_level, scan_level, issues):
    if not issues:
        return 0
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            today = datetime.date.today()
            for issue in issues:
                cur.execute(
                    "INSERT INTO hd_security_issue "
                    "(skill_id, skill_name, report_id, risk_level, scan_level, rule_id, severity, category, "
                    "file_path, line_number, snippet, fix_strategy, scan_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        skill_id, skill_name, report_id, risk_level, scan_level,
                        issue.rule_id, issue.severity, issue.category,
                        issue.file_path, issue.line_number, issue.snippet,
                        issue.fix_strategy, today,
                    ),
                )
            conn.commit()
            return len(issues)
    finally:
        conn.close()
