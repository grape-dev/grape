#!/usr/bin/env python3
import os

from .gitcode_merger import (
    is_auto_merge_enabled,
    update_repo_status,
    parse_pr_url,
    merge_pr,
    assign_pr,
    assign_merger,
)
from .gitcode_issue import (
    get_pr_creator_login,
    create_report_issue,
    add_issue_label,
    close_issue,
    build_issue_body,
    REPORT_ISSUE_LABEL,
    get_repo_numeric_id,
    ensure_access_token,
    resolve_assignee_id,
    set_issue_assignee,
)

# PR 合并人 login — 可配置项：环境变量 GITCODE_PR_ASSIGNEE 覆盖，默认 g30074593
# 注意：必须是目标仓库 Owner/Maintainer 级别用户，否则 API 返回"操作权限不匹配"
ASSIGNEE_LOGIN = os.getenv("GITCODE_PR_ASSIGNEE", "lccode")


def run_pr_orchestration(*, issue_id, pr_url, skill_name, eval_result, metrics,
                         security_risk, report_id, report_url, eval_ok=True,
                         dry_run=False) -> None:
    """评测后的 PR 治理编排。按序：
       1) 设置审查人 = GITCODE_PR_ASSIGNEE（v5 assignees，login）
       2) 设置合并人 = GITCODE_PR_ASSIGNEE_ID（内网 API，数字 id）
       3) 自动合入（仅当 AUTO_MERGE_PR=on；off 时不写 repo 状态）
       4) 创建评测报告 ISSUE（创建→加标签→正文 @创建人→关闭）；eval_ok=False 时跳过
       所有动作都独立 try/except，互不阻塞；缺 token 或缺 repo 时跳过并告警。
    """
    if dry_run or not issue_id or not pr_url:
        return
    token = os.getenv("GITCODE_TOKEN", "")
    if not token:
        print("    Warning: GITCODE_TOKEN not set, skip PR orchestration")
        return
    parsed = parse_pr_url(pr_url)
    if not parsed:
        print(f"    Warning: cannot parse PR URL {pr_url}")
        return
    owner, repo, pr_number = parsed["owner"], parsed["repo"], parsed["pr_number"]

    def _set_repo_status(status, url):
        try:
            return update_repo_status(issue_id, status, url)
        except Exception as e:
            print(f"    Warning: repo status update failed: {e}")
            return False

    try:
        result = assign_pr(pr_url, token, ASSIGNEE_LOGIN)
        if result["assigned"]:
            print(f"    Assignee set -> {ASSIGNEE_LOGIN} (PR {pr_number})")
        else:
            print("    Warning: assignee not set (assignees_number=0)")
    except Exception as e:
        print(f"    Warning: assign PR failed: {e}")

    # 合并人（内网 API，数字用户 id；独立 try/except，失败不阻断）
    try:
        access_token = ensure_access_token()
        if access_token:
            m = assign_merger(pr_url, access_token)
            if m["assigned"]:
                print(f"    Merger set -> id {os.getenv('GITCODE_PR_ASSIGNEE_ID', '11027048')} (PR {pr_number})")
            else:
                print("    Warning: merger not set (回读确认失败)")
        else:
            print("    Warning: GITCODE_ACCESS_TOKEN not available, skip merger")
    except Exception as e:
        print(f"    Warning: assign merger failed: {e}")

    if not is_auto_merge_enabled():
        print(f"    Repo status -> 不写入 (auto-merge off, issue {issue_id})")
    else:
        try:
            if _set_repo_status("processing", pr_url):
                print(f"    Repo status -> processing (issue {issue_id})")
            result = merge_pr(pr_url, token)
            if result["merged"]:
                if _set_repo_status("done", result["html_url"]):
                    print(f"    Repo status -> done (issue {issue_id})")
            else:
                if _set_repo_status("failed", pr_url):
                    print(f"    Repo status -> failed (issue {issue_id})")
        except Exception as e:
            print(f"    Warning: PR merge failed: {e}")
            if _set_repo_status("failed", pr_url):
                print(f"    Repo status -> failed (issue {issue_id})")

    if not eval_ok:
        return
    try:
        try:
            creator = get_pr_creator_login(owner, repo, pr_number, token)
        except Exception as e:
            print(f"    Warning: get PR creator login failed: {e}")
            creator = None
        title = f"{skill_name} 评测报告"
        body = build_issue_body(
            skill_name, pr_url, eval_result, metrics,
            security_risk, report_id, report_url, creator,
        )
        number = create_report_issue(owner, repo, title, body, token)
        if not number:
            print("    Warning: report issue create returned no number")
            return
        print(f"    Report issue created: #{number}")
        if creator:
            try:
                access_token = ensure_access_token()
                if access_token:
                    project_id = get_repo_numeric_id(owner, repo, token)
                    if project_id:
                        assignee_id = resolve_assignee_id(project_id, creator, access_token)
                        if assignee_id:
                            if set_issue_assignee(project_id, number, [assignee_id], access_token):
                                print(f"    Issue assignee set -> {creator} (#{number})")
                        else:
                            print(f"    Warning: cannot resolve assignee id for {creator}")
                    else:
                        print("    Warning: cannot resolve repo numeric id, skip issue assignee")
                else:
                    print("    Warning: GITCODE_ACCESS_TOKEN not available, skip issue assignee")
            except Exception as e:
                print(f"    Warning: issue assignee set failed: {e}")
        if add_issue_label(owner, repo, number, REPORT_ISSUE_LABEL, token):
            print(f"    Issue label added: {REPORT_ISSUE_LABEL}")
        if close_issue(owner, repo, number, token):
            print(f"    Issue #{number} closed")
    except Exception as e:
        print(f"    Warning: report issue creation failed: {e}")
