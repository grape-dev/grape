# GitCode/GitHub 优选项目模板方案

> 每天晚上定期从 GitCode 和 GitHub 获取 Top100 云原生相关项目，存入 Grape 系统，作为项目模板供用户使用。

## 1. 现状分析

### 1.1 现有项目模板系统

| 组件 | 文件 | 说明 |
|------|------|------|
| 配置文件 | `configs/project-templates.json` | 6 个分类（hello-world/agent-best-practices/skills/ai-office/cloud-native/iac） |
| 后端 API | `src/api/routes/templates.py` | `GET /api/project-templates` 返回分类+模板；`POST /api/project-templates/create` 一键创建 |
| 前端 | `src/api/static/js/pages/templates.js` | 水平卡片展示 + 分页 + 收藏 |
| 模板来源 | `source: "local"` / `source: "git"` | local = 本地目录复制；git = clone 远程仓库 |

### 1.2 IaC 分类当前结构

```json
{
  "id": "iac",
  "name": "IaC 项目模板",
  "icon": "🏗️",
  "description": "基础设施即代码项目起点",
  "scene_id": "iac",
  "templates": [
    {
      "id": "tf-infra",
      "name": "Terraform Infra",
      "source": "git",
      "source_repo": "devops-bot/terraform-infra",
      ...
    }
  ]
}
```

### 1.3 API 调研结果

| 平台 | 搜索 API | 认证 | 限流 | 关键字段 |
|------|---------|------|------|---------|
| GitCode | `GET https://api.gitcode.com/api/v5/search/repositories?q=cloud+native&sort=stars&order=desc` | `PRIVATE-TOKEN` header | 无明确限制 | full_name, stargazers_count, language, description, web_url, http_url_to_repo |
| GitHub | `GET https://api.github.com/search/repositories?q=cloud+native&sort=stars&order=desc` | 无认证可用（60次/小时） | search: 10次/分钟 | full_name, stargazers_count, language, description, html_url, clone_url, topics |

## 2. 方案设计

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────┐
│  Cron Job（每天 02:00 执行）                             │
│  scripts/sync_featured_templates.py                      │
│                                                         │
│  1. 搜索 GitCode 云原生 Top100                           │
│     q=kubernetes OR terraform OR docker OR "cloud native"│
│     OR helm OR istio OR prometheus OR k8s                │
│     sort=stars, per_page=20, 5 pages                    │
│                                                         │
│  2. 搜索 GitHub 云原生 Top100                             │
│     q=cloud native kubernetes terraform docker           │
│     sort=stars, per_page=20, 5 pages                    │
│                                                         │
│  3. 去重 + 过滤 + 存入 DB（featured_templates 表）        │
│                                                         │
│  4. 更新 configs/project-templates.json                  │
│     在 iac 分类后面新增两个分类                           │
└─────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────┐
│  Grape 前端（templates.js）                              │
│                                                         │
│  分类列表：                                              │
│  1. Hello World 新手模板                                 │
│  2. Agent 最佳实践                                       │
│  3. Skills 项目模板                                      │
│  4. AI office 项目模板                                   │
│  5. 云原生项目模板                                       │
│  6. IaC 项目模板                                         │
│  7. GitCode 优选项目模板  ← 新增                         │
│  8. GitHub 优选项目模板  ← 新增                          │
└─────────────────────────────────────────────────────────┘
```

### 2.2 DB 变更

#### featured_templates 表

```sql
CREATE TABLE IF NOT EXISTS featured_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,          -- 'gitcode' / 'github'
    repo_full_name TEXT NOT NULL,       -- 'owner/repo'
    repo_name   TEXT NOT NULL,          -- 'kubernetes'
    description TEXT DEFAULT '',
    language    TEXT DEFAULT '',
    stars       INTEGER DEFAULT 0,
    forks       INTEGER DEFAULT 0,
    topics      TEXT DEFAULT '[]',      -- JSON array
    html_url    TEXT DEFAULT '',         -- web URL
    clone_url   TEXT DEFAULT '',         -- git clone URL
    category    TEXT DEFAULT 'cloud-native',  -- 搜索类别
    fetched_at  TEXT DEFAULT '',         -- 抓取时间
    UNIQUE(platform, repo_full_name)
)
```

#### DDL + 迁移（db_setup.py）

```python
_FEATURED_TEMPLATES_DDL = """
CREATE TABLE IF NOT EXISTS featured_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    description TEXT DEFAULT '',
    language    TEXT DEFAULT '',
    stars       INTEGER DEFAULT 0,
    forks       INTEGER DEFAULT 0,
    topics      TEXT DEFAULT '[]',
    html_url    TEXT DEFAULT '',
    clone_url   TEXT DEFAULT '',
    category    TEXT DEFAULT 'cloud-native',
    fetched_at  TEXT DEFAULT '',
    UNIQUE(platform, repo_full_name)
)
"""
```

### 2.3 爬取脚本

#### scripts/sync_featured_templates.py

```python
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

# 云原生搜索关键词
CN_KEYWORDS = [
    "cloud native",
    "kubernetes", "k8s",
    "terraform",
    "docker", "containerd",
    "helm",
    "istio", "envoy",
    "prometheus", "grafana",
    "serverless",
    "microservice",
]

GITCODE_PAT = os.environ.get("GITCODE_PAT", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # 可选，无则用匿名


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
                params={"q": kw, "per_page": min(20, limit - len(results)),
                        "sort": "stars", "order": "desc"},
                headers={"PRIVATE-TOKEN": GITCODE_PAT},
                timeout=15
            )
            if resp.status_code != 200:
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
                    "description": r.get("description", "") or "",
                    "language": r.get("language", "") or "",
                    "stars": r.get("stargazers_count", 0) or 0,
                    "forks": r.get("forks_count", 0) or 0,
                    "topics": r.get("topics") or [],
                    "html_url": r.get("web_url", "") or "",
                    "clone_url": r.get("http_url_to_repo", "") or "",
                    "category": "cloud-native",
                    "fetched_at": datetime.datetime.now().isoformat(),
                })
                if len(results) >= limit:
                    break
            time.sleep(1)  # 限流
        except Exception as e:
            print(f"GitCode search '{kw}' failed: {e}", file=sys.stderr)
    return results


def search_github(keywords, limit=5):
    """搜索 GitHub 云原生项目"""
    results = []
    seen = set()
    # 合并关键词为 OR 查询
    q = " ".join(f"{kw}" for kw in keywords[:3])  # GitHub 搜索 q 太长会截断
    try:
        resp = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": q, "per_page": min(limit, 30),
                    "sort": "stars", "order": "desc"},
            headers={"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {},
            timeout=15
        )
        if resp.status_code == 200:
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
                    "description": r.get("description", "") or "",
                    "language": r.get("language", "") or "",
                    "stars": r.get("stargazers_count", 0) or 0,
                    "forks": r.get("forks_count", 0) or 0,
                    "topics": r.get("topics") or [],
                    "html_url": r.get("html_url", "") or "",
                    "clone_url": r.get("clone_url", "") or "",
                    "category": "cloud-native",
                    "fetched_at": datetime.datetime.now().isoformat(),
                })
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"GitHub search failed: {e}", file=sys.stderr)
    return results


def save_to_db(items, db_path=None):
    """存入 DB（SQLite/MySQL 兼容）"""
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


def update_templates_json(gitcode_items, github_items):
    """更新 configs/project-templates.json，在 iac 后面新增两个分类"""
    config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "project-templates.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    cats = config.get("categories", [])

    # 移除旧的优选分类（如果有）
    cats = [c for c in cats if c["id"] not in ("gitcode-featured", "github-featured")]

    # 找到 iac 的位置
    iac_idx = next((i for i, c in enumerate(cats) if c["id"] == "iac"), len(cats) - 1)

    # 构建 GitCode 优选分类
    gitcode_cat = {
        "id": "gitcode-featured",
        "name": "GitCode 优选项目模板",
        "icon": "🟢",
        "description": "GitCode 云原生热门项目，每日自动同步",
        "scene_id": "default",
        "page_size": 12,
        "templates": [_to_template(r, "gitcode") for r in gitcode_items],
    }

    # 构建 GitHub 优选分类
    github_cat = {
        "id": "github-featured",
        "name": "GitHub 优选项目模板",
        "icon": "⚫",
        "description": "GitHub 云原生热门项目，每日自动同步",
        "scene_id": "default",
        "page_size": 12,
        "templates": [_to_template(r, "github") for r in github_items],
    }

    # 插入到 iac 后面
    cats.insert(iac_idx + 1, gitcode_cat)
    cats.insert(iac_idx + 2, github_cat)

    config["categories"] = cats
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"Updated project-templates.json: +{len(gitcode_items)} gitcode +{len(github_items)} github")


def _to_template(r, platform):
    """将 DB 记录转为模板格式"""
    return {
        "id": f"{platform}-{r['repo_full_name'].replace('/', '-')}",
        "name": r["repo_name"],
        "description": r["description"][:120],
        "icon": "📦",
        "source": "git",
        "source_repo": r["repo_full_name"],
        "source_platform": platform,
        "clone_url": r["clone_url"],
        "html_url": r["html_url"],
        "default_repo_name": r["repo_name"].lower()[:30],
        "tags": r.get("topics", [])[:5] or [r["language"]] if r["language"] else [],
        "scene_id": "default",
        "stats": {
            "show": True,
            "stars": r["stars"],
            "language": r["language"],
            "platform": platform,
        }
    }


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
```

### 2.4 后端 API 新增

#### GET /api/project-templates/featured

```python
@router.get("/featured")
async def list_featured_templates(user: dict = Depends(get_current_user)):
    """返回优选项目模板（从 DB 读取，支持按平台筛选）"""
    from src.session_manager import db, db_lock
    platform = None  # 可从 query param 获取
    with db_lock:
        c = db.cursor()
        if platform:
            c.execute("SELECT * FROM featured_templates WHERE platform=? ORDER BY stars DESC", (platform,))
        else:
            c.execute("SELECT * FROM featured_templates ORDER BY platform, stars DESC")
        rows = c.fetchall()
    # 转为模板格式
    ...
```

### 2.5 前端展示

#### templates.js 适配

前端无需改动——新增的分类会在 `GET /api/project-templates` 返回的 `categories` 中自动出现。前端已有水平卡片 + 分页 + 收藏逻辑，直接渲染新分类。

#### 模板卡片展示统计信息

```javascript
// templates.js 中 _renderHorizontalCards 已有 stats 展示
// 新增 stats.show=true + stats.stars + stats.language + stats.platform
// 前端已有逻辑展示 stars/language
```

### 2.6 Cron 定时任务

#### 配置方式

在 Grape 的 `_refreshTimer` 机制中添加，或用系统 crontab：

```bash
# crontab -e
# 每天凌晨 2:00 同步优选项目模板（凭据走环境变量注释，见 2.1 节）
# crontab 中真实凭据放在 ~/.cronenv（chmod 600）里 source，避免写死进 git
0 2 * * * cd /home/grape/grape-new-version2 && source ~/.cronenv && .venv/bin/python scripts/sync_featured_templates.py --limit 100 >> /tmp/featured-sync.log 2>&1
```

或用 Grape 内置的 cronjob 机制（如果已部署 Hermes Agent）。

### 2.7 创建模板时的适配

`create_from_template` 中 `source: "git"` 的模板会调用 `push_template_to_repo`。对于优选模板：

- GitCode 优选：`source_repo` = GitCode 上的 `owner/repo`，clone 到本地后 push 到用户仓库
- GitHub 优选：`clone_url` = GitHub 上的 git URL，clone 后 push 到用户 GitCode 仓库

`push_template_to_repo` 需要适配 `clone_url` 字段（当前只支持 `source_repo`）：

```python
# templates.py 中 push_template_to_repo
clone_url = tpl.get("clone_url") or f"https://gitcode.com/{tpl.get('source_repo')}.git"
# 如果 source_platform == "github"，用 clone_url（GitHub）
# 如果 source_platform == "gitcode"，用 source_repo（GitCode）
```

## 3. 实施步骤

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1 | `src/api/db_setup.py` | 新增 featured_templates DDL + _ensure_column |
| 2 | MySQL RDS | `CREATE TABLE featured_templates` |
| 3 | `scripts/sync_featured_templates.py` | 新增爬取脚本 |
| 4 | `configs/project-templates.json` | 运行脚本后自动更新（iac 后新增两个分类） |
| 5 | `src/api/routes/templates.py` | `push_template_to_repo` 适配 clone_url |
| 6 | `src/api/static/js/pages/templates.js` | 卡片展示 stars/language/platform（如有需要） |
| 7 | crontab | 配置每天 02:00 定时执行 |
| 8 | 测试 | `--limit 5` 先获取 5 条测试 |

## 4. 验证用例

### 4.1 爬取脚本验证

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V1-GitCode搜索 | `python scripts/sync_featured_templates.py --limit 5 --platform gitcode` | 获取 5 个 GitCode 云原生项目，存入 DB |
| V2-GitHub搜索 | `python scripts/sync_featured_templates.py --limit 5 --platform github` | 获取 5 个 GitHub 云原生项目，存入 DB |
| V3-全部搜索 | `python scripts/sync_featured_templates.py --limit 5` | GitCode 5 + GitHub 5 = 10 个项目 |
| V4-DB写入 | 运行后查 `SELECT * FROM featured_templates` | 每条记录有 platform/repo_full_name/stars/html_url |
| V5-JSON更新 | 运行后查 `configs/project-templates.json` | iac 后面有 gitcode-featured + github-featured 两个分类 |
| V6-去重 | 重复运行 | 不产生重复记录（UNIQUE(platform, repo_full_name)） |
| V7-限流恢复 | GitHub 限流后重试 | 脚本等待后重试或跳过 |

### 4.2 API 验证

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V8-模板列表 | `GET /api/project-templates` | categories 包含 gitcode-featured + github-featured |
| V9-模板数量 | 检查 gitcode-featured.templates | 5 个模板（测试阶段） |
| V10-模板字段 | 检查模板字段 | 有 id/name/description/source/html_url/stats.stars |
| V11-创建模板 | `POST /api/project-templates/create` 用 GitHub 模板 | clone GitHub 仓库 → push 到用户 GitCode 仓库 → 托管 |

### 4.3 前端验证

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V12-页面展示 | 打开项目模板页 | IaC 后面有 GitCode 优选 + GitHub 优选两个分类 |
| V13-卡片内容 | 查看优选模板卡片 | 显示项目名/描述/stars/language |
| V14-分页 | 超过 12 个模板时分页 | 分页正常工作 |
| V15-创建 | 点击模板卡片创建 | 弹出创建对话框 → 创建成功 |

### 4.4 定时任务验证

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V16-cron执行 | `crontab -l` 检查 | 有每天 02:00 的条目 |
| V17-手动触发 | `python scripts/sync_featured_templates.py --limit 5` | 正常执行并更新 DB + JSON |
| V18-日志 | 检查 `/tmp/featured-sync.log` | 有搜索/保存/更新日志 |

### 4.5 自动化测试

```python
# tests/test_featured_templates.py

class TestSyncScript:
    """V1-V7: 爬取脚本验证"""

    def test_search_gitcode(self):
        """V1: GitCode 搜索返回结果"""
        from scripts.sync_featured_templates import search_gitcode
        results = search_gitcode(["kubernetes"], limit=5)
        assert len(results) > 0
        assert all(r["platform"] == "gitcode" for r in results)
        assert all(r["repo_full_name"] for r in results)
        assert all(r["html_url"] for r in results)

    def test_search_github(self):
        """V2: GitHub 搜索返回结果"""
        from scripts.sync_featured_templates import search_github
        results = search_github(["cloud native"], limit=5)
        assert len(results) > 0
        assert all(r["platform"] == "github" for r in results)
        assert all(r["stars"] > 0 for r in results)

    def test_db_unique(self):
        """V6: 重复运行不产生重复"""
        from scripts.sync_featured_templates import save_to_db
        items = [{"platform": "gitcode", "repo_full_name": "test/repo",
                  "repo_name": "repo", "description": "", "language": "Go",
                  "stars": 10, "forks": 5, "topics": [], "html_url": "",
                  "clone_url": "", "category": "cloud-native", "fetched_at": ""}]
        save_to_db(items)
        save_to_db(items)  # 重复
        # DB 中只有一条
        assert count_where(platform="gitcode", repo_full_name="test/repo") == 1


class TestFeaturedAPI:
    """V8-V11: API 验证"""

    def test_list_includes_featured(self, client, auth_header):
        """V8: 模板列表包含优选分类"""
        resp = client.get("/api/project-templates", headers=auth_header)
        cats = resp.json()["categories"]
        ids = [c["id"] for c in cats]
        assert "gitcode-featured" in ids
        assert "github-featured" in ids

    def test_featured_has_templates(self, client, auth_header):
        """V9: 优选分类有模板"""
        resp = client.get("/api/project-templates", headers=auth_header)
        cats = {c["id"]: c for c in resp.json()["categories"]}
        assert len(cats["gitcode-featured"]["templates"]) > 0
        assert len(cats["github-featured"]["templates"]) > 0
```

## 5. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 存储方式 | DB 表 + JSON 配置文件 | DB 记录完整数据；JSON 供前端读取（与现有模板系统一致） |
| 爬取频率 | 每天 02:00 | 避免频繁调用 API；夜间低峰 |
| 搜索关键词 | 云原生相关 12 个关键词 | 覆盖 k8s/terraform/docker/helm/istio/prometheus 等 |
| 每平台数量 | Top 100（测试 5） | 足够覆盖热门项目；测试阶段先 5 条 |
| 分类位置 | IaC 后面 | 用户要求 |
| 分类图标 | GitCode 🟢 / GitHub ⚫ | 平台品牌色 |
| 创建方式 | clone 源仓库 → push 到用户 GitCode | 与现有 source=git 一致 |
| 去重 | UNIQUE(platform, repo_full_name) | 避免重复爬取 |
| 限流处理 | sleep 1s between GitCode calls | GitCode 无明确限流但礼貌等待 |
| GitHub 认证 | 可选 token | 无 token 用匿名（60次/小时）；有 token 5000次/小时 |

## 6. 风险与回退

### 6.1 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| GitCode API 变更 | 低 | 爬取失败 | 脚本捕获异常跳过 |
| GitHub 限流 | 中 | 获取不全 | 等待重试或用 token |
| 项目不适合做模板 | 中 | 用户困惑 | 人工筛选关键词 |
| clone 大仓库超时 | 中 | 创建失败 | 设置 clone 超时 + shallow clone |

### 6.2 回退方案

1. 删除 `featured_templates` 表 + JSON 中的两个分类
2. 脚本可独立运行/停止，不影响现有模板系统
3. 前端无需改动（分类动态从 API 返回）
