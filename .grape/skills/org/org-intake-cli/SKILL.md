---
name: org-intake-cli
version: 1.1.0
description: |
  组织需求接收（org_intake 节点）：读取组织 Issue、组织仓库与仓库地图，选择下派目标并
  产出 dispatch plan，再通过 A2A 交给 org_dispatch。只负责接收与规划——不创建下派批次、
  不写子 Issue、不改组织 Issue 状态。
tools:
  - grape
  - bash
---

# 组织需求接收（org-intake-cli v1.1 · grape CLI）

## 一、安全硬约束（必须遵守）

1. **CLI 唯一通道**：组织流水线的平台交互只走 `grape` CLI，禁止绕过 CLI 直连平台 API
   （curl/python httpx 等）。CLI 未装/版本过低 → **自愈安装/升级**（
   `timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`，安装可能无
   输出，必须加 timeout 防误判）→ 装好再验；仍不可用 → 输出「前置检查未通过：<原因>」
   并结束任务（不降级直连）。安装与升级指引见 `references/grape-cli-installation.md`。
2. **只用系统注入的 token**：`$GRAPE_SESSION_TOKEN` 由平台注入、grape CLI 自动读取。
   严禁从 `data/`、`configs/`、`sessions.db`、日志、历史记录或任何文件中寻找、读取、
   猜测其他 token；token 不打印、不写盘、不写评论。
3. **组织边界**：只处理 `$ORG_ID` 对应组织仓库中的 Issue。不得猜测组织 ID——历史事故：
   intake 侧猜测 `/api/orgs/1|2|3` → 全部 400/404，导致自动下派失效。`$ORG_ID` 缺失时
   按「三、前置检查门」用 CLI 定位，定位不到立即 STOP。
4. **只接收不执行**：本技能只产出 dispatch plan 并交办 `org_dispatch`。**禁止**创建下派
   批次、写子 Issue、选择目标仓库的最终场景、修改组织 Issue 状态（这些属于
   `org-dispatch-cli` 与平台职责）。
5. **不伪造成功**：Issue / 仓库地图 / 场景读不到，或 A2A 交办返回失败、被拦截，必须
   输出真实错误码与可读原因。禁止编造 targets、dispatch plan、交办结果或「已下派」结论。
6. **401/403 严格区分**：401=任务级 token 失效 → 说明「凭据已失效」并停止该操作，
   不换 token、不绕行；403=权限不足 → 报告「需要组织成员或 owner/admin 权限」，
   停止写操作，等待人工配置。
7. **写操作纪律**：发组织 Issue 评论、A2A 交办属流程内动作，可直接执行；删除、取消、
   改状态等破坏性操作必须人工确认。
8. **幂等**：同一组织 Issue 重复进入本节点时，先读已有评论/计划并复用，不重复刷评论。
   A2A 建议带幂等键 `org-<issue>:coding`；同键重试命中终态旧事件时，使用 `force=true`
   或新轮次键（如 `org-<issue>:coding:r2`），不得因「重复」静默停摆。

## 二、环境变量表（平台注入，org 上下文）

| 变量 | 语义 | 用在哪 | 何时有 |
|------|------|--------|--------|
| `GRAPE_SERVER_URL` / `GRAPE_SESSION_TOKEN` | 服务端地址 / 会话 token（tk_）| grape CLI 自动读取 | 全部任务 |
| `ORG_ID` / `GRAPE_ORG_ID` | 当前组织 ID | `--org_id`；组织权限校验 | 组织场景任务 |
| `ORG_ISSUE_NUMBER` | 组织 Issue 编号（数字 ident）| `--number`；dispatch plan | 组织场景任务（数字编号时）|
| `REPO_FULL_NAME` | 组织仓库全名（owner/repo）| A2A `repository`；repo_key 拼装 | 全部任务 |
| `REPO_OWNER` / `REPO_NAME` | 组织仓库属主 / 仓库名 | CLI `--owner` / `--repo` | 全部任务 |
| `ACCOUNT` | 触发人账号（actor）| A2A payload `account` | 全部任务 |
| `GRAPE_SCENE_ID` | 当前组织场景 ID | `grape scenes detail` 校验节点链 | 场景任务 |
| `DEVOPS_EVENT_TYPE` | 事件类型 | 入口/路由判断 | 全部任务 |

派生变量：`REPO_OWNER` = `${REPO_FULL_NAME%/*}`、`REPO_NAME` = `${REPO_FULL_NAME#*/}`。
`ORG_ISSUE_NUMBER` 仅在数字 ident 时注入；Gitee `IK*` 等字符串 ident 缺失时，用
`grape tasks current --json-out` 或 `grape git-repo issues get` 兜底定位。
敏感变量（token 类）**不 printenv、不落盘**。

## 三、前置检查门（自愈 → 重试 → STOP）

| 检查项 | 判定命令 | 通过标准 | 失败动作 |
|--------|---------|---------|---------|
| CLI 可用 | `grape --version` | ≥0.3.2 | **自愈**：按 `references/grape-cli-installation.md` 安装/升级 → 再验证；仍失败 → 输出「前置检查未通过：<原因>」结束 |
| 契约刷新 | `grape refresh` | 拉取最新契约成功 | **重试** ≤2 次；仍失败 → 继续（回退内置快照），命令「不识别」时再 refresh |
| 命令组存在 | `grape commands "org repo map"` + `grape commands "org dispatches"` | 下派命令组含 `create`/`list`/`detail` | 缺失 → `CLI_CONTRACT_UNAVAILABLE`，STOP（禁止裸 HTTP 兜底）|
| 组织上下文 | `grape tasks current --json-out` | 能解析 `ORG_ID` / 组织 Issue 编号 / 仓库 | 缺失 → 用 `grape git-repo issues get` 定位；定位不到 → STOP（不写操作）|
| 组织仓库可读 | `grape org repos list --org_id "$ORG_ID" --json-out` | 返回组织仓库列表 | 403 → STOP；空列表 → 输出 `NO_TARGETS` |
| 场景可读 | `grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out` | 含 `org_intake`/`org_dispatch`/`org_aggregate` 或简化链路 | 缺失 → STOP，输出场景配置错误 |
| 瞬时失败（网络/限流）| — | — | **重试** ≤3 次（退避 1s/2s/4s）|

> 组织运行态接口是 **dual scope**：worker 的 `$GRAPE_SESSION_TOKEN` 可调用，
> 但服务端仍按组织成员/管理员校验。命令组由 `org_*` operationId 生成，固定为
> `grape org ...`，不是 `grape orgs ...`。

## 四、定位与职责边界

**职责**：把组织需求转换为可执行的 dispatch plan——组织 Issue + 组织仓库 + 仓库地图
→ 目标仓库与验收条件 → 评论留痕 → A2A 交办 `coding`（即 `org_dispatch` 节点）。

| 行为 | 态度 |
|------|------|
| 读取组织 Issue、仓库地图、目标仓库知识 | ✅ **必须做**（先读后写）|
| 依据显式 targets 或服务端召回生成 plan | ✅ **必须做** |
| 写组织 Issue 评论（计划摘要/目标/验收）| ✅ 必须做（幂等）|
| A2A 交办 `coding` 并校验 `handoff_result` | ✅ **必须做** |
| 结束任务时调用 `grape tasks complete`（结构化字段）| ✅ **必须做** |
| 自行决定最终场景 / 创建下派批次 / 写子 Issue | ❌ 禁止（org-dispatch 职责）|
| 用全部关联仓库当 targets 兜底 | ❌ 禁止（无召回就显式选或标记补能力地图）|
| 伪造 dispatch plan / 交办成功 | ❌ 禁止 |
| curl / 裸 HTTP 直连平台 API | ❌ 禁止 |
| 读取 token 或 Grape 内部数据 | ❌ 禁止 |

**入口**：组织仓库 Issue 绑定组织场景后，`issue_opened` 由首节点 `org_intake` 启动；
后续评论/A2A 事件同样进入本节点。调用方必须处于对应组织上下文。

## 五、执行流程（先读后判，禁止跳过）

**流程总览（一行）**：⓪ 任务上下文 → ① 读组织 Issue → ② 校验场景/组织仓库 →
③ 选择 targets → ④ 读目标仓库上下文 → ⑤ 生成 dispatch plan → ⑥ 写评论 →
⑦ A2A 交办 → ⑧ tasks complete。

0. **读取任务上下文**：`grape tasks current --json-out`（必要时
   `grape events detail --seq <seq> --json-out` 读取前序 A2A payload）。确认本任务
   属于组织场景，记录 `ORG_ID`、组织 Issue 编号、`GRAPE_SCENE_ID`。
1. **读取组织 Issue 标题、正文与目标提示**：

   ```bash
   grape git-repo issues get --owner "$REPO_OWNER" --repo "$REPO_NAME" \
     --number "$ORG_ISSUE_NUMBER" --json-out
   ```

2. **校验场景与组织仓库**：

   ```bash
   grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out
   grape org repos list --org_id "$ORG_ID" --json-out
   ```

   场景必须包含组织接收/下派/聚合节点（或对应的简化链路）；组织仓库列表用于校验
   显式 targets 是否已关联。核对组织仓库与关联仓库后再继续，禁止跳过读取直接「规划」。
3. **选择下派目标**（全自动优先）：

   ```bash
   # 可选：读取召回详情用于计划摘要/原因说明
   grape refresh
   grape commands "org repo map"
   grape org repo map recall --org_id "$ORG_ID" \
     --q "$ORG_ISSUE_TITLE $ORG_ISSUE_BODY" --limit 5 --json-out
   ```

   - 请求中存在显式 `targets` 时，以显式列表为准；
   - 否则 **不要**自行使用全部已关联仓库，也 **不要**把仓库列表当 targets：
     dispatch plan 中 `targets` 留空 `[]`，由服务端按组织仓库地图自动召回并下派；
   - 多仓需求允许 plan 中 `targets` 包含多个 `repo_key`；服务端会为每个目标独立建
     子单，并把该仓的职责/接口/不负责边界写入子单，按各自仓库流水线执行；
   - 可以调用 `org repo map recall` 读取推荐结果，用于在 plan/评论里解释「为什么派给
     这些仓库」；最终仍以服务端 `org dispatches create` 的自动召回结果为准；
   - 服务端只返回达到置信度阈值的目标仓库；无命中时返回 `NO_CONFIDENT_TARGET`，
     不会回落全部仓库。此时由 intake agent 基于候选与 `summary.md` 显式选择 targets
     （写进 plan），或标记需求需要补充仓库能力地图，禁止把全部关联仓库当 targets。
4. **读取目标仓库知识与可解析场景**：对每个候选/显式目标读取仓库职责、接口、依赖与
   可用场景。显式 `scene_id` 优先；否则交给下派层按目标仓库默认场景解析。只做
   可行性判断，不替目标仓库做最终选场景。
5. **生成 dispatch plan**（字段约束见「六、Dispatch Plan」与
   `references/dispatch-plan.md`），并写入组织 Issue 评论或结构化 artifact。
   禁止在未读取目标前伪造成功结果。
6. **写组织 Issue 评论**（计划摘要/目标仓库/验收条件，不含 token 与内部请求头）：

   ```bash
   grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
     --number "$ORG_ISSUE_NUMBER" --json '{"body":"[Org Intake] dispatch plan ..."}'
   ```

7. **A2A 交办**（见「八、A2A 交接」）。
8. **任务收尾**（见「九、任务收尾」）。

## 六、Dispatch Plan

计划 JSON 必须包含 `org_issue_number`、`targets`、`context`、`acceptance`。
完整字段约束见 `references/dispatch-plan.md`。

```json
{
  "org_issue_number": 123,
  "targets": ["gitcode:org/repo-a", "gitcode:org/repo-b"],
  "title": "组织需求标题",
  "description": "组织需求正文",
  "scene_id": "",
  "done_when": "issue_done",
  "context": {
    "summary": "需求摘要",
    "constraints": [],
    "decisions": [],
    "knowledge_refs": [],
    "acceptance": ["验收条件"]
  }
}
```

- `targets` 每项必须是 `provider:owner/repo`；留空 `[]` = 服务端自动召回。
- `scene_id` 是批次级可选值；留空表示按各目标仓库默认场景解析。
- `done_when` 可选 `issue_done`（默认）/`pr_merged`/`task_terminal`，按需求验收强度选择；
  非法值会被服务端以 `INVALID_DONE_WHEN` 拒绝。

## 七、失败输出（错误码与可读原因）

- `NO_TARGETS`：请求没有显式 targets，组织也没有可召回的关联仓库。
- `NO_CONFIDENT_TARGET`：仓库地图无达到阈值的目标——显式选择候选或标记补能力地图，
  禁止回落全部关联仓库。
- `INVALID_TARGET`：目标格式不是 `provider:owner/repo`，或未关联当前组织。
- `NO_TARGET_SCENE`：目标既无显式可用场景，也无默认或唯一可用场景。
- `PERMISSION_DENIED`：调用方不是组织成员或没有组织上下文。
- `CLI_CONTRACT_UNAVAILABLE`：CLI 未暴露下派命令组。

失败时输出错误码与可读原因，不得伪造成功或 dispatch plan。所有 Grape/GitCode 访问
只使用注入的 `$GRAPE_SESSION_TOKEN`，不得打印、写盘或回显 token。

## 八、A2A 交接（流水线推进，必做）

产出 dispatch plan 后必须通过 A2A 交给 `org_dispatch` 节点，否则流水线停在
`org_intake`。组织节点 tag 复用 `coding`，因此 A2A `agent` 固定为 `coding`；
服务端按 Issue 场景的 tag/id 匹配节点，不需要传 `org_dispatch`（不在公开
agent 白名单内）。

```bash
grape a2a handoff --json '{
  "agent": "coding",
  "idempotency_key": "org-'"$ORG_ISSUE_NUMBER"':coding",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": "'"$ORG_ISSUE_NUMBER"'",
    "context": "组织需求已接收，dispatch plan：{targets, scene_id, done_when, acceptance}。请执行 grape org dispatches create 下派。"
  }
}' --json-out
```

- **必须校验响应**：`handoff_result` 为 `ok` 且 `seq>0` 才算交办成功；
  `rejected`（同仓同角色门禁/场景拦截，如 `409 not_in_scene`）与 `failed`（派发链路失败）
  都必须显式输出原因。
- 交办失败 → 重试 ≤3 次（退避 1s/2s/4s，同幂等键；命中终态旧事件时用 `force=true`
  或新轮次键）→ 仍失败则写入组织 Issue 评论并标注需人工介入，**不得伪造成功**。
  此时仍须调用 `grape tasks complete` 释放资源：服务端自动推进钩子会再尝试一次
  A2A；仍失败则流水线停在当前节点等待人工处置。
- 交办成功后不得再声称流水线当前节点未完成；下一步由 `org-dispatch-cli` 执行。

## 九、任务收尾（必做）

交办完成后输出人类可读摘要，并调用：

```bash
grape tasks complete --json '{
  "result_text":  "组织需求接收完毕：targets={n}，已交办 org_dispatch",
  "actions":      ["读取组织 Issue", "校验组织场景/仓库", "选择 targets: {显式/自动召回}", "生成 dispatch plan", "评论: 组织 Issue", "A2A: coding"],
  "conclusion":   "dispatch plan 已生成并交办 org_dispatch",
  "artifacts":    ["dispatch plan（targets/scene_id/acceptance）", "组织 Issue 评论"],
  "issue_summary":"组织需求 #{n} 已接收，targets={列表或自动召回}",
  "next_step":    "a2a_coding"
}'
```

⚠️ 结构化字段（`actions[]`/`conclusion`/`artifacts[]`/`issue_summary`）必须真实填充；
只传 `result_text` 会丢失下游归档。某动作未完成（如交办失败）时如实说明，不伪造。

## 十、参考文件清单

| 文件 | 用途 |
|------|------|
| `references/dispatch-plan.md` | dispatch plan 字段契约、target 选择规则与 A2A 契约 |
| `references/grape-cli-installation.md` | grape CLI 安装/升级指引（前置门自愈用）|

> 命令参数不确定时，先 `grape <命令组> --help` 查精确 flag，禁止凭记忆拼写。