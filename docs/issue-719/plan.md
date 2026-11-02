# Plan: Issue #719 — issue-runbook 提交核对命令更正

## 改动资产
| 仓库 | 文件 | base |
|------|------|------|
| grape-skills | skills/issue/feedback-loop/issue-cli/references/issue-runbook.md | main（该仓无 weekly-08-mys，默认分支为 main） |
| grape（本仓） | docs/issue-719/{spec,plan,task}.md 索引副本 | weekly-08-mys |

## 实施步骤
1. Step 6 ① 命令更正：`grape git log --oneline -20 --all` → `grape git-repo commits --owner "$REPO_OWNER" --repo "$REPO_NAME" --per_page 20 --json-out`，标注「API 调用、无需工作目录为 git 仓库」。
2. 同段补充两级降级：
   - 本地 `git log` 仅在仓库根可用（核对未推送 commit 时先 cd 仓库根）；
   - API 不可用时以 `git-repo branches`（分支最新 commit）+ `git-repo pulls` 交叉核对兜底。
3. 全文 grep `grape git log` 残留（实测仅 Step 6 ① 一处）；错误排查速查表补充「`No such command: grape git log` → 用 `git-repo commits`」条目。
4. #615 联动：Step 6 补「统一核对口径（依赖 #615 合入后启用）」段落，含过渡兜底写法与 `--ref` 验证命令占位。

## 验证
- `grep -n "grape git log" issue-runbook.md` 无结果；
- 实测 `grape git-repo commits --owner grape-dev --repo grape --per_page 20 --json-out` 在非 git 仓库工作目录可正常返回；
- `grape git-repo branches` / `pulls` 兜底命令实测可用。

## 风险
- #615 未合入 → 统一口径段落标注过渡兜底（branches + pulls 交叉核对），不阻塞本次文档改动。
