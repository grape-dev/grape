# 两轴审查指南（QA-MattPocock）

> 内化 mattpocock `code-review`：两轴并行子代理（Standards vs Spec），分轴报告，防一轴掩盖另一轴。

## 为什么两轴分离

一个变更可能通过一轴但挂另一轴：
- **Standards pass, Spec fail**：每行都合规范，但实现的是错误的东西。
- **Spec pass, Standards fail**：完全按需求实现了，但破坏项目约定。
分轴报告 → 不合并排名，防止一轴掩盖另一轴。

## Standards 轴

**源**：
1. 仓库写规范的文件（`CODING_STANDARDS.md` / `CONTRIBUTING.md` 必查）；
2. **Fowler 异味基线**（即使仓库没文档也适用，恒为判断项非硬违规）：
   - 神秘命名 / 重复代码 / Feature Envy / 数据泥团 / 原始迷恋 / 重复 switch / 霰弹枪修改 / 发散式修改 / 投机通用 / 消息链 / 中间人 / 拒绝继承。

**规则**：
- 仓库规范 > 基线；仓库认可基线的某个"异味"→ 压制该条。
- 判断项（标注"可能 Feature Envy"）不是硬违规。
- 工具已强制的跳过。
- 报告每条：读出"是什么 → 怎么修"，对 diff 命中处引用 hunk。

## Spec 轴

**源**：originating issue / Spec（mattpocock 版，含用户故事/决策/Out of Scope）。
**规则**：
- 找 Spec：commit 关联 Issue → Spec 评论/文件；没找到先问用户；无 Spec → 跳过并注明。
- 报告三类：(a) Spec 要求但缺失/部分（引 Spec 行）；(b) diff 中未要求的（scope creep）；(c) 实现可疑。

## 聚合报告模板

```markdown
[QA Agent] 两轴审查报告

## Standards
{分轴结果：per file/hunk，每条引规范或命名异味；<400 词}

## Spec
{分轴结果：缺失/越界/可疑；引 Spec 行；<400 词}

**汇总**：Standards {n} 项（最差：{..}） | Spec {m} 项（最差：{..}）
**Merge Danger**：Door {one-way|two-way} | Blast Radius {..}
```

## Merge Danger 门

- **Door**：one-way（不可逆/难回滚）还是 two-way（可廉价回滚）。涉及破坏性动作/难逆决策 → one-way。
- **Blast Radius**：变更潜在影响面（布局移动、消费者破坏、响应式等）。
- 报告末尾显式标注，供人工合入决策。

## 分流

- 发现需修 → A2A coding-mattpocock（agent=coding，带两轴清单），回 coding 修复后复审。
- 通过 → 评论合入提示（本场景无 sre，人工合入/部署）。