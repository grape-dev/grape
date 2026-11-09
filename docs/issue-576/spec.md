# Spec: Issue #576 — spec-generation.md Spec 评论发布示例对长多行 body 转义不足

> 来源：用户反馈（后台 id=148）| 可开发性评级：ready | Spec 评论 id=197176634

## Objective

修复 issue-cli 技能 `references/spec-generation.md`「发布方式 → 作为 Issue 评论发布」小节：为长多行 Spec body 补充基于 `jq -n --rawfile` 的安全发布写法，并明确两种写法的适用边界。

- **根因**：现有示例用 shell 内插 / `jq --arg` 构造 body，`jq --arg` 会把换行符字面化为 `\n` 文本，导致多行 Spec 评论换行渲染错误，实际造成过一次评论重发返工。
- **成功标准**：新示例可直接照抄发布多行 Spec；评论换行渲染正确、无需重发；短写法边界清晰；与 vod-runbook printf 系列根因差异可查。

## Scope

- In：`spec-generation.md`「作为 Issue 评论发布」小节新增 `jq -n --rawfile` 推荐写法（`/tmp/spec.md` 文件中转）；保留短文本内插写法并注明适用边界（仅单行、无引号/反斜杠/换行等特殊字符）；注明与 vod-runbook printf 拼 JSON 系列（issue 101 系列 #34/#36/#37 等）相关但根因不同，不合并处理。
- Out：不改 vod-runbook、不改 grape CLI `--json` 行为、不改 Spec 模板结构。

## Affected Modules

| 模块 | 改动类型 | 说明 |
|------|---------|------|
| grape-skills `skills/issue/feedback-loop/issue-cli/references/spec-generation.md` | 修改 | 发布方式小节双写法 + 边界 + 根因差异说明（实际改动仓库为 grape-dev/grape-skills） |

## Acceptance Criteria

1. 示例可直接照抄执行；
2. 用新示例实测发布一条多行 Spec 评论，换行渲染正确、无需重发；
3. 短写法边界说明清晰；
4. 根因差异注明、不合并处理；
5. 纯 Markdown 变更，无其它行为变化。
