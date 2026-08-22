# Skill 规范定义

每个 Skill 由一个目录和一个 `SKILL.md` 文件组成。`SKILL.md` 遵循以下结构：

## 文件格式

```markdown
---
name: skill-name
version: 1.0.0
description: 一句话描述技能用途
tools:
  - git
  - docker
  - kubectl
---

# Skill 标题

## 场景 / When to Use
描述该 Skill 适用的场景。

## 知识 / Knowledge
提供 Agent 处理该场景时需要的领域知识、最佳实践、检查清单等。

## 步骤 / Steps
Agent 应遵循的处理步骤（有序）。

## 判断标准 / Verification
Agent 据此判断处理结果是否达标。

## 输出规范 / Output Format
Agent 输出应遵循的格式。
```

## 字段说明

### Front Matter (YAML)

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | Skill 唯一标识，与目录名一致（kebab-case） |
| `version` | 是 | 语义化版本号 |
| `description` | 是 | 一句话描述 |
| `tools` | 否 | 该 Skill 依赖的外部工具（供 Agent 判断环境是否满足） |
| `triggers` | 否 | 触发词（用于 ClawHub 搜索匹配） |
| `tags` | 否 | 标签数组（≤5 个，用于 ClawHub 分类） |

### Body (Markdown)

| 章节 | 必填 | 说明 |
|------|------|------|
| `场景 / When to Use` | 是 | 描述适用场景 + Don't use for 反例，帮助 Agent 判断是否激活该 Skill |
| `知识 / Knowledge` | 是 | 领域知识、最佳实践、检查项等，是 Skill 的核心价值 |
| `步骤 / Steps` | 是 | Agent 应遵循的处理流程，按序号排列（主流程 Step 1/2/3，操作为子步骤） |
| `判断标准 / Verification` | 否 | 处理结果的验收标准（推荐 checkbox 格式） |
| `输出规范 / Output Format` | 否 | 输出格式要求，如 JSON / Markdown 表格 / curl 示例格式 |

### 额外段落（规范未定义但允许）

以下段落可按需添加，不影响规范合规：

- 安全硬约束
- 核心原则
- 评论规范
- 硬规则
- 参考文档 / References
- 任务结束

## 命名规范

- 目录名和 `name` 字段使用 kebab-case：`blog-manager`、`cms-toolkit`
- 每个 Skill 目录只包含一个 `SKILL.md`，如需附带资源文件可放在同目录下
- 段落名统一用中文 + 英文别名：`## 场景 / When to Use`、`## 知识 / Knowledge`、`## 步骤 / Steps`

## 发现与触发机制

Grape **不读取、不注入** Skill 内容。Grape 只做一件事：将 Skills 目录路径通过环境变量 `DEVOPS_SKILLS_DIR` 传给 Agent。

Agent 的职责：

```
1. 读取 DEVOPS_SKILLS_DIR 环境变量
2. 遍历目录下的子目录，每个子目录即一个 Skill
3. 读取每个 Skill 的 SKILL.md，按其正文说明判断是否适用
4. 加载适用 Skills 的完整内容（知识 + 步骤 + 判断标准）
5. 按技能定义执行处理流程
```

Agent 自行从 `DEVOPS_SKILLS_DIR` 发现和加载所需技能，Grape 不干预。

---

## REST API skill 适配说明

rest-api-skill-creator 生成的 REST API skill 除满足上述通用规范外，各段落应包含以下内容：

### 场景 / When to Use

- 从用户需求提取使用场景（如"查询文章列表""发布文章""管理标签"）
- 必须包含 Don't use for 反例（如"非本 API 的操作"）

### 知识 / Knowledge

知识段落包含以下子段：

| 子段 | 说明 |
|------|------|
| API Base URL | base_url 格式 + 认证说明（是否需要 header） |
| Endpoints | Method / Path / Body / Description（从 API 文档解析，按资源分组） |
| 认证方式 | none/basic/token/api_key + 凭据环境变量 + 凭据前缀推导规则 + 4 级读取优先级说明 + 项目知识 JSON 配置示例 |
| Common Pitfalls | 至少 5 条常见陷阱 + 解决方案 |
| 字段说明 | 字段 / 类型 / 必填 / 默认值 / 说明 |

base_url 解析优先级（Configuration 段落）：项目知识（.project-info/ 下 JSON 配置文件）> 当前上下文（A2A context / 用户提供）> 环境变量 {PREFIX}_BASE_URL > 交互输入

### 步骤 / Steps

主流程按序号排列，每个操作是步骤里的子步骤：

```
### 1. 检查 API 可达性
### 2. 查询可用资源（标签/分类等）
### 3. 执行操作（按用户需求选择）
#### 查询列表
#### 查询详情
#### 创建
#### 更新
#### 删除
```

每个操作包含：curl 示例（或 python3 scripts/{name}.py 示例）+ Expected 返回值 + 参数说明

### 判断标准 / Verification

Verification Checklist（checkbox 格式）：
- [ ] API 可达（GET /health 返回 200）
- [ ] 只读操作返回有效数据（非空/字段完整）
- [ ] 脚本 --help 无语法错误
- [ ] 退出码正确（0=成功/2=参数错误/3=缺少配置/4=API失败）

### 输出规范 / Output Format

- 默认输出：JSON
- 可选输出：Markdown 表格（--format md）
- curl 示例格式

---

## 系统级 skill 适配说明

rest-api-skill-creator 通过 grape A2A 链路触发，生成的 skill 默认为系统级——参与 A2A 链路（creator → tester → SRE Agent 发布）。除标准段落外额外生成以下段落：

| 额外段落 | 位置 | 必填 | 说明 |
|---------|------|------|------|
| 安全硬约束 | 场景前 | 是 | token 不写文件/不读内部数据/curl --max-time 30/调用 API 以 references 为准 |
| 评论规范 | 安全硬约束后 | 是 | [$AGENT_NAME] 开头 + 引用原文 |
| Configuration | 评论规范后 | 是 | base_url 解析 4 级优先级 + 配置指导（环境变量方式 + 项目知识 JSON 方式 + 示例） |
| 输出规范（Issue 评论格式） | 输出规范段落内 | 是 | 成功/失败两种 Issue 评论 body 格式模板（操作结果表格 + 错误分析） |
| 分析结论示例 | 参考文档后 | 是 | 成功/失败两种场景的分析结论输出参考 |
| 任务结束 | 分析结论示例后 | 是 | JSON 块（actions/conclusion/artifacts/next_step/issue_summary） |

### 安全硬约束内容

**通用约束（所有 skill 必含）**：

1. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
2. API 返回 403/无权限时：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
3. 所有 curl / API 调用设 30s 超时——超时判定失败，不 hang。
4. 严禁 mock 模式/假数据：所有 API 调用必须真实访问目标系统。
5. 调用目标 API 一律以 `references/api-reference.md` 为准：严禁根据经验/记忆/猜测自行拼写 API 路径。

**动态约束（按认证方式生成，无认证时省略）**：

- 有认证（Basic Auth / Bearer Token / API Key）：凭据按 4 级优先级读取（与 base_url 一致）——项目知识（.project-info/ 下 JSON 配置文件的 secrets.{CRED_PREFIX}_*）> 环境变量（`{CRED_PREFIX}_*`）> 交互输入。凭据不得写入任何文件/日志/评论。
- 无认证：省略此条。

### 评论规范内容

- 所有 Issue/PR 评论 body 必须以 `[$AGENT_NAME]` 开头
- 引用用户原文：`> {用户评论}\n\n[$AGENT_NAME] {回复}`

### 任务结束内容

```json
{
  "actions": ["做了什么"],
  "conclusion": "处理结论",
  "artifacts": ["产生的产物"],
  "next_step": "下一步建议",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```
