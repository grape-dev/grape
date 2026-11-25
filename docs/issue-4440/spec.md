# Spec: Issue #4440 — Coding 提前结束且无关联 PR，事件仍显示成功

> Target Branch: weekly-mys
> 来源：Issue [issue agent] Spec 评论（评级 ready），本文件为仓内存档同步。

## Objective
在 Coding 把任务/事件记录为成功之前，向托管平台核实原需求是否存在可核实的关联
PR；核实失败（无 PR / 查询失败）不得走成功路径，改记「Coding 未交付」并携带
原因，停止成功路径自动推进（不触发下游 QA/流转）。

## Scope
### In Scope
- 统一「交付核实」校验入口：输入 repo_key + 原需求编号 + 工作流，输出
  已交付 / 未交付（含原因与机器可读 code）。
- Coding（atomcode-bug-fix 工作流）成功收尾记录前置核实。
- **所有成功通道都必须核实**，不只在入口 `exit_code==0` 时：
  ① 入口即成功；② `exit_code!=0` 但带完整结构化产出将「复活为 completed」
  （如 -15）；③ 瞬时故障降级为 completed（transient_retry 写证据 + 有产出）；
  ④ `enrich_task_on_late_report` 的 failed 占位「迟到自报提升 completed」。
- 命中核实清单但**无需求编号**（issue_id 为空）不得跳过核实：按 `no_issue_number`
  记未交付（与瞬时通道 fail-closed 同口径），不再以 issue_id 非空作为闸门前提。
- 远程 Worker 收尾路径（service.complete_task）与 direct 收尾路径
  （direct_executor._execute_agent_inner）共用同一入口。
- 未通过 → 任务/事件 failed + `exit_code=1`（与 worker 同口径）+ 「Coding 未交付」
  原因 + 结构化错误码 CODING_DELIVERY_UNVERIFIED；成功钩子（自动推进/回帖/会话
  idle/后端熔断释放）不触发，且不得先于核实执行。
- 瞬时降级被核实拒绝时，调用方不得沿用原 exit_code（如 -1）静默判死：
  统一落 `exit_code=1` + 事件 result「Coding 未交付」前缀（worker / direct 同口径）。
- PR 订阅伪 issue_id（`#pr_<n>_<scene>`）不要求新交付 PR → 两条路径同口径跳过
  核实（direct 侧同时保留 `_pr_sub_pr_number` 显式守卫）。
- PR 全量翻页查询在带墙钟超时的子线程执行（默认 15s），超时按查询失败处理，
  不长时间占用收尾请求线程。
- 核实异常兜底只对**命中核实清单**的工作流 fail-closed；未命中清单的 coding
  工作流即便核实链路异常也不得判死（防误杀）。
- 回归测试：无 PR / 查询失败 / 有 PR 三类场景 × 两条路径，覆盖 -15 结构化复活、
  瞬时降级（含被拒口径）、迟到提升、无需求编号、成功副作用时序、PR 订阅不对称、
  direct exit_code。
- 回归测试：无 PR / 查询失败 / 有 PR 三类场景 × 两条路径，覆盖 -15 结构化复活、
  瞬时降级、成功副作用时序、PR 订阅不对称、direct exit_code。

### Out of Scope
- 其他 Coding 工作流（skill 未命中核实清单）行为调整。
- 新增/补建 PR、改动交付内容。
- 前端展示大改（状态与原因经既有 result/errors 字段呈现）。
- `list_prs` 按 head / 时间窗收敛调用量（复审判断项）：本需求的 PR 查询按
  「原需求编号」匹配，head 命名/时间窗过滤均可能漏掉真实关联 PR 而把已交付
  误判未交付（fail-closed 反向误杀）；当前保留有界全量翻页（≤20×100）+ 墙钟
  超时，调用量问题另立优化项跟踪。

## Technical Approach
1. 新增 `src/domains/runtime/engine/delivery_verify.py`：
   `verify_coding_delivery()` 唯一入口；核实清单默认 `atomcode-bug-fix`，
   环境变量 `GRAPE_DELIVERY_VERIFY_SKILLS` 可覆盖；PR↔Issue 关联判据复用
   issues_center（`_pr_vcs_client` / `_pr_related_issue_nums`），fail-closed。
2. worker 路径：`complete_task()` 在 #403 瞬时重试之前核实；未通过
   exit_code=1（failed），事件 result 前缀「Coding 未交付」+ 原因，
   agent_tasks.errors 落结构化错误码；结构化产出「复活为 completed」的
   通道对交付核实失败显式关闭。
3. direct 路径：`_execute_agent_inner` 在终态写入（update_task_status /
   update_event_complete）与 #403 重试之前同口径核实，未通过 new_status=failed
   且 exit_code=1；崩溃兜底同样落 CODING_DELIVERY_UNVERIFIED 结构化码。
4. 瞬时降级：`transient_retry.retry_transient_task` 在降级 completed 前调用
   `verify_task_delivery`，未通过则不降级并通过 `delivery_out` 回传核实结果，
   调用方统一落 exit_code=1 + 「Coding 未交付」前缀。
5. 副作用时序：worker `note_task_success` 与 direct 会话 `status='idle'` 移到
   核实通过之后；`_mark_session_idle` 抽为可断言函数。
6. 迟到提升：`enrich_task_on_late_report` 的 failed→completed 分支前置
   `verify_coding_delivery_safe`，未通过保持 failed 并落结构化错误码。
7. 兜底与去重：`verify_coding_delivery_safe` 统一异常兜底（仅命中清单 fail-closed）；
   `_issue_text` 合并 PR 伪 id / 编号解析。

## Acceptance Criteria
- [x] atomcode-bug-fix 无关联 PR 时不记录为成功，显示「Coding 未交付」+ 原因
- [x] 查询失败（网络/权限/平台异常/无 token）按未交付处理，不静默记成功
- [x] 存在可核实关联 PR 时正常记成功并继续推进
- [x] 远程 Worker 与 direct 两条收尾路径共用同一校验入口（源级守卫测试锁定）
- [x] 其他 Coding 工作流行为不变（未命中清单不核实、不外呼查询）
- [x] 全部成功通道（入口成功 / -15 结构化复活 / 瞬时降级 / 迟到提升）均经核实
- [x] 无需求编号（issue_id 为空）命中清单时同样记未交付，不再 fail-open
- [x] 未交付 exit_code=1、结构化错误码与「Coding 未交付」展示双路径同口径
- [x] 瞬时降级被拒后调用方落 exit_code=1 + 未交付前缀（worker / direct 同口径）
- [x] 成功副作用（后端熔断释放 / 会话 idle）在核实通过后执行
- [x] PR 订阅伪 issue_id 双路径同口径跳过（不误判未交付）
- [x] 核实异常兜底不误杀未命中清单的 coding 工作流
- [x] 新增测试覆盖上述场景（tests/runtime/test_issue_4440_delivery_verify.py，38 用例）
