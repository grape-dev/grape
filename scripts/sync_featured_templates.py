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


def _build_reason(item, search_kw):
    """根据项目信息生成简短推荐理由。"""
    name = item.get("repo_name", "")
    lang = item.get("language", "")
    stars = item.get("stars", 0)
    desc = item.get("description", "")
    topics = item.get("topics", [])
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except Exception:
            topics = []

    parts = []
    # 星数描述
    if stars >= 50000:
        parts.append(f"热门项目（{stars:,} ⭐）")
    elif stars >= 10000:
        parts.append(f"高星项目（{stars:,} ⭐）")
    elif stars >= 1000:
        parts.append(f"社区活跃（{stars:,} ⭐）")
    elif stars > 0:
        parts.append(f"值得关注（{stars} ⭐）")

    # 语言/技术栈
    if lang:
        lang_map = {"Go": "Go 语言", "Python": "Python", "Java": "Java", "TypeScript": "TypeScript",
                    "Rust": "Rust", "C++": "C++", "Shell": "Shell 脚本", "HTML": "前端"}
        parts.append(lang_map.get(lang, lang))

    # 关键词匹配
    kw_desc = {
        "kubernetes": "Kubernetes 生态", "k8s": "K8s 编排",
        "terraform": "基础设施即代码", "docker": "容器化",
        "helm": "Helm 包管理", "istio": "服务网格",
        "prometheus": "监控告警", "grafana": "可视化仪表盘",
        "serverless": "Serverless 架构", "microservice": "微服务架构",
        "cloud native": "云原生核心", "containerd": "容器运行时",
    }
    kw_tags = []
    for kw in [search_kw] + (topics if isinstance(topics, list) else []):
        for k, v in kw_desc.items():
            if k.lower() in str(kw).lower() and v not in kw_tags:
                kw_tags.append(v)
    if kw_tags:
        parts.append(" / ".join(kw_tags[:3]))

    # 描述摘要
    if desc and len(desc) > 20:
        parts.append(desc[:60])

    return " · ".join(parts[:3]) if parts else "云原生优选项目"


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
                    "recommend_reason": "",
                    "fetched_at": datetime.datetime.now().isoformat(),
                })
                # 生成推荐理由
                results[-1]["recommend_reason"] = _build_reason(results[-1], kw)
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
                    "recommend_reason": "",
                    "fetched_at": datetime.datetime.now().isoformat(),
                })
                # 生成推荐理由
                results[-1]["recommend_reason"] = _build_reason(results[-1], kw)
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
                "stars, forks, topics, html_url, clone_url, category, recommend_reason, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["platform"], item["repo_full_name"], item["repo_name"],
                 item["description"], item["language"], item["stars"],
                 item["forks"], json.dumps(item["topics"]), item["html_url"],
                 item["clone_url"], item["category"], item.get("recommend_reason", ""),
                 item["fetched_at"])
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
        "recommend_reason": r.get("recommend_reason", ""),
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


def _categorize_by_keywords(t):
    """根据 tags/name/description/reason 关键词将精选模板归类到场景分类。

    返回分类 id：general / mobile / iac / skill-dev / ai / content
    """
    import re
    tags = [x.lower() for x in (t.get("tags") or [])]
    name = (t.get("name") or "").lower()
    desc = (t.get("description") or "").lower()
    reason = (t.get("recommend_reason") or "").lower()
    all_text = " ".join(tags) + " " + name + " " + desc + " " + reason

    def has_kw(kw):
        return re.search(r"\b" + re.escape(kw) + r"\b", all_text) is not None

    if has_kw("skill"):
        return "skill-dev"
    if any(k in all_text for k in ["harmonyos", "arkts", "flutter"]):
        return "mobile"
    if "ai" in tags or any(k in all_text for k in [
        "agent", "automation", "llm", "gpt", "ai-native", "ai-gateway",
        "n8n", "machine-learning", "artificial-intelligence",
        "ai workflow", "ai capabilities", "native ai", "ai-native-development",
    ]):
        return "ai"
    if any(has_kw(k) for k in ["blog", "pdf", "memo", "cheatsheet", "cheatsheets"]):
        return "content"
    if "note-taking" in all_text or "pdf-converter" in all_text:
        return "content"
    if any(k in all_text for k in [
        "k8s", "kubernetes", "docker", "terraform", "helm", "cncf", "container",
        "cloud-native", "cloudnative", "iac", "devops", "ansible", "microservice",
        "serverless", "istio", "gateway", "golang", "prometheus", "grafana",
        "argo", "cilium", "etcd", "minio", "cockroach", "vaultwarden", "bitwarden",
        "helm-charts", "charts", "chart", "containerd", "cni", "ebpf", "bpf",
        "k3s", "kops", "kuboard", "k9s", "colima", "faas", "gitops",
        "distributed-database", "distributed-systems", "consensus",
        "api-gateway", "api-management", "cloud-storage", "amazon-s3", "cloudstorage",
        "vcluster", "coder", "semaphore", "xpipe", "homelab", "90days",
        "oh-my-zsh", "cli-app", "self-hosted", "monitor", "monitoring",
    ]):
        return "iac"
    return "general"


def update_templates_json(gitcode_items, github_items):
    """更新 configs/project-templates.json：按关键词将精选模板分配到场景分类。

    去重：同名项目优先保留 GitCode，从 GitHub 中移除重复项。
    幂等：先移除各分类中已有 source_platform 字段的旧精选模板，再重新分配。
    """
    config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "project-templates.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    cats = config.get("categories", [])

    # 去重：GitCode 优先，GitHub 移除同名项目
    gc_names = set(r["repo_name"] for r in gitcode_items)
    github_items_dedup = [r for r in github_items if r["repo_name"] not in gc_names]
    if len(github_items_dedup) < len(github_items):
        print(f"Dedup: removed {len(github_items) - len(github_items_dedup)} GitHub duplicates (kept GitCode)")

    all_items = [_to_template(r, "gitcode") for r in gitcode_items] + \
                [_to_template(r, "github") for r in github_items_dedup]

    # 幂等：移除各分类中已有 source_platform 字段的旧精选模板
    for cat in cats:
        if cat.get("id") == "hello-world":
            continue
        cat["templates"] = [t for t in (cat.get("templates") or []) if not t.get("source_platform")]

    # 按关键词分类并追加到对应分类
    cat_map = {c["id"]: c for c in cats}
    added = {"general": 0, "mobile": 0, "iac": 0, "skill-dev": 0, "ai": 0, "content": 0}
    for t in all_items:
        target = _categorize_by_keywords(t)
        if target in cat_map:
            cat_map[target]["templates"].append(t)
            added[target] += 1

    config["categories"] = cats

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    parts = [f"{k}+{v}" for k, v in added.items() if v]
    print(f"Updated project-templates.json: {', '.join(parts)}")


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

    # 去重：GitCode 优先，GitHub 移除同名项目
    gc_name_set = set(r["repo_name"] for r in gitcode_items)
    github_items_dedup = [r for r in github_items if r["repo_name"] not in gc_name_set]
    if github_items_dedup and len(github_items_dedup) < len(github_items):
        print(f"Dedup: removed {len(github_items) - len(github_items_dedup)} GitHub duplicates (kept GitCode)")
    all_items = gitcode_items + github_items_dedup
    if all_items:
        save_to_db(all_items)
        update_templates_json(gitcode_items, github_items_dedup if github_items_dedup else github_items)
    else:
        print("No items found, skipping DB/JSON update")
