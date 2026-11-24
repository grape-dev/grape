# Issue #3914 Spec — direct/worker 技能标识一致化

## 目标
让仓级/用户级自定义技能在「本机直接执行（local_claimer）」与「远端 worker」两条链路上行为一致：
派发携带权威 `skill_id`，任务行回写解析后的 `skill_id`，direct 侧 evt 重建时桥接技能标识并复用既有 OBS 取包通道，
门禁按「明示回退」口径判定，消除 PR 订阅 QA 随机断链（`[skill_mismatch]` 误拒 / `materialized official checker ... instead`）。

## 现状（工作分支 arch-weekly-11-ljt 复核）
| 环节 | 现状 | 后果 |
|------|------|------|
| PR 订阅补派/重派 | `_enqueue_pipeline_task(..., 0, qa_skill, ...)`（event_pipeline.py ~1622/~1708）硬编码 `skill_id=0` | 自定义 QA 技能丢失 id 绑定 |
| 入队 | `service.enqueue_task` 解析出 `_sid` 只用于 `skill_files{__obs_key__}`，列值仍取调用方入参 | 任务行 `skill_id` 恒 0，下游无源 |
| direct evt 重建 | `local_claimer._rebuild_evt_from_task` 只回填 repo/issue/type/account/payload/title | `_skill_id`/`_skill_path` 缺失 → 按名找官方目录 → 官方检查器兜底 |
| direct 物化 | `_skill_primary` 仅在 `evt["_skill_id"]` 非 0 时走 skill 表 → OBS zip | 取包通道已存在，只是被 id 门控（C 为验证项） |
| 一致性门禁 | direct 侧 `verify_skill_execution_consistency` 未传 `skill_fallback` | worker 判 `declared_fallback`，direct 判 `local_cache/engine_native` → 误拒 |
| 场景保存 | nodes_json 只落 `skill` 名，`skill_id` 由前端决定 | 新场景节点继续产出 `skill_id=0` |

## 范围（In）
- A 派发传权威 `skill_id`（PR 订阅补派 + 重派两处）
- B 入队回写解析后的 `skill_id` + direct evt 重建桥接（`_skill_id` / `_skill_path` / `__obs_key__`）
- C 复核并复用既有 OBS 取包通道（验证项，不新增通道）
- D 门禁传 `skill_fallback`（必须与 A/B 同批上线）
- E 场景保存存名必存 id + 存量回填脚本（dry-run 先出对照清单）

## 范围（Out）
- #3636 自愈扩展到仓级技能（另单）
- worker 侧取包链路（实测可用，不改）
- 技能包本体（实测完整，不动）

## 验收标准
- [ ] 仓级 QA 技能 + PR 订阅 + 强制落 `local_claimer` 的任务 completed；日志无 `materialized official checker ... instead`、无 `[skill_mismatch]`
- [ ] 同场景 10 连跑（worker/direct 各半）全 completed，工位 `.skills/<绑定技能>/SKILL.md` 存在
- [ ] 反向回归：技能确缺失/损坏时仍 fail-fast，保留 `[skill_mismatch]`，不夹带替身目录、不给误导性回退
- [ ] 场景保存后节点 `skill_id` ≥1；存量回填先出灰度对照清单
- [ ] 新增用例覆盖 `_rebuild_evt_from_task` 的 `skill_id` / `__obs_key__` 桥接
- [ ] 现有测试全绿

## 风险
| 风险 | 影响 | 缓解 |
|------|------|------|
| B/A 触及入队与物化主链路 | High | 灰度单场景、worker/direct 双路径对照；桥接对 `skill_id=0` 保持既有降级 |
| D 单独上线掩盖真失败 | High | 与 A/B 同批合入；回退仅按 materialize 结果 status=fallback 声明 |
| E 存量批量误改 | Med | 脚本默认 dry-run 出对照清单，`--apply` 才写库，幂等 |
