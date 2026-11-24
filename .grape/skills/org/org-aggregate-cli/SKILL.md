---
name: org-aggregate-cli
version: 1.1.0
description: |
  组织结果聚合（org_aggregate 节点）：读取 #2112 批次详情，汇总目标仓库的子 Issue、
  任务、PR 与状态；全终态时输出结论并幂等写入组织 Issue，未全终态时输出 remaining
  并由服务端安排下一轮聚合。
tools:
  - grape
  - bash
---

# 组织结果聚合（org-aggregate-cli v1.1 · grape CLI）

## 一、安全硬约束（必须遵守）

1. **CLI 唯一通道**：平台交互只走 `grape` CLI，禁止 curl/python httpx 直连平台 API。
   CLI 未装/版本过低 → **自愈安装/升级**
   （`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`）→ 装好
   再验；仍不可用 → 输出「前置检查未通过：<原因>」并结束。安装指引见
   `references/grape-cli-installation.md`。
2. **只用系统注入的 token**：认证只用 `$GRAPE_SESSION_TOKEN`（grape CLI 自动读取）；
   严禁从 `data/`、`configs/`、`sessions.db`、日志或历史记录寻找其他 token；
   token 不打印、不写盘、不写评论。
3. **组织边界**：只聚合 `$ORG_ID` 当前组织的批次；`dispatch_id` 必须来自任务上下文
   （`grape tasks current` / `grape events detail --seq`）或 `grape org dispatches list`，
   **不得猜测** org_id / dispatch_id。
4. **不伪造终态**：target 状态、子 Issue、PR 必须来自 `grape org dispatches detail`；
   未全终态不得输出完成结论，未拿到证据不得声称「已完成/已合入」。
5. **不提前交办**：`org_aggregate` 是末节点（`next=done`、`handoff=complete`），
   不得调用 `grape a2a handoff`；由服务端在 `tasks complete` 时判定收尾或安排下一轮。
6. **401/403 严格区分**：401=token 失效 → 报告「凭据已失效」并停止；403=权限不足 →
   报告「需要组织成员权限」，不换 token、不绕行。
7. **评论幂等**：服务端在批次进入终态时已幂等写入聚合评论并归一组织 Issue 状态；
   若已存在本批次 `[Org Aggregate]` 评论，本节点只复核、不重复评论。
8. **写操作纪律**：聚合评论属流程内动作，可直接执行；删除评论、关闭/修改组织 Issue
   状态需人工确认（服务端幂等收口除外）。

## 二、环境变量与任务上下文

| 变量 / 来源 | 语义 | 用在哪 | 何时有 |
|------------|------|--------|--------|
| `GRAPE_SERVER_URL` / `GRAPE_SESSION_TOKEN` | 服务端地址 / 会话 token（tk_）| grape CLI 自动读取 | 全部任务 |
| `ORG_ID` / `GRAPE_ORG_ID` | 当前组织 ID | `--org_id` | 组织场景任务 |
| `ORG_ISSUE_NUMBER` | 组织 Issue 编号（数字 ident）| 评论 `--number`；list 过滤 | 组织场景任务 |
| `REPO_FULL_NAME` | 组织仓库全名（owner/repo）| 评论 `--owner`/`--repo` | 全部任务 |
| `REPO_OWNER` / `REPO_NAME` | 组织仓库属主 / 仓库名 | CLI `--owner` / `--repo` | 全部任务 |
| `DISPATCH_ID` | 批次 ID（任务 payload 注入）| `--dispatch_id` | 终态钩子/重排任务 |
| `aggregate_round` | 聚合轮次（payload 可选）| 输出与日志标注 | 重排任务 |

`DISPATCH_ID` 的获取顺序（不得猜测）：

```bash
# 1) 任务上下文（首选）
grape tasks current --json-out
# 2) 从事件 payload 读取（任务上下文未带时）
grape events detail --seq "$SOURCE_SEQ" --json-out
# 3) 兜底：按组织 Issue 查最近批次（取最新一条在途/终态批次）
grape org dispatches list --org_id "$ORG_ID" \
  --org_issue_number "$ORG_ISSUE_NUMBER" --limit 20 --offset 0 --json-out
```

仍无法确定 `dispatch_id` → 输出 `DISPATCH_ID_REQUIRED` 并结束，**不聚合、不写结论**。

## 三、前置检查门（自愈 → 重试 → STOP）

| 检查项 | 判定命令 | 通过标准 | 失败动作 |
|--------|---------|---------|---------|
| CLI 可用 | `grape --version` | ≥0.3.2 | **自愈**：按 `references/grape-cli-installation.md` 安装/升级 → 再验证；仍失败 → 输出「前置检查未通过：<原因>」结束 |
| 契约刷新 | `grape refresh` | 拉取最新契约成功 | **重试** ≤2 次；仍失败 → 继续（回退内置快照），命令「不识别」时再 refresh |
| 命令组存在 | `grape commands "org dispatches"` | 含 `detail` | 缺失 → `CLI_CONTRACT_UNAVAILABLE`，STOP（禁止裸 HTTP 兜底）|
| 组织上下文 | `grape tasks current --json-out` | 能解析 `ORG_ID` / 组织 Issue / dispatch | 缺失 → 用 `list --org_issue_number` 兜底；仍缺失 → STOP |
| 批次可读 | `grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out` | 返回 dispatch + targets | 404 → `DISPATCH_NOT_FOUND`；403 → STOP |
| 瞬时失败（网络/限流）| — | — | **重试** ≤3 次（退避 1s/2s/4s）|

> 组织运行态接口是 **dual scope**：worker 的 `$GRAPE_SESSION_TOKEN` 可调用
> `detail`；服务端仍执行组织成员校验。命令组固定为 `grape org dispatches ...`，
> 不是 `grape orgs dispatches ...`。

## 四、职责边界

**职责**：批次结果的**只读聚合器 + 收口记录**——读 detail → 归一 target 终态 →
输出结论或 `remaining` → 写评论（幂等）→ `tasks complete`。不改子 Issue、不重试
target、不取消批次（这些是管理操作，交人工/Web 端）。

| 行为 | 态度 |
|------|------|
| 读取 dispatch detail 与每个 target 字段 | ✅ **必须做** |
| 归一 target 终态并给出 dispatch 结论 | ✅ **必须做** |
| 未全终态时输出 `remaining` + 等待语义 | ✅ **必须做** |
| 全终态时幂等写组织 Issue 聚合评论 | ✅ 必须做（已有评论则只复核）|
| 调用 `grape tasks complete` 释放执行资源 | ✅ **必须做**（每轮都调用）|
| 调用 `grape a2a handoff` 推进下一节点 | ❌ 禁止（末节点、服务端判定收尾）|
| 提前输出完成结论 / 置 Issue 完成 | ❌ 禁止 |
| 修改子 Issue / 重试 target / 取消批次 | ❌ 禁止（管理操作，交人工）|
| 伪造 target 状态 / PR / 子 Issue | ❌ 禁止 |

## 五、执行流程

1. **确定 dispatch_id**（见「二、环境变量与任务上下文」）。
2. **读取批次与目标**：

   ```bash
   grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
   ```

3. **对每个 target 提取**：`repo_key`、`child_issue_number`、`child_task_id`、
   `pr_number`、`status`、`error`、`done_when`、`started_at`、`finished_at`。
4. **终态归一**：`completed`、`needs_review`、`failed`、`cancelled` 是终态。
   `needs_review` = 子任务已结束但待人工审核（如 QA 通过待合入/待确认），属于终态，
   不得再按「未终态」无限重排。`pending`/`creating`/`running` 为非终态。
5. **未全终态** → 输出 `remaining`，明确每个剩余 target 的 repo 与当前 status；
   不得提前完成聚合。**最终答复必须显式包含这句等待语义**：
   `仍有 target 未终态，等待下一轮聚合（next_step: a2a-qa）`，让服务端终止钩子
   即使早于 `tasks complete` 收到结果，也不会误置 `pending_review`。
6. **全部终态** → 按 dispatch 状态输出：
   - `completed`：全部完成，给出完成结论。
   - `needs_review`：子任务已结束但有目标待人工审核，列出待审目标与子 Issue。
   - `partial_failed`：部分 target failed，列出成功与失败目标。
   - `failed`：全部 target failed，给出失败原因。
   - `cancelled`：编排已取消，列出取消范围。
7. **写组织 Issue 评论（幂等）**：

   ```bash
   grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
     --number "$ORG_ISSUE_NUMBER" --json '{"body":"[Org Aggregate] ..."}'
   ```

   评论只包含结论、目标状态、子 Issue/任务/PR 引用和错误摘要。
   若组织 Issue 已存在本批次的 `[Org Aggregate]` 聚合评论（服务端终态钩子已写），
   本步骤只做复核，不重复评论。
8. **任务收尾**：见「七、任务收尾」。

## 六、输出契约

```json
{
  "dispatch_id": "dp_...",
  "status": "running",
  "remaining": [
    {"repo_key": "gitcode:org/repo-b", "status": "running"}
  ],
  "targets": []
}
```

字段语义与终态映射见 `references/aggregation.md`。失败或非终态不得伪造完成评论。
输出建议同时给人类可读摘要（status / targets / remaining / 证据链）。

## 七、任务收尾（每轮必做）

- **全部 target 未终态时**：输出 `remaining` 并明确说明本轮未完成，**本轮仍须调用
  `grape tasks complete` 释放执行资源**，并把 `next_step` 写成 `a2a-qa`（自指聚合节点）。
  服务端识别到 `remaining`/`next_step=a2a-qa` 后不会把 Issue 提前置 `pending_review`，
  而是延迟安排下一轮 `org_aggregate` 重新读取 dispatch；目标进入终态后自然收口。
  不得把 Issue 置为完成。

```bash
grape tasks complete --json '{
  "result_text": "本轮聚合未完成：仍有 target 处于非终态，等待下一轮聚合。",
  "actions": ["读取 dispatch detail", "汇总 target 状态"],
  "conclusion": "仍有 target 未终态，输出 remaining 并等待下一轮聚合",
  "artifacts": ["remaining 列表"],
  "issue_summary": "组织聚合等待中 | remaining=<N>",
  "next_step": "a2a-qa"
}'
```

- **全部 target 终态时**：写入组织 Issue 结论评论后调用 `grape tasks complete`
  自报完成（`next_step` 留空或写 `done`）；服务端按场景 `next=done` 收尾，
  不产生额外 A2A 事件。

```bash
grape tasks complete --json '{
  "result_text": "组织下派批次已收口：status={status}，targets={n}",
  "actions": ["读取 dispatch detail", "汇总 target 状态", "写组织 Issue 结论评论"],
  "conclusion": "{completed/needs_review/partial_failed/failed/cancelled 结论}",
  "artifacts": ["targets 列表（repo/child_issue/pr/status）", "组织 Issue 评论"],
  "issue_summary": "组织需求 #{n} 聚合完成 | status={status}",
  "next_step": "done"
}'
```

⚠️ 结构化字段必须真实填充；只传 `result_text` 会丢失下游归档。

## 八、参考文件清单

| 文件 | 用途 |
|------|------|
| `references/aggregation.md` | 终态映射、remaining 语义与评论模板 |
| `references/grape-cli-installation.md` | grape CLI 安装/升级指引（前置门自愈用）|

> 命令参数不确定时，先 `grape <命令组> --help` 查精确 flag，禁止凭记忆拼写。