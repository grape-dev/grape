# GRP-DESIGN: help.html 页面内容改由 help/ 目录动态提供

> Issue：#97（help 文档独立目录，已合入）→ 本期将 `/static/help.html`（当前硬编码 435 行静态页）改造为"壳页面"，其内容全部由仓库根目录 `help/`（usage + api markdown）提供。
> 状态：设计稿（spec-ready 待审）

---

## 1. 背景与诉求

Issue #97/PR #100 已把系统帮助文档从 `src/api/static/help.html` 拆分到：

- `help/usage/`：9 个使用指南 markdown（overview/quickstart/agents/pipeline/skills/runtimes/monitoring/knowledge-base/faq）
- `help/api/`：13 个 API 文档 markdown（源自 `docs/openapi/`）

但拆分后 **`help.html` 仍保留整份硬编码 HTML 内容**（435 行），与 `help/` 下的 markdown 形成**双份维护**：改 md 不生效、改 help.html 与 md 漂移。

**本期诉求**：`/static/help.html` 这个静态页面继续存在、URL 与前端引用不变，但页面内容**改由项目根目录 `help/` 提供**——端到端消除双份来源，`help/` 成为唯一事实源。

## 2. 现状盘点

| 项 | 结论 |
|----|------|
| help.html 位置 | `src/api/static/help.html`，经 `main.py:483` `/static` 挂载对外 |
| 前端引用点 | `index.html:92` 直链 `/static/help.html`（target=_blank）；`app.js:575` 帮助抽屉 iframe 加载同一路径 |
| help/usage 内容形态 | 纯 markdown，**无残留 HTML 标签**（`tests/test_help_directory.py` 有硬约束）；H1 自带章节标题（如 overview 的 H1 = "Grape 使用指导文档"） |
| help/api 内容形态 | 与 docs/openapi 逐字节一致；正文仅有行内 code 占位（`<api_key>` 等均在反引号内），无真实 HTML 标签 |
| markdown 渲染依赖 | **无**：requirements.txt 无 markdown/mistune；本地环境仅有传递安装的 markdown-it-py（不可依赖） |
| 启动钩子 | `main.py:18` `lifespan()` 内已有"模板自愈"等启动检查，可平级挂 help 生成 |
| 部署形态 | 生产 git pull → sync_skills/sync_templates → `systemctl restart grape`；服务以 root 运行，`/opt/grape` 可写 |

## 3. 方案对比

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|------|------|------|------|------|
| **A. 启动期生成静态 help.html**（推荐） | 新增生成模块，启动时读 `help/**/*.md` 渲染并覆写 `src/api/static/help.html`；前端/URL 零改动 | 单一事实源=help/；零前端改动；无外部 CDN；改 md 重启即生效；部署零手工步骤 | 启动时写盘一次；`help.html` 变为生成物需治理（§4.2） | ✅ 推荐 |
| B. help.html 变 JS 壳 + 后端 API 渲染 | 新增 `/api/help/*`（列表 + 内容），壳页 fetch 渲染 | 无需启动写盘；content 可复用给其他入口 | 前端大改、新增路由/鉴权豁免、help.html 依赖网络交互；复杂度高 | 备选 |
| C. 纯前端 CDN marked + fetch 原始 md | 壳页拉 `help/*.md` 客户端渲染 | 后端零改动 | 内网/离线不可用；渲染质量与 XSS 依赖第三方 CDN；与"静态页仍在"心智冲突 | 不采用 |

**结论**：选 **方案 A** —— 最贴合"静态网页还在、内容来自 help/"的诉求，且与仓库"pull→restart"部署形态天然契合。

## 4. 方案 A 详细设计

### 4.1 依赖

`requirements.txt` 增加：

```
markdown>=3.4
```

- python-markdown：纯 Python、无原生依赖、支持扩展 `fenced_code`/`tables`/`toc`/`sane_lists`/`nl2br`，满足 usage(纯 md) 与 api(表格/代码块) 的渲染。
- 备选 `markdown-it-py`（本地已有传递安装），主选 markdown 以降低最低引入面。

### 4.2 ★ 生成物治理决策（关键点）

`help.html` 当前是 **git tracked** 文件。启动期覆写会产生两个问题：

1. 部署目录每次 restart 后 `git status` 出现 `M src/api/static/help.html`
2. 若远端未来改动 help.html，pull 与本地生成内容冲突

**决策（推荐）**：将 `help.html` 转为**生成物（generated artifact）**：

- `git rm --cached src/api/static/help.html` + 在 `.gitignore` 增加 `src/api/static/help.html`
- `help/` 成为唯一受版本管理的内容源
- 首次部署/克隆后服务启动即生成；`git pull` 不再触碰该文件

**备选（若团队希望仓库内保持即时可访问的 help.html）**：保留 tracked，但约定"改 md 必须同时重新生成并提交 help.html"——这仍会回到双份维护，不推荐。

> 对测试的影响与兜底：`tests/test_help_directory.py` 的 `test_help_html_exists` 仍在工作区校验（本地有生成物即通过）；建议新测试在 fixture 中先调用一次 `generate()`，保证 CI/克隆环境可运行。

### 4.3 生成模块（新文件）

`src/help_page_gen.py`（或 `src/api/help_page.py`，按仓库分层习惯定）：

```python
USAGE_ORDER = [
    "overview.md", "quickstart.md", "agents.md", "pipeline.md",
    "skills.md", "runtimes.md", "monitoring.md", "knowledge-base.md", "faq.md",
]
# API 文档顺序：README 置顶，其余按文件名（与 tests/test_help_directory.py 的 EXPECTED_* 保持一致）
API_ORDER = sorted(...)   # README.md 提至首位

def render_markdown(text: str) -> str:
    import markdown
    return markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "toc", "sane_lists", "nl2br"],
    )

def build_help_html() -> str:
    """读 help/usage + help/api，渲染 markdown，拼装为完整 help.html（UTF-8 无 BOM）。"""

def generate(help_dir=..., out_path=...):
    """写 src/api/static/help.html；返回值用于日志/自愈提示。"""
```

页面壳保留现有视觉体系（`--bg/--c/--b/--t1/--t2/--t3/--a` 主题、`.wrap/.grid/.toc`、`pre/code/table/blockquote` 样式、`.back-top` 回到顶部、移动端响应式），改造点：

- **左侧目录导航**：根据 usage 9 章 H1 + API 13 文件生成锚点链接；保留现有章节序号语义
- **主内容**：usage 章节 → api 章节拼接渲染 HTML；合法 UTF-8、无 BOM
- **锚点**：用 `toc` 扩展的 slug 规则生成 `<h1/h2/h3 id="...">`，保证目录跳转稳定
- **保留迁移注释**：`<head>` 内"本文档由 help/ 生成，请编辑 help/"说明（满足既有测试 `test_migration_comment_in_head`）

### 4.4 启动钩子

`main.py` `lifespan()`（紧邻现有"模板自愈"块）新增：

```python
try:
    from src.help_page_gen import generate as generate_help_page
    _n = generate_help_page()
    log.info(f"Help: help.html regenerated ({_n} chapters/md files)")
except Exception as e:
    log.warning(f"Help: help.html generation failed (use shipped last good): {e}")
```

- 失败仅 warning，不阻断启动（保留上一次已生成文件）
- 服务重启即刷新内容，部署文档（DEPLOYMENT-REMOTE.md）补充一句"git pull 后 restart 即自动重生成 help.html"

### 4.5 渲染规则与视觉还原

- usage md 纯 markdown：标题、列表、引用块、代码块、表格直接渲染
- **保留原有提示卡片视觉（增强项，非阻塞）**：生成器对 `> 💡`/`> ⚠️` 开头的引用块后处理，套 `.card tip` / `.card warn` / `.card danger` 样式，还原原 help.html 的提示卡片观感
- FAQ 章节：md 中的 `### Q*` 渲染为普通条目（原 HTML 有 JS 折叠交互，md 无法表达；保守起见默认全部展开，折叠交互列为可选增强）
- API 章节：直接渲染表格 + `fenced_code`，确保 `docs/openapi` 的 13 份文档原样可读

### 4.6 安全

- 渲染内容为**仓库自维护**的 markdown（信任域内，非用户输入），不引入用户可控注入面
- python-markdown 默认保留原始 HTML：当前 usage/api 源均无真实 HTML 标签（有测试硬约束），风险低；不引入 sanitize 依赖
- 生成文件路径固定，不接受外部参数拼接

### 4.7 测试

新增 `tests/test_help_page_generation.py`（或扩展 `test_help_directory.py`）：

1. fixture 中先调用 `generate()` → `help.html` 存在、无 BOM、UTF-8 可干净解码
2. 每个 `help/usage/*.md` 的首个 H1 文本均出现在生成的 help.html
3. usage 9 个 + api 13 个文件名全部出现在 help.html 目录区
4. 渲染后的代码块/表格结构存在（如 api/10-git-repos.md 的表格行出现）
5. 既有 `tests/test_help_directory.py` 全部保持通过（存在 + migration 注释约束不变）

### 4.8 文件清单（增量）

| 文件 | 动作 | 说明 |
|------|------|------|
| `src/help_page_gen.py` | 新增 | 渲染 + 拼装生成模块 |
| `src/api/main.py` | 修改 | lifespan 增加 help 生成钩子 |
| `requirements.txt` | 修改 | 增加 `markdown>=3.4` |
| `.gitignore` | 修改 | 增加 `src/api/static/help.html`（生成物） |
| `src/api/static/help.html` | 移除跟踪（`git rm --cached`）+ 由生成物托管 | 首次 git 还需保留一个"初始生成物"供纯静态环境回退（可选） |
| `tests/test_help_page_generation.py` | 新增 | 见 §4.7 |
| `DEPLOYMENT-REMOTE.md` | 修改 | 部署/回滚说明补一段 |

## 5. 验收标准

- [ ] `help/`（usage 9 + api 13）为唯一内容源；删除任意 usage 章节后重启，help.html 目录/正文同步消失
- [ ] 修改任一 `help/**/*.md` 并 restart，`/static/help.html` 内容即时更新（无需手工编辑 HTML）
- [ ] `index.html:92` 直链与 `app.js:575` 抽屉 iframe 均可正常打开渲染页，视觉与现版基本一致（提示卡片若未做可选增强至少为可读的引用块）
- [ ] 部署目录 restart 后 `git status` 不含 `src/api/static/help.html`（生成物已忽略）
- [ ] 新增测试 + 既有 `test_help_directory.py` 全绿
- [ ] 无外部 CDN 依赖，内网可访问

## 6. 排期（P2）

| 阶段 | 内容 |
|------|------|
| M0 | 引入 markdown 依赖 + 生成模块渲染 usage 9 章，人工比对正文完整性 |
| M1 | 加 api 13 章渲染 + 目录导航 + 样式壳，产出与现版对应用户可读 |
| M2 | 生成物治理（git rm --cached + gitignore）+ 启动钩子 |
| M3 | 新增测试 + 部署文档 + 回归 `test_help_directory.py` |
| M4 | 部署验证（生产 restart 后 help 可达、内容为最新 md） |