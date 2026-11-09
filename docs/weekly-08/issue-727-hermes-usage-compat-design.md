# Issue #727 设计：hermes 用量采集 0.9.0 兼容（方案 + 任务拆解 + 版本契约）

> 需求见 `issue-727-hermes-usage-compat-requirements.md`。

## 1. 双路径架构（治本 + 兜底）

```
hermes ACP 子进程
  └─ session/prompt PromptResponse（result.usage）
       └─ hermes_acp._extract_prompt_usage() ── yield ("usage", {...})   【治本：直报】
            └─ agent.py 事件循环：_usage_col.feed("usage", data)（不进消息流）
                 └─ UsageCollector.finish()：_fed 非空 → 直接返回直报记录

state.db（cwd-hash 隔离目录）
  └─ usage_collect._hermes_rows()                                        【兜底：差值】
       ├─ session_model_usage 表存在（hermes ≥0.10）→ 按原逻辑取差值
       └─ 表缺失（hermes 0.9.0）→ 告警一次，回退 sessions 表取差值
```

- **互斥不双计**：事件流喂到过 usage（`_fed` 非空）就以直报为准，跳过 state.db；
  直报缺失（旧调用方/响应丢失）才读 state.db 兜底。
- **失败纪律不变**：任何采集异常降级为「本次无用量」，不拖垮任务主链路。

## 2. 改动点

| 文件 | 改动 |
|---|---|
| `src/agent/hermes_acp.py` | 新增 `_extract_prompt_usage()`（归一化 result.usage 的 token 字段命名，复用 `src.observability.llm_usage.normalize_tokens`，导入失败退化手工别名解析）；收尾段解析 `prompt_result["result"]` 并 `yield ("usage", rec)`；事件类型表补充说明 |
| `src/runtime/usage_collect.py` | ① `feed()` 接受 `("usage", dict)` 事件（全 0 不入账）；② `finish()` hermes 分支 `_fed` 优先；③ `_hermes_rows()` 缺表回退 `sessions` 表 + 一次性告警（`_note_hermes_missing_table_once`）；④ `_cleanup()` 复位 `_tmpdir`（finish 幂等）；模块头文档更新 |
| `src/runtime/agent.py` | 事件循环新增 `usage` 事件路由：只喂 `_usage_col.feed`，`continue` 不进消息流/逐步日志（内部观测数据） |
| `tests/test_obs_llm_usage.py` | 新增 5 个用例：直报优先不双计 / 缺表回退+一次性告警 / finish 二次幂等无 Errno 2 / result.usage 解析（含别名与垃圾输入）/ 全 0 不入账 |
| 本文档 | 版本契约 + 拆解 + 验收 |

## 3. 任务拆解（执行顺序）

1. T1 `_cleanup()` 复位 `_tmpdir`（最小止血，独立可验）；
2. T2 `_hermes_rows()` sessions 表回退 + 一次性告警；
3. T3 `feed()`/`finish()` 直报路径（usage 事件）；
4. T4 `hermes_acp.py` `_extract_prompt_usage` + yield usage 事件；
5. T5 `agent.py` usage 事件路由；
6. T6 测试用例 + 文档（本文件）。

## 4. 关键决策

- **不聚合直报记录**：每条 PromptResponse 对应一条记录（带 `turn` 序号），
  与 codex `step_finish` 用量同构；台账侧幂等键 `(source, model, turn)` 天然去重。
- **usage 事件不进消息流**：worker 侧 `report_messages`/逐步日志/前端实时日志
  均不需要内部用量数据；主控侧（chat/event 流水线）`MasterRun.feed` 同样受益
  （collector.feed 直接识别该事件），未知事件类型在各调用方本就被忽略。
- **回退口径**：`sessions` 表 token 列同为「累计值」，沿用 start 记基线、finish
  取差值的既有语义；模型粒度退化为 session 粒度（sessions.model 单值），
  task 粒度缺失（`_source_suffix` 为空）——降级但可用。

## 5. hermes 版本契约（worker 用量采集兼容矩阵）

| hermes 版本 | state.db 形态 | PromptResponse `result.usage` | 采集路径 | 行为 |
|---|---|---|---|---|
| 0.9.x | 仅 `sessions` 表（无 `session_model_usage`） | ✅ 携带 | 直报（治本）＋ sessions 回退兜底 | 零异常、条数 > 0、缺表降级告警一次 |
| ≥0.10 | `sessions` + `session_model_usage` | ✅ 携带 | 直报优先；兜底走 `session_model_usage`（含 task 粒度） | 同上 |
| <0.9（无 acp 子命令） | — | —（ACP 不可用） | 不适用 | 引擎注册健康门拦截（见 `docs/weekly-09/hermes-acp-compat-matrix.md`） |

- **契约承诺**：worker 用量采集**不依赖** `session_model_usage` 表存在；
  任何表结构缺失只降级（告警 + 回退），不抛异常。
- **字段命名容忍**：`result.usage` 的 token 字段经 `normalize_tokens`
  归一化（`input_tokens`/`input`/`prompt_tokens` 等别名均可）。

## 6. 验收复核

- `python3 -m pytest tests/test_obs_llm_usage.py` 全绿（29 → 34 用例）；
- 直报与 state.db 兜底互斥（不双计）有用例钉死；
- 二次 finish 无异常（Errno 2 回归）有用例钉死；
- 缺表降级告警「一次」有用例钉死（caplog 断言）。
