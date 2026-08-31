#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval.config import SESSION_ROOT, SECURITY_SKILL_PATH, DB_CONFIG
from eval.log_parser import parse as parse_logs
from eval.scorer import score
from eval.db_writer import get_skill_id, get_baseline, write_all, write_security_issues, SecurityIssue
from eval.flow_status import get_multica_issue_id, update_verify_status, build_verify_url
from eval.gitcode_merger import parse_pr_url
from eval.pr_orchestrator import run_pr_orchestration
from eval.description_retrieval import (
    build_skill_index, retrieval_hit_count, extract_triggers_from_content,
    load_default_repo, repo_cache_path,
)


def _find_audit_script():
    candidates = [
        os.getenv("SKILL_AUDIT_SCRIPT", ""),
        os.path.join(os.path.expanduser("~/.agents/skills/huawei-cloud-skill-audit"),
                     "huawei-cloud-skill-audit", "scripts", "skill_audit.py"),
        os.path.join(os.path.expanduser("~/.agents/skills/huawei-cloud-skill-audit"),
                     "scripts", "skill_audit.py"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "huawei-cloud-skill-audit",
                     "huawei-cloud-skill-audit", "scripts", "skill_audit.py"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "huawei-cloud-skill-audit",
                     "scripts", "skill_audit.py"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return ""


AUDIT_SCRIPT = _find_audit_script()


def _parse_audit_report(output):
    issues = []
    fix_strategies = {}
    fix_pattern = re.compile(r"^\s*\[(CRITICAL|ERROR|WARNING)\]\s+(.+?)$", re.MULTILINE)
    for m in fix_pattern.finditer(output):
        sev = m.group(1)
        cat = m.group(2).strip()
        strategy_match = re.search(
            rf"^\s*\[{re.escape(sev)}\]\s+{re.escape(cat)}\s*\n\s*Strategy:\s*(.+)$",
            output, re.MULTILINE,
        )
        if strategy_match:
            fix_strategies[(sev, cat)] = strategy_match.group(1).strip()

    detail_pattern = re.compile(
        r"\[(CRITICAL|ERROR|WARNING)\]\s*—\s*(.+?)\n"
        r"\s+(\S+)\s+L(\d+)\s+(\S+)\n"
        r"\s+Snippet:\s*(.+?)\n"
        r"\s+(.+)",
        re.MULTILINE,
    )
    for m in detail_pattern.finditer(output):
        severity = m.group(1)
        category = m.group(2).strip()
        file_path = m.group(3).strip()
        line_number = int(m.group(4))
        rule_id = m.group(5).strip()
        snippet = m.group(6).strip()
        fix = fix_strategies.get((severity, category), "")
        issues.append(SecurityIssue(
            rule_id=rule_id, severity=severity, category=category,
            file_path=file_path, line_number=line_number,
            snippet=snippet, fix_strategy=fix,
        ))
    return issues


def evaluate_security(skill_path, scan_level="high"):
    if not os.path.exists(AUDIT_SCRIPT):
        print(f"    Warning: Audit script not found at {AUDIT_SCRIPT}, defaulting to medium risk")
        return "medium", []
    try:
        result = subprocess.run(
            ["python3", AUDIT_SCRIPT, "--target", skill_path, "--scan-level", scan_level, "--no-install"],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        print(f"    Audit output:\n{output}")
        issues = _parse_audit_report(output)
        print(f"    Parsed {len(issues)} issue(s) from report")
        if "Gate Verdict: PASS" in output:
            risk_level = "high"
        elif "CRITICAL" in output:
            risk_level = "low"
        elif "ERROR" in output:
            risk_level = "low"
        elif "WARNING" in output:
            risk_level = "medium"
        else:
            risk_level = "high"
        return risk_level, issues
    except Exception as e:
        print(f"    Warning: Security evaluation failed: {e}, defaulting to medium risk")
        return "medium", []


def main():
    parser = argparse.ArgumentParser(description="Evaluate a skill run and persist to DB")
    parser.add_argument("session_id", help="Session ID to evaluate")
    parser.add_argument("--agent", default="opencode", help="Agent name")
    parser.add_argument("--skill-name", required=True, help="Skill name for DB lookup")
    parser.add_argument("--skill-path", default=None, help="Skill directory path (for security eval)")
    parser.add_argument("--session-root", default=None, help="Override session storage root")
    parser.add_argument("--db-host", default=None)
    parser.add_argument("--db-user", default=None)
    parser.add_argument("--db-password", default=None)
    parser.add_argument("--db-name", default=None)
    parser.add_argument("--scan-level", default="high",
                        choices=["critical", "high", "quick", "standard", "deep"],
                        help="Security audit scan level (default: high)")
    parser.add_argument("--pr-url", default=None, help="GitCode PR URL (unused for node_verify_url; node_verify_url always uses multica issue URL)")
    parser.add_argument("--dry-run", action="store_true", help="Score without writing to DB")
    parser.add_argument("--keep-session", action="store_true", default=True,
                        help="Keep session directory after cleanup (default: True)")
    parser.add_argument("--no-keep-session", dest="keep_session", action="store_false",
                        help="Delete session directory after cleanup")
    args = parser.parse_args()

    if args.db_host: DB_CONFIG["host"] = args.db_host
    if args.db_user: DB_CONFIG["user"] = args.db_user
    if args.db_password: DB_CONFIG["password"] = args.db_password
    if args.db_name: DB_CONFIG["database"] = args.db_name

    missing = [k for k, v in DB_CONFIG.items() if k != "charset" and not v]
    if missing and not args.dry_run:
        print(f"Error: Missing DB config: {', '.join(missing)}")
        print(f"Set env vars: EVAL_DB_HOST, EVAL_DB_USER, EVAL_DB_PASSWORD, EVAL_DB_NAME")
        sys.exit(1)

    session_root = args.session_root or SESSION_ROOT
    session_dir = os.path.join(session_root, args.session_id)

    if not os.path.isdir(session_dir):
        print(f"Error: Session directory not found: {session_dir}")
        sys.exit(1)

    skill_path = args.skill_path
    if not skill_path:
        meta_path = os.path.join(session_dir, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            skill_path = meta.get("skill_path", "")

    # --- 验证节点状态: processing 已由 run_skill.sh 在 skill 运行前写入 ---
    # （此处不再重复写 processing，避免刷新 node_verify_time）
    issue_id = None
    flow_url = None
    eval_failed = False
    report_id = None
    report_url = ""
    eval_result = None
    metrics = None
    try:
        issue_id = get_multica_issue_id(skill_path) if skill_path else None
        flow_url = build_verify_url(issue_id, args.pr_url) if issue_id else None
        if issue_id:
            print(f"    Flow issue: {issue_id}")
        else:
            print("    Warning: multica_issue_id not found, skipping flow status update")
    except Exception as e:
        print(f"    Warning: flow status update failed: {e}")

    try:
        print(f"==> Parsing session logs: {args.session_id}")
        metrics = parse_logs(session_dir, args.agent)

        security_issues = []
        if skill_path and os.path.isdir(skill_path):
            print(f"==> Evaluating security via skill_audit.py (scan-level={args.scan_level})...")
            risk_level, security_issues = evaluate_security(skill_path, scan_level=args.scan_level)
            metrics.security_risk_level = risk_level
            print(f"    Security risk level: {risk_level}")
            print(f"    Issues found: {len(security_issues)}")

        print(f"==> Looking up skill in DB: {args.skill_name}")
        skill_id = get_skill_id(args.skill_name)

        print(f"==> Checking for baseline...")
        baseline = get_baseline(skill_id)
        if baseline:
            print(f"    Found baseline report_id={baseline['report_id']}")
        else:
            print(f"    No baseline found (first run) — will give full scores")

        # --- 触发词充足度（替换原提示词匹配） ---
        try:
            repo_root = None
            if args.pr_url:
                _pr = parse_pr_url(args.pr_url)
                if _pr:
                    repo_root = f"/tmp/fetch-pr-skill/{_pr['owner']}_{_pr['repo']}"
            if not repo_root:
                _default = load_default_repo()
                if _default:
                    repo_root = repo_cache_path(_default)
            if repo_root:
                _index = build_skill_index(repo_root)
                _content = _index.get(args.skill_name, "")
                triggers = extract_triggers_from_content(_content)
                metrics.prompt_match_count = retrieval_hit_count(triggers, args.skill_name, _index)
                print(f"    Retrieval hits: {metrics.prompt_match_count}/{len(triggers)}")
            else:
                metrics.prompt_match_count = 0
                print("    Warning: no repo_root (PR URL missing and no DEFAULT_REPO configured), retrieval = 0")
        except Exception as e:
            print(f"    Warning: retrieval evaluation failed: {e}")
            metrics.prompt_match_count = 0

        print(f"==> Scoring...")
        eval_result = score(metrics, baseline)

        print(f"")
        print(f"=== Evaluation Result ===")
        print(f"  Total:       {eval_result.total_score}")
        print(f"  Stability:   {eval_result.stability_score}")
        print(f"  Cost:        {eval_result.cost_score}")
        print(f"  Efficiency:  {eval_result.efficiency_score}")
        print(f"  Security:    {eval_result.security_score}")
        print(f"  First run:   {eval_result.is_first_run}")
        print(f"")
        for item in eval_result.item_scores:
            print(f"  {item.category}/{item.item_name}: {item.actual_score}/{item.max_score}")

        if args.dry_run:
            print(f"\n[dry-run] Skipping hd_eval_* DB writes")
            if issue_id:
                try:
                    if update_verify_status(issue_id, "done", flow_url):
                        print(f"    Verify status -> done (issue {issue_id})")
                except Exception as e:
                    print(f"    Warning: flow status update failed: {e}")
            print("\n==> All done!")
            return

        print(f"\n==> Writing to DB...")
        report_id = write_all(
            skill_name=args.skill_name,
            skill_id=skill_id,
            agent_name=args.agent,
            session_id=args.session_id,
            metrics=metrics,
            eval_result=eval_result,
        )
        print(f"    Report ID: {report_id}")

        # --- 生成评测报告并上传 OBS ---
        try:
            from eval.report_uploader import (
                load_obs_config, build_report_content, upload_report,
                build_download_url, update_report_url,
            )
            obs_config = load_obs_config()
            if obs_config:
                object_key = f"report/{args.skill_name}_{datetime.date.today().strftime('%Y%m%d')}_{report_id}.md"
                content = build_report_content(
                    args.skill_name, args.pr_url, eval_result, metrics,
                    metrics.security_risk_level, report_id, args.session_id,
                )
                if upload_report(obs_config, object_key, content):
                    url = build_download_url(obs_config, object_key)
                    report_url = url
                    if update_report_url(report_id, url):
                        print(f"    Report URL saved: {url}")
                    else:
                        print("    Warning: report_url update failed (non-blocking)")
                else:
                    print("    Warning: OBS upload failed (non-blocking)")
            else:
                print("    Warning: OBS config not found, skipping report upload")
        except Exception as e:
            print(f"    Warning: report upload failed: {e}")

        print(f"==> Evaluation complete!")

        if security_issues:
            print(f"\n==> Writing {len(security_issues)} security issue(s) to DB...")
            count = write_security_issues(
                skill_id=skill_id,
                skill_name=args.skill_name,
                report_id=report_id,
                risk_level=metrics.security_risk_level,
                scan_level=args.scan_level,
                issues=security_issues,
            )
            print(f"    Written: {count} issue(s)")

        # done 在 cleanup 之前设置（cleanup 失败不覆盖评测结论）
        if issue_id:
            if update_verify_status(issue_id, "done", flow_url):
                print(f"    Verify status -> done (issue {issue_id})")

        # --- Resource Cleanup ---
        try:
            print(f"\n==> Cleaning up resources...")
            from eval.resource_cleaner import cleanup_session, send_failure_notification

            skill_output_dir = skill_path if skill_path and os.path.isdir(skill_path) else None
            cleanup_result, failed_resources = cleanup_session(
                session_dir, skill_output_dir, keep_session=args.keep_session
            )

            print(f"    Cloud resources released: {cleanup_result.resources_released}/{len(cleanup_result.resources_found)}")
            print(f"    Session cleaned: {cleanup_result.local_cleaned}")
            print(f"    Skill dir cleaned: {cleanup_result.skill_dir_cleaned}")

            if failed_resources:
                print(f"    WARNING: {len(failed_resources)} cloud resource(s) failed to release")
                send_failure_notification(failed_resources, args.session_id, args.skill_name)
        except Exception as ce:
            print(f"    Warning: resource cleanup failed (does not affect verdict): {ce}")
    except Exception as e:
        print(f"    ERROR: evaluation failed: {e}")
        if issue_id:
            if update_verify_status(issue_id, "failed", flow_url):
                print(f"    Verify status -> failed (issue {issue_id})")
        eval_failed = True

    # --- 评测报告 ISSUE（创建→负责人→关闭）---
    if issue_id and args.pr_url and not args.dry_run:
        run_pr_orchestration(
            issue_id=issue_id,
            pr_url=args.pr_url,
            skill_name=args.skill_name,
            eval_result=eval_result,
            metrics=metrics,
            security_risk=metrics.security_risk_level if metrics else "",
            report_id=report_id,
            report_url=report_url,
            eval_ok=not eval_failed,
            dry_run=args.dry_run,
        )

    if eval_failed:
        sys.exit(1)

    print("\n==> All done!")


if __name__ == "__main__":
    main()
