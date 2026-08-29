# GRP-DESIGN: 博客 skill 开发工具链方案

## 一、概述

在 hello-skill 仓新增两个 skill，专门面向博客系统的 skill 开发与测试：

- **blog-skill-creator**：基于用户需求 + 博客 API 文档，自动生成博客管理 skill
- **blog-skill-tester**：测试生成的博客 skill

走 grape A2A 链路（vod→issue→coding→qa→sre），与 hcs-* skill 同级。
生成的博客 skill 支持个人/项目级别发布（复用 skill-publish）。
后续上传到个人仓库使用，不依赖 grape 的 agent-skill-mapping.json 场景路由。

## 二、与现有 skill-creator/tester 的区别

| 维度 | skill-creator（华为云） | blog-skill-creator（博客） |
|------|------------------------|---------------------------|
| 目标 API | 华为云服务（ECS/VPC/OBS 等） | 博客系统 REST API |
| 调研模式 | CLI→SDK→OpenAPI 三级回退 | OpenAPI/Swagger→Markdown 文档→基础信息三级回退 |
| 认证 | AK/SK（华为云动态扫描） | Basic Auth（用户名+密码），后续扩展 Token/API Key |
| 调用方式 | hcloud CLI / Python SDK / curl | curl/requests（REST API） |
| 生成物 | Python 脚本调 huaweicloudsdk | Python 脚本调 requests |
| 凭据变量 | HUAWEI_AK / HUAWEI_SK | BLOG_USERNAME / BLOG_PASSWORD |
| 测试凭据 | AK/SK 环境变量 + repo-credentials.json | BLOG_USERNAME/PASSWORD 环境变量 + repo-credentials.json |
| **子命令** | **由 API 调研结果决定（不预设）** | **由 API 文档解析结果决定（不预设）** |

## 三、核心设计原则：不预设 API 结构

blog-skill-creator 是通用的 REST API skill 生成器，**不预设博客 API 的端点结构、子命令数量或操作类型**。

子命令、端点路径、参数定义全部由用户提供的 API 文档动态决定：
- 用户提供 OpenAPI/Swagger 文档 → 自动解析所有端点 → 每个端点生成一个子命令
- 用户提供 Markdown 文档 → agent 理解后生成子命令
- 用户提供基础信息 → agent 用 curl 探测确认后生成子命令

**示例**：如果博客 API 文档定义了以下端点：
```
GET    /api/articles          → list-articles
POST   /api/articles          → create-article
GET    /api/articles/{id}     → show-article
PUT    /api/articles/{id}     → update-article
DELETE /api/articles/{id}     → delete-article
GET    /api/tags              → list-tags
POST   /api/tags              → create-tag
GET    /api/categories        → list-categories
```
则生成的 skill 有 8 个子命令，名称和数量完全由文档决定。

如果另一个博客系统的端点是 `/api/posts` / `/api/blogs`，生成的子命令就相应变化。

## 四、blog-skill-creator

### 文件结构

```
skills/blog-skill-creator/
├── SKILL.md
├── scripts/
│   └── create_blog_skill_pr.sh    # git commit + push + 创建 PR
└── references/
    ├── api-research-guide.md      # REST API 调研指南
    ├── gitcode-api-reference.md   # GitCode API 参考
    └── blog-skill-template.md     # 博客 skill 脚本生成模板
```

### 脚本复用方式

blog-skill-creator/scripts/create_blog_skill_pr.sh：从 grape 仓 skills/skill-creator/scripts/create_skill_pr.sh 复制一份独立副本到 hello-skill 仓。原因：
- hello-skill 仓和 grape 仓是独立仓库，不能跨仓引用
- create_skill_pr.sh 是自包含的（不依赖外部库），复制即可用
- 后续 hello-skill 仓的个人 skill 不依赖 grape 仓的脚本

### 核心流程（与 skill-creator 对齐）

1. **查重**：检查仓库是否已有同名 skill
2. **需求澄清**：确认博客系统 API 地址、认证方式、用户提供 API 文档的形式
3. **技术调研**（REST API 文档三级回退）：
   - 优先：用户提供 OpenAPI/Swagger 文档（JSON/YAML）→ 自动解析全部端点/参数/响应结构
   - 回退：用户提供 Markdown 格式 API 文档 → agent 阅读理解，提取端点/参数/响应
   - 最后：用户提供 API 基础信息（base_url + 口头描述端点）→ agent 用 curl 逐个探测确认
   - **解析结果**：得到端点列表（method + path + 参数 + 响应结构），每个端点对应一个子命令
   - 时间盒：每端点 ≤5 分钟，超时标记 ⛔
   - 失败门控：所有端点都 ⛔ → 终止，不生成任何文件
4. **骨架生成**（基于解析结果动态生成，不预设结构）：
   - SKILL.md（frontmatter + 使用说明 + 参数确认表 — 由解析的端点列表生成）
   - scripts/{name}.py（Python 脚本 — 子命令数量和参数由解析结果决定）
   - templates/test-vars.json（测试用例 — 每个子命令一条测试）
   - references/api-reference.md（API 端点文档 — 记录解析到的端点列表）
5. **自测**：用 Basic Auth 凭据真实调用一次只读端点（如 GET 列表），确认 API 可达
6. **创建 PR**：调 create_blog_skill_pr.sh
7. **A2A 交棒**：通知 blog-skill-tester 测试

### 子命令生成规则（动态，不预设）

API 文档解析后，按以下规则生成子命令：

| API 端点特征 | 子命令名生成规则 | 示例 |
|-------------|----------------|------|
| GET /api/{resource} | list-{resource} | GET /api/articles → list-articles |
| POST /api/{resource} | create-{resource} | POST /api/articles → create-article |
| GET /api/{resource}/{id} | show-{resource} | GET /api/articles/42 → show-article |
| PUT/PATCH /api/{resource}/{id} | update-{resource} | PUT /api/articles/42 → update-article |
| DELETE /api/{resource}/{id} | delete-{resource} | DELETE /api/articles/42 → delete-article |
| 其他端点 | 按 path 末段 + method 生成 | GET /api/search → search |

- resource 名取 path 中的资源段（复数转单数）
- 如果文档已定义 operationId / summary，优先用文档指定的名称
- 子命令参数由 API 文档的 parameters / requestBody 定义决定

### 生成的脚本凭据读取规范

```python
def _load_credentials():
    """动态扫描环境变量获取博客系统认证凭据。"""
    username, password = '', ''
    for k, v in os.environ.items():
        u = k.upper()
        if u.startswith('BLOG') or u.startswith('BLOG_'):
            if 'USERNAME' in u or u.endswith('_USER'):
                username = v or username
            if 'PASSWORD' in u or u.endswith('_PASS'):
                password = v or password
    return username, password
```

- 环境变量缺失则退出码 3
- 支持 repo-credentials.json 的 secrets.BLOG_USERNAME / secrets.BLOG_PASSWORD 回退

### 生成的脚本调用模式

```python
# 使用 requests 库调 REST API + Basic Auth
import requests
from requests.auth import HTTPBasicAuth

auth = HTTPBasicAuth(username, password)
resp = requests.post(f"{base_url}/api/articles", json=payload, auth=auth, timeout=30)
```

- base_url 从环境变量 BLOG_API_BASE_URL 读取（或 API 文档中指定的地址）
- 超时 30s
- 退出码：0=成功；2=参数错误；3=缺少认证；4=API 调用失败

## 五、blog-skill-tester

### 文件结构

```
skills/blog-skill-tester/
├── SKILL.md
├── scripts/
│   ├── spec_check.py              # 规范检查（从 grape 仓复制独立副本）
│   └── merge_skill_pr.sh          # 合并脚本（从 grape 仓复制独立副本）
└── references/
    ├── test-report-format.md      # 测试报告格式
    └── gitcode-api-reference.md   # GitCode API 参考
```

### 脚本复用方式

从 grape 仓 skills/skill-tester/scripts/ 复制 spec_check.py 和 merge_skill_pr.sh 到 hello-skill 仓 blog-skill-tester/scripts/。原因同上：独立仓库不能跨仓引用，脚本自包含，复制即可用。

### 与 skill-tester 的区别

| 维度 | skill-tester（华为云） | blog-skill-tester（博客） |
|------|----------------------|--------------------------|
| 凭据检测 | HUAWEI_AK/SK 环境变量 + repo-credentials.json | BLOG_USERNAME/PASSWORD 环境变量 + repo-credentials.json |
| 只读测试 | 真实调华为云 API（list-buckets 等） | 真实调博客 API（list-* / show-* 等只读子命令） |
| 变更类测试 | 需用户确认（Create/Delete 资源） | 需用户确认（Create/Delete 文章等） |
| 规范检查 | spec_check.py（frontmatter/结构/mock 检测） | 复用 spec_check.py（相同规范） |
| 合并脚本 | merge_skill_pr.sh | 复用 merge_skill_pr.sh |
| **子命令识别** | 固定 10 个子命令 | **动态识别**：读 templates/test-vars.json 获取子命令列表 |

### 测试策略（动态适配生成的子命令）

- **只读查询类**（GET 方法端点：list-* / show-*）→ 自动真实调用 API
- **变更类**（POST/PUT/DELETE 方法端点：create-* / update-* / delete-*）→ 逐项确认后真实执行 + 记录资源变更
- **资源生命周期测试**：create-{resource} → show-{resource} 验证 → delete-{resource} 清理（每步用户确认）
- 子命令列表从 templates/test-vars.json 读取（creator 生成时写入），不写死

## 六、A2A 链路

```
Issue（用户提需求"创建博客管理 skill"）
  → vod（需求澄清）
  → issue（分类/标签/Spec）
  → coding（blog-skill-creator 生成 skill + PR）
  → qa（blog-skill-tester 测试 + 合并）
  → sre（skill-publish 发布到远程仓库）
```

### 场景路由

grape 托管仓库走 A2A 链路时，通过 agent-skill-mapping.json 的 `blog-skill-dev` 场景路由到 blog-skill-creator/tester。
个人仓库使用时不走 A2A，不需要 agent-skill-mapping.json 配置。

## 七、发布

生成的博客 skill 发布到远程仓库时：
- **个人级别**：发布到用户个人仓库（如 yangaiwu/blog-skills）
- **项目级别**：发布到项目仓库（如 signway/grape_skill）
- 复用 skill-publish 的 sync_skill_platform.sh 脚本 + ClawHub API 发布

## 八、子命令动态决定的后续优化方向

当前方案：子命令在生成时由 API 文档解析决定，硬编码到生成的 Python 脚本里（和 hcs-* skill 一样用 argparse subparsers）。

后续可优化为：
1. **外部配置驱动**：子命令从 skill.json / skill-config.yaml 声明文件读取，脚本动态加载。API 变更后只需改配置文件，不改脚本代码。
2. **API 文档重新解析**：API 变更后重新跑 blog-skill-creator，自动 diff 新旧端点，增量更新子命令（而非全量重新生成）。
3. **运行时自发现**：脚本启动时调 GET / 或 GET /api（OpenAPI endpoint）自动发现可用端点，动态注册子命令。依赖博客系统暴露 OpenAPI 文档端点。

## 九、改动清单

| 文件 | 类型 | 说明 |
|------|------|------|
| skills/blog-skill-creator/SKILL.md | 新建 | 博客 skill 生成器（通用 REST API，不预设端点） |
| skills/blog-skill-creator/scripts/create_blog_skill_pr.sh | 新建 | 从 grape 仓 create_skill_pr.sh 复制独立副本 |
| skills/blog-skill-creator/references/api-research-guide.md | 新建 | REST API 调研指南（OpenAPI/Markdown/探测三级） |
| skills/blog-skill-creator/references/gitcode-api-reference.md | 新建 | GitCode API 参考 |
| skills/blog-skill-creator/references/blog-skill-template.md | 新建 | 博客 skill 脚本生成模板（动态子命令模板） |
| skills/blog-skill-tester/SKILL.md | 新建 | 博客 skill 测试器（动态子命令测试） |
| skills/blog-skill-tester/scripts/spec_check.py | 新建 | 从 grape 仓复制独立副本 |
| skills/blog-skill-tester/scripts/merge_skill_pr.sh | 新建 | 从 grape 仓复制独立副本 |
| skills/blog-skill-tester/references/test-report-format.md | 新建 | 测试报告格式 |
| skills/blog-skill-tester/references/gitcode-api-reference.md | 新建 | GitCode API 参考 |

## 十、确认点

1. ~~放 hello-skill 仓是否合适？~~ → 确认，后续上传到个人仓库
2. ~~agent-skill-mapping.json 场景路由？~~ → 个人仓库不需要，grape 托管仓库才配置
3. ~~脚本复用怎么复用？~~ → 从 grape 仓复制独立副本到 hello-skill 仓（自包含，不跨仓引用）
4. ~~认证方式？~~ → 先 Basic Auth，后续扩展
5. ~~子命令动态决定可否优化？~~ → 当前生成时硬编码（和 hcs-* 一致）；后续优化方向见第八章（外部配置驱动 / API 重新解析 / 运行时自发现）
