# Skill Review and Quality

Skill（技能）是 DevOps Bot 的能力定义文件，位于 `skills/*/SKILL.md`。
它为 Agent 提供特定领域的指令、行为原则、执行流程和约束规则。

## 审查维度

### 1. 格式规范
- 文件以 `---` frontmatter 开头
- frontmatter 包含 `name`、`version`、`description`、`triggers` 字段
- `description` 清晰说明 skill 的用途和不适用范围

### 2. 完整性
- 包含 `核心原则`（守门不修 bug / 外部 claim 验证 / 读 session 历史 / 禁止探索 Bot 内部）
- 包含强制流程步骤
- 包含 `硬约束` 表
- 包含 `参考` 章节

### 3. 引用路径
- 引用的参考文件（`references/*.md`）路径存在且正确
- API 端点、占位符（`{owner}`、`{repo}`、`{number}`）格式正确

### 4. 向后兼容
- 已有事件类型的处理流程未被破坏
- `version` 已递增（破坏性变更升大版本）
- 无循环引用（不读取自身 SKILL.md）
