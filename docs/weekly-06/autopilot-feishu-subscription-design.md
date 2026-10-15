# 自动化任务飞书订阅通知设计（autopilot-feishu-subscription-design）

> 版本: v1.0
> 日期: 2026-10-12
> 状态: 待主人审批（本文档只做设计，不含已实施改动）
> 涉及仓库: grape 主仓库（工作目录，分支 mys-local）

## 一、背景与目标

autopilot 定时任务（cron 触发）经远程 worker 执行后，结果落库 autopilot_executions，前端通过 `GET /api/autopilot/executions`（JWT）查看。现状缺口：

1. **触达缺失**：任务执行完毕无人知晓，只有主动打开页面才能看到结果。
2. **概览缺失**：findings_json 只有"一句话 summary"，前端和通知都缺少结论性概览。
3. **派发入口深**：把 finding 派发为 Issue 需要进网页操作，移动场景不可用。

目标：

- 任务详情页一键「飞书订阅」，执行完毕自动推送飞书卡片（离线可收，飞书为存储转发模型）。
- 卡片展示执行结论 + findings 摘要，支持两段式「卡片唤醒卡片」交互，在飞书内完成 finding 派发。
- prompt / 前端 / 卡片措辞全面去"巡检"化——autopilot 是通用自动化任务，巡检只是 skill 层的一种用法。

## 二、核心架构决策（讨论定稿）

| 决策点 | 结论 | 理由 |
|---|---|---|
| 通知触发位置 | 服务端内部，任务终态写库处直接调发送 | 自动化任务与流水线同为远程 worker 模式，结果由 `_finalize_autopilot_task` / `_poll_dispatched_completion` 收回服务端，进程内调用，**零鉴权需求** |
| 鉴权模型 | 上报侧零鉴权（服务内部）；订阅侧 JWT（网页登录态） | 不引入 API Key / tk_，不发放任何新凭证 |
| 可见性 vs 推送 | executions 查询（全员按仓库权限可见）与订阅推送（纯增量）分离 | 未订阅者显示逻辑不受影响 |
| 飞书一键派发 | 两段式卡片（卡片唤醒卡片），**不用** PATCH 置灰 | PATCH 需快照表 + ~150-200 行；两段式第二段卡为点击时活数据，天然防重 |
| finding 选择 | select_static 下拉组件（选项不受按钮数量限制） | 纯按钮版受 action 区域限制，几十 findings 放不下 |
| 结果判级 | findings JSON 加结构化 `result` 字段 | 5 行换确定性，前端/卡片颜色不靠关键词猜测 |
| 措辞体系 | 「自动化任务 / 执行结论」，判级 ok/warning/error | 去巡检化，任务语义中立 |

## 三、数据模型

### 3.1 新表：autopilot_subscriptions

```sql
CREATE TABLE IF NOT EXISTS autopilot_subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,   -- autopilot_tasks.id
    user_id      INTEGER NOT NULL,   -- 平台用户 id
    created_at   TEXT,
    UNIQUE(task_id, user_id)         -- 重复订阅幂等
);
```

不加 repo_key 冗余列（经 task JOIN 可得）；订阅权限校验时现查。

### 3.2 findings_json 结构扩展（无迁移）

新增两个 JSON 字段（旧数据缺省回落，见 §7 兼容性）：

```json
{
  "result": "ok | warning | error",
  "summary": "执行结论：整体判定 + 关键产出/需关注事项，2-4 句",
  "findings": [
    {"id": "f1", "title": "...", "severity": "high|medium|low",
     "category": "security|quality|documentation|performance|other",
     "description": "...", "suggested_assignee": "coding",
     "suggested_skill": "implementation", "dispatched_issue_number": "…派发后回写…"}
  ]
}
```

`result` 语义（与单个 finding 的 severity 分层，勿混淆）：

- `ok`：任务目标达成，无需要关注的问题
- `warning`：任务完成但存在需要关注的事项
- `error`：任务未能达成目标或发现需立即处理的问题

## 四、服务端改动

### 4.1 prompt 模板（src/runtime/scheduler.py `_FINDINGS_FORMAT_TEMPLATE`）

- 示例 JSON 改为含 `"result"` 与新 summary 措辞；规则区新增：
  - `result: ok/warning/error` 三档判定规则（如上）
  - summary 是结论性概览：先给整体判定，再概括关键产出与需关注事项，2-4 句，不罗列所有 finding
- 同文件 `_build_autopilot_prompt` 输出格式说明同步措辞（"问题列表"→"任务产出/问题列表"语境）。

### 4.2 parse_findings（src/runtime/scheduler.py:91）

- 返回 dict 新增 `"result": data.get("result", "")`（一行）。
- 兼容：无 result 时返回空串，消费端回落关键词/缺省黄色。

### 4.3 订阅接口（src/api/routes/autopilot.py）

三个薄端点，鉴权复用 `_check_task_access`（task 存在 + check_repo_access 仓库归属——**无仓库权限不能订阅**）：

```
POST   /api/autopilot/tasks/{task_id}/subscribe      → INSERT（UNIQUE 冲突=幂等已订阅）
DELETE /api/autopilot/tasks/{task_id}/subscribe      → DELETE
GET    /api/autopilot/tasks/{task_id}/subscription   → {subscribed: bool}
```

### 4.4 终态通知（本设计核心）

挂载点两处（remote 模式两条终态路径，先到者发，以 exec 已通知状态做幂等护栏防双发）：

- `_finalize_autopilot_task`（scheduler.py:436，worker 回调收尾）
- `_poll_dispatched_completion`（scheduler.py:604，轮询发现终态）

在 `_update_execution_result(exec_id, "success"/"failed", ...)` 之后追加：

```python
_notify_subscribers(task_id, exec_id, status, result_summary, findings_result)
```

`_notify_subscribers` 流程：

1. `SELECT user_id FROM autopilot_subscriptions WHERE task_id=?` → 空则 return（零开销）。
2. 逐个订阅者：
   a. `_get_feishu_open_id_by_user_id(user_id)` → 未绑定记日志跳过（不回落他人 bot）；
   b. `_resolve_user_chat_bot(bm, user_id)` → 本人 chat bot，未配置跳过；
   c. `build_exec_card(...)` 组卡（见 §5）；
   d. `asyncio.create_task(api.send_card_to_user(...))` 异步发送，不阻塞调度循环。
3. 打点 `_im_notify_counter("sent_ok"/"sent_failed"/"skipped")`。

**重试约束**：`_execute_task` 的 max_retry 循环中，中间失败不通知，仅最终一次（成功或重试耗尽）触发，避免订阅者一次任务收 N 张失败卡。

**卡片内容数据源**：与前端同源——直接使用终态处的 findings_result / autopilot_executions.findings_json，保证网页与卡片展示一致。

### 4.5 命令 handler（src/im/handler_async.py，照 /task-open 模式新增两条）

**`/finding-list`**（第一段卡按钮触发）：

1. 从 `_card_value` 取 exec_id；按 sender_open_id 反查平台 user_id；
2. 校验该用户对 exec 所属仓库有权限（check_repo_access 语义）；
3. 读 findings_json 现查（活数据：已派发 finding 选项标注「✅ 已派发 #issue」）；
4. `api.reply_card` 回复第二段卡（问题列表 + select_static 下拉 + 派发按钮）。

**`/finding-dispatch`**（第二段卡派发按钮触发）：

1. 取 exec_id + 下拉选中的 finding_id；反查 user_id + 权限校验；
2. 服务端内部调用 `dispatch_finding` 核心逻辑（autopilot.py:501 抽出可复用函数，避免路由层重复）；
3. `reply_card` 回结果卡：「已派发为 Issue #xxx」/「已派发过 (Issue #xxx)」(409 语义) /「无权限」/「无权限派发他人任务」。

身份来源说明：点按钮的人身份来自飞书 open_id ↔ 平台 user 绑定，非网页 JWT——同一信任模型，`_task-open` 已验证可行。

### 4.6 `_parse_card_action` 扩展（src/im/feishu_async.py:894）

select_static 组件点击同样走 card.action.trigger，但选中值结构在 `action.value`（或 `action.option`，以飞书实际事件为准开发时验证）中与 button 的 cmd 结构不同：新增解析分支，把「选中的 finding_id」合入返回的 `_card_value`（预计 ~15 行）。**开发时需用真实卡片点击事件验证字段路径，不拍脑袋**。

### 4.7 dispatch_finding 抽取复用

`POST /executions/{exec_id}/dispatch` 路由函数中「查 finding → 查重 → create_task_via_issue → 回写 dispatched_issue_number」抽为独立函数，路由与 `/finding-dispatch` handler 共用，避免两份实现漂移。

## 五、卡片设计

### 5.1 第一段卡（终态推送）

```
头部: ✅ 任务执行完成 · {任务名}      ← 模板色按 result 映射（见 5.3）
正文: result 判定中文 + summary 结论（2-4 句）
fields: 仓库 / 执行时间 / 产出 N 个问题（findings 数）
findings 标题列表（纯文本，每条 severity emoji + 标题，数量不受限）
按钮:
  [📋 查看问题详情]   value={cmd:"/finding-list", exec_id}
  [查看网页详情 ↗]    url=Web 详情页
```

### 5.2 第二段卡（点击「查看问题详情」唤醒，reply_card）

```
头部: 🔍 任务产出列表 · {任务名}
正文: 每个 finding: severity 标记 + 标题 + 描述截断（~80 字）
      已派发项标注 ✅ 已派发 #issue_number
交互: select_static 下拉（选项=全部 findings，含已派发标注）
按钮: [🚀 派发所选问题] value={cmd:"/finding-dispatch", exec_id}
```

### 5.3 result → 卡片模板色映射

| result | 卡头色 | 场景 |
|---|---|---|
| ok | green | 目标达成 |
| warning | yellow | 有需关注事项 |
| error | red | 未达成 / 需立即处理 |
| 缺省/解析失败 | grey（或黄） | 旧数据/未知 |

执行失败（worker 超时、任务 cancelled）不分 result，直接红卡 + error 摘要。

## 六、前端改动

1. **任务详情页**：「🔔 飞书订阅」toggle 按钮（GET 订阅状态 → POST/DELETE 切换）；按钮旁小字提示"需在个人设置绑定飞书并配置本人 chat bot，否则收不到卡片"。
2. **执行详情/列表**：顶部新增「执行结论」区块——result 判级徽标（绿/黄/红）+ summary 正文，替代裸 findings 列表的入口体验。
3. **i18n**：⚠️ 已知陷阱：t(key) 找不到返回 key 本身（truthy），`||` 兜底不生效——**新文案键必须 zh/en 两边同步补齐**；同时清理现有"巡检"措辞键（任务/指令/执行语境）。

## 七、兼容性

- 旧 findings_json 无 result 字段：`data.get("result","")` 缺省，卡片灰/黄、前端显示"未判定"。
- 未订阅任何人的任务：`_notify_subscribers` 第 1 步空列表 return，行为与现状完全一致。
- 订阅者未绑飞书/未配本人 bot：跳过并记日志（`_im_notify_skip` 语义），不影响其他订阅者。
- 重复派发竞态（多人先后点同一 finding）：dispatch 查重 409 语义兜底，第二人收「已派发过」回复卡；第二段卡为点击时活数据，重新唤醒即见最新派发状态。
- 两个终态挂载点双触发：以 autopilot_executions 通知状态（或发送前 status 复核）做幂等护栏。

## 八、不做什么（明确出界）

- 不做 PATCH 卡片置灰（快照表方案留作 v1，视真实竞态频率决定）。
- 不引入任何 API Key / tk_ / 新鉴权凭证——通知链路全程服务端内部 + 现有 JWT。
- 不做广播/群发——推送仅限订阅者，可见性走现有 executions 查询。
- 不做通知偏好细分（按级别过滤订阅）——留 v1。
- 不改 DB 既有列名（findings/summary 类型中立，改列名是大动作，不值）。

## 九、改动清单汇总

| 文件 | 改动 | 量级 |
|---|---|---|
| src/runtime/scheduler.py | prompt 模板措辞 + result 字段；parse_findings +1 行；两处终态挂 _notify_subscribers | ~120 行 |
| src/api/routes/autopilot.py | 3 个订阅端点；dispatch_finding 抽函数 | ~80 行 |
| src/im/handler_async.py | /finding-list、/finding-dispatch 两条命令 | ~150 行 |
| src/im/feishu_async.py | _parse_card_action select 值解析分支 | ~15 行 |
| src/api/db_setup.py | autopilot_subscriptions 建表 DDL | ~10 行 |
| 前端 static | 订阅按钮 + 结论区块 + i18n zh/en | ~100 行 |

预计总改动 ~475 行，6 个文件，1 张新表，零新凭证。

## 十、测试要点

- 订阅幂等（重复点击）、退订后不再收卡。
- 无订阅者任务终态零开销（不查飞书、不发请求）。
- 未绑飞书订阅者跳过且有日志。
- 重试场景只收最终一张卡。
- 两个终态挂载点只发一次（幂等护栏）。
- 旧 findings_json（无 result）渲染不炸、显示未判定。
- select_static 点击 → _card_value 携带 finding_id（真实事件验证）。
- 重复派发：第二人收「已派发过」卡，不重复建 Issue。
- 无仓库权限用户点击卡片按钮 → 拒绝回复卡。
