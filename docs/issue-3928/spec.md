# Issue #3928 Spec — codearts 型 runtime 缺 CLI 仍参与认领（认领即失败）

## 目标
- 引擎 runtime 参与认领前做后端可执行文件（CLI）能力自检。
- 认领后后端启动失败不直接判死：自动重派到同类型健康实例，并对故障 runtime 熔断。
- 跨引擎最终执行升 WARNING，带 task_id / runtime_id / engine 与计数指标，按 task 去重。
- 收口 17641(codearts-cred0) 缺 CLI 根因并给出修复/规避。

## 范围
### In
1. worker 注册/心跳上报后端能力（available / degraded / unavailable / unknown），带探针缓存，避免每次心跳起进程。
2. 注册/心跳/认领候选（含 tag=all、池可达性判定）统一按后端健康态过滤。
3. spawn 期“后端不可执行/未找到”识别为可恢复失败：requeue（熔断故障 runtime、幂等、默认上限 2）。
4. runtime 级熔断：冷却窗口内不参与认领；冷却后允许一次半开试探；探活 ok / 任务成功自动恢复。
5. 跨引擎执行：WARNING + task/runtime/engine 字段 + 进程内计数，按 task 去重。
6. 17641 根因收口：缺 CLI 实例在注册/心跳链路被当作 skip 空操作、且旧认领闸门对 `probe_status=fail` 在 worker_version 非空时短路放行。
7. 四类场景自动化用例 + 回归。

### Out
- 认领/session 指针契约重构（#3881）。
- 角色-引擎匹配（#658）。
- worker 分发包体系重做（#2525 / #3642）。
- 历史记录治理、seq=96767 手工重派（运维侧）。

## 行为定义
- `available`：对应后端命令可解析。
- `unavailable`：明确未找到后端命令（不参与认领）。
- `degraded`：命令存在但探活/spawn 失败（不参与认领，保留恢复观察）。
- `unknown`：未纳管引擎或探针故障；不摘除，保持旧行为（避免全量停摆）。

## 验收
- [ ] 缺 CLI runtime 注册/心跳后即 `healthy=0`、`backend_status=unavailable`，带 runtime_id 告警，认领候选集不含它。
- [ ] 认领后后端启动失败：任务 requeue 到健康同类型实例并成功产出；故障实例熔断，冷却期内不再认领。
- [ ] 重派超上限（默认 2）判 failed 并告警，不无限重试。
- [ ] 跨引擎执行 WARNING 含 task_id/runtime_id/engine，按 task 去重。
- [ ] 17641 根因结论与规避措施写入 Issue。
- [ ] 正常 codearts 实例认领执行零回归；新增用例 + 现有测试全绿。