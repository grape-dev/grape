# Spec: 多场景 PR 订阅 — 人类用户主路由补派剩余场景（#318 回归修复）

> 目标分支: mys-local（基线 fa4168eb7，含 !449 #318 修复）
> 关联: Issue #318 的方案 B 修复引入的回归

## 1. 问题陈述

### 1.1 现象

仓库内多个场景（A、B）的 PR 订阅（`pr_subscription_enabled=1`）指向同一个工作分支时：

- **#318 修复前**（a93b796a9）：人类用户开 PR → 产生 **3 条事件**（A a2a_event + B a2a_event + A pull_request_opened）。A 场景双重派发（#318 要修的 bug），B 场景正常触发。
- **#318 修复后**（fa4168eb7，当前线上）：人类用户开 PR → 产生 **1 条事件**（仅 A 的 pull_request_opened）。A 场景双重派发已修复，**但 B 场景完全不再触发**。

bot 用户开 PR 的行为修复前后不变（1.5 预匹配派发全部场景）。

### 1.2 根因

PR 订阅的多场景派发路径只有两条：

| 路径 | 多场景能力 | #318 修复后的开放范围 |
|------|-----------|----------------------|
| 1.5 预匹配循环（`for _extra in _pr_sub_matches_early: _dispatch_pr_subscription_extra(...)`，event_pipeline.py ~1166 行） | 能派发全部匹配场景 | **仅 bot actor** |
| 主路由（`_first = _pr_sub_matches[0]`，~1378 行） | **只取第一个场景** | 人类用户（非 bot actor） |

修复前人类用户"歪打正着"：1.5 预匹配对所有 actor 开放，A、B 都被派发（B 的触发依赖这条 bug 路径捎带），主路由再重复派 A。#318 把 1.5 预匹配对人类关闭后，人类用户只剩主路由单场景通道——**第二个及之后的场景失去了唯一触发通道**。

### 1.3 影响面

- 任何"多场景订阅同一分支 + 人类用户开 PR/推新 commit"的组合，第 2 个及之后的场景的 QA 审查静默丢失（无报错、无 skipped 记录，纯漏跑）。
- 单场景订阅（绝大多数仓库）不受影响。
- bot 用户 PR 不受影响。
- 注意 `_filter_pr_sub_matches` 第 3 步会按 qa_skill 去重（同 skill 场景只保留第一个）——这是设计行为，本需求不改变它；受影响的是**不同 qa_skill 的多场景**。

## 2. 需求目标

人类用户触发 PR 订阅事件（pull_request_opened / pull_request_updated 带新 commit）时：

1. **全部匹配场景都被派发**，每个场景恰好一条事件（不多、不少）；
2. 第一个场景沿用现有主路由机制（pull_request_opened 主事件），保持 #318 修复后的单次派发语义；
3. 剩余场景（`_pr_sub_matches[1:]`）通过 `_dispatch_pr_subscription_extra` 并行派发（a2a_event），与 bot 路径的 1.5 预派发行为对称；
4. bot actor 行为保持不变（1.5 预派发 + 自循环拦截，主路由不执行）；
5. **单场景订阅不得双重触发**——只有 1 个匹配场景时，主路由派发该场景后不得再产生额外的 a2a_event（`_pr_sub_matches[1:]` 为空，循环体不执行，天然满足；但必须作为验收用例显式锁死，防止后续改动破坏该不变量——这是 #318 修复的核心成果，本需求为最高优先约束）。

### 非目标

- 不改 `_filter_pr_sub_matches` 的去重规则（Issue 场景排除、同 skill 去重）；
- 不改 `_dispatch_pr_subscription_extra` 内部的引擎选择/派发逻辑；
- 不处理 pull_request_merged（设计上不订阅）。

## 3. 技术方案

### 改动点（单处，~5-8 行）

`src/api/event_pipeline.py` 主路由 PR 订阅段（~1378 行附近），在 `_first = _pr_sub_matches[0]` 设定主场景之后、`agent_type = "qa"` 之前，追加剩余场景循环：

```python
if _pr_sub_matches:
    _first = _pr_sub_matches[0]
    evt["_pr_sub_scene_id"] = _first[0]
    ...（现有 _first 字段注入保持不变）
    # —— 新增：补派剩余场景（人类用户主路由全量派发，与 bot 1.5 预派发对称）——
    for _extra in _pr_sub_matches[1:]:
        _dispatch_pr_subscription_extra(repo_key, evt, event_type, _extra)
    agent_type = "qa"
```

要点：

- 人类路径：主路由派 `_first`（1 条主事件）+ 循环派 `_pr_sub_matches[1:]`（各 1 条 a2a_event）→ 每场景恰 1 条；
- bot 路径：自循环检测已 return，这段代码不可达，行为零变化；
- `_early_dispatched` 兜底去重（#318 改动 2）在 `_pr_sub_matches` 过滤阶段已生效，与本改动无冲突；
- 场景循环内 `_dispatch_pr_subscription_extra` 自带 try/except，单场景派发失败不影响其余场景（现状语义保持）。

### 派发形态汇总（修复后预期）

| actor | 场景数 | 事件形态 |
|-------|--------|---------|
| 人类 | 1 | 1 × pull_request_opened |
| 人类 | 2（不同 skill） | 1 × pull_request_opened(A) + 1 × a2a_event(B) |
| 人类 | 3（不同 skill） | 1 × pull_request_opened(A) + 2 × a2a_event(B,C) |
| bot | N | N × a2a_event（1.5 预派发），主路由被自循环拦截 |
| 人类 | 2（同 skill） | 1 条（`_filter` 同 skill 去重，设计行为） |

## 4. 测试方案

### 4.1 回归测试（新增 tests/test_pr_subscription_multiscene.py 或并入 #318 测试文件）

拟真数据：托管仓库 + 场景 A(qa-cli)/B(pr-reviewer) 订阅同一 work_branch，不 mock 派发函数、事件真实落库（沿用 test_sim_318_two_scenes.py 的拟真脚手架）：

- [ ] T1 人类用户开 PR → 恰 2 条事件，A、B 场景各 1，无重复
- [ ] T2 人类用户 PR update 带新 commit（oldrev）→ 同样 2 场景都触发
- [ ] T3 bot 用户开 PR → 仅 2 条 a2a_event（1.5 预派发），主路由无事件（#318 行为不回归）
- [ ] T4 单场景仓库人类 PR → **仍恰 1 条 pull_request_opened，无任何 a2a_event**（单场景双重触发禁令，最高优先约束——#318 修复成果的锁死用例）
- [ ] T5 同 skill 双场景 → 仍 1 条（`_filter` 去重设计行为不回归）
- [ ] T6 #318 原有 5 用例全数通过（双重派发不回归）

### 4.2 存量对照

- 基线 a93b796a9 上跑 T1 复现"B 场景丢失"（3 条 → 期望修复后 2 条），确认测试有效性。

## 5. 影响范围与风险

- **改动面**：event_pipeline.py 单处 +5-8 行，纯加法（新循环），不改任何现有行；
- **风险**：
  - 多场景仓库的人类 PR 事件量增加（B 场景恢复触发）——这是修复目的而非风险，但若线上有依赖"只跑一个场景"的隐含假设需确认；
  - `_dispatch_pr_subscription_extra` 内 `pick_engine` 返回 None 时该场景标 failed（现状语义），多场景意味着更多这种 failed 噪音——接受，与 bot 路径现状一致；
- **不改动**：worker/direct 派发、引擎选择、event_active_guard 幂等、Flyway、前端。

## 6. 验收标准

1. T1-T6 全部通过；
2. 拟真三场景（A/B/C 不同 skill，人类 PR）→ 恰 3 条事件，scene_id 集合 = {A,B,C}；
3. **拟真单场景（人类 PR）→ 恰 1 条 pull_request_opened，a2a_event 数 = 0**（双重触发禁令实测锁死）；
4. `pytest tests/test_pr_subscription_dedup.py`（#318 官方用例）5/5 通过；
5. `node --check` / 语法检查通过（本次不涉及前端）。
