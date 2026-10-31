# Plan: Issue #576

## 方案

纯文档级修改，落在 issue-cli 技能分发源仓库 grape-dev/grape-skills（`skills/issue/feedback-loop/issue-cli/references/spec-generation.md`）：

1. 「作为 Issue 评论发布」小节改写为两种写法并列：
   - **推荐（多行/含特殊字符）**：先把 Spec 正文写入 `/tmp/spec.md`，再
     `grape git-repo issues comments create ... --json "$(jq -n --rawfile spec /tmp/spec.md '{body:("[issue agent] 📋 Spec Document\n\n" + $spec)}')"`。
     `--rawfile` 原样读入多行内容并由 jq 自动完成 JSON 转义。
   - **快捷（短文本）**：保留现有 `--json '{"body":"..."}'` 内插写法，注明仅适用单行、无引号/反斜杠/换行等特殊字符，并给出误用后果（换行字面化、需删除重发）。
2. 两种写法之间加「适用边界」警告。
3. 注明与 vod-runbook printf 拼 JSON 系列（issue 101 系列 #34/#36/#37 等）相关但根因不同（printf 未转义 vs `jq --arg` 字面化 `\n`），不合并处理。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 多行 body 构造 | `jq -n --rawfile` 文件中转 | 自动 JSON 转义，天然支持多行/特殊字符 |
| 短文本写法 | 保留 + 边界注明 | 单行场景更简洁，删除降低可用性 |
| 与 printf 系列是否合并 | 分开 | 根因不同，合并掩盖差异、增加回归风险 |

## 实施

- 分支：grape-skills `feat/issue-576`（基于 `main`），commit `fix: #576 use jq -n --rawfile ...`，PR grape-skills#81。
- 本仓库仅沉淀 docs/issue-576/（spec/plan/task），PR base `weekly-08-mys`。
