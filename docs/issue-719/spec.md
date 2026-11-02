# Spec: Issue #719 — issue-runbook `grape git log` 命令引用更正与提交核对降级路径

> 来源：Issue #719 评论已发布的 Spec Document（Issue Agent，评级 ready；跨流水线复核维持）。
> 目标分支：weekly-08-mys（场景注入基线，grape 仓）。实际改动资产归属 grape-skills 仓 `skills/issue/feedback-loop/issue-cli/references/issue-runbook.md`（该仓基线为 main，PR 以 main 为 base）。

## Objective
更正 issue-runbook Step 6 ① 的提交核对命令引用（`grape git log` → `grape git-repo commits`），明确其 API 调用属性与工作目录前提，并文档化失败降级路径；联动 #615 补全分支过滤后给出统一提交核对口径。

## In Scope
1. issue-runbook Step 6 ① 命令引用更正：删除 `grape git log --oneline -20 --all`（本地裸库前提，命令名与 grape CLI 实际命令组不符），改为 `grape git-repo commits --owner ... --per_page 20 --json-out`（API 调用、不依赖本地 git 仓库）。
2. runbook 明确工作目录前提与失败降级：如确需本地 `git log`（如核对未推送 commit），须先 cd 到仓库根；API 路径不可用时以 `git-repo branches`（分支最新 commit）+ `git-repo pulls` 交叉核对兜底。
3. runbook 全文排查其他 `grape git log` 引用统一更正（含错误排查速查表补充条目）。
4. 联动 #615：`git-repo commits --ref` 合入后补充统一核对口径（按分支查 commits → 比对 PR commits → 确认覆盖），过渡期标注兜底写法。

## Out of Scope
- 不修改 grape CLI / 服务端代码（分支过滤属 #615 范围）。
- 不新增 `grape git log` 命令组（直接移除引用）。
- 不改动 vod-cli 及其他 skill 的 runbook（后续发现另行开单）。

## Acceptance Criteria
- [ ] issue-runbook 全文无 `grape git log` 引用
- [ ] 提交核对步骤在 issue agent 工作目录可直接执行（不依赖工作目录是 git 仓库）
- [ ] 失败降级路径文档化（本地 git 需 cd 仓库根；API 不可用 → branches 最新 commit + pulls 交叉核对）
- [ ] #615 合入后统一核对口径段落可复用其 `--ref` 验证命令（本次先落过渡兜底段落）
- [ ] 无残留旧命令引用；错误排查速查表含对应条目

## 依赖
- `git-repo commits`（已有，可用）；`git-repo commits --ref`（#615，pending_review）→ 统一口径段落软依赖，本次标注过渡兜底。
