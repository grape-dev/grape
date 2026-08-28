# Codegraph 命令速查参考

## 概述

Codegraph 是代码图谱分析工具，通过静态分析构建项目的符号索引、调用关系和依赖图。

## 安装

```bash
# 全局安装
npm install -g codegraph

# 验证
codegraph --version
```

安装部署指南：https://github.com/colbymchenry/codegraph

依赖：Node.js >= 18

## 命令列表

### codegraph init

初始化项目的 codegraph 索引。

```bash
codegraph init
```

- 在项目根目录创建 `.codegraph/` 目录
- 扫描源代码，构建符号索引和调用关系
- 首次执行耗时 1-3 分钟（取决于项目大小）
- 需要在项目根目录执行
- `.codegraph/` 目录应加入 `.gitignore`

---

### codegraph_explore

基于自然语言查询探索代码库，返回最相关的文件和符号。

```bash
codegraph_explore --query "查询内容" --maxFiles {N}
```

| 参数 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `--query` | 是 | 自然语言查询 | - |
| `--maxFiles` | 否 | 返回的最大文件数 | 10 |

常用查询：

| 场景 | query |
|------|-------|
| 应用入口 | `"main entry point application startup"` |
| API 路由 | `"API routes endpoints handlers"` |
| 数据模型 | `"database models schema ORM"` |
| Agent 架构 | `"agent executor skill dispatch"` |
| 认证逻辑 | `"authentication authorization login"` |
| 配置管理 | `"configuration settings environment"` |
| 错误处理 | `"error handling exception recovery"` |

---

### codegraph_files

获取项目的文件树结构。

```bash
codegraph_files --format {tree|flat|json}
```

| 参数 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `--format` | 否 | 输出格式：`tree`/`flat`/`json` | `tree` |

---

### codegraph_callees

追踪指定符号的调用链（向下：谁被它调用）。

```bash
codegraph_callees --symbol "符号名" --depth {N}
```

| 参数 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `--symbol` | 是 | 函数/类/方法名 | - |
| `--depth` | 否 | 追踪深度 | 2 |

---

### codegraph_callers

查找谁调用了指定符号（向上：谁调用了它）。

```bash
codegraph_callers --symbol "符号名" --depth {N}
```

参数同 `codegraph_callees`。

---

### codegraph_impact

分析指定文件/符号变更的影响范围。

```bash
codegraph_impact --target "文件路径或符号" --depth {N}
```

| 参数 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `--target` | 是 | 目标文件路径或符号名 | - |
| `--depth` | 否 | 影响分析深度 | 2 |

## 多分支索引策略

1. **主索引**：在 main/master 分支上执行 `codegraph init`
2. **分支探索**：对其他分支，切换后重新初始化

```bash
git checkout {branch_name}
codegraph init
codegraph_explore --query "..." --maxFiles 10
git checkout main
```

仅分析关键分支：

| 分支类型 | 是否分析 |
|---------|---------|
| main/master | 必须 |
| develop | 推荐 |
| feat/* | 最近活跃的 2 个 |
| fix/* / release/* | 跳过 |

## 与 Issue Agent 的集成点

| Issue Agent 步骤 | 命令 | 用途 |
|-----------------|------|------|
| 项目知识基线 | `codegraph init` | 初始化索引 |
| 项目知识基线 | `codegraph_files` | 获取目录结构 |
| 项目知识基线 | `codegraph_explore` | 探索核心架构 |
| 项目知识基线 | `codegraph_callees` | 追踪核心流程 |
| 可开发性分析 | `codegraph_impact` | 评估改动影响 |
| 可开发性分析 | `codegraph_callers` | 查找模块依赖方 |

## 异常处理

| 异常 | 处理 |
|------|------|
| `codegraph init` 超时 | 终止初始化，降级为 README 分析 |
| `codegraph_explore` 返回空 | 调整 query 措辞重试 |
| `codegraph_callees` 找不到符号 | 用 `codegraph_explore` 先定位 |
| 索引损坏 | 删除 `.codegraph/`，重新 `codegraph init` |
