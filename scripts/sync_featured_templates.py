#!/usr/bin/env python3
"""定期同步 GitCode/GitHub 云原生优选项目模板。

用法：
  python scripts/sync_featured_templates.py [--limit 5] [--platform all]

  --limit     每平台获取数量（默认 100，测试用 5）
  --platform  all/gitcode/github（默认 all）

输出：
  - 写入 DB featured_templates 表
  - 更新 configs/project-templates.json（在 iac 后面新增两个分类）
"""
import json, os, sys, time, datetime
import requests

CN_KEYWORDS = [
    "cloud native", "kubernetes", "k8s", "terraform", "docker",
    "helm", "istio", "prometheus", "serverless", "microservice",
    "containerd", "grafana",
]

GITCODE_PAT = os.environ.get("GITCODE_PAT", "yXgZq-rf19PW2cmMoyXF_xbK")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "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")


def search_gitcode(keywords, limit=5):
    """搜索 GitCode 云原生项目"""
    results = []
    seen = set()
    for kw in keywords:
        if len(results) >= limit:
            break
        try:
            resp = requests.get(
                "https://api.gitcode.com/api/v5/search/repositories",
                params={"q": kw, "per_page": min(20, limit - len(results) + 5),
                        "sort": "stars", "order": "desc"},
                headers={"PRIVATE-TOKEN": GITCODE_PAT},
                timeout=15
            )
            if resp.status_code != 200:
                print(f"  GitCode search '{kw}' HTTP {resp.status_code}", file=sys.stderr)
                continue
            items = resp.json()
            if not isinstance(items, list):
                continue
            for r in items:
                full_name = r.get("full_name", "")
                if not full_name or full_name in seen:
                    continue
                seen.add(full_name)
                results.append({
                    "platform": "gitcode",
                    "repo_full_name": full_name,
                    "repo_name": r.get("name", ""),
                    "description": (r.get("description") or "")[:200],
                    "language": r.get("language") or "",
                    "stars": r.get("stargazers_count", 0) or 0,
                    "forks": r.get("forks_count", 0) or 0,
                    "topics": r.get("topics") or [],
                    "html_url": r.get("web_url") or "",
                    "clone_url": r.get("http_url_to_repo") or "",
                    "category": "cloud-native",
                    "fetched_at": datetime.datetime.now().isoformat(),
                })
                if len(results) >= limit:
                    break
            time.sleep(1)
        except Exception as e:
            print(f"GitCode search '{kw}' failed: {e}", file=sys.stderr)
    return results


def search_github(keywords, limit=5):
    """搜索 GitHub 云原生项目"""
    results = []
    seen = set()
    # 用多个关键词分别搜索，合并去重取 top
    for kw in keywords[:6]:
        if len(results) >= limit:
            break
        try:
            headers = {}
            if GITHUB_TOKEN:
                headers["Authorization"] = f"token {GITHUB_TOKEN}"
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": kw, "per_page": min(10, limit),
                        "sort": "stars", "order": "desc"},
                headers=headers,
                timeout=15
            )
            if resp.status_code != 200:
                print(f"  GitHub search '{kw}' HTTP {resp.status_code}", file=sys.stderr)
                if resp.status_code == 403:
                    print("  GitHub rate limited, waiting 60s...", file=sys.stderr)
                    time.sleep(60)
                continue
            items = resp.json().get("items", [])
            for r in items:
                full_name = r.get("full_name", "")
                if not full_name or full_name in seen:
                    continue
                seen.add(full_name)
                results.append({
                    "platform": "github",
                    "repo_full_name": full_name,
                    "repo_name": r.get("name", ""),
                    "description": (r.get("description") or "")[:200],
                    "language": r.get("language") or "",
                    "stars": r.get("stargazers_count", 0) or 0,
                    "forks": r.get("forks_count", 0) or 0,
                    "topics": r.get("topics") or [],
                    "html_url": r.get("html_url") or "",
                    "clone_url": r.get("clone_url") or "",
                    "category": "cloud-native",
                    "fetched_at": datetime.datetime.now().isoformat(),
                })
                if len(results) >= limit:
                    break
            time.sleep(2)  # GitHub search rate limit: 10/min
        except Exception as e:
            print(f"GitHub search '{kw}' failed: {e}", file=sys.stderr)
    # 按 stars 降序取 top limit
    results.sort(key=lambda x: x["stars"], reverse=True)
    return results[:limit]


def save_to_db(items):
    """存入 DB"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for item in items:
            c.execute(
                "INSERT OR REPLACE INTO featured_templates "
                "(platform, repo_full_name, repo_name, description, language, "
                "stars, forks, topics, html_url, clone_url, category, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["platform"], item["repo_full_name"], item["repo_name"],
                 item["description"], item["language"], item["stars"],
                 item["forks"], json.dumps(item["topics"]), item["html_url"],
                 item["clone_url"], item["category"], item["fetched_at"])
            )
        db.commit()
    print(f"Saved {len(items)} items to DB")


def _to_template(r, platform):
    """将 DB 记录转为模板格式"""
    topics = r.get("topics", [])
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except Exception:
            topics = []
    tags = topics[:5] if topics else ([r["language"]] if r.get("language") else [])
    return {
        "id": f"{platform}-{r['repo_full_name'].replace('/', '-')}",
        "name": r["repo_name"],
        "description": r.get("description", "")[:120],
        "icon": "📦",
        "source": "git",
        "source_repo": r["repo_full_name"],
        "source_platform": platform,
        "clone_url": r.get("clone_url", ""),
        "html_url": r.get("html_url", ""),
        "default_repo_name": r["repo_name"].lower()[:30],
        "tags": tags,
        "scene_id": "default",
        "stats": {
            "show": True,
            "stars": r.get("stars", 0),
            "language": r.get("language", ""),
            "platform": platform,
        }
    }


def update_templates_json(gitcode_items, github_items):
    """更新 configs/project-templates.json，在 iac 后面新增两个分类"""
    config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "project-templates.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    cats = config.get("categories", [])
    # 移除旧的优选分类
    cats = [c for c in cats if c.get("id") not in ("gitcode-featured", "github-featured")]
    # 找到 iac 的位置
    iac_idx = next((i for i, c in enumerate(cats) if c.get("id") == "iac"), len(cats) - 1)

    gitcode_cat = {
        "id": "gitcode-featured",
        "name": "GitCode 优选项目模板",
        "icon": "🟢",
        "description": "GitCode 云原生热门项目，每日自动同步",
        "scene_id": "default",
        "page_size": 12,
        "templates": [_to_template(r, "gitcode") for r in gitcode_items],
    }
    github_cat = {
        "id": "github-featured",
        "name": "GitHub 优选项目模板",
        "icon": "⚫",
        "description": "GitHub 云原生热门项目，每日自动同步",
        "scene_id": "default",
        "page_size": 12,
        "templates": [_to_template(r, "github") for r in github_items],
    }

    cats.insert(iac_idx + 1, gitcode_cat)
    cats.insert(iac_idx + 2, github_cat)
    config["categories"] = cats

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"Updated project-templates.json: +{len(gitcode_items)} gitcode +{len(github_items)} github")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--platform", choices=["all", "gitcode", "github"], default="all")
    args = parser.parse_args()

    gitcode_items = []
    github_items = []

    if args.platform in ("all", "gitcode"):
        print(f"Searching GitCode (limit={args.limit})...")
        gitcode_items = search_gitcode(CN_KEYWORDS, limit=args.limit)
        print(f"  Got {len(gitcode_items)} items")

    if args.platform in ("all", "github"):
        print(f"Searching GitHub (limit={args.limit})...")
        github_items = search_github(CN_KEYWORDS, limit=args.limit)
        print(f"  Got {len(github_items)} items")

    all_items = gitcode_items + github_items
    if all_items:
        save_to_db(all_items)
        update_templates_json(gitcode_items, github_items)
    else:
        print("No items found, skipping DB/JSON update")
