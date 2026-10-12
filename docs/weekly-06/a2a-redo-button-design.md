# A2A 重新派发按钮设计（事件成功界面）

> 背景：Agent 实际未执行 A2A 交办，但任务已显示 completed，用户无任何补救手段，只能重跑整个任务。
> 目标：在任务成功界面提供「A2A 重新派发」按钮，让用户手动补发下游交办。
> 结论先行：**不复用失败重派（redispatch）链路**——语义不同。本功能 = 新增一条 A2A 交办事件，原事件/task 状态完全不动。

## 1. 语义边界（与 redispatch 的区别）

| | redispatch（已有） | A2A 重新派发（本设计） |
|---|---|---|
| 目的 | 本事件任务重跑 | 补发下游交办 |
| 影响对象 | 原事件置 running、task 重置/重入队 | 原事件/task 一律不动，只新增一条事件 |
| 走的链路 | routes/events.py `/{seq}/redispatch` | webhook.py `POST /api/a2a` 的组事件+入队逻辑 |

## 2. 交互方案（已定：方案 B）

- 事件详情弹窗，`data.status === 'completed'` 且事件有关联 Issue（issue_id 非空、非 `#pr_` 伪 ID）时，在 Status 行右侧显示按钮「A2A 重新派发」（样式沿用 `bt bt-o bt-s`，与重派按钮一致）。
- 点击弹确认框（复用 redispatch 的毛玻璃弹窗样式），核心是一个 **目标 Agent 选择列表**：
  - 五个选项 vod / issue / coding / qa / sre；
  - 调 `GET /api/events/{seq}` 已有数据无法直接拿到场景 nodes，弹窗打开时请求新端点拿场景归属，**不在当前 Issue 场景 pipeline nodes 内的选项置灰**（与 a2a 的 `not_in_scene` 拦截对齐，防止用户选了也白发）；
  - 场景未绑定/读取失败 → 全部可选（服务端仍兜底校验）；
  - **默认预选**：场景 pipeline nodes 中当前事件 agent 的下一个节点（如 coding→qa）默认选中高亮；无场景或当前是末节点 → 不预选。仅 UI 预选，用户可改。
- **无「推荐派发」数据源**：A2A 拦截返回只有 skipped 原因，agent 的 structured_output 无结构化推荐目标字段（个别 Agent 结论文本里有"建议下一步"自然语言，格式不保证）。因此不做机器推荐，agent 结论（conclusion）作为参考文本展示在弹窗内（有则显示，无则隐藏），不参与派发逻辑。
- 确认后调新端点派发，成功 toast + 关闭弹窗；下游事件出现在事件列表（与正常 A2A 一致）。

## 3. 后端：新端点

`POST /api/events/{seq}/a2a-redo`（routes/events.py，scope：user_only，`require_no_session_token`）

请求体：`{"agent": "qa"}`

处理流程：
1. 取事件 seq → 校验存在、`repo_key`/`issue_id`/`actor` 齐全；事件必须是 completed（running 中不允许补发，防止双跑）。
2. 归属校验：复用 redispatch 的 managed_repos 归属人判定（L386）。
3. 幂等：同 `repo_key + issue + agent` 存在**未终态**的 agent_tasks，或 30s 内已补发过（meta `a2a-redo:{seq}:{agent}`，同 redispatch:{seq} 模式）→ 409 拒绝。
4. 内部复用 `POST /api/a2a` 的核心逻辑（a2a_endpoint 中段：场景节点校验、agent 启用检查、_execute_agent 触发）：
   - payload 自动组装：repository=repo_key 去前缀形式、account=actor、issue_number=issue_id、context=「用户手动补发 A2A：原事件 seq=N 已完成但未交办」；
   - 身份：服务端以**当前登录用户 JWT**身份走用户级分支（get_managed_repos 校验），不走 bot token 通道——与用户在页面点击的行为一致。
5. 返回：`{"status": "ok", "seq": <新事件>, "agent": ...}`；下游被 not_in_scene / agent disabled 拦截时原样透传 skipped 原因给前端展示。

实现注意：a2a_endpoint 是 FastAPI Request 级处理函数，直接 import 调用不现实。将其中「校验+派发」中段抽成内部函数 `_do_a2a_handoff(repo_key, agent, payload, user)`，两端点共用（纯重构，不改行为）。

## 4. 前端（events.js）

- `_a2aRedoVisible`：`data.status === 'completed' && data.issue_id && !String(data.issue_id).startsWith('#pr_')`。
- 按钮渲染在现有重派按钮同位置（Status 行内），文案新 key（中文直写，I18N t() 找不到返回 key 本身的坑）。
- 弹窗：目标 Agent 选择列表（服务端返回 in_scene 信息，置灰不可选）+ 确认/取消。
- `a2aRedoEvent(seq)` → `API.post('/events/' + seq + '/a2a-redo', {agent})`，409/403/skipped 分支 toast 具体原因。
- index.html 版本号 +1（缓存）。

## 5. 不做的事

- 不改 /api/a2a 对外协议与 CLI。
- 不自动推断下游（方案 A 已否）。
- 不重置/回写任何原事件与原 task 状态。
- 不做通用「同 issue 同 agent」全局限流（仅本按钮 30s 幂等 + 未终态任务拦截）。

## 6. 影响面

- 新增：routes/events.py 一个端点 + webhook.py 一处函数抽取（行为不变）；events.js 按钮+弹窗。
- 触及文件：`src/api/routes/webhook.py`、`src/api/routes/events.py`、`src/api/static/js/pages/events.js`、`src/api/static/locales/zh/events.json`、`index.html`（版本号）。
- 测试：只补 `tests/` 下针对新端点的用例（幂等/归属/场景拦截三分支）。

## 7. 验收标准

1. completed 事件出现按钮，running/failed 不出现（failed 走既有重派）。
2. 选择场景外 agent → 前端置灰；服务端同样拦截（双保险）。
3. 重复点击 → 第二次 409 提示。
4. 补发后事件列表出现新 a2a_event，原事件状态仍为 completed。
