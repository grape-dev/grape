# Issue #600 方案设计

## 根因
1. **后端**：`src/api/routes/events.py` 三处重派路径（A2A L1246、run_pipeline L1290、onboarding L492）将原事件标记 `failed`；`failed` 是失败终态，进入流水线聚合后永久残留红失败卡片。
2. **后端**：`src/api/routes/issues.py` Issue 详情端点 `archive-first`，与 `/pipeline` 端点 `events-first` 口径不一致。
3. **前端**：`pipeline.js` / `issues.js` 流水线卡片只在导航进入时拉取一次，无轮询；`events.js` 重派成功后跳回**旧** seq（后端刚标记为失败/取消的那个），而不是新 seq。

## 修改点
| # | 文件 | 修改 |
|---|------|------|
| 1 | `src/api/routes/events.py` | 新增 `_mark_event_redispatched(seq, new_seq, channel)`：`reset_event_terminal` + `update_event_complete(seq,"cancelled",result=接管说明)`；三处重派路径改用它 |
| 2 | `src/api/routes/issues.py` | 详情端点改为 events-first，archive 兜底（与 `/pipeline` 对齐） |
| 3 | `src/api/static/js/pages/events.js` | `_doRedispatch` / `_doLocalRetry` 跳转返回的 `event_seq`（新 seq） |
| 4 | `src/api/static/js/pages/pipeline.js` | 新增 `_pipeFingerprint` / `startPipePolling` / `stopPipePolling`：15s 静默轮询，指纹变化才重建流程图卡片 |
| 5 | `src/api/static/js/app.js` | pipe 视图启动轮询、离开视图停止 |
| 6 | `src/api/static/js/pages/issues.js` | 任务详情流水线卡片 15s 轮询重渲染 |

## 状态语义
- `compute_node_statuses` 已按 agent 取最新事件（后覆盖前），重派新事件成功后节点自然翻绿；旧事件改标 `cancelled` 后不再参与 any_failed 聚合（cancelled≠failure），done 阶段视同 completed。
- 终态保护不动：`reset_event_terminal` 仅重派路径调用，与既有 `test_event_redispatch.py` 语义一致。

## 测试
- 新增 `tests/test_issue_600_redispatch_status.py`：
  - UT-1 原事件 failed → `_mark_event_redispatched` → cancelled + result 说明；
  - UT-2 重派后同 agent 新事件 completed → `compute_node_statuses` 节点=completed（旧失败不残留）；
  - UT-3 旧失败事件改 cancelled 后不参与失败聚合（statuses 不含 3，除非另有失败事件）。
- 回归：`tests/test_pipeline_terminal_status.py`、`tests/test_event_redispatch.py`。
