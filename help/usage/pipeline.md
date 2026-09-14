# 5. 流水线机制

## 4.1 自动流水线

当 GitCode Issue 被创建时，Grape 自动启动流水线：

```
Issue 创建
  → VOD Agent 分析需求、判断类型
    → Issue Agent 细化 Spec、打标签
      → Coding Agent 编写代码、提交 PR
        → QA Agent 审查 PR
          → 等待维护者合并 PR
            → Issue 关闭（流水线完成）
```

## 4.2 A2A（Agent-to-Agent）通信

Agent 之间通过 A2A 事件传递上下文。例如 Issue Agent 完成后会产生 `a2a_event`，携带 Spec 上下文交给 Coding Agent。每次 A2A 交接都会在流水线中显示为一个节点。

## 4.3 手动触发

- 在 **VOD Agent** 页面输入需求文本，手动触发分析
- 在 **流水线** 页面点击"重试"可重新执行失败的节点
- 在 Issue 评论区 @bot 可以触发特定 Agent
