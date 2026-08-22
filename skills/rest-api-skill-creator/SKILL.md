---
name: rest-api-skill-creator
version: 0.2.0
description: |
  按需求自动创建 REST API 管理 skill：查重 → 读取 Spec → OpenAPI/文档/基础信息三级调研 →
  骨架生成（脚本+SKILL.md+端点文档+测试用例）→ 自测 → 创建 PR → A2A 交棒 QA Agent 测试。
  通用不限目标系统（博客/CMS/Wiki/商城），认证方式按 Spec 确认。
  凭据变量名由 skill name 推导前缀（如 blog-manager → BLOG_MANAGER_*）。
tools:
  - curl
  - git
---

# REST API Skill Creator（通用 REST API skill 生成器）

## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 $BOT_GITCODE_TOKEN，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。
5. 所有 curl / git 调用设 30s 超时（curl --max-time 30）——超时判定失败，不 hang。
6. **调用 GitCode API 一律以 `references/gitcode-api-reference.md` 为准**：严禁根据经验/记忆/猜测自行拼写 API 路径。

## 核心原则

### 专注创建 REST API 管理 skill

负责查重、需求澄清、API 文档调研、骨架生成、创建 PR。不负责 Issue 分类、打标签、分配负责人。

### 不预设 API 结构

生成的子命令数量、端点路径、参数定义全部由用户提供的 API 文档动态决定。不预设"一定有 create-post / list-posts"。

### 依赖前序分析

通过 A2A 被调用时，prompt 包含前序 Agent 的处理摘要。必须通过 GitCode API 获取 Issue 完整信息。

### 禁止探索 Grape 内部

所有 Issue/仓库信息通过 GitCode API 获取，不读取 data/、configs/、sessions.db 等 Grape 内部数据。

## 评论规范

- 所有 Issue/PR 评论 body 必须以 `[Coding Agent]` 开头
- 引用用户原文：`> {用户评论}\n\n[Coding Agent] {回复}`

## Configuration

本 skill 无外部 API 地址配置需求。GitCode API token 从环境变量 $BOT_GITCODE_TOKEN 获取，目标系统凭据由生成的 skill 自身管理。

## 场景 / When to Use

| 触发场景 | 执行流程 |
|---------|---------|
| worktree 无 skills/{name}/ 目录 → 新建 skill | Step 0 → Step 1 → Step 2 → Step 3 → Step 3.5 → Step 4 → Step 5 → Step 6 |
| worktree 已有 skills/{name}/ 目录 → 修复 skill（tester 回退后重新执行） | Step 0（跳过查重）→ Step 1（跳过确认）→ Step 2 → Step 3（更新产物）→ Step 3.5 → Step 4 → Step 5 → Step 6 |
| 远程已有同名 skill → 已存在 | 回复「已存在，可复用/更新」——不重复造 |

Don't use for: 测试 skill（tester 职责）/ 发布 skill（publish 职责）/ Issue 分类打标签（Issue Agent 职责）

## 知识 / Knowledge

### 前置条件

1. **Python 3.8+** with `requests` package available（生成的脚本依赖 requests 调 REST API）
2. **用户提供目标系统 API 文档**（OpenAPI/Swagger JSON/YAML 或 Markdown 格式或基础信息描述）
3. **目标系统可访问**（API base_url 可达，凭据可用）

### 环境说明

```
· 工作区：worktree 已就绪（分支 feat/issue-{N}），可直接 git add / commit / push
· 令牌已注入（不用查 env）：
  $BOT_GITCODE_TOKEN / $REPO_FULL_NAME / $ISSUE_NUMBER / $BOT_USERNAME
· 目标系统凭据（自测用，前缀由 skill name 推导）：
  ${PREFIX}_BASE_URL（必须，所有认证方式都需要）
  ${PREFIX}_USERNAME / ${PREFIX}_PASSWORD（Basic Auth）
  ${PREFIX}_TOKEN（Bearer Token）
  ${PREFIX}_API_KEY（API Key）
  无认证模式只需 BASE_URL
· 脚本路径：
  bash {{SKILL_DIR}}/rest-api-skill-creator/scripts/create_rest_api_skill_pr.sh "${SKILL_NAME}"
```

### 硬规则

| 行为 | 态度 |
|------|------|
| 创建 PR 后必须在 Issue 发评论告知 | ✅ **必须做，不可跳过** |
| 创建 PR 后必须通过 A2A 通知测试 Agent | ✅ **必须做，不可跳过** |
| A2A 必须携带 issue_number 和 pr_number | ✅ **必须做** |
| 一个 PR 只改一个 Skill 目录 | ✅ **必须做** |
| API 端点只认文档解析/curl 探测结果 | ✅ **必须做** |
| 按命名模式猜测/推断 API 端点 | ❌ **禁止** |
| 直接合并 PR | ❌ **禁止**（仅创建，交 tester 判断） |
| 读取 Grape 内部数据 | ❌ **禁止** |
| 生成含 mock 模式/假数据的脚本 | ❌ **禁止** |
| 预设子命令（不根据 API 文档动态生成） | ❌ **禁止** |

### Skill 规范

生成的 SKILL.md 必须符合 `references/skill-spec.md` 规范——生成前先读取该文件了解段落要求。

rest-api-skill-creator 通过 grape A2A 链路触发，生成的 skill 参与 A2A 链路（creator → tester → SRE Agent 发布），默认生成**系统级 skill**——除标准段落外额外生成系统级段落。

必填段落（缺失 = validate-skill.sh 失败）：

| 段落 | 说明 |
|------|------|
| 安全硬约束 | token 不写文件/不读内部数据/curl --max-time 30/调用 API 以 references 为准 |
| 评论规范 | [$AGENT_NAME] 开头 + 引用原文（未注入时用 skill name） |
| Configuration | base_url 解析 4 级优先级 + 配置指导（环境变量方式 + 项目知识 JSON 方式 + 示例） |
| YAML Frontmatter | name + description + version（x.y.z 格式，如 0.1.0）+ triggers + tags（≤5） |
| 场景 / When to Use | 适用场景 + Don't use for 反例。从需求提取 |
| 知识 / Knowledge | API 技术栈 + 认证方式 + base_url 优先级 + 端点表 + Common Pitfalls + 字段说明 |
| 步骤 / Steps | 主流程按序号（Step 1/2/3），每个操作是子步骤，含 curl 示例 + Expected 返回值 |
| 判断标准 / Verification | Verification Checklist（checkbox 格式） |
| 输出规范 / Output Format | JSON / Markdown 表格 / curl 示例格式 |
| 参考文档 / References | references/ 下文档链接 |
| 分析结论示例 | 成功/失败两种场景的分析结论输出参考 |
| 任务结束 | JSON 块（actions/conclusion/artifacts/next_step/issue_summary） |

可选段落（按需添加）：

| 段落 | 说明 |
|------|------|
| 判断标准 / Verification | Verification Checklist（checkbox 格式） |
| 输出规范 / Output Format | JSON / Markdown 表格 / curl 示例格式 |

### 边界情况

- 用户跳过提问直接说「开始」→ 提醒：需求澄清必须先完成
- 凭据环境变量缺失或为空，且 .project-info/ 下 JSON 配置文件中也无 → 提示用户提供；用户不提供 → 终止流程
- API 调研超 5 分钟未确认端点 → 标记 ⛔（API 不存在）→ 走失败门控
- 无法修复 → 在 Issue 评论说明失败原因，不创建 PR，不交棒 tester

### 分析结论示例

```
分析结论：
- 场景：新建 skill（从 Issue Agent 触发）
- 需求：创建管理 {目标系统} API 的 skill
- 技术模式：REST API（无认证）
- 操作类型：只读查询 + 变更类
- 必须执行：查重 → 需求确认 → API 调研 → 骨架生成 → 自测 → 创建 PR → A2A 交棒 tester
```

## 步骤 / Steps

### Step 0: 查重

检查目标仓库 skills/ 目录是否已有同名 skill：

```bash
curl -s --max-time 30 -H "PRIVATE-TOKEN: $BOT_GITCODE_TOKEN" \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/contents/skills" 2>/dev/null
```

- 远程已存在 → 回复「已存在——可复用/更新」——不重复造
- 不存在 → 继续

### Step 1: 需求澄清

从 Issue 获取完整内容。如果用户填写了需求模板（`templates/requirement-template.md`），直接从模板提取信息；否则按以下维度逐一澄清：

1. **目标系统 API 地址** — base_url（如 https://blog.example.com/api）
2. **认证方式** — Basic Auth（用户名+密码）/ Bearer Token / API Key（Header 或 Query）/ 无认证（公开 API）
3. **API 文档形式** — OpenAPI/Swagger 文件 / Markdown 文档 / 口头描述
4. **需要哪些操作** — 用户期望的管理能力（发布/编辑/删除/查询/分类管理等）

凭据变量前缀推导规则：skill name 转大写 + 下划线（如 `blog-manager` → `BLOG_MANAGER`），生成的脚本扫描该前缀的环境变量：
- Basic Auth：`{PREFIX}_USERNAME` + `{PREFIX}_PASSWORD`
- Bearer Token：`{PREFIX}_TOKEN`
- API Key：`{PREFIX}_API_KEY`
- 无认证：不需要凭据变量

覆盖所有维度后 → 展示需求摘要表（含认证方式 + 凭据变量名）→ 等用户确认。

### Step 2: 技术调研（REST API 文档三级回退）

详细调研方法见 `references/api-research-guide.md`。

核心规则：**API 端点只认文档解析或 curl 探测结果，严禁猜测**。

- 优先：用户提供 OpenAPI/Swagger 文档 → 自动解析全部端点/参数/响应结构
- 回退：用户提供 Markdown 文档 → agent 阅读理解，提取端点/参数/响应
- 最后：用户提供基础信息 → agent 用 curl 逐个探测确认

解析结果得到端点列表（method + path + 参数 + 响应结构），每个端点对应一个子命令。

- 时间盒：每端点 ≤5 分钟，超时标记 ⛔
- 失败门控：所有端点都 ⛔ → 终止，不生成任何文件

【真实 API 验证】：凭据可用时，用 curl 真实调用一次只读端点（如 GET 列表），确认 API 可达 + 返回数据结构。
- 凭据获取：扫描 {PREFIX} 开头环境变量（由 skill name 推导前缀）；环境变量缺失时递归扫描 .project-info/ 下所有 JSON 文件
- 凭据缺失 → 提示用户提供；不提供 → 终止流程
- 真实调用成功 → 记录返回数据结构用于骨架生成
- 真实调用失败 → 标记 API 不可用，走失败门控

### Step 3: 骨架生成

> 生成前先读取 `references/skill-spec.md` 了解 Skill 规范要求。

目录：`skills/{name}/`（name=小写连字符）

- `SKILL.md`（≤500 行，按 skill-spec.md 规范生成——必填段落：场景/知识/步骤 + 可选：判断标准/输出规范）
- `scripts/{name}.py`（Python 入口 — 所有代码放 scripts/ 目录下，不生成根目录 main.py。端点少（<10）用单文件；端点多（≥10）用 `scripts/{name}.py` 入口 + `scripts/{name}/` 包目录做模块化。子命令数量和参数由 API 文档解析结果决定）
- `templates/test-vars.json`（测试用例 — 每个子命令一条测试，结构见下方）
- `references/api-reference.md`（API 端点文档 — 记录解析到的端点列表）
- `requirements.txt`（Python 依赖声明 — 至少含 `requests>=2.20.0`）

⚠️ **只生成以上 5 个文件/目录**，不生成 tests/、根目录 main.py、README.md、requirements-dev.txt、.gitignore 等额外文件。agent 不得自作主张添加生成清单之外的文件。

**脚本生成约束（必须遵守）**：

1. `--format` 参数必须加到每个子命令的 parser 上（`add_common_args(子parser)`），**不能加到全局 parser**。这样用户可以在子命令后面使用 `--format md`（如 `python3 {name}.py articles-list --format md`）。
2. `requirements.txt` 必须用真实换行符（`\n`），不能写字面量 `\\n`。
3. 脚本必须按 `references/rest-api-skill-template.md` 的 Python 脚本模板结构生成——`add_common_args` 函数 + `sub.add_parser` + `add_common_args(p_N)` 模式。
4. 脚本必须用 `requests` 库调 REST API，**不能用 `urllib`**。模板的 API client 代码基于 requests 编写（认证/请求/错误处理），生成物必须与模板一致。`requirements.txt` 必须声明 `requests>=2.20.0`。
5. SKILL.md 中的 API 地址一律用 `{base_url}` 占位符，**不硬编码真实地址**。Configuration/步骤/知识段落里的示例地址用 `(示例)` 标注。`references/api-reference.md` 同样不硬编码真实地址，用 `{base_url}` 占位符。

**test-vars.json 结构**（creator 生成时按此结构写入，tester 读取时按此结构解析）：

test-vars.json 只含测试用例和元数据，**不含真实地址/凭据值**——可以安全提交到 git。真实地址和凭据从环境变量/项目知识获取。

```json
{
  "skill_name": "{skill_name}",
  "entry_file": "scripts/{skill_name}.py",
  "test_cases": [
    {
      "id": "TC-01",
      "name": "health-check",
      "command": "python3 scripts/{skill_name}.py health-check",
      "expected": "{\"status\":\"ok\"}",
      "method": "GET",
      "category": "readonly",
      "description": "检查 API 可达性"
    },
    {
      "id": "TC-02",
      "name": "list-articles",
      "command": "python3 scripts/{skill_name}.py list-articles --page 1 --size 10",
      "expected": "{\"code\":200,\"data\":[...]}",
      "method": "GET",
      "category": "readonly",
      "description": "查询文章列表"
    },
    {
      "id": "TC-03",
      "name": "create-article",
      "command": "python3 scripts/{skill_name}.py create-article --title test --content hello",
      "expected": "{\"code\":200,\"data\":{\"id\":...}}",
      "method": "POST",
      "category": "mutable",
      "description": "创建文章",
      "cleanup": "执行 delete-article 删除刚创建的测试文章"
    }
  ],
  "env_vars_required": ["{CRED_PREFIX}_BASE_URL（API 地址）", "有认证时：{CRED_PREFIX}_TOKEN / {CRED_PREFIX}_USERNAME / {CRED_PREFIX}_PASSWORD（凭据）"],
  "notes": "凭据和 base_url 从环境变量或 .project-info/ 下 JSON 配置文件获取，不写入此文件"
}
```

字段说明：
- `skill_name`：skill 名称（tester 由此推导凭据变量前缀：skill name 转大写下划线）
- `entry_file`：Python 入口文件路径（固定为 `scripts/{skill_name}.py`）
- `test_cases[].id`：测试用例 ID（TC-01/TC-02/...）
- `test_cases[].name`：子命令名
- `test_cases[].command`：测试命令
- `test_cases[].expected`：预期返回值描述
- `test_cases[].method`：HTTP 方法（GET/POST/PUT/PATCH/DELETE）
- `test_cases[].category`：readonly（只读，自动测试）/ mutable（变更，自动执行+自动清理）
- `test_cases[].description`：测试说明
- `env_vars_required`：描述需要的环境变量（不含值，只含变量名说明）
- `notes`：说明凭据和 base_url 的获取方式
- `test_cases[].description`：测试说明

**test-vars.json 最低覆盖要求**（生成时必须满足）：

1. `health-check`（必填）
2. 每类资源的至少 1 个只读查询（如 articles-list / lables-list / users-list）
3. 至少 1 个写操作用例（category=mutable，含 cleanup 字段描述清理步骤）
4. `capability-list`（必填）

不要求每个子命令都写测试用例——只读用例覆盖每类资源即可，写操作至少 1 个含 cleanup。

**test-vars.json 字段完整性要求**：每个 test_cases 用例必须包含 `name`、`command`、`method`、`category`、`description` 五个字段，不得省略。只读用例 category=readonly，写操作用例 category=mutable。
- `test_cases[].cleanup`：可选，变更类测试的清理说明（描述性文本，如"执行 delete-article 删除刚创建的测试资源"）。agent 执行 create 命令后从返回 JSON 中提取资源 ID，自行拼接 cleanup 命令执行——不假设返回字段名

**子命令生成规则**（由 API 文档动态决定，不预设）：

| API 端点特征 | 子命令名 | 示例 |
|-------------|---------|------|
| GET /api/{resource} | list-{resource} | GET /api/articles → list-articles |
| POST /api/{resource} | create-{resource} | POST /api/articles → create-article |
| GET /api/{resource}/{id} | get-{resource} | GET /api/articles/42 → get-article |
| PUT/PATCH /api/{resource}/{id} | update-{resource} | PUT /api/articles/42 → update-article |
| DELETE /api/{resource}/{id} | delete-{resource} | DELETE /api/articles/42 → delete-article |
| 特殊路径（restore/reply/heat/top 等） | 按 path 语义段 + 动词 | POST /api/articles/{id}/restore → restore-article；GET /api/articles/heat/top → top-articles |
| 其他 | 按 path 末段 + method | GET /api/search → search |

规则补充：
- resource 名取 path 资源段（复数转单数）
- 文档有 operationId / summary 时优先用文档指定名称。operationId 转子命令名时：下划线转连字符（list_comments → list-comments）、驼峰转连字符（listComments → list-comments）、已是连字符保持不变。operationId 含特殊字符或过长不可读时忽略，用路径规则
- **拼写修正**：如果 API 路径有拼写错误（如 lables 应为 labels），子命令用正确拼写，但在脚本内部请求时用 API 实际路径（lables）。在 SKILL.md 的 Core Commands 表格里注明端点实际路径
- **特殊路径处理**：路径含 restore/reply/heat/top 等语义段时，子命令名取语义段 + resource 名（restore-article / reply-message / top-articles）
- **Web 页面端点排除**：GET / 和 GET /article/{id} 等返回 HTML 页面的端点不生成子命令（非 API 端点）
- **文件上传特殊处理**：requestBody 为 multipart/form-data 时，子命令的 _api_request 调用用 `files=` 参数（不是 `json=`）。单文件用 `files={'字段名': f}`（dict value 是单个 file object，字段名由 API 文档定义），批量上传用 `files=[('字段名', f) for f in files]`（元组列表，不能用 dict value 为列表——requests 会报 unpack 错误）
- 子命令参数由 API 文档的 parameters / requestBody 定义决定

**生成的脚本凭据读取规范**（前缀由 skill name 推导，认证方式由需求澄清决定）：

凭据和 base_url 统一按 4 级优先级读取（与 Configuration 段落一致）：
1. **项目知识** — 递归扫描 `.project-info/` 下所有 JSON 文件（secrets.{PREFIX}_USERNAME / secrets.{PREFIX}_TOKEN 等）
2. **环境变量** — 项目知识缺失时扫描 `{PREFIX}_*` 开头变量
3. **当前上下文** — A2A context 已注入的环境变量（已包含在步骤 2）
4. **交互输入** — 以上都无时提示用户输入

详细代码模板见 `references/rest-api-skill-template.md` 的 `_load_credentials` + `_load_from_project_knowledge` 函数。

- 环境变量和项目知识都缺失则退出码 3（缺少配置——地址或认证）

**生成的脚本调用模式**（按认证方式生成不同代码）：

```python
import requests

# Basic Auth
from requests.auth import HTTPBasicAuth
auth = HTTPBasicAuth(username, password)
resp = requests.get(f"{base_url}/{path}", auth=auth, timeout=30)

# Bearer Token
headers = {"Authorization": f"Bearer {token}"}
resp = requests.get(f"{base_url}/{path}", headers=headers, timeout=30)

# API Key（Header，名由需求澄清决定，如 X-API-Key / Authorization）
headers = {_API_KEY_HEADER: api_key}
resp = requests.get(f"{base_url}/{path}", headers=headers, timeout=30)

# API Key（Query，参数名由需求澄清决定，如 api_key / key）
params = {_API_KEY_HEADER: api_key}
resp = requests.get(f"{base_url}/{path}", params=params, timeout=30)

# 无认证（公开 API）
resp = requests.get(f"{base_url}/{path}", timeout=30)
```

脚本生成模板见 `references/rest-api-skill-template.md`。

⚠️ 生成的脚本只支持真实 API 调用，严禁 mock 模式/假数据/MOCK_DATA/--mock 参数。

**name 匹配目录名**：frontmatter name 必须跟 skills/{name}/ 目录名一致
**文件扩展名白名单**：.md .txt .json .yaml .yml .py .sh .csv .cfg .conf .xml .html .css .svg
**包大小限制**：总文件内容 ≤40MB，总文件数 ≤50，SKILL.md ≤500 行

### Step 3.5: 自测

- 跑 `python3 skills/{name}/scripts/{name}.py --help` → 确认无语法错误
- 跑 validate-skill.sh 做结构校验：

  ```bash
  bash {{SKILL_DIR}}/rest-api-skill-creator/scripts/validate-skill.sh skills/{name}/
  ```
  校验项（16 项）：
  - SKILL.md frontmatter（name/version/description/triggers/tags/tools）
  - name 与目录名一致（kebab-case）
  - 必填段落（安全硬约束/评论规范/Configuration/场景/知识/步骤/判断标准/输出规范/参考文档/分析结论示例/任务结束）
  - Python 入口文件存在 + 可执行
  - references/api-reference.md 存在
  - templates/test-vars.json 存在
  - requirements.txt 存在
  - 无 mock 代码
  - 无硬编码 IP 地址（SKILL.md + api-reference.md）
  - 安全硬约束不含 GitCode
  - 脚本用 requests 不用 urllib
  - test-vars.json 无敏感字段（base_url/cred_prefix/auth_type）
  - test-vars.json 字段完整（id/name/command/method/category/description）
  - SKILL.md ≤500 行
  - 文件数 ≤50
- 跑真实 API 调用一次（只读端点，如 `python3 skills/{name}/scripts/{name}.py list-{resource}`）→ 确认 API 可达
- validate-skill.sh 或 API 调用失败 → 修复后重试；无法修复 → Issue 评论说明，不创建 PR
- 全部通过 → 继续 Step 4

### Step 4: 创建 PR

调 `bash {{SKILL_DIR}}/rest-api-skill-creator/scripts/create_rest_api_skill_pr.sh "${SKILL_NAME}"`

⚠️ **创建 PR 后必须发 Issue 评论告知**：

```bash
curl -s --max-time 30 -X POST \
  -H "PRIVATE-TOKEN: $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "[Coding Agent] ✅ Skill 已创建并提交 PR\n\n**Skill 名称**：{name}\n**功能摘要**：{一句话描述}\n**API 端点数**：{N} 个\n**认证方式**：{AUTH_TYPE}\n**PR 链接**：{PR_URL}\n**测试状态**：已 A2A 交棒 QA Agent，测试中\n\n**下一步**：\n- 测试通过后自动合并 → 发布"}' \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/comments"
```

### Step 5: 清理

删除临时调研产物。

### Step 6: A2A 交棒 tester

```bash
curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "qa",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "pr_number": "'"$PR_NUMBER"'",
      "context": "Issue #'"$ISSUE_NUMBER"' skill 已创建，PR #'"$PR_NUMBER"' 已提交。请对 skill 进行功能验证。Skill 名称：{name}"
    }
  }'
```

## 判断标准 / Verification

- 脚本自测：--help 无语法错误 + 真实 API 调用返回有效数据 = 通过
- validate-skill.sh：frontmatter / 文件结构 / 无 mock / 无硬编码地址 / 段落规范（场景/知识/步骤）全过 = 通过
- PR 创建：GitCode API 返回 PR URL = 成功
- Issue 评论：评论发送成功（API 返回 200）= 成功
- A2A 交棒：api/a2a 返回 status=ok = 成功
- 以上全部通过 = skill 创建完成，等 tester 测试
- 任一步骤失败 → 不创建 PR / 不交棒 tester，Issue 评论说明原因

## 输出规范 / Output Format

- 提交优先：git commit + push 完成后才写详细报告
- 回评结论式：Issue 评论 = 结论 + PR 链接 + 下一步
- 过程不刷屏：只在关键节点输出

## 参考 / References

- **Skill 规范**：读取 `references/skill-spec.md`——生成 SKILL.md 前必须先读
- 调用 GitCode API 时，必须以 `references/gitcode-api-reference.md` 为准
- API 调研详细方法，读取 `references/api-research-guide.md`
- 脚本生成模板，读取 `references/rest-api-skill-template.md`
- 用户需求模板，引导用户填写 `templates/requirement-template.md`

## 分析结论示例

成功时：

```
分析结论：
- 场景：skill 创建（A2A 触发，Issue Agent 交棒 Coding Agent）
- Skill：{skill_name} v{version}
- 产物：SKILL.md + scripts/{skill_name}.py + references/ + templates/test-vars.json
- 验证：validate-skill.sh ✅ / API 调用 ✅
- 必须执行：创建 PR → A2A 交棒 QA
```

失败时：

```
分析结论：
- 场景：skill 创建
- Skill：{skill_name}
- 结果：🔴 失败（validate-skill.sh 失败 / API 调研失败）
- 必须执行：Issue 评论报告错误 → A2A 回 Issue Agent
```

## 任务结束

完成所有业务动作后，在最终答复末尾输出 JSON 块，然后结束任务：

```json
{
  "actions": ["做了什么，如 created:rest-api-skill-xxx / submitted:PR#N / a2a:qa"],
  "conclusion": "处理结论（skill 已创建/创建失败）",
  "artifacts": ["产生的产物：PR#N / skill 目录 / 评论"],
  "next_step": "下一步建议",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```
