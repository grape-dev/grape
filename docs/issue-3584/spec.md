# Spec: Issue #3584 — pick_engine 多引擎负载分摊（编码落地版）

> 前序 Issue Agent 已发布完整 Spec（见 Issue 评论 Spec Document），本文为编码阶段的落地口径，
> 与 Spec 保持一致，不重复目标/风险论述。

## 实现摘要

- `src/domains/runtime/engine/engine_select.py`
  - 新增 `_engine_loads(engine_ids)`：单次 GROUP BY 批量查询各引擎
    `agent_tasks` 中 `running/dispatched/pending_self_report` 计数，
    口径与 `service._real_active_count`（方案 D 真实在跑数）一致，不引入第二套负载定义。
  - 新增 `_attach_loads(members)`：返回带 `load` 字段的候选**副本**（缺省 0），
    不就地改写调用方 dict（QA 复审修正）。
  - 新增 `_pick_decision(cand, ..., cfg, advance=True)`：优先级档内最小负载优先，负载并列按进程内
    单调轮转 seq（`_RotationSeq`，加锁 advance/peek，`advance=False` 时只读 peek）对
    runtime_id 升序集合取模轮转；`rule=random` 分支保持随机不变；**未知 rule 值显式
    告警并沿用旧语义回退 random**（旧代码非 order 即 random，避免配置拼写错误
    静默改变派发口径）；cfg 由调用方传入，热路径不再每候选重复读取
    engine-selection.json；候选日志推导抽 `_decision_candidates` 两分支共用，
    仅在 INFO 决策日志开启时构造候选全集，标签批量预取（单次 IN 查询）避免逐候选
    N+1；同时输出 `engine_select_decision` 结构化日志
    （候选全集 rid/priority/load/reasons + selected + reason 标注 load/rotation）。
  - `_engine_loads` 查询异常 fail-open 时输出 `engine_select_decision`
    （result=fail_open）留痕，负载读取失败对取证可见。
  - 新增 `_select_rid_from_members(members, cfg, ..., advance=True)`：档位 → 健康过滤 →
    档内最小负载 + seq 轮转的统一选择入口；`pick_engine` legacy 多引擎分支经它执行
    （原 `min(priority, runtime_id)` 语义替换）。基线 Issue #3396 已删除 SRE 禁广播/禁回退
    特例（`_sre_no_broadcast`），SRE 与其它角色同走通用选择链，候选全灭时按通用语义回退
    会话主引擎；本改动的分摊/轮转语义对全部角色一致生效。
  - `explain_engine_selection` 候选输出补 `load` 字段，与决策日志口径一致；
    `selected` 改为复用 `_select_rid_from_members` 于**同一候选快照**并传
    `advance=False`（只读 peek），不再二次调用 pick_engine，取证调用不推进真实轮转
    序号、无快照漂移。
- 不改 plugin 广播语义（`pick_engine` 返回 0 + `pool_engine_ids`）与 claim 侧认领逻辑。

## 轮转语义

- 轮转序号为进程内单调递增计数器，**按「repo/角色/场景/同档候选 runtime_id 全集」分桶**
  维护（桶键取该优先级档全部候选 runtime_id 升序，而非当次最小负载子集，避免负载
  波动导致 seq 反复归零），不同场景互不干扰（避免多场景交织下奇偶恒定命中同一台，AC1）；
  仅在并列候选 >1 时推进序号，单候选场景不消耗 seq。
- 轮转池桶数量设上限（默认 4096），超出按插入顺序 FIFO 淘汰最旧桶，避免长生命周期
  进程在 scene/候选集合频繁变化时桶字典无限增长；被淘汰桶下次命中从 0 重新计数。
- 多主控部署下各实例独立计数，允许短窗口重复选同台、最终收敛（Spec 风险项既定缓解）。
- **进程重启影响**：seq 归零后首个批次按「最小负载 → runtime_id 升序取模」重新
  从头轮转，短窗口内向小 id 引擎轻微倾斜；随派发次数增长重新均匀，
  不产生持续单点（负载优先仍持续纠偏）。
- 负载并列集合按 runtime_id 升序后取 `seq % len`，保证可复现、可测试。

## 测试（tests/runtime/test_issue_3584_engine_load_balance.py）

- 最小负载优先（满载的最小 id 引擎被绕开）
- 同负载轮转，6 连发覆盖全部 3 台绑定引擎（无空转）
- 单点塌陷修复：满载引擎负载释放后重新参与分摊
- 档位优先级仍先于负载（repo 档满载优先于 global 档空载）
- SRE 同一通用链（repo 档两台并列负载轮转；Issue #3396 去特例后无独立 SRE 分支）
- 决策日志含候选 load 与 load=/rotation= 选中理由
- explain_engine_selection 候选含 load
- QA 复审回归（TestReviewFixes）：未知 rule 回退 random + 告警（断言 random 分支特征）；
  cfg 传入后热路径不再重复读配置；`_attach_loads` 返回副本不改写输入；explain
  `selected` 与候选同一快照（不二次调用 pick_engine）；`_engine_loads` fail-open 输出决策日志
- QA 二次复审回归（TestSecondReviewFixes）：SRE 绑定引擎标签全不匹配 + fallback 非零
  → 按通用链回退 fallback（Issue #3396 去特例，不再强制 None）；explain 不推进轮转序号
  且 selected 与下一次真实派发一致；INFO 未开启时不构造决策候选全集（热路径门控）
- QA rebase 复审回归（TestThirdReviewFixes）：候选全集含被标签过滤成员；SRE 无健康 /
  健康兜底选中 / 全档位不健康无兜底三类零派发均输出 engine_select_decision
- QA rebase 复审回归（TestFourthReviewFixes）：轮转序号按场景/候选集合分桶
  （同引擎多场景互不干扰，AC1）；disabled/plugin/fallback 决策日志复用 `_decision_candidates`
  统一 schema（含 candidates 列表，AC4）
- QA 复审回归（TestFifthReviewFixes）：AC4 fallback 留痕——绑定引擎被标签全过滤回落
  主引擎时，决策日志 candidates 带入已加载候选（含被过滤成员与 load）；轮转池桶数量设上限
  （超限 FIFO 淘汰最旧桶），长生命周期进程不再无限增长

## 验收对照

- `gt_kit.py up --new-repo --workers 3 --scenes 3 --issues 100` 分布验证属环境级回归，
  由 QA/运维侧执行；单元级等价断言（轮转覆盖全集、最小负载优先）已由上述测试覆盖。
