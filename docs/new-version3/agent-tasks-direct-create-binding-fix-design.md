# agent_tasks 直接创建 INSERT 绑定数不匹配 — 修复设计（Seq #1991/#1992 事故）

> 版本：v1.0 ｜ 日期：2026-09-01 ｜ 状态：**已分析，方案待实施（未改代码）**
> 事故对象：生产 `Jason7602/hello-flask-9` 模板引导事件 `Seq #1991`（onboarding_exp）/ `#1992`（onboarding_explore）失败
> 结论：**系统级代码缺陷**（`_create_direct_task` / `_create_failed_task` 的 agent_tasks INSERT 列数与值数不匹配 39/37），非环境偶发。

---

## 一、现象

| 项 | 值 |
|----|----|
| 仓库 | `gitcode:Jason7602/hello-flask-9`（模板一键创建 → 首跑引导 Onboarding） |
| Seq | `1991` onboard_exp（引导·快速部署体验）→ failed，`exit_code=1` |
|      | `1992` onboarding_explore（引导·探索方向）→ failed，无 exit_code |
| 结果文本 | `中断：running 超时无任务执行记录（sweeper 兜底回收）` |
| `agent_logs` | `[]`（两个事件均无任何执行日志） |
| `agent_tasks` | 该仓库 **0 条**（事件未创建出任务行） |
| 附带证据 | 生产日志自 16:10 起 seq=1982~1988 连续同报错 |
| 引导结果 | 仅一个 target 落地 Issue #1（`issue_state #1 vod` + 子事件 #1993），#2 缺失，`onboarded:gitcode:...` 已置 1 |

生产日志（关键，process 2642196）：

```
create_direct_task failed for seq=1982: Incorrect number of bindings supplied. The current statement uses 39, and there are 37 supplied.
create_direct_task failed for seq=1983 ... 1988 ... （同样 39/37）
```

---

## 二、根因

**`src/api/event_pipeline.py` 的 `_create_direct_task`（:119–135）与 `_create_failed_task`（:199 同款字面量）值元组硬编码 37 个占位，但列清单 `_TASK_COLUMNS_LIST` 已是 39 列。**

```python
# event_pipeline.py:119-135（直接执行前创建任务 + 设 started_at）
from src.runtime.service import _TASK_COLUMNS_LIST          # ← 39 列（含 pool_tag / pool_engine_ids）
c.execute(
    f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
    f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})",        # ← 生成 39 个 ?
    (task_id, agent_session_id, engine_id, seq, issue_id, repo_key or "",
     agent_type or "", skill_name or "", skill_id or 0,
     "", "", "", "", "", 0, repo_gitcode_token or "", "{}",
     "{}", "", "", "{}", "{}", "{}", 0, "", 0, "", "",
     "queued", None, "", "", "", "{}", now, None, now)      # ← 硬编码 37 个值 → 39? / 37 bindings
)
# 走队列路径的 enqueue_task 能正确补齐 pool_tag/pool_engine_ids（service.py:431），故只影响 direct 路径
```

- `service.py:132` `_TASK_COLUMNS` 末尾含 `..., pool_tag, pool_engine_ids` → 39 列；
- `event_pipeline.py` 两处字面量元组仍是 37 值，缺 `pool_tag`、`pool_engine_ids` → SQLite 抛绑定数错误 → `create_direct_task` 返回 `""`，**任务行未创建**。

**链路**：
```
direct 事件 → _create_direct_task：
   update_event_config(seq, inst, skill, now)   # 写 started_at（1991 有值，1992 更早中断无）
   → INSERT agent_tasks 39/37 失败 → 返回 ""
→ 无任务行、无 agent_logs → 事件卡 running → sweeper 兜底置 failed
```

---

## 三、为何现在频发（触发背景）

1. 该缺陷由「插件模式 / 多任务」主链路合入带入（历史上 `c3cd1dd4` 修复过、后 `1a609558` 又被 revert，当前远端 93730855 存在此 bug）；
2. **引擎治理把服务器改为「本机直连」后事件更多走 direct 执行** → 缺陷从"偶发"变"必现"；
3. 队列（queue_remote）路径不受影响，只 direct 受影响，易被误判为"本机直连有问题"。

---

## 四、次要问题（一并记录）

| 问题 | 位置/现象 |
|------|----------|
| 重派失效 | 对 1992 重派 → `Pipeline skipped: repository='' or event_type=... not recognized`（redispatch payload 未带 repository，`gitcode:1992/redispatch`）→ UI 重派救不回引导事件 |
| 引导状态不一致 | `onboarded:*` 标记已置 1，但仅 1/2 引导落地 → 该仓库后续不会自动补 2 |
| sweeper 文案误导 | `running 超时无任务执行记录`掩盖真实根因（实为创建任务失败非超时） |

---

## 五、修复方案

### 5.1 P0（必做，一行级）：对齐 `_TASK_COLUMNS_LIST` 补齐 39 值

`event_pipeline.py`：
- `_create_direct_task`（:125-129）与 `_create_failed_task`（同款）的值元组末尾按列顺序补 `""` 占位两个：

```python
# 改后（39 值，顺序须与 _TASK_COLUMNS_LIST 完全一致，pool_tag、pool_engine_ids 补空串）
(..., "queued", None, "", "", "", "{}", now, None, now, "", "")
```

> 安全性要求：不要手数位置——建议以 `_TASK_COLUMNS_LIST` 为唯一来源生成占位（如 `dict`/`functools.partial` 或测试断言 `len(tuple) == len(_TASK_COLUMNS_LIST)`），防止下次加列再漂移。

### 5.2 P1：重派链路补齐 repository

事件重派（`events.py` redispatch）构造 pipeline 入参时带上 `repository`/`owner`/`repo` 完整字段，避免 onboarding 等 `source='template'` 事件重派 `repo=''` 被静默跳过。

### 5.3 P1：引导状态自愈

`trigger_onboarding`/`_finalize_onboarding` 增加幂等补做：若某 target 主事件 failed 且未产出对应 Issue，允许在该 target 上重试（不因 `onboarded:*` 全量标记一次性放弃两个 target）。

### 5.4 P2：观测与防御

- sweeper 文案区分「running 超时」与「创建任务失败」（后者结果写明 `create_direct_task failed` 原因）；
- `tests` 增加 `_create_direct_task` 契约测试：`len(_TASK_COLUMNS_LIST)` 与 INSERT 占位数一致性断言；mock 一次 direct 事件全链路（append_event → create_direct_task → 执行 → 完成）回归。

---

## 六、改动清单

| # | 文件 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/api/event_pipeline.py` | `_create_direct_task` / `_create_failed_task` 值元组补齐 pool_tag/pool_engine_ids（对齐 39） | P0 |
| 2 | `src/api/routes/events.py` | redispatch 透传 repository/owner | P1 |
| 3 | `src/api/onboarding.py` | 引导 target 级幂等补做 | P1 |
| 4 | `tests/test_event_pipeline.py`（或新增） | 列/值一致性断言 + direct 全链路回归 | P2 |

---

## 七、验证与回归

1. 单元：新增列/值数一致性断言通过；`pytest -m "not e2e_ui"` 全量回归不红；
2. 端到端（生产预检）：新模板仓库一键创建 → 引导两事件 completed → `agent_tasks` 出现 direct 任务行、有 agent_logs；重派一个 failed 事件能恢复；
3. 观测：`sweeper` 不再把"创建失败"误标为 running 超时。

---

## 八、风险

| 项 | 说明 |
|----|------|
| 手数占位错序 | 强制以 `_TASK_COLUMNS_LIST` 驱动生成占位 + 契约测试，禁止手工数 |
| direct 事件在修复前持续 fail | 修复上线部署前，direct 类事件（含新仓库引导）仍会失败——建议尽快合入 new-version3/main 并部署 |
| 存量 failed 事件 | 本方案不回溯清理已 failed 的 1982-1988/1991-1992；如需可在部署后按需重派或补引导标记 |

*本方案仅文档，未改动任何代码。*