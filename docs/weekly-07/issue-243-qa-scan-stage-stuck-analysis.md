# 需求中心「雷达扫描」卡在 qa——QA 空收尾导致 stage 未推进（issue #243 分析）

> 状态：根因已定位，待处置（2026-10-14）｜环境：单机生产 `<PROD_HOST>`（DEPLOYMENT-REMOTE.md，weekly-07-lyz）
> 现象：需求 #243（hello-calculator-13 Issue #14「我要开发一个专属的sre技能」）QA（seq 2315）事件已完成，但需求中心一直显示 qa 阶段（雷达扫描），stage 未推进。

---

## 1. 事实（DB 快照）

**issue id=243**
```
repo_key=gitcode:Jason7602/hello-calculator-13  issue_number=14
status=processing  stage=qa  owner_agent=qa
scene_id=grape-skill-dev  conversation_id=9a927d9c…
updated_at=2026-10-14 03:05:09
```

**事件链（complete A2A 全部跑完）**
| seq | event_type | agent | status | completed_at | result |
|-----|-----------|-------|--------|--------------|--------|
| 2312 | issue_comment | vod | completed | 02:52:44 | 完整 JSON |
| 2313 | a2a_event | issue | completed | 02:56:01 | 完整 JSON |
| 2314 | a2a_event | coding | completed | 03:05:36 | 完整 JSON（含 PR #9）|
| 2315 | a2a_event | **qa** | **completed** | 03:07:54 | **空 ''** |

**QA（2315）关键字段**
- `self_reported=0`（未调 tasks complete）
- `result_text=''`、`errors=''`、`exit_code=0`
- agent_logs 末条：`{"type":"done","msg":{'exit_code':0,'text':'\n',... 'text_count':1,'tool_count':19}}`
  → **最终 text 仅为 `'\n'`**，Step 9（2 分钟思考）后空完成，无任何收尾命令。

**场景**：`grape-skill-dev` nodes = `vod→issue→coding→qa→sre→done`，sre 末节点（next=done）。

## 2. Stage 推进机制（正确模型，本次无 bug）

Stage **由 Agent 显式动作驱动**，服务端不自动猜流程：

1. **A2A handoff**（`src/api/routes/webhook.py:315 /api/a2a` → L554-566）：
   - Agent handoff 到 `agent_name` → `set_issue_stage(..., stage=agent_name, owner=agent_name)`
   - 末节点场景（agent==done 或 next==done）→ `stage=pending_review`
   - 即：**agent 显式 handoff 给谁，stage 就设成谁**（`qa→sre` 则 stage=sre；末节点则 pending_review）。
2. **explicit pending-review**（`src/api/routes/issues.py:945`）：ga 显式置审核点 → `stage=pending_review`（不改 stage 语义的内部实现）。
3. **explicit review-pass target=done** → 置 done。

> ⚠️ 结论：**"需求中心状态 = stage 忠实反映 Agent 最近一次动作归属"**。服务端不因事件 completed 就自动推进 stage——这是设计意图（flow 由 skill 控制），**不应改为「流结束自动推进下一节点」**。

## 3. 根因：QA 未按 skill 契约收尾

QA skill（`skills/qa/auto-testing/grape-skill-validator/SKILL.md`）明示收尾契约：

| 契约要求 | 本次 QA 实际 |
|---------|-------------|
| 检查通过 + **PR 未合入** → **必须 `grape issues pending-review` 置审核点**（L92/L106）| ❌ 未调 → stage 停在 qa |
| 结束必须 **`grape tasks complete`** 自报完成（L80/L93/L107）| ❌ 未调（self_reported=0）|
| 完成后结束任务，不再思考等待 | Step 9 只输出 `'\n'` 后 done（tool_count=19 无收尾命令）|

**结果**：QA 既未 handoff sre（PR #9 未合入，本不该 sre），也未置 pending-review——两件必做动作都没做，引擎空收尾。服务端无从推进，stage 停在 `qa`，需求中心显示 qa 扫描态。

## 4. 为什么 `completed` 了还叫"扫描"

- 事件级 `status=completed`（引擎流结束）与 **需求中心 stage 展示是两层**：
  - 事件 completed：引擎执行真实结束（stream_ended=1）✓ 已感知
  - stage：推进权在 Agent 动作（pending-review / a2a / complete），未执行则不推进。
- 需求中心"雷达扫描" = **stage=qa 徽标**，忠实反映"当前归属 QA 且未继续"。
- **这不是"系统不知道任务结束"，而是"任务该走的下一步动作（pending-review）没执行**。

## 5. 结论一句话

> 需求 #243 卡在 qa 扫描态，是 **QA（seq 2315）在检查通过+PR 未合入的场景下，未按 skill 契约执行 `pending-review`/`tasks complete` 便空收尾**（最终 text='\n'、self_reported=0），服务端 stage 归属停留在 qa。Stage 推进机制本身正确（由 Agent 显式动作驱动），无需改动；问题在 QA 执行稳定性/契约遵循。

## 6. 处置建议

### 6.1 运维止血（生产，可立即）
- 手动补 `grape issues pending-review --issue_id 243` 置审核点 → stage=pending_review，需求中心退出 qa 扫描态。
- 或：issue 243 手动 A2A/置 done（若确认无需用户审核）。

### 6.2 根因修复（代码/契约层）
- **A. QA 执行稳定性**：Step 9 静默空结束（2 分钟思考后 text='\n'）——查 opencode 末步是否超时/截断；`grape-skill-validator` 若已将 `pending-review` 与 `tasks complete` 均发出但被吞，需在 agent_logs 里留痕。
- **B. 服务端兜底（可选，谨慎）**：末节点或 pending 契约下，若 **事件 completed 且 `self_reported=0` 且 flow 允许**，由服务端按 skill 语义补记警告或提示（**不改 stage，仅可观测**），便于识别"空收尾"case。
- **不引入**："流结束自动推进 stage" 的改动（违背 A2A 显式归属模型）。

## 7. 验证

| # | 用例 | 断言 |
|---|------|------|
| V1 | 手动 pending-review #243 后 | issue.status=awaiting? / stage=pending_review，需求中心退出 qa 扫描 |
| V2 | 复跑同流程 QA | QA 若正常执行 pending-review + tasks complete，stage 自动 pending_review |

## 8. 落档
- 2026-10-14：stage 推进机制正常（Agent 显式动作驱动），issue #243 卡 qa 系 QA 空收尾（未 pending-review / 未 complete）；处置见 §6。已更新本文档至 weekly-07。