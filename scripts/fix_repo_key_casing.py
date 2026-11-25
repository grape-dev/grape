#!/usr/bin/env python3
"""按 VCS 实际 full_name 修正 managed_repos 中大小写不符的 repo_key（Issue #3397）。

- 只修正**纯大小写差异**（casefold 相等）：组织/仓库名真变更多走仓库迁移流程。
- 默认 dry-run：输出影响行数与目标写法，不写库；`--apply` 才执行迁移（幂等）。
- 目标写法权威来源：VCS `GET /repos/{owner}/{repo}` 返回的 `full_name`。
- 迁移复用 `src.kernel.repo_migrate.migrate_repo_key`（跨域关联键原子改写）。

用法：
    python scripts/fix_repo_key_casing.py                      # dry-run
    python scripts/fix_repo_key_casing.py --provider gitcode   # 只处理 gitcode
    python scripts/fix_repo_key_casing.py --apply              # 执行修正
    python scripts/fix_repo_key_casing.py --json               # 结构化输出
"""
import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.kernel.repo_key import parse_repo_key  # noqa: E402
from src.kernel.config import log  # noqa: E402


def _fetch_actual_full_name(provider: str, owner: str, repo: str, token: str) -> str:
    """VCS 仓库实际 full_name（大小写权威）。失败返回空串。"""
    from src.domains.mgmt.user import repo_manager
    try:
        if provider == "gitcode":
            url = (f"{repo_manager.API_BASE}/repos/"
                   f"{urllib.parse.quote(owner, safe='')}/"
                   f"{urllib.parse.quote(repo, safe='')}")
            data = repo_manager._req("GET", url, token)
        elif provider == "github":
            url = (f"{repo_manager._GITHUB_API_BASE}/repos/"
                   f"{urllib.parse.quote(owner, safe='')}/"
                   f"{urllib.parse.quote(repo, safe='')}")
            data = repo_manager._github_req("GET", url, token)
        else:
            return ""
    except Exception as e:
        log.warning(f"fetch actual full_name failed ({provider}:{owner}/{repo}): {e}")
        return ""
    full_name = str((data or {}).get("full_name") or "") if isinstance(data, dict) else ""
    return full_name


def _repo_token(provider: str, repo_key: str) -> str:
    from src.domains.mgmt.user import db as user_db
    try:
        if provider == "gitcode":
            return user_db.get_gitcode_token(repo_key) or ""
        if provider == "github":
            return user_db.get_github_token(repo_key) or ""
    except Exception as e:
        log.warning(f"repo token lookup failed ({repo_key}): {e}")
    return ""


def plan(provider_filter: str = "") -> list[dict]:
    """返回待修正项：[{repo_key, provider, old_full_name, new_full_name, status}]。"""
    from src.domains.mgmt.user import db as user_db
    items = []
    for row in user_db.get_all_managed_repos():
        repo_key = str(row.get("repo_key") or "")
        provider, owner, repo = parse_repo_key(repo_key)
        if not provider or not owner or not repo:
            continue
        if provider_filter and provider != provider_filter:
            continue
        old_full = f"{owner}/{repo}"
        token = _repo_token(provider, repo_key)
        if not token:
            items.append({"repo_key": repo_key, "provider": provider,
                          "old_full_name": old_full, "new_full_name": "",
                          "status": "skipped:no_token"})
            continue
        actual = _fetch_actual_full_name(provider, owner, repo, token)
        if not actual:
            items.append({"repo_key": repo_key, "provider": provider,
                          "old_full_name": old_full, "new_full_name": "",
                          "status": "skipped:fetch_failed"})
            continue
        if actual == old_full:
            continue
        if actual.casefold() != old_full.casefold():
            items.append({"repo_key": repo_key, "provider": provider,
                          "old_full_name": old_full, "new_full_name": actual,
                          "status": "skipped:not_case_only"})
            continue
        items.append({"repo_key": repo_key, "provider": provider,
                      "old_full_name": old_full, "new_full_name": actual,
                      "status": "fixable"})
    return items


def apply(items: list[dict]) -> list[dict]:
    """执行修正；只处理 status=fixable。返回每项结果。"""
    from src.kernel.repo_migrate import migrate_repo_key
    results = []
    for item in items:
        if item.get("status") != "fixable":
            results.append({**item, "applied": False})
            continue
        provider = item["provider"]
        new_owner, _, new_repo = str(item["new_full_name"]).partition("/")
        try:
            stats = migrate_repo_key(item["repo_key"], new_owner, new_repo, operator_id=0)
            results.append({**item, "applied": True, "stats": stats})
        except Exception as e:
            log.warning(f"fix repo key casing failed ({item['repo_key']}): {e}")
            results.append({**item, "applied": False, "error": str(e)})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix managed_repos repo_key casing")
    parser.add_argument("--apply", action="store_true", help="execute the fixes (default: dry-run)")
    parser.add_argument("--provider", default="", help="only this provider (gitcode/github)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    items = plan(args.provider)
    fixable = [i for i in items if i.get("status") == "fixable"]
    result = {"dry_run": not args.apply, "planned": len(fixable),
              "skipped": len(items) - len(fixable), "items": items}
    if args.apply:
        result["results"] = apply(items)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{'DRY-RUN' if not args.apply else 'APPLY'} planned={len(fixable)} "
              f"skipped={len(items) - len(fixable)}")
        for item in items:
            print(json.dumps(item, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
