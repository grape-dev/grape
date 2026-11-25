---
name: org-dispatch-cli
version: 1.1.0
description: |
  组织需求下派（org_dispatch 节点）：调用 #2112 下派契约，校验 dispatch plan、
  为目标仓库选择可执行场景并创建批次，输出 dispatch_id 与各目标状态。
  创建成功后不交办聚合——批次进入终态由服务端自动触发 org_aggregate。
tools:
  - grape
  - bash
---

# 组织任务派发（org-dispatch-cli v1.1 · grape CLI）

## 一、安全硬约束（必须遵守）

1. **CLI 唯一通道**：本技能只允许通过 `grape` CLI 调用 Issue #2112 契约，**禁止 curl、
   python httpx 或任何裸 HTTP 绕过 CLI**。CLI 未装/版本过低 → **自愈安装/升级**
   （`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`）→ 装好
   再验；仍不可用 → 输出「前置检查未通过：<原因>」并结束（不降级直连）。
   安装指引见 `references/grape-cli-installation.md`。
2. **只用系统注入的 token**：认证只用 `$GRAPE_SESSION_TOKEN`（grape CLI 自动读取）。
   严禁从 `data/`、`configs/`、`sessions.db`、日志或历史记录寻找其他 token；
   token 不打印、不写盘、不写评论。
3. **组织边界**：只对 `$ORG_ID` 当前组织、当前组织 Issue 下派；目标必须是该组织已关联
   仓库。**不得猜测 org_id**（历史事故：猜测 `/api/orgs/1|2|3|test_team` → 400/404，
   自动下派失效）；`$ORG_ID` 缺失时用 `grape tasks current --json-out` 定位，定位不到 STOP。
4. **幂等优先**：服务端按「组织 + 组织 Issue + 稳定 targets 集合」幂等；看到
   `DISPATCH_IN_PROGRESS` 必须先用 `list` 读取已有批次并复用，禁止新建第二个编排。
5. **不伪造状态**：子 Issue、dispatch_id、target 状态、场景选择理由必须来自
   CLI 返回；CLI 失败/降级/被拦截时如实输出错误码，不得把失败包装成成功。
6. **401/403 严格区分**：401=任务级 token 失效 → 报告「凭据已失效」并停止；
   403=权限不足（含 worker 调管理操作）→ 报告「需要组织 owner/admin 或 Web 端操作」，
   不换 token、不绕行。
7. **管理操作不在本节点执行**：`cancel` / `target retry` 是 user_only（JWT/API Key），
   worker 的 `$GRAPE_SESSION_TOKEN` 调用必 403。需要取消/重试时输出目标与原因，
   交由人工在 Web 端处理，不得伪造成功。
8. **写操作纪律**：创建下派批次、发组织 Issue 评论属流程内动作，可直接执行；
   取消批次、删除 Issue/评论等破坏性操作必须人工确认。

## 二、环境变量表（平台注入，org 上下文）

| 变量 | 语义 | 用在哪 | 何时有 |
|------|------|--------|--------|
| `GRAPE_SERVER_URL` / `GRAPE_SESSION_TOKEN` | 服务端地址 / 会话 token（tk_）| grape CLI 自动读取 | 全部任务 |
| `ORG_ID` / `GRAPE_ORG_ID` | 当前组织 ID | `--org_id` | 组织场景任务 |
| `ORG_ISSUE_NUMBER` | 组织 Issue 编号（数字 ident）| dispatch plan / 评论 | 组织场景任务（数字编号时）|
| `REPO_FULL_NAME` | 组织仓库全名（owner/repo）| A2A / repo_key 拼装 | 全部任务 |
| `REPO_OWNER` / `REPO_NAME` | 组织仓库属主 / 仓库名 | CLI `--owner` / `--repo` | 全部任务 |
| `ACCOUNT` | 触发人账号（actor）| A2A payload `account` | 全部任务 |
| `GRAPE_SCENE_ID` | 当前组织场景 ID | 校验场景节点链 | 场景任务 |
| `DEVOPS_EVENT_TYPE` | 事件类型 | 入口判断 | 全部任务 |

派生变量：`REPO_OWNER` = `${REPO_FULL_NAME%/*}`、`REPO_NAME` = `${REPO_FULL_NAME#*/}`。
`ORG_ISSUE_NUMBER` 缺失（Gitee `IK*` 等字符串 ident）时，用 `grape tasks current`
或 `grape git-repo issues get` 兜底。敏感变量（token 类）**不 printenv、不落盘**。

## 三、前置检查门（自愈 → 重试 → STOP）

| 检查项 | 判定命令 | 通过标准 | 失败动作 |
|--------|---------|---------|---------|
| CLI 可用 | `grape --version` | ≥0.3.2 | **自愈**：按 `references/grape-cli-installation.md` 安装/升级 → 再验证；仍失败 → 输出「前置检查未通过：<原因>」结束 |
| 契约刷新 | `grape refresh` | 拉取最新契约成功 | **重试** ≤2 次；仍失败 → 继续（回退内置快照），命令「不识别」时再 refresh |
| 下派命令组 | `grape commands "org dispatches"` | 列出 `create`/`list`/`detail`/`cancel`/`target retry` | 核心命令缺任一条 → `CLI_CONTRACT_UNAVAILABLE`，STOP；`scene catalog` 缺失不阻断，降级由服务端解析场景（禁止裸 HTTP 兜底）|
| 分析文档命令 | `grape commands "org repo analysis"` | 含 `docs` | 缺失 → 跳过文档、只用地图召回，并在输出标注降级 |
| 组织上下文 | `grape tasks current --json-out` | 能解析 `ORG_ID` / 组织 Issue / plan | 缺失 → `grape git-repo issues get` 定位；定位不到 → STOP |
| 认证可用 | `grape org dispatches list --org_id "$ORG_ID" --limit 1 --json-out` | 2xx | 401 → STOP；403 → STOP（权限问题）|
| 瞬时失败（网络/限流）| — | — | **重试** ≤3 次（退避 1s/2s/4s）|

> 组织运行态接口是 **dual scope**：worker 的 `$GRAPE_SESSION_TOKEN` 可调用
> `create`/`list`/`detail`/`context`/`scene catalog`；`cancel`/`target retry` 是
> user_only 管理操作（JWT/API Key），worker 调用必 403——不要尝试。
> 命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`，不是
> `grape orgs dispatches ...`。

## 四、职责边界

**职责**：把 `org-intake-cli` 的 dispatch plan 落成真实批次——校验 plan → 为每个目标
选择可执行场景 → `create` → 输出 dispatch/目标状态。**不等待子任务完成、不聚合、
不提前交办聚合节点**。

| 行为 | 态度 |
|------|------|
| 校验 plan 字段与目标归属 | ✅ 必须做 |
| 读取 AI 仓库分析文档 + 场景目录 | ✅ 必须做（选 targets / 选场景依据）|
| 为目标仓库选择 `executable=true` 场景并写 `scene_reasons` | ✅ 必须做 |
| `grape org dispatches create` 创建批次 | ✅ **必须做** |
| 输出 dispatch_id 与每个 target 状态（含 child_issue_number）| ✅ **必须做** |
| 结束任务时调用 `grape tasks complete` | ✅ **必须做**（不调用会占用执行资源）|
| 手动 A2A 到 `qa`/`org_aggregate` | ❌ 禁止（服务端终态钩子负责）|
| 轮询等待所有子任务完成 | ❌ 禁止（本节点不负责等待）|
| 伪造 dispatch/子 Issue/PR/场景 | ❌ 禁止 |
| curl / 裸 HTTP 直连平台 API | ❌ 禁止 |
| 用 worker token 调 `cancel`/`target retry` | ❌ 禁止（必 403，交人工）|

## 五、执行流程

0. **读取 AI 仓库分析文档（优先，必须走 CLI）**：

   ```bash
   grape org repo analysis docs --org_id "$ORG_ID" --json-out
   ```

   返回 `repos[]`：`repo_key / full_name / purpose / core_functions /
   core_responsibilities / other_capabilities / routing / domains / keywords /
   interfaces / depends_on / confidence / summary_md`，文档分支为
   `grape-org`。
   - 选仓优先看 `purpose`、`core_functions[].task_examples / not_for /
     owned_entities` 与 `core_responsibilities`；`routing` 是仓库级判别器；
   - 每个选中目标必须写清「命中了哪个核心能力」以及「为什么不是其他仓」；
   - `interfaces` / `keywords` 只作为命中和解释依据；
   - 用「组织需求 + 上述文档」做 AI 选仓，生成 `targets` 与每个目标的匹配理由；
   - `targets` 必须是组织关联仓库（组织自动仓库不参与下派）；
   - 低置信度（无明确匹配 / confidence 低）时回落到
     `grape org repo map recall --org_id "$ORG_ID" --q "$需求" --limit 5 --json-out`
     的确定性召回；仍无目标 → `NO_CONFIDENT_TARGET`，禁止静默全量下派；
   - 把选择理由写入 `context.decisions` 与 description，便于审计。
   - 该命令不可用（老版本 CLI）时，跳过本步、只用地图召回，并在输出标注「文档降级」。
1. **为已选 targets 选择场景**

   对每个 target 调用场景目录（**不要读取组织仓库自己的场景**；候选必须是 target
   仓库的）：

   ```bash
   grape org dispatches scene catalog --org_id "$ORG_ID" --repo_key "$REPO_KEY" \
     --include executable --json-out
   ```

   若该命令尚未随 CLI 契约开放，**不要阻断下派**：跳过目录预检，直接调用
   `create` 由服务端解析场景，并在输出标注 `SCENE_CATALOG_UNAVAILABLE`。

   返回 `repos[].dev_scene / scene_recommend_config / scenes[]`。按以下顺序为每个
   target 选择 `scene_id`：

   1. `scene_recommend_config.rules` 的条件命中（确定性优先）；
   2. 在 `executable=true` 的场景中按需求语义匹配；
   3. `dev_scene`（若可执行）；
   4. 唯一可执行场景；
   5. 仍选不出：不要硬塞场景，把该仓库排除或留给服务端回退，并在
      `scene_reasons` 写明原因。

   - `executable=false` 的场景不得选择；
   - 选择理由写入 `scene_reasons[repo_key]`；
   - 组织自动仓库不参与下派，也不要为它选场景。

2. **校验 plan**：组织 Issue 编号、目标列表、context、acceptance、`target_scenes`
   完整；`targets` 每项为 `provider:owner/repo`；`done_when` 只能是
   `issue_done`/`pr_merged`/`task_terminal`。
3. **创建批次**（显式 targets + per-target `target_scenes`）：

   ```bash
   grape org dispatches create --org_id "$ORG_ID" --json-file /tmp/org-dispatch.json
   ```

   长 JSON 优先 `--json-file`（避免 shell 转义）；短 JSON 用 `--json '<plan>'`。
   - 批次级 `scene_id` 留空；不要传 `org_scene_id`；
   - 服务端会对每个 target 再做存在性/可执行性校验，推荐失效时回退到 `dev_scene`
     或唯一场景；仍解析不到则该 target 失败，不影响其他 target；
   - 服务端按组织、组织 Issue 与稳定 targets 集合幂等，客户端不额外传递幂等键。
4. **输出结果**：`dispatch_id`、dispatch 状态和每个 target 的 `repo_key`、`scene_id`、
   `child_issue_number`、`status`、`error`。
5. **处理 `DISPATCH_IN_PROGRESS`**：先用 `grape org dispatches list --org_id "$ORG_ID"
   --org_issue_number "$ORG_ISSUE_NUMBER" --json-out` 读取已有批次并复用，不新建第二个
   编排。
6. **后续状态只读**：轮询或人工查询统一使用
   `grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out`，
   不根据本地推断伪造状态。

## 六、CLI 契约（命令组）

```bash
grape org dispatches create --org_id "$ORG_ID" --json '<dispatch-plan>'
grape org dispatches scene catalog --org_id "$ORG_ID" --repo_key "$REPO_KEY" \
  --include executable --json-out
grape org dispatches list --org_id "$ORG_ID" --org_issue_number "$ORG_ISSUE_NUMBER" \
  --limit 50 --offset 0 --json-out
grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
grape org dispatches cancel --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
grape org dispatches target retry --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" \
  --repo_key "$REPO_KEY" --json-out
```

> 最后两条是 **user_only**（worker token → 403），仅供人工/管理员或 Web 后端使用；
> agent 侧不得调用，也不得改用其他 token 绕过。完整请求/响应字段见
> `references/api-contract.md`（文档中的 HTTP 路径仅作契约说明，执行必须走 CLI）。

## 七、错误处理

- `INVALID_TARGET`：目标未关联组织或格式非法。输出错误码和目标，不重试。
- `NO_CONFIDENT_TARGET`：仓库地图没有达到置信度阈值的目标。不要回落全部关联仓库；
  把候选明细写回计划/评论，由 intake 显式选择 targets 或标记需补充能力地图。
- `NO_TARGET_SCENE`：目标无可用场景（或推荐场景不可执行且无默认/唯一场景可回退）。
  保留 target 失败状态，其他目标继续；在 `scene_reasons`/输出中写明仓库与原因，
  修复场景绑定后由人工调用 `target retry` 重试该目标。
- `INVALID_DONE_WHEN`：`done_when` 不在允许集合；修正 plan 后重试一次。
- `PERMISSION_DENIED`：调用方无权下派。停止写操作并返回可读原因。
- `ORG_NOT_FOUND` / `ORG_REPO_NOT_FOUND`：组织或组织仓库不存在。
- `CLI_CONTRACT_UNAVAILABLE`：当前 `grape` 契约未暴露核心下派命令。停止，不调用裸 HTTP。
- `SCENE_CATALOG_UNAVAILABLE`：`scene catalog` 未开放；跳过预检，直接 create 由服务端解析场景，不阻断。
- `DISPATCH_IN_PROGRESS`：已有同 Issue 批次在途；读取并复用，不新建。

失败结果必须显式输出，不得把 CLI/HTTP 错误转换为成功。取消与 retry 只输出处置建议，
由人工在 Web 端执行，并复用已有 `child_issue_number`。

## 八、流水线推进（重要：不要提前交办聚合）

`create` 成功后**不要**调用 `grape a2a handoff` 交给 `org_aggregate`。

原因：组织流水线的设计是

```
org_intake → org_dispatch（创建批次）
   → 等待批次内所有目标仓库的子任务终态
   → 系统自动触发 org_aggregate
```

`create` 只代表「批次已创建」，不代表「子任务已完成」。服务端在批次进入终态时会
自动触发 `org_aggregate` 节点（持久化复核事件，进程重启不丢）。如果本节点提前
A2A，聚合节点会在子任务还 running 时提前执行一遍，导致组织流水线看起来「已经
走完」，而关联仓库的任务仍在运行。

因此本节点的正确收口是：

1. 输出 `dispatch_id`、每个 target 的 `repo_key` / `child_issue_number` /
   `status` / `error`；
2. 写组织 Issue 评论（可选，但建议：批次摘要 + 目标清单 + 场景理由）；
3. 调用 `grape tasks complete` 结束本节点任务（见「九、任务收尾」）；
4. **不交办下一个节点**——批次终态由服务端终态钩子触发聚合。

禁止：
- 为了「推进流水线」而手动 A2A 到 `qa`/`org_aggregate`；
- 轮询等待所有子任务完成（本节点不负责等待，服务端终态钩子负责）；
- 根据 target 的 running 状态伪造聚合结论。

## 九、任务收尾（必做）

```bash
grape tasks complete --json '{
  "result_text":  "组织下派批次已创建：dispatch_id={id}，targets={n}",
  "actions":      ["读取 AI 仓库分析/地图召回", "场景目录选择", "创建下派批次", "输出目标状态"],
  "conclusion":   "批次已创建，等待子任务终态后由服务端触发 org_aggregate",
  "artifacts":    ["dispatch_id={id}", "targets 列表（repo_key/child_issue_number/status）"],
  "issue_summary":"组织需求 #{n} 已下派 {n} 个目标仓库，等待终态聚合",
  "next_step":    "wait_for_dispatch"
}'
```

⚠️ 结构化字段（`actions[]`/`conclusion`/`artifacts[]`/`issue_summary`）必须真实填充；
部分目标失败时如实写入 `actions`/`conclusion`，不得只报总数掩盖失败。

## 十、参考文件清单

| 文件 | 用途 |
|------|------|
| `references/api-contract.md` | 下派 API 契约、CLI 映射、字段与错误码 |
| `references/grape-cli-installation.md` | grape CLI 安装/升级指引（前置门自愈用）|

> 命令参数不确定时，先 `grape <命令组> --help` 查精确 flag，禁止凭记忆拼写。