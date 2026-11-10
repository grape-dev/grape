# Issue #1841 — 实现方案

## 核心设计

统一入口放在 `src/runtime/repogit.py`（`_read_scene_work_branch` 附近），三条注入路径与保存校验、存量脚本复用同一套分支列表查询/缓存逻辑：

- `list_repo_branches(repo_key)`：优先本地 bare mirror 的 `refs/remotes/origin` 分支列表（无网络开销），无镜像再穿透 VCS `/branches` API；结果按仓库缓存（成功 90s、失败 30s）。仅使用「分支列表显式比对」，不依赖 GitCode ref/tree 查询（ref 不存在会静默回退默认分支）。默认分支由 mirror 的 `origin/HEAD`（`ensure_repo_base` 刷新时记录）或 VCS 仓库信息 API 提供；分页上限 50 页，超限按「不可核实」处理，避免截断误判。
- `resolve_work_branch(repo_key, configured)`：有效分支原样返回；失效则回退仓库默认分支（`default_branch` → `main`/`master` → 原配置）并输出 WARNING（原配置值 + 仓库 + 生效分支）。分支列表不可核实（无 token/网络失败）时保持原配置，不误判。
- `validate_work_branch_exists(repo_key, branch)`：保存前校验，失效返回可直接展示的明确错误与建议默认分支；不可核实时不阻塞保存。
- `read_effective_scene_work_branch(repo_key, issue_id)`：场景读取 + 校验统一入口。
- `refresh_repo_branch_mirror(repo_key, branch)` + `invalidate_branch_list_cache(repo_key)`：建分支成功后增量刷新本地 mirror 并失效缓存，下一次查询强制走远端，避免刚建分支被判不存在。

## 注入路径

| 路径 | 改动 |
|------|------|
| `src/runtime/scheduler.py` autopilot | `task.work_branch` 经 `resolve_work_branch` 后注入 `env_extra`；autopilot prompt 标注生效分支/回退原因 |
| `src/api/event_pipeline.py` 正常入队 | `read_effective_scene_work_branch` 替代直接读配置 |
| `src/api/event_pipeline.py` `build_task_env`（重派） | 同上 |
| `src/runtime/direct_executor.py` direct 模式 | 同上，保持与 worker 模式一致 |
| `src/prompt_builder.py` | 场景高级选项指引中追加「实际生效分支」及回退原因 |
| `src/api/routes/autopilot.py` | 定时任务创建/更新校验 `work_branch` 存在性 |

## 配置保存校验

`src/api/routes/scenes.py` `PUT /scenes/{scene_id}/advanced-options` 在非空校验后调用 `validate_work_branch_exists`，失效返回 400（`field=work_branch`），前端既有 `.catch` toast 展示 `detail.message`；场景市场应用流程失败提示附带错误明细。`src/api/routes/autopilot.py` 的定时任务创建/更新同样校验 `work_branch`（清空值视为关闭开关，跳过校验）。

## 存量校正

新增 `scripts/audit_scene_work_branch.py`：

- 默认 dry-run，扫描 active 场景的 `advanced_options.work_branch`，输出 INVALID / UNKNOWN 清单（仓库、场景、原值、建议默认分支）。
- `--apply` 将失效值改写为默认分支并保留其它字段；幂等；`--repo` / `--limit` / `--json` 支持运维筛选与结构化输出。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 注入链路新增分支查询开销 | 本地 mirror 优先 + 按仓库缓存 + 失败短 TTL |
| 无 token/离线导致误回退 | 分支列表不可核实时保持原配置，仅告警 |
| ref 查询静默回退干扰判断 | 只比对分支列表，不依赖 tree/contents/ref 结果 |
| 与 worktree `_default_base_branch` 逻辑漂移 | 不改其既有 `show-ref` 兜底，仅作为第二道防线 |