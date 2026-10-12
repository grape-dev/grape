# 方案：取消 PR merged「QA 阶段直接跳 SRE」特例，避免合入自动触发部署

> 状态：技术方案（2026-10-11）｜分支：weekly-06-lyz
> 动机：当前 `pull_request_merged` 事件在 `issue.stage=qa` 时会自动派发 **SRE** 并特地把 stage `qa→sre`（event_pipeline.py:1397-1405 的"跳过 QA 直接部署"特例 + `_STAGE_EVENT_AGENT_MAP` L607 的 `qa→sre`）。该行为使「PR 一旦合入就自动触发 SRE 部署」，与预期流水线（QA 完成后**显式 A2A 交办** SRE）冲突，形成干扰。
> 目标：**PR 合入不再自动驱动 SRE**；SRE 触发收敛回显式路径（A2A 交办 / stage=sre 的常规事件路由）。不影响 coding/qa 既有合入回执行为。

---

## 1. 现状（代码核对 2026-10-11）

| 位置 | 现状 |
|------|------|
| `_STAGE_EVENT_AGENT_MAP["pull_request_merged"]`（event_pipeline.py:607） | `{"vod":"issue","issue":"issue","coding":"coding","qa":"sre","sre":"sre","pending_review":"","done":""}` —— **stage=qa 合入 → 派 SRE** |
| 特例块（event_pipeline.py:1397-1405） | `if event_type=="pull_request_merged" and agent_type=="sre": _st.stage=="qa" → set_issue_stage(qa→sre)` —— 合入时把 stage 从 qa 直接推进到 sre |
| `_match_pr_subscription`（L100-125）+ 设置（L1348-1349） | PR 订阅仍预取 `sre_skill/_pr_sub_sre_engine_ids` 写入 evt——**无任何消费方（只写不读，死字段）** |

> 组合效果：QA 阶段 PR 合入 → 自动派 SRE + stage 被强推 sre → SRE 直接开跑部署，绕开了"QA 审完 → 显式 A2A → SRE"的正常交办链。

---

## 2. 方案设计

### 2.1 矩阵改：`pull_request_merged` stage=qa 不再派 SRE

```python
"pull_request_merged": {"vod": "issue", "issue": "issue", "coding": "coding",
                        "qa": "", "sre": "sre", "pending_review": "", "done": ""},
```

- `qa → ""`：合入时 stage=qa 一律**不分发**（PR 合入只落库/回执，不自动驱动任何 agent；如需部署由显式 A2A）。
- 保留：`coding → coding`（编码阶段合入回执，非 SRE 跳转）；`sre → sre`（SRE 已在部署态，合入事件正常触达）。

### 2.2 删除 SCREEN 特例块（event_pipeline.py:1397-1405）

整体删除：

```python
    # PR merged 跳过 QA 直接路由 SRE：补推 stage qa→sre（原由 QA A2A handoff 推进）
    if event_type == "pull_request_merged" and agent_type == "sre":
        ... set_issue_stage(qa→sre) ...
```

> 删除后该分支永远不会命中（矩阵已不再向 stage=qa 的合入派 SRE），连同 `_gis`/`set_issue_stage` 的import与推进全部移除。

### 2.3 清理死字段（PR 订阅预取的 sre 技能）

- 删除 event_pipeline.py:1348-1349 的 `evt["_pr_sub_sre_skill"] = _first[3]` / `evt["_pr_sub_sre_engine_ids"] = _first[4]`（grep 确认仅这两处写、无读）。
- `_match_pr_subscription` 返回元组里保留 `sre_skill/sre_engine_ids`（供将来**显式** A2A 到 SRE 时取用，本方案不删，仅不再被自动路由消费）。

---

## 3. 各模块改动

| 位置 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | ① L607 矩阵 `pull_request_merged.qa`: `"sre"→""`；② 删除 L1397-1405 特例块；③ 删除 L1348-1349 死字段设置 |
| `tests/` | 路由用例（见 §5） |

---

## 4. 影响面 / 边界

| 场景 | 改前 | 改后 |
|------|------|------|
| stage=qa + `pull_request_merged` | 派 SRE + stage qa→sre（自动部署）| **不分发**（合入仅落库/回执，stage 保持 qa） |
| stage=qa + `pull_request_opened/updated` | qa 审查（矩阵） | 不变 |
| stage=sre + 任何 PR 事件 | sre | 不变（SRE 部署态正常处理） |
| stage=coding + merged | coding 回执 | 不变 |
| QA 审完 → 要部署 | 自动跳 SRE（隐式）| **需显式 A2A 交办 SRE**（回到设计本意） |
| PR 订阅场景 | 解析 qa+sre skill，sre 技能仅被预写（未消费）| sre 技能保留解析（供显式用），死字段不再写 |

> 明确不做：不改 QA→SRE 的正常 A2A 交办；不改 SRE 的 `cloud-deploy/iac-sre/skill-publish` 技能链路；`_match_pr_subscription` 的 qa 路由保留。

---

## 5. 验证

| 用例 | 断言 |
|------|------|
| UT-01 stage=qa + `pull_request_merged` → 路由 `agent_type=""`（不派 SRE），stage 仍 qa | 取消跳转 |
| UT-02 stage=qa + `pull_request_opened/updated` → qa（回归） | 不误伤 |
| UT-03 stage=sre + `pull_request_merged` → sre，stage 保持 sre | SRE 态正常 |
| UT-04 stage=coding + `pull_request_merged` → coding | 回执保留 |
| UT-05 `_pr_sub_sre_skill` 不再写入 evt | 死字段清理 |
| 回归 | PR 订阅 qa 审查、QA A2A→SRE 交办链路不受影响 |

---

## 6. 落档修订说明

- 只取消「合入自动跳 SRE」这一条隐式路径；SRE 触发收敛为：流水线 A2A 显式交办 + stage=sre 的常规事件 + 手动/API 选择含 SRE 场景。
- 死字段清理以"grep 无读方"为准，避免遗留误导的预取逻辑。
- 若后续要让"合入→部署"重新自动化，应改为**场景配置/订阅显式声明**（如 PR 订阅带 `deploy_on_merge=true`），而非暗示式阶段跳转——本方案留出该扩展位。