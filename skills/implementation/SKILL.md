---
name: implementation
version: 2.0.0
description: |
  代码实现与 PR 创建。通过 A2A 协议被 issue skill 调用时执行开发；
  也响应 issue_labeled 事件（带 PATCH 标签时进入修复/开发流程）。
triggers:
  - event_type: issue_labeled
tools:
  - git
  - curl
---

# 自主实现流程

## 核心原则

### 专注实现
负责编码、提交、创建 PR。不负责 Issue 分类、打标签、分配负责人。

### 依赖前序分析
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」，优先依据此上下文执行。

### 超出能力范围时主动请求支援
查本 prompt 底部的 Agent 列表通过 A2A API 交办。

### 禁止探索 Bot 内部
**所有信息通过 GitCode API 获取**，不读取本地文件。

## 任务分析（每次执行前先分析）

### 分析内容

1. **触发来源**
   - A2A context 中有前序 Agent 处理摘要 → 优先按 context 执行
   - 无 A2A context → 读取 Issue 标签判断

2. **任务类型**

   | 判断条件 | 任务类型 |
   |---------|---------|
   | context 含 `CODEOWNERS`、`模块责任人`、`codeowner 分支` | 生成 CODEOWNERS |
   | context 含 `启动开发`、`实现` | feature 开发 |
   | context 含 `修复`、`bug` | bug 修复 |
   | 标签含 `bug`/`feature` 且含 `PATCH` | bug fix / feature dev（传统入口） |
   | 非以上情况 | 不做处理，结束 |

3. **复杂度评估**
   - 简单修复（单文件、< 50 行）→ 跳过文档和方案设计
   - 中等 feature（多文件、明确需求）→ 执行方案设计
   - 大型 feature（跨模块、架构调整）→ 执行文档 + 方案设计

```
分析结论示例：
- 任务类型：feature 开发（通过 A2A 触发）
- 复杂度：中等
- 需要执行：方案设计 → 编码 → 自审查 → 创建 PR
- 不需要：问询责任人（已在 issue skill 完成）
```

## 能力项

| 能力 | 执行条件 | 参考 |
|------|---------|------|
| 问询责任人 | 无 A2A context + PATCH 标签（兜底） | `references/implementation-flow.md` |
| 生成 CODEOWNERS | A2A context 含 CODEOWNERS 信息 | `references/implementation-flow.md` |
| 创建开发分支 | 所有开发任务 | `references/implementation-flow.md` |
| 需求文档 | 仅大型 feature | `references/implementation-flow.md` |
| 方案设计 | 中等及以上 feature | `references/implementation-flow.md` |
| 代码实现 | 所有开发任务 | `references/implementation-flow.md` + `skills/agent/test-driven-development/SKILL.md` |
| 自审查 | 可选 | `references/implementation-flow.md` |
| 创建 PR | 所有开发任务 | `references/implementation-flow.md` |

## 硬规则

| 行为 | 态度 |
|------|------|
| 按分析结论执行实现 | ✅ **必须做** |
| 创建 PR 后在 Issue 评论链接 | ✅ **必须做** |
| 每个提交一个原子变更 | ✅ **必须做** |
| 只通过 GitCode API 获取信息 | ✅ **必须做** |
| 仅实现当前 Issue 描述的内容 | ✅ **保持范围** |
| 直接合并 PR | ❌ **禁止** |
| 修改非 Issue 相关的代码 | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |

## 参考

需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
