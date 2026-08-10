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

## 场景
描述该 Skill 适用的 DevOps 场景。

## 知识
提供 Agent 处理该场景时需要的领域知识、最佳实践、检查清单等。

## 步骤
Agent 应遵循的处理步骤（有序）。

## 判断标准
Agent 据此判断处理结果是否达标。

## 输出规范
Agent 输出应遵循的格式。
```

## 字段说明

### Front Matter (YAML)

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | Skill 唯一标识，与目录名一致 |
| `version` | 是 | 语义化版本号 |
| `description` | 是 | 一句话描述 |
| `tools` | 否 | 该 Skill 依赖的外部工具（供 Agent 判断环境是否满足） |

### Body (Markdown)

| 章节 | 必填 | 说明 |
|------|------|------|
| `场景` | 是 | 描述适用场景，帮助 Agent 判断是否激活该 Skill |
| `知识` | 是 | 领域知识、最佳实践、检查项等，是 Skill 的核心价值 |
| `步骤` | 是 | Agent 应遵循的处理流程，按序号排列 |
| `判断标准` | 否 | 处理结果的验收标准 |
| `输出规范` | 否 | 输出格式要求，如 Markdown 表格、JSON Schema 等 |

## 命名规范

- 目录名和 `name` 字段使用 kebab-case：`code-review`、`build-debug`
- 每个 Skill 目录只包含一个 `SKILL.md`，如需附带资源文件可放在同目录下

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

> 事件 → Skill 的路由由 Grape 阶段矩阵/静态映射决定（见 `vod-skill-upgrade-plan.md` §3.0.3）；SKILL.md 不再声明 `triggers`。

Agent 收到的运行环境：

```
环境变量:
  DEVOPS_SKILLS_DIR=/path/to/skills
  DEVOPS_SESSION_ID=sess-xxx

CLI 参数:
  --prompt "事件模板渲染结果"
  --workspace "/path/to/repo"
```

Agent 自行从 `DEVOPS_SKILLS_DIR` 发现和加载所需技能，Grape 不干预。
