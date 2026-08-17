# Codegraph 项目分析指南（repo-onboarding 版）

> ⚠️ **与 issue-advance 的差异**：issue-advance 场景下知识基线由服务端统一生成并内联注入 prompt（Agent 不再自行生成）；但 **repo-onboarding 没有服务端预生成**（builder 仅在 `issue_opened` 时才刷新），且本次执行是仓库首次托管、project-info 尚不存在——**本技能必须自行构建知识基线**，并写到共享位置 `data/workspaces/{owner}/{repo}/project-info/`，供后续 VOD / issue-advance 复用。

## 执行前提

- 仓库为首次托管，`project-info/` 下通常无知识文档（有则检查是否过期，复用无需重建）
- 事件类型为 onboarding（experience / explore 分支都先建基线）

## Codegraph 安装部署

如果本地未安装 codegraph，需先完成安装：

```bash
npm install -g codegraph

# 验证安装
codegraph --version
```

> ⚠️ codegraph 依赖 Node.js 环境。如未安装 Node.js >= 18，先安装；安装/初始化失败则降级（见降级方案）。

## 存储位置（共享，供下游复用）

```
data/workspaces/{owner}/{repo}/project-info/
├── project-graph.md          # 项目架构图
├── project-capabilities.md   # 项目能力特性文档
└── .metadata.json            # 元数据（更新时间、分支、commit sha）
```

其中 `{owner}/{repo}` 从环境变量 `$REPO_FULL_NAME` 获取。`$ONBOARDING_RESULT`（onboarding-agent/ 下的引导结果文件）与本基线位置不同，不要混淆。

## 流程（按顺序执行）

### Step 1: 检查 codegraph 索引

检查当前项目工作目录下是否存在 `.codegraph/`：

```bash
ls -la .codegraph/
```

**索引不存在** → 执行初始化：

```bash
codegraph init
```

> ⚠️ `codegraph init` 可能需要 1-3 分钟。初始化完成后索引持久化，后续无需重复。

### Step 2: 检查 project-info 是否需要生成

```bash
ls -la data/workspaces/{owner}/{repo}/project-info/ 2>/dev/null
```

**无 `project-graph.md` + `project-capabilities.md`** → 进入 Step 3 生成。
**已有且未过期**（如距上次更新 <7 天且 commit 未变）→ 直接复用，跳过生成。

### Step 3: 获取仓库结构（codegraph）

#### 3.1 文件树

```bash
codegraph_files --format tree
```

识别：源代码目录、测试目录、配置目录、文档目录。

#### 3.2 核心架构探索

```bash
codegraph_explore --query "main entry point application startup" --maxFiles 10
codegraph_explore --query "API routes endpoints handlers" --maxFiles 10
codegraph_explore --query "database models schema" --maxFiles 10
codegraph_explore --query "module import dependency" --maxFiles 15
```

#### 3.3 核心调用链（可选）

```bash
codegraph_callees --symbol "main" --depth 3
```

### Step 4: 生成 project-graph.md

按模板生成并写入 `data/workspaces/{owner}/{repo}/project-info/project-graph.md`：

```markdown
# Project Graph: {owner}/{repo}

> Last updated: {datetime} | Commit: {sha}

## Architecture Overview
{项目整体架构描述：分层、模块关系、核心入口}

## Module Dependency Graph
{模块间依赖关系图}

## Core Call Chains
{关键业务流程的调用链路}

## Key Files
{核心文件及其职责}
```

**长度限制**：不超过 3000 字。优先保留 Architecture Overview 与 Module Dependency Graph。

### Step 5: 生成 project-capabilities.md

信息来源：codegraph 分析 + README + `requirements.txt`/`package.json`/`Cargo.toml` + `GET /repos/{owner}/{repo}` 仓库信息。

按模板写入 `data/workspaces/{owner}/{repo}/project-info/project-capabilities.md`：

```markdown
# Project Capabilities: {owner}/{repo}

> Last updated: {datetime}

## Tech Stack
{语言、框架、关键依赖及版本}

## Supported Features
{项目已支持的功能特性清单}

## Startup & Deployment
{启动命令、环境要求、部署方式}

## API Endpoints
{暴露的 API 端点列表}

## Configuration
{配置项和环境变量说明}
```

> 3A（体验）分支重点提炼「Startup & Deployment」；3B（探索）分支重点提炼「Supported Features」。

**长度限制**：不超过 3000 字。

### Step 6: 更新 .metadata.json

```json
{
  "last_updated": "{ISO8601 datetime}",
  "last_commit_sha": "{main分支HEAD sha}",
  "graph_version": 1
}
```

## 降级方案（codegraph 不可用时）

1. **README**：`GET /repos/{owner}/{repo}/readme` → Base64 解码
2. **仓库信息**：`GET /repos/{owner}/{repo}` → description, language, topics
3. **目录结构**：`GET /repos/{owner}/{repo}/contents/` → 顶层文件列表
4. **依赖清单**：`GET /repos/{owner}/{repo}/contents/requirements.txt`（或 package.json 等）

降级生成的文档标注 `[degraded - codegraph unavailable]`。

## 异常处理

| 异常 | 处理 |
|------|------|
| `codegraph init` 失败 | 记录日志，降级为 README + API 分析 |
| `codegraph_explore` 超时 | 减小 `maxFiles` 重试，或跳过该查询 |
| project-info 写入失败 | 记录日志，不阻塞引导后续步骤 |
