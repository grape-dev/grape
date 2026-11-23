# QA-MattPocock 执行走查表（runbook）

> 目的：把「两轴审查 → 分轴报告 → 分流」的**步骤 + 确切命令**固化，Agent **照着执行、不要查 `--help`**。
> 环境变量：`$REPO_OWNER`/`$REPO_NAME`/`$ISSUE_NUMBER`/`$PR_NUMBER`/`$REPO_FULL_NAME`/`$GRAPE_WORK_BRANCH`。owner 只用 `$REPO_OWNER`。
> 评论前缀硬规：`[QA Agent]`。
> 防抖纪律：一条命令取到即继续；命令失败先看完整错误；owner 不硬猜。

---

## 统一流程

### Step 0 · 前置 + 上下文（必须最先）
```bash
grape --version; grape refresh
grape tasks current --json-out
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape git-repo pulls comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
```

### Step 1 · 固定审查点 + 找 Spec + 收集 standards 源（必做）
```bash
# 固定点：HEAD..merge-base（三分点）与提交列表
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out   # base/head
# Spec 来源：commit 关联 Issue/Spec 文件（GitCode 本地用 issues detail 找）
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path GLOSSARY.md --raw 2>/dev/null  # 领域词汇
grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path {CODING_STANDARDS|CONTRIBUTING}.md --raw 2>/dev/null
```
- 找 Spec：Issue 评论（mattpocock Spec）/ Spec 文件；找不到 → 问用户；无 Spec → Spec 轴跳过并注明。
- standards：repo 文档 + 异味基线（见 `two-axis-review-guide.md`）。

### Step 2 · 两轴并行子代理审查（必做）
同时发起两个子代理（Standards / Spec），互不共享上下文，聚合报告：

- **Standards 子代理**：diff + standards 文件 + 异味基线（全文）；报告（a）违反文档规范（引用 文件+规则）、（b）基线异味（命名 + 引用 hunk）；区分硬违规 vs 判断项；工具已强制的跳过；<400 词。
- **Spec 子代理**：diff + Spec 全文；报告（a）Spec 要求但缺失/部分（引 Spec 行）、（b）diff 中未要求的（scope creep）、（c）实现可疑；<400 词。
- 本地执行：用 `grape git diff`/读取仓库文件喂给子代理；或按工具能力开并行子进程（agent 自实现表单代理）。

### Step 3 · 聚合分轴报告（必做）
发布 PR/Issue 评论（body 以 `[QA Agent]` 开头）：
```bash
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[QA Agent] 两轴审查报告\n\n## Standards\n{分轴结果}\n\n## Spec\n{分轴结果}\n\n{汇总 + merge-danger 门}"}' --json-out
```
- 两轴分别 `## Standards` / `## Spec`，**不合并排名**；末尾单行各轴 total + 该轴最差项 + **merge-danger 门**（door 单向/双向 + blast radius）。

### Step 4 · 分流（必做）
- **发现需修** → A2A coding-mattpocock（agent 名 coding，带两轴清单）：
```bash
grape a2a handoff --json '{
  "agent": "coding",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "两轴审查发现：<Standards {n} 项 + Spec {m} 项清单>。请修复并更新 PR，随后回 QA 复审。"
  }
}' --json-out
```
- **通过** → 评论「合入提示」（本场景无 sre，合入/部署交人工）：
```bash
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[QA Agent] 两轴审查通过（Standards 0 / Spec 0）。merge-danger: {door}/{blast}。可人工合入。"}' --json-out
```

### Step 5 · 任务完成流程（必做）
按 SKILL「任务完成流程」调用 `grape tasks complete`（next_step=`a2a_coding` 或 `reply_comment`）。`already:true` 忽略，结束任务。

---

## 错误排查速查

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 命令名错，核对 runbook/reference；仍不确定 `grape commands <关键词>` |
| `unknown request field(s): ...` | `--json` 字段越界，看 `grape <g> <v> --help` Body fields 删多余 |
| PR 评论 405（已合并） | 改发 Issue 评论 |
| 401/403 | 说明凭据失效/需 `grape_`，不换 token |
| 找不到 Spec | 先问用户；无 Spec 则 Spec 轴跳过并注明 |

## 明确禁止

- ❌ 合并双轴排名 / 挑跨轴 winner。
- ❌ 直接合入 PR / 修改代码 / 删除 PR。
- ❌ 虚构证据 / 未观察标"已确认"。
- ❌ 无 Spec 却臆断 Spec pass。
- ❌ 裸 `git`（一律 `grape git`）。
- ❌ 不因"等待"跳过 Step 收尾（tasks complete）。