# openspec-pr-reviewer 执行走查表（runbook）

> 目的：把 PR 开源合规审查的每一步固化，Agent 照着执行、不查 `--help` 探索。
> 环境变量直接使用：`$REPO_OWNER` / `$REPO_NAME` / `$REPO_FULL_NAME` / `$ISSUE_NUMBER` / `$PR_NUMBER` / `$GRAPE_ISSUE_ID` / `$GRAPE_SCENE_ID` / `$BOT_USERNAME`。
> **评论前缀硬规**：PR 审查报告 body 一律以 `[QA Agent]` 开头。
> ⚠️ **纯审查**：全程只读 + PR 评论发布；禁 git 写操作 / 合并 / 改文件。

### Step 0 · 前置 + 读任务上下文（必须最先）

```bash
grape tasks current --json-out
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
grape events detail --seq {seq} --json-out        # 读前序 A2A payload，取 pr_number/context
grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out   # 场景节点核验（确认目标 agent 节点存在）
```

- `$PR_NUMBER` 未注入时，从 A2A payload 的 `pr_number` 取；仍缺 → `grape git-repo pulls list --owner "$REPO_OWNER" --repo "$REPO_NAME" --state open --json-out` 找待审 PR，无法确定 → 评论说明并收尾（next_step=reply_comment）。
- **先读 `references/openspec-checklist.md`**（审查项唯一依据）。

### Step 1 · 拉取 PR 审查材料（必做，全只读）

```bash
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out       # 元信息 + head/base/sha
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out     # 变更清单 + patch diff 全文
grape git-repo commits --owner "$REPO_OWNER" --repo "$REPO_NAME" --json-out                                # commit message（按 head sha 对照）
grape git-repo pulls comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out  # 已有评论（防重复报告）
```

- 新增文件需要全文核查时：`grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path "{path}" --ref "{head_sha}" --raw`。
- 大 PR（变更文件 > 数百）：分批聚焦——优先审查新增/修改文件 diff 与全部 commit message，纯删除/二进制只登记不改写。
- 已有同轮报告评论（前缀 `[QA Agent]` 且对应最新 head sha）→ 不重复发布，直接收尾。

### Step 2 · 逐章审查（必做）

对每个检查项（`openspec-checklist.md` §0-§7 逐条）：

1. **敏感信息扫描（§1.1）**：对 patch diff 与新增文件内容逐条比对 §1.1 模式清单（云厂商密钥 `AKIA…`/密钥 PEM/GitHub PAT `ghp_…`/GitLab PAT `glpat-…`/数据库口令明文/SSH 口令/内网网段 `192.168.*` `10.*` `172.16-31.*`/弹性公网 IP/`*.intra` 域名/内部账号/私有依赖源）。命中 → 证据定位（文件+diff 行号）。
2. **commit message 审查（§2）**：逐条校验英文 + Conventional Commits（`feat|fix|docs|refactor|chore(scope): ...`）+ `(Closes #N)`/`(Issue #N)` 后缀保留。
3. **删除/替换清单对照（§1.4）**：新增文件命中删除路径（configs 真实配置、`.env*`、含拓扑/口令的 deploy 等）→ 不符合；内部文档（`docs/internal*`）仅要求占位符脱敏、不判删除。
4. **收尾质量清单（§3）**：涉及 LICENSE/README/.gitignore/CI 配置等改动时逐项对照。
5. 每项结论三态：**符合 / 不符合（附证据）/ 不适用（PR 未涉及）**；证据不足或需业务判断（如账号是否内部人员）→ **需人工复核**，不直接判死。

### Step 3 · 汇总报告（必做）

报告结构（详见 `openspec-checklist.md` §报告模板）：

```
[QA Agent] PR #{N} 开源合规审查报告（head sha: {sha}）
## 总体结论：✅ 通过 / ❌ 不通过（N 项不符合，M 项需人工复核）
## 逐项明细表：| 规范章节 | 检查项 | 结论 | 证据定位 |
## 整改建议清单：逐条「不符合项 → 建议修复方式」
```

- 敏感值一律掩码（`ghp_****`），只留定位。
- 结论不通过时，整改建议按规范章节给出（脱敏删除/替换占位符/commit message 规范化等）。

### Step 4 · 发布 PR 评论（必做）

```bash
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[QA Agent] PR #'"$PR_NUMBER"' 开源合规审查报告（head sha: {sha}）\n## 总体结论\n..."}'
```

- 返回的 comment id 记入 `tasks complete` 的 artifacts。
- 发布失败重试 ≤3 次；仍失败 → Issue 评论说明 + 标注人工介入。

### Step 5 · 收尾（必做，任何分支都执行到最后）

1. 输出人类可读摘要：PR 编号、检查项计数、结论、评论 id。
2. 审查不通过需 A2A 交办整改前，**先做场景节点核验**（必须 A2A 前）：
   ```bash
   echo "scene_id=${GRAPE_SCENE_ID:+set}"
   SCENE_NODES="$(grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out 2>/dev/null)"
   echo "$SCENE_NODES" | jq -r '[.nodes[]?.tag // .nodes[]?.skill] | unique | join(",")' 2>/dev/null
   ```
   - 命中 coding 节点 → 可 A2A；未命中 → **评论说明「当前场景未配置 coding 整改环节」+ 不 A2A**，直接收尾（next_step=reply_comment/done）。
3. `grape tasks complete`（见 SKILL.md 任务完成流程模板；结构化字段必填）。
   - 通过且无需整改 → `next_step` 按流程（如 `a2a_sre` 或 `done`）。
   - 不通过需整改 → 必要时 `a2a handoff --json '{"agent":"coding","payload":{"repository":"...","account":"...","issue_number":N,"pr_number":P,"context":"审查报告列明 N 项不符合，请按整改建议修复"}}'`（交办 agent 名 coding，非技能名；**仅 scene 命中 coding 节点时发起**）。
4. 调完即结束任务，不继续等待。

## 错误排查速查

| 现象 | 处理 |
|------|------|
| `pulls files` 无 patch 字段 | 大 diff 被截断 → 用 `git-repo contents --ref {head_sha}` 拉全文；仍缺 → 标「需人工复核」 |
| `commits` 未含 PR head | 用 `pulls get` 的 head sha 过滤；per_page 不足加大分页 |
| 5xx | 用 `grape tasks wait` / 内置退避重试，不 sleep 固定时长 |
| 401/403 | 按安全硬约束处理，不换 token |
| PR 已关闭/已合并 | 照常审查存量 diff；报告注明当前 PR 状态 |

## 明确禁止

- ❌ 修改文件 / `grape git add|commit|push` 等任何 git 写操作 / `pulls merge`。
- ❌ 报告复述完整密钥、口令、内网 IP 完整拓扑（掩码处理）。
- ❌ 凭记忆转述规范（一律对照 `openspec-checklist.md`）。
- ❌ 无证据直接判「不符合」（存疑 → 需人工复核）。
- ❌ 重复发布同轮报告 / 跳过 Step 5 收尾。
