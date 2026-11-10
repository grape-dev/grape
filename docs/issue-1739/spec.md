# Spec: Issue #1739 — 流水线 QA 失败时 Issue 不因 PR 提交/合入被自动关闭

## Objective
- 在 Issue 自动关闭/置完成的统一判定链路增加 **QA 结论门禁**：同一流水线 qa 节点最新结论为 fail 时，
  PR 提交/合入不得把 Issue 自动关闭或置为 completed。
- QA 失败时 Issue 保持可继续处理，并在 Issue 上保留 QA 失败原因与关联 PR，便于回流修复。
- qa 通过或流水线无 qa 节点时，关闭链路行为完全不变（回归）。

## Scope
### In Scope
- 统一的 QA 结论查询与判定（`src/api/qa_gate.py`）：按 issue 取最新 qa 事件，兼容
  `status=failed` 与 `status=completed` 但结论文本为“不通过”的实际形态。
- 关闭/完成判定点接入门禁：
  - `auto_finalize_issue_on_last_node`（末节点自动结单）
  - `_review_pass(target=done)` / `_complete_issue_done`（Agent 审核通过收尾）
  - `git-repo pulls create` 的 `auto_close_issues` 透传（qa 失败时强制不自动关闭）
  - `git-repo pulls merge` 合入前门禁（qa 失败时 409 阻断合入，避免 VCS 侧自动关闭）
- QA 失败时在 Issue 发布可追溯评论（QA 结论/事件 seq/关联 PR），best-effort 不阻断主流程。
- 三类场景单元测试：qa fail / qa pass / 无 qa 节点。

### Out of Scope
- 不改 qa 节点自身执行、重试、A2A 回流逻辑。
- 不改 PR 代码审查与人工 confirm-done 的语义（门禁只作用于自动关闭/完成）。
- 不做历史误关闭 Issue 的数据回溯修复。

## Technical Approach
1. `qa_gate_check(repo_key, issue_number)`：
   - 校验该 Issue 所属流水线是否存在启用中的 `qa` 节点；无 qa 阶段直接放行（`qa_present=false`）。
   - 查询该 Issue 最新 qa 事件（`agent_type='qa'`，兼容 `#N` / `N` 两种 issue_id 历史形态）。
   - 判定：事件 `status=failed/cancelled` → fail；`completed` 时解析 `result` 结论文本，
     识别“不通过/未通过/阻塞/阻断/驳回/整改/需人工复核/失败”等失败语义，并排除
     “无阻塞/非阻塞/无 Critical/无 Required” 等否定语境。
   - 无 qa 事件或结论不可判定 → 不拦截（避免误杀）。
2. `annotate_qa_failure(...)`：QA 失败被门禁拦截时，以 bot token 在 Issue 评论中输出
   QA 结论、事件 seq 与关联 PR（幂等标记 `<!-- grape-qa-gate -->`，降级不阻断）。
3. 关闭判定点统一调用 `qa_gate_check`：命中 fail 时不落 completed、不关闭 VCS Issue，
   返回机器可读原因（`qa_failed_blocked` / `noop:qa_failed` / 409 gate）。
4. qa pass、无 qa 节点、无 qa 事件、查询异常均放行，原关闭链路不变。

## Acceptance Criteria
- [ ] qa 失败 + PR 提交/合入 → Issue 不被置为 closed/completed，仍可继续处理。
- [ ] 该 Issue 可看到 QA 失败结论/原因与关联 PR。
- [ ] 回归：qa 通过或无 qa 阶段后提交/合入 PR → Issue 按现有逻辑正常关闭。
- [ ] 所有现有测试通过；新增测试覆盖 qa fail / qa pass / 无 qa 三类场景。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 结论文本误判导致误拦截 | High | 仅识别明确失败语义；否定语境先剔除；不可判定/异常一律放行 |
| 门禁覆盖不全（旁路关闭） | Med | 自动结单、审核收尾、PR 创建自动关闭、PR 合入四点统一接入 |
| 额外查询开销 | Low | 仅在关闭/合入动作时各执行一次，按 seq 取单条 |

## Open Questions
- 无。