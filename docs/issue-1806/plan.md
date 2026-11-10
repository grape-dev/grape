# Plan: Issue #1806 折叠任务 context 守卫

## 现状
- `src/api/routes/webhook.py::a2a_endpoint` 中 `context = payload.get("context", "")` 无任何校验。
- 折叠方向只隐含在 `sweeper-v2-pr754-fold620` 这类 idempotency_key 中。
- 本仓无 sweeper 折叠派发代码（`src/runtime/cleanup.py` 为 workdir 清理器），本单交付端点守卫 + context 契约。

## 改动
### 1. 新增 `src/api/a2a_context_guard.py`
- `is_fold_task(idempotency_key)`：单源正则 `^sweeper-v2-(?P<source>.+)-fold(?P<target>\d+)$`。
- `normalize_fold_context(context)`：兼容文本行（`source_pr: 754`）、dict、JSON 字符串与中文字段别名。
- `missing_fold_context_fields(context)`：按固定顺序返回去空白后缺失的四要素。
- `fold_context_enforcement_enabled()`：`GRAPE_A2A_FOLD_CONTEXT_ENFORCE` 默认开启，`0/false/off/no` 降级。
- `build_fold_context_error(idempotency_key, missing_fields)`：错误体含 idempotency_key、缺失清单、schema 示例、hint。

### 2. `src/api/routes/webhook.py::a2a_endpoint`
- 折叠任务 context 在幂等键格式校验后先归一为字符串；四要素守卫放在鉴权与
  `find_event_by_idempotency_key` 幂等命中短路之后、事件创建之前，保证已存在事件
  同键重试仍返回已有事件。
- 命中折叠任务且四要素缺失：
  - 默认 `HTTPException(400, detail=build_fold_context_error(...))`；
  - 降级模式放行，拒绝/降级均输出 `a2a_fold_context_event=` 结构化 JSON 告警事件
    （含 task 标识、idempotency_key、缺失字段、仓库与单号）。
- 折叠任务的非字符串 context 归一为 JSON 字符串，保证下游 `context[:500]` 与去重键逻辑不变。
- 非折叠任务不进入守卫，payload 结构与幂等语义保持原样。

### 3. 测试
- `tests/test_issue_1806_fold_context.py`：判定单源、缺失字段、空/缺项 400、文本/结构化放行、降级告警、非折叠幂等回归。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 空 context 处理 | 默认 400 + 配置可降级 | 折叠任务误落点是实质伤害；灰度期可降级避免误伤 |
| 判定位置 | 幂等命中短路后、事件创建前 | 纯读判断；已存在 fold 事件同键重试不得被 context 误判 400 |
| 判定来源 | idempotency_key 单源函数 | 端点仅可见 payload 与幂等键，集中一处便于演进 |
| context 形态 | 文本行字段名 + 结构化 dict/JSON | 人读与机器校验兼顾，顶层 payload 结构不变 |

## 风险
| 风险 | 缓解 |
|------|------|
| 未改造的折叠派发方首次上线被误伤 | 配置开关降级为告警，先灰度观察 |
| 识别规则与 sweeper 命名演进脱节 | 判定函数单源 + 契约注释标注约定 |
| 校验影响幂等链路 | 守卫只读，置于事件创建前；非折叠任务完全旁路 |