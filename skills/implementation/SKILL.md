---
name: implementation
version: 2.0.0
description: |
  代码实现与 PR 创建/优化。通过 A2A 协议被 issue skill 调用时执行新开发；
  或被 PR skill 调用时根据审查意见优化 PR 代码实现。
tools:
  - git
  - curl
---

# 自主实现流程

## 核心原则

### 专注实现
负责编码、提交、创建 PR 或优化已有 PR。不负责 Issue 分类、打标签、分配负责人（这是 issue skill 的职责）。

### 依赖前序分析
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。
- 从 issue skill 触发 → 摘要包含需求分析结论
- 从 PR skill 触发 → 摘要包含审查意见和需要修改的问题点

此摘要可作为快速参考，但**不保证完整性**，必须通过 API 获取完整信息后再开始工作。

### 超出能力范围时主动请求支援
如果开发过程中发现需要其他技能协助，查本 prompt 底部的 Agent 列表通过 A2A API 交办。

### 禁止探索 Bot 内部
**所有信息通过 GitCode API 获取**，不读取本地文件。

## 场景识别

收到事件或 A2A 调用后，先判断触发场景：

| A2A context 特征 | 场景 | 执行流程 |
|-----------------|------|---------|
| 含「PR 审查反馈」「审查发现问题」「需要修改」「优化」 | **代码优化**（从 PR skill） | `references/pr-optimization-flow.md` |
| 含「启动开发」「实现」「feature」「bug」 | **新开发**（从 issue skill） | `references/new-dev-flow.md` |
| 含 `CODEOWNERS`、`模块责任人`、`codeowner 分支` | **生成 CODEOWNERS** | `references/new-dev-flow.md` |
| 其他 | 不做处理，结束 | — |

## 硬规则

| 行为 | 态度 |
|------|------|
| 按场景识别结果执行对应流程 | ✅ **必须做** |
| 所有 feature 开发必须先输出需求文档和方案设计再编码 | ✅ **必须做** |
| 所有开发任务必须包含测试用例 | ✅ **必须做** |
| ⚠️ 创建 PR 前必须先执行自审查 | ✅ **必须做，不可跳过** |
| ⚠️ 创建 PR 后必须在 Issue 发评论告知 | ✅ **必须做，不可跳过** |
| 代码优化场景修改完成后必须在 PR 评论告知 | ✅ **必须做** |
| 每个提交一个原子变更 | ✅ **必须做** |
| 只通过 GitCode API 获取信息 | ✅ **必须做** |
| 仅实现当前 Issue 描述的内容 | ✅ **保持范围** |
| 直接合并 PR | ❌ **禁止**（仅创建，等待人工审批） |
| 修改非 Issue 相关的代码 | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |

## 参考

需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
