# Plan: Issue #541

## 根因链路（已在开发分支核实）
1. `templates.py:292-296` 模板覆盖：`apply_market_scene_by_name(...)` 返回 bool 被丢弃，`set_repo_scene(repo_key, tpl_scene_id)` 把模板名（如 `hello-world`）写进 `managed_repos.dev_scene`。
2. `pipeline_config.py:648` `apply_market_scene_by_name` 内部调 `scenes.apply_market_scene`（返回 `{"message", "scene_id": custom-{uuid}}`），但未提取 scene_id 回传。
3. onboarding 读 `dev_scene` 绑定 Issue → 派发侧 `get_pipeline_config(scene_id, repo_key)` 仓内未命中回退全局模板行（无引擎）→ SRE `_sre_no_broadcast` 空 ids → None → 统一报「场景绑定的引擎全部离线」。

## 修复方案（沿链路逆向贯通）
| # | 模块 | 改动 |
|---|------|------|
| 1 | `pipeline_config.py` | `apply_market_scene_by_name` → `str \| None`：提取 `apply_market_scene` 返回的 scene_id；market 行缺失 / repo_key 无效 / HTTPException → None。新增 `scene_exists_in_repo(scene_id, repo_key)`（查 `devops_pipeline_scene` repo_key+scene_id+is_active） |
| 2 | `templates.py` | 抽 `_apply_template_scene(repo_key, tpl_scene_id, user_id) -> str`：真实 ID 非空才 `set_repo_scene`；失败 log.warning 保留默认场景 |
| 3 | `user/db.py` | `set_repo_scene` 写前 `scene_exists_in_repo` 校验，未命中 log.warning（不阻断，兼容历史直写路径） |
| 4 | `engine_select.py` | `get_scene_pool_engine_ids`：scene_id+repo_key 且仓内不存在 → log.warning；SRE 不回退全局行直接 None（非 SRE 维持 `""` 广播原行为）。新增 `scene_dispatch_failure_reason(...)`：区分 ①仓内不存在 ②未绑定引擎 ③引擎全部离线 |
| 5 | `event_pipeline.py` / `webhook.py` / `routes/events.py` | 报错文案改用 `scene_dispatch_failure_reason`（`_fail_scene_all_offline` 内 ids 为空 → 未绑定文案；webhook A2A error 字段与 events 重派 409 同步） |
| 6 | `scene_binding_repair.py`（新） | `repair_scene_bindings()`：扫描 `managed_repos.dev_scene`、`issue.scene_id`；值仓内不存在且 scene_market 有同名 official 模板 → 按 `market_scene_id` 取仓内副本（updated_at 最新一条）改绑；幂等可重跑；`main.py` lifespan 启动挂载（try/except 不阻断启动） |

## 兼容性决策
- `apply_market_scene_by_name` 返回 `str|None`：session_store.py:542 未消费返回值，truthiness 兼容。
- `set_repo_scene` 校验仅告警：避免误伤显式直写场景名的历史路径（派发侧另有防线）。
- weekly-09 语义对齐：`apply_market_scene` 每次生成 `custom-{uuid}`，本修复只回传该值，不改创建语义；模板建仓单次应用天然幂等。
- 存量改绑按 `market_scene_id` 取 `updated_at` 最新副本（多副本时取最近应用），改绑前 log 留痕。

## 测试 seam（公开接口）
`apply_market_scene_by_name` / `_apply_template_scene` / `set_repo_scene` / `get_scene_pool_engine_ids` / `scene_dispatch_failure_reason` / `repair_scene_bindings`——全部走真实 sqlite 测试库（tests/test_issue_524 风格）。

## TDD 切片
1. 红：`apply_market_scene_by_name` 返回真实 scene_id（模板写入真实 ID类测试）→ 绿：pipeline_config + templates 改造
2. 红：`set_repo_scene` 仓外 scene 告警但写入 → 绿：db.py 校验
3. 红：`scene_dispatch_failure_reason` / SRE 仓内未命中 None → 绿：engine_select + 报错文案接入
4. 红：`repair_scene_bindings` 改绑 dev_scene 与 issue.scene_id → 绿：repair 模块 + main.py 挂载
