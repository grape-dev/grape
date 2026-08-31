#!/usr/bin/env python3
"""
通过 GitCode API v5 获取 Issue 列表。

API 端点: https://gitcode.com/api/v5/repos/{owner}/{repo}/issues
认证方式: Authorization: Bearer {token}

Token 获取顺序:
1. 环境变量 GITCODE_TOKEN
2. git remote URL 中的 token (https://USER:TOKEN@gitcode.com/...)
3. 命令行参数 --token
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 缺少 requests 库。请运行: pip install requests", file=sys.stderr)
    sys.exit(1)


GITCODE_API_BASE = "https://gitcode.com/api/v5"


def get_token(cli_token: str = None, repo_dir: str = None) -> str:
    """获取 GitCode API token。

    优先级: 命令行参数 > 环境变量 > git remote URL
    """
    # 1. 命令行参数
    if cli_token:
        return cli_token

    # 2. 环境变量
    token = os.environ.get("GITCODE_TOKEN", "")
    if token:
        return token

    # 3. git remote URL
    candidate_dirs = [Path.cwd(), Path.home()]
    if repo_dir:
        candidate_dirs.insert(0, Path(repo_dir))
    for repo_dir_item in candidate_dirs:
        if not (repo_dir_item / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=str(repo_dir_item), capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "gitcode.com" in line and "@" in line:
                match = re.search(r":([^:@]+)@gitcode\.com", line)
                if match:
                    return match.group(1)

    # 4. git global config
    result = subprocess.run(
        ["git", "config", "--global", "--get-regexp", "remote.*.url"],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if "gitcode.com" in line and "@" in line:
            match = re.search(r":([^:@]+)@gitcode\.com", line)
            if match:
                return match.group(1)

    return ""


def fetch_issues(
    owner: str,
    repo: str,
    token: str = None,
    state: str = "all",
    per_page: int = 50,
    max_pages: int = 20,
    repo_dir: str = None,
) -> list:
    """获取仓库的全部 Issue 列表（通过统一封装脚本 gitcode_api.sh）。

    Args:
        owner: 仓库 owner（必填）
        repo: 仓库名（必填）
        token: GitCode API token（封装脚本统一读取，此处可忽略）
        state: Issue 状态 (open/closed/all)
        per_page: 每页数量
        max_pages: 最大页数
        repo_dir: 仓库目录（用于从 git remote 提取 token）

    Returns:
        list: Issue 列表，每个 Issue 为 dict
    """
    script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "gitcode_api.sh"
    )
    all_issues = []
    page = 1

    while page <= max_pages:
        try:
            proc = subprocess.run(
                [script, "issue", "list", owner, repo,
                 "--per-page", str(per_page), "--page", str(page)],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                print(
                    f"❌ 获取 Issue 失败 (page {page}): {proc.stderr.strip()}",
                    file=sys.stderr,
                )
                break
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            print(f"❌ 调用 gitcode_api.sh 失败: {e}", file=sys.stderr)
            break

        if not proc.stdout.strip():
            break

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            print(
                f"❌ 解析 Issue 列表失败 (page {page})",
                file=sys.stderr,
            )
            break

        if not data:
            break

        all_issues.extend(data)

        # 检查是否还有更多页
        if len(data) < per_page:
            break
        page += 1

    return all_issues


def normalize_issue(raw: dict) -> dict:
    """将 GitCode API 返回的 Issue 数据标准化。

    处理不同 API 版本的字段名差异。
    """
    # 处理 assignee
    assignee = raw.get("assignee") or {}
    assignee_name = ""
    if isinstance(assignee, dict):
        assignee_name = assignee.get("name") or assignee.get("login") or ""
    elif isinstance(assignee, str):
        assignee_name = assignee

    # 处理 labels
    labels = raw.get("labels") or []
    label_names = []
    for label in labels:
        if isinstance(label, dict):
            label_names.append(label.get("name", ""))
        elif isinstance(label, str):
            label_names.append(label)

    # 处理创建时间
    created_at = raw.get("created_at") or raw.get("createdAt") or ""

    return {
        "number": raw.get("number") or raw.get("iid", 0),
        "title": raw.get("title", ""),
        "state": raw.get("state", "open"),
        "created_at": created_at,
        "assignee_name": assignee_name,
        "assignee_login": assignee.get("login", "") if isinstance(assignee, dict) else "",
        "labels": label_names,
        "html_url": raw.get("html_url") or raw.get("url", ""),
        "body": raw.get("body", ""),
    }


def extract_skill_from_title(title: str) -> str:
    """从 Issue 标题中提取 skill 名称。

    支持的格式:
    - [skill-name] 标题内容
    - 【skill-name】标题内容
    - [ skill-name ] 标题内容
    """
    # 匹配 [skill-name] 或 【skill-name】
    patterns = [
        r"[\[【]\s*(huawei-cloud-[a-z0-9-]+)\s*[\]】]",
    ]
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def filter_issues_by_skill(issues: list, skill_name: str) -> list:
    """按 skill 名称过滤 Issue 列表。"""
    filtered = []
    for issue in issues:
        title = issue.get("title", "")
        if skill_name.lower() in title.lower():
            # 更精确: 检查标题中是否有 [skill-name] 模式
            extracted = extract_skill_from_title(title)
            if extracted == skill_name:
                filtered.append(issue)
            elif not extracted and skill_name.lower() in title.lower():
                # 没有明确的 skill 标记但标题包含 skill 名称
                filtered.append(issue)
    return filtered


def group_issues_by_skill(issues: list) -> dict:
    """将 Issue 按 skill 名称分组。

    Returns:
        dict: {skill_name: [issue, ...]}, 未匹配的归入 "_unknown"
    """
    groups = {}
    for issue in issues:
        skill = extract_skill_from_title(issue.get("title", ""))
        if not skill:
            skill = "_unknown"
        if skill not in groups:
            groups[skill] = []
        groups[skill].append(issue)
    return groups


def format_issue_row(issue: dict) -> str:
    """格式化 Issue 为 Markdown 表格行。

    Args:
        issue: 标准化后的 issue dict
    """
    number = issue.get("number", 0)
    state = issue.get("state", "open")
    state_icon = "🟢 open" if state == "open" else "🔴 closed"
    created_at = issue.get("created_at", "")
    # 截断时间到秒
    if created_at and "T" in created_at:
        created_at = created_at.replace("T", " ")[:19]

    assignee = issue.get("assignee_name", "")
    assignee_login = issue.get("assignee_login", "")
    if assignee and assignee_login:
        assignee_str = f"{assignee} @{assignee_login}"
    elif assignee_login:
        assignee_str = f"@{assignee_login}"
    else:
        assignee_str = assignee or "—"

    labels = ", ".join(issue.get("labels", []))
    title = issue.get("title", "")
    html_url = issue.get("html_url", "")

    issue_link = f"[#{number}]({html_url})" if html_url else f"#{number}"

    return f"| {issue_link} | {state_icon} | {created_at} | {assignee_str} | {labels} | {title} |"


def main():
    """命令行入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="获取 GitCode Issue 列表")
    parser.add_argument("--owner", required=True, help="仓库 owner")
    parser.add_argument("--repo", required=True, help="仓库名")
    parser.add_argument("--token", help="GitCode API token")
    parser.add_argument("--repo-dir", help="仓库目录（用于从 git remote 提取 token）")
    parser.add_argument("--state", default="all", choices=["open", "closed", "all"])
    parser.add_argument("--skill", help="按 skill 名称过滤")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--group", action="store_true", help="按 skill 分组输出")
    args = parser.parse_args()

    issues = fetch_issues(
        owner=args.owner,
        repo=args.repo,
        token=args.token,
        state=args.state,
        repo_dir=args.repo_dir,
    )

    # 标准化
    issues = [normalize_issue(i) for i in issues]

    if not issues:
        print("未获取到任何 Issue", file=sys.stderr)
        sys.exit(1)

    if args.skill:
        issues = filter_issues_by_skill(issues, args.skill)

    if args.json:
        if args.group:
            print(json.dumps(group_issues_by_skill(issues), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(issues, ensure_ascii=False, indent=2))
    else:
        print(f"\n共 {len(issues)} 个 Issue\n")
        if args.group:
            groups = group_issues_by_skill(issues)
            for skill, skill_issues in sorted(groups.items()):
                print(f"\n{'='*50}")
                print(f"  {skill}: {len(skill_issues)} 个")
                print(f"{'='*50}")
                for issue in skill_issues:
                    print(f"  #{issue['number']} [{issue['state']}] {issue['title'][:60]}")
        else:
            for issue in issues:
                print(f"  #{issue['number']} [{issue['state']}] {issue['title'][:80]}")


if __name__ == "__main__":
    main()
