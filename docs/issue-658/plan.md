# Issue #658 方案设计（plan）

## 现状与根因

- 引擎注册（worker `/api/runtime/register`）写入 `agent_runtimes`，并自动补
  `engine_capabilities.tag='all'` —— 任意引擎可认领任意 `pool_tag`（agent_type）任务。
- 派发链路：
  - plugin 广播：`pipeline_executor` → `get_scene_pool_engine_ids`（健康过滤）→
    `agent_tasks.pool_engine_ids`；空池 = 全量广播 → `claim_by_tag` 按 tag（含 'all'）认领。
  - legacy：`pick_engine`（scope→priority→health）。
- 两处均无「引擎类型 ↔ 节点角色」约束：#90 中 vod 节点任务被 codearts 引擎
  （tag='all'）认领，`codearts exited rc=1`。

## 方案（与 Spec 一致）

### 1. 数据模型：`agent_runtimes.role_tags`（显式声明字段）

- `db_setup.py`：DDL + 增量 ALTER 增加 `role_tags TEXT DEFAULT ''`（JSON 数组字符串）。
- `runtime.py register`：`RuntimeInfo` 接受 `role_tags: list[str]`；INSERT 写入，
  UPDATE 时非空才覆盖（不冲掉手工配置）。

### 2. 引擎角色解析（双轨：显式声明 + 类型默认映射兜底）

`engine_select.py`：

```python
_DEFAULT_ENGINE_ROLE_TAGS = {
    "opencode": ["coding", "issue", "qa"],
    "hermes":   ["vod", "chat", "sre"],
    "codearts": ["coding", "issue", "qa"],
}

def engine_role_tags(name, role_tags_json) -> list[str] | None
```

- 显式 `role_tags` 非空 → 使用显式声明；
- 否则引擎类型命中默认映射 → 使用默认角色集；
- 引擎类型不在映射内（codex/cursor/自定义等存量引擎）→ 返回 `None`（不限角色，
  零配置兼容，不因缺声明被全量排除）。

### 3. 派发器角色硬过滤（候选筛选最前置）

`engine_select.py` 新增 `_filter_by_role(members, agent_type)` +
`_role_no_candidates_detail(...)`（错误信息拼装：agent_type + 各候选引擎 role_tags）：

- `pick_engine`（legacy / SRE 两条路径）：`_engines_by_ids` 取出 role_tags 后，
  **在 `_filter_by_tags`/scope/priority/health 之前**先做角色硬过滤；无候选 →
  `log.error` 明确错误 + `_decision_log(result="role_no_candidates")` → 返回 `None`
  （engine_unavailable，调用方显式报错，不静默降级）。
- `get_scene_pool_engine_ids`（plugin 广播收口）：members 构建（含 SRE 档位过滤）后
  健康过滤前先角色硬过滤；无候选 → `_decision_log(result="role_no_candidates")` →
  返回 `None`（调用方 `pipeline_executor` 已有 None → 明确报错路径，Issue #259 方案A 同款）。
- `_candidate_reject_reasons` 补 `role_mismatch(...)` 原因（Issue #605 取证口径）。

### 4. 认领侧角色守卫（堵全量广播漏洞）

`service.py claim_by_tag`：标签能力校验后加载引擎 `name + role_tags` 计算有效角色集；
FIFO 跳过循环内对每个候选任务校验 `task.pool_tag ∈ 有效角色集`（角色集为 None 的引擎
不限）；不匹配 → 跳过该任务继续尝试下一个（与 pool_engine_ids 守卫同款跳过语义，
不阻塞队列）。bound claim（expected_task_id）路径不匹配 → fail-closed 返回 None。
——全量广播（pool 为空）时 vod 任务也不会再被 codearts 认领（#90 反例的兜底闭环）。

### 5. 回归测试 `tests/test_issue_658_engine_role_filter.py`

- 反例（#90）：场景 vod 节点绑定 codearts 引擎 → `pick_engine` 返回 None、
  `get_scene_pool_engine_ids` 返回 None；广播队列中 vod 任务不被 codearts
  `claim_by_tag` 认领。
- 正向：coding 节点 → opencode/codearts 可选中；vod 节点 → hermes 可选中。
- 显式 role_tags 覆盖默认映射：codearts 声明 `["vod"]` 后可服务 vod。
- 未知引擎类型（如 codex）零配置不被角色过滤排除。

## 影响面

| 文件 | 改动 |
|------|------|
| src/api/db_setup.py | role_tags 列（DDL + ALTER） |
| src/api/routes/runtime.py | 注册接受 role_tags |
| src/runtime/engine_select.py | 角色解析 + 硬过滤（pick_engine / pool / 取证） |
| src/runtime/service.py | claim_by_tag 认领侧角色守卫 |
| tests/test_issue_658_engine_role_filter.py | 回归测试 |

不改动：priority/scope 排序、前端展示、#653/#659 范畴。
