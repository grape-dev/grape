# Issue #49 补充设计：PR 事件归一化细分——消除「通过审查」被当 pull_request_opened 误触发

> Issue：https://gitcode.com/grape-dev/grape/issues/49
> 类型：优化（token 浪费 / 无意义触发）| P1 | 状态：spec-ready（待评审）
> 分支：`weekly-02`
> 日期：2026-09-10
> 上游方案：`docs/weekly-01/issue-49-webhook-action-filter-fix-design.md`（L1/L2/L3 三层模型）
> 范围：本方案是 Issue #49 的 **PR 部分细化**——修正 `GitCodeBackend.normalize()` 对 Merge Request Hook 的二元分类，
> 让「通过审查 / 审查类」动作不再被并成 `pull_request_opened`，从源头消除无意义 coding agent 触发。

## 一、问题复现与根因

现象：PR「当前通过审查」（GitCode 审查/评价类动作，action 非 `merge`）被归一化为 **`pull_request_opened`**，
随后走标准 PR 阶段路由（stage=coding → coding / stage=qa → qa），导致**无意义 agent 触发与 token 消耗**。

根因（已核对 `src/vcs/gitcode.py`）：

```python
# :16  Merge Request Hook → 一律先映射 pull_request_merged
# :37-40
if evt_type == "pull_request_merged":
    if obj.get("action") != "merge":
        evt_type = "pull_request_opened"     # ★ 凡非 merge 动作全变 opened
```

- **二元分类**（merge / 非 merge）把 `open / update / reopen / close / approve(通过审查)…` 全部并成 `pull_request_opened`；
- 原始 action 仅在 `object_attributes.action`，且 normalize **未上抛**给上层（`routing` / `run_pipeline` 看不到 action）→ L1 无法按 action 过滤、L2 阶段路由只能按“opened”误分派。

## 二、目标

1. **按真实 action 细分 PR 事件**：`open→pull_request_opened`、`update→pull_request_updated`、`reopen/close`、`merge→pull_request_merged`、审查类（通过审查/评审）单独识别；
2. **action 与操作者上抛**：normalize 把 `action`/`actor`/`commits` 信息写入 evt 顶层，供 L1 过滤与 L2 分派对账；
3. **无意义触发归零**：「通过审查 / 纯 metadata PR 变化 / bot 操作」不触发 coding/qa 执行；仅「用户 push 了新 commit」才允许 PR update 重新审查。

## 三、方案

### 3.1 normalize() 细分（`src/vcs/gitcode.py`）

| 请求头 | object_attributes.action | 归一化事件 | 说明 |
|--------|--------------------------|------------|------|
| Merge Request Hook | `open` / `create` | `pull_request_opened` | 新 PR 审查 |
| Merge Request Hook | `merge` | `pull_request_merged` | 合并 → QA/SRE 流水线 |
| Merge Request Hook | `update`（且有新 commit 由用户 push） | `pull_request_updated` | 代码更新需重新审查 |
| Merge Request Hook | `update`（无 commit / bot 操作 / 纯 metadata） | `pull_request_updated`（标记 no-op） | 交由 L1 过滤 |
| Merge Request Hook | 审查类（`approve`/`review`/「通过审查」等） | `pull_request_updated` 或独立 `pull_request_reviewed` | 需求确认：若 GitCode 有独立 action → 独立事件；否则并入 update 由 L1 判 no-op |
| Merge Request Hook | `reopen` / `close` | 保留原类型 + 上抛 action | 交由 L1 过滤（reopen/close→丢弃） |

实现要点：

```python
def _normalize_merge_request(header, payload):
    obj = payload.get("object_attributes", {}) or {}
    action = str(obj.get("action", "")).lower()
    actor = payload.get("user", {}).get("username", "") or payload.get("user_name", "")
    evt = {
        "type": _pr_action_to_type(action),
        "action": action,              # ★ 上抛
        "actor": actor,                # ★ 上抛
    }
    if action == "update":
        # 是否「有效代码更新」：有 commits 且 actor 非 bot（commits 判定放 L1）
        evt["has_commits"] = bool(payload.get("commits"))
    return evt
```

> 说明：`routing.PR_EVENT_TYPES` 需把 `pull_request_updated`（此前 normalize 已并入 opened）纳入；若新增独立 `pull_request_reviewed`，同样纳入。

### 3.2 L1 规则补充（对齐 issue-49 §4.1 的 `event_action_filter`）

默认规则（PR 部分）修正为按 action 精确匹配：

| event_type | action | allow | 判定 |
|------------|--------|-------|------|
| `pull_request_opened` | `open` | ✅ | 新 PR 审查 |
| `pull_request_updated` | `update` | 按操作者/changes | actor 非 bot 且 `has_commits=true` → allow；`approve/检查通过等审查动作、无 commit、bot` → **过滤** |
| `pull_request_updated` | `reopen`/`close` | ❌ | 丢弃 |
| `pull_request_merged` | `merge` | ✅ | 合并流水线 |
| （可选独立）`pull_request_reviewed` | 审查动作 | ❌ | 通过审查不触发 coding/qa 执行（仅可观测记录） |

- 命中过滤写 `event_skipped`（reason 带 `action` + `actor` + `has_commits`），前端可回溯（issue-49 §4.2）。
- 规则仍存 DB `event_action_filter`，热生效（Q3 已确认可配置）。

### 3.3 路由（L2）对齐

- `pull_request_opened`：维持现状（按 stage 分派）；仅「真新 PR」「真代码更新」能到达这里。
- `pull_request_updated`：在 `_resolve_issue_stage_agent` 的 PR 分支中按现有 `pull_request_opened`/复审特例处理（① coding+QA 历史→qa 复审 ② 否则按 stage）。
- `pull_request_reviewed`（若有）：直接不进入触发（或仅记录）。

## 四、改动清单

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/vcs/gitcode.py` normalize | Merge Request Hook 按 action 细分事件 + 上抛 `action/actor/has_commits` | P0 |
| 2 | `src/api/routing.py` | `PR_EVENT_TYPES` 纳入 `pull_request_updated`（含可选 `pull_request_reviewed`） | P0 |
| 3 | `src/api/event_rule.py` / `event_action_filter` seed | PR 规则补 `updated(按操作者/commits)`、`reviewed(过滤)`、`reopen/close(丢弃)` | P0 |
| 4 | `src/api/event_pipeline.py` | L1 判定支持 `has_commits/actor`；L2 PR 分支对齐 updated | P0 |
| 5 | `src/api/db_setup.py` | （如有独立 reviewed 事件）`event_action_filter` 默认 seed | P1 |
| 6 | `tests/` | · update+新 commit→updated 放行 · approve/通过审查→过滤 · open/merge 保持 · 历史用例回归 | P1 |
| 7 | 文档 | 本方案 + issue-49 §三/#六 同步 | P1 |

## 五、验收标准

- [ ] PR「通过审查 / 审查类」不再产生 `pull_request_opened` 误触发 coding/qa；`event_skipped` 有 reason（action/actor）
- [ ] `pull_request_opened` 仅来自真·新 PR；`pull_request_updated` 能标明有无新 commit
- [ ] `update + 用户 push 新 commit` → 触发重新审查；`update（无 commit / bot）` → 过滤
- [ ] `merge` → `pull_request_merged`（QA→SRE）语义不变；现有 webhook/onboarding 回归全绿
- [ ] 规则热生效（改 `event_action_filter` 无需重启）

## 六、风险与兼容

| 风险 | 影响 | 缓解 |
|------|------|------|
| 历史/三方客户端依赖「一切非 merge 都是 opened」 | 中 | 仅改变 normalize 输出；`PR_EVENT_TYPES` 增 updated 兼容；行为对齐现状语义（open 仍→opened） |
| GitCode 审查类无独立 action（仅有 update） | 中 | 归入 `pull_request_updated` 并用 `has_commits=false + 审查 actor` 判 no-op；不依赖假想 action 名（沿 issue-49 审计结论） |
| 阶段路由回归 | 中 | 逐格对照现状 `_resolve_issue_stage_agent`；updated 与 opened 走相同 PR 分支特例（①/②） |

## 七、实施顺序

① normalize 细分 + 上抛 → ② routing 集合 → ③ L1 规则/combo 判定 → ④ L2 对齐 → ⑤ 测试（含生产样本回放：PR 通过审查/assign 等）→ ⑥ 部署 → ⑦ 文档 issue-49 回写。

> 上下游：本方案依赖 issue-49 已确认的 **L1/L2/L3 三层模型**与 **action+changes 组合判定**（weekly-01 文档 §四/§五），此处仅把 PR 部分的「事件归一化来源」补正确。