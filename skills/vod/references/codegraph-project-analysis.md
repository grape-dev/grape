# Codegraph 项目分析指南

> ⚠️ **职责上收（multi-issue-isolation-design.md §2.6）**：本项目知识基线的生成已**上收为服务端统一维护**（`src/project_info/builder.py` 调用 codegraph CLI 生成，issue_opened 时后台触发，结果写回服务端），执行机上的 Agent **不再自行生成**。Agent 只需读取 prompt 注入的「项目知识基线」章节；本指南作为服务端 builder 的实现手册保留（已落地）。

## 执行前提

- 当前事件为 `issue_opened` 或 `issue_updated`（评论流程不重复生成）
- 项目知识文档不存在或已过期（见增量更新策略）

## Codegraph 安装部署

如果本地未安装 codegraph，需先完成安装：

```bash
# 安装 codegraph（参考：https://github.com/colbymchenry/codegraph）
npm install -g codegraph

# 验证安装
codegraph --version
```

详细安装部署指南参见：https://github.com/colbymchenry/codegraph

> ⚠️ codegraph 依赖 Node.js 环境。如目标服务器未安装 Node.js，需先安装 Node.js >= 18。
> 如安装失败，将降级为基于 README + GitCode API 的项目信息获取（见降级方案）。

## 存储位置

```
data/workspaces/{owner}/{repo}/project-info/
├── project-graph.md          # 项目架构图
├── project-capabilities.md   # 项目能力特性文档
└── .metadata.json            # 元数据（更新时间、分支、commit sha）
```

其中 `{owner}/{repo}` 从事件的 `repository` 字段获取。

## 流程（按顺序执行）

### Step 1: 检查 codegraph 索引

检查项目工作目录下是否存在 `.codegraph/` 目录：

```bash
ls -la .codegraph/
```

**索引不存在** → 执行初始化：

```bash
codegraph init
```

> ⚠️ `codegraph init` 可能需要 1-3 分钟。初始化完成后索引持久化，后续无需重复。

**索引已存在** → 跳过初始化，进入 Step 2。

### Step 2: 检查项目知识文档是否需要更新

读取 `.metadata.json`：

```bash
cat data/workspaces/{owner}/{repo}/project-info/.metadata.json
```

**文件不存在** → 需要生成，进入 Step 3。

**文件存在** → 检查更新条件（满足任一即重新生成）：

| 条件 | 检查方式 |
|------|---------|
| 当前 main 分支 HEAD commit 与记录不同 | `GET /repos/{owner}/{repo}/commits?per_page=1` 对比 sha |
| 距上次更新超过 7 天 | 对比 `.metadata.json.last_updated` |
| project-graph.md 或 project-capabilities.md 缺失 | 检查文件是否存在 |

**无需更新** → 跳过后续步骤，直接使用现有文档。

### Step 3: 获取分支列表

```bash
curl -s -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches?per_page=50"
```

**筛选分析分支**（最多 3 个）：

1. `main` 或 `master`（必须）
2. 最近活跃的开发分支（按 `commit.last_activity_at` 排序取前 2）

### Step 4: 使用 codegraph 分析项目结构

对每个待分析分支执行以下探索：

#### 4.1 获取文件树

```bash
codegraph_files --format tree
```

识别：源代码目录、测试目录、配置目录、文档目录。

#### 4.2 探索核心架构

```bash
codegraph_explore --query "main entry point application startup" --maxFiles 10
codegraph_explore --query "API routes endpoints handlers" --maxFiles 10
codegraph_explore --query "database models schema" --maxFiles 10
codegraph_explore --query "agent executor skill" --maxFiles 10
```

#### 4.3 追踪核心调用链

```bash
codegraph_callees --symbol "main" --depth 3
codegraph_callees --symbol "run_pipeline" --depth 3
codegraph_callees --symbol "build_prompt" --depth 3
```

#### 4.4 分析模块依赖

```bash
codegraph_explore --query "module import dependency" --maxFiles 15
```

### Step 5: 生成 project-graph.md

基于 Step 4 的分析结果，按以下模板生成：

```markdown
# Project Graph: {owner}/{repo}

> Last updated: {datetime} | Branches: {branch_list} | Commit: {sha}

## Architecture Overview
{项目整体架构描述：分层、模块关系、核心入口}

## Module Dependency Graph
{模块间依赖关系图}

## Core Call Chains
{关键业务流程的调用链路}

## Branch Structure
{各分支的定位和当前状态}

## Key Files
{核心文件及其职责}
```

**长度限制**：不超过 3000 字。超出时优先保留 Architecture Overview 和 Module Dependency Graph。

写入文件：

```bash
mkdir -p data/workspaces/{owner}/{repo}/project-info/
cat > data/workspaces/{owner}/{repo}/project-info/project-graph.md << 'EOF'
{内容}
EOF
```

### Step 6: 生成 project-capabilities.md

信息来源优先级：

| 信息 | 来源 |
|------|------|
| Tech Stack | `requirements.txt` / `package.json` / `Cargo.toml` + codegraph 分析 |
| Supported Features | README + codegraph_explore + skills 目录 |
| API Endpoints | codegraph_explore("routes endpoints") + routing 文件 |
| External Integrations | codegraph_explore("external API service") + 配置文件 |
| Skills & Agents | `configs/agent-description.json` + `skills/` 目录 |
| Data Model | codegraph_explore("database model schema") + DB 文件 |
| Configuration | `configs/` 目录 + `.env` + `config.py` |

模板：

```markdown
# Project Capabilities: {owner}/{repo}

> Last updated: {datetime}

## Tech Stack
{语言、框架、关键依赖及版本}

## Supported Features
{项目已支持的功能特性清单}

## API Endpoints
{暴露的 API 端点列表}

## External Integrations
{外部服务集成}

## Skills & Agents
{已配置的 Agent 和 Skill 列表}

## Data Model
{核心数据表/模型概览}

## Configuration
{配置项和环境变量说明}
```

**长度限制**：不超过 3000 字。

### Step 7: 更新 .metadata.json

```json
{
  "last_updated": "{ISO8601 datetime}",
  "branches_analyzed": ["main", "feat/v2-fullstack-rewrite"],
  "last_commit_sha": "{main分支HEAD sha}",
  "graph_version": 1
}
```

## 增量更新策略

| 场景 | 动作 |
|------|------|
| 文档不存在 | 完整生成（Step 3-7） |
| commit sha 变化 | 完整重新生成 |
| 超过 7 天未更新 | 完整重新生成 |
| 文档存在且未过期 | 跳过，直接使用 |

## 异常处理

| 异常 | 处理 |
|------|------|
| `codegraph init` 失败 | 记录日志，降级为仅基于 README 和 API 获取项目信息 |
| `codegraph_explore` 超时 | 减小 `maxFiles` 参数重试，或跳过该查询 |
| 分支列表获取失败 | 仅分析当前分支 |
| 文件写入失败 | 记录日志，不阻塞后续分析流程 |

## 降级方案

当 codegraph 工具不可用时，通过以下方式获取基础项目信息：

1. **README**：`GET /repos/{owner}/{repo}/readme` → Base64 解码
2. **仓库信息**：`GET /repos/{owner}/{repo}` → description, language, topics
3. **目录结构**：`GET /repos/{owner}/{repo}/contents/` → 顶层文件列表
4. **最近提交**：`GET /repos/{owner}/{repo}/commits?per_page=10` → 活跃度

降级生成的文档标注 `[degraded - codegraph unavailable]`。
