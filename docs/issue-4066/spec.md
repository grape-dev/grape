# Spec: Issue #4066 — Autopilot custom_fields 写侧保留前缀校验 + 保留语义收窄 + 注入噪声收敛

> 基线：`arch-weekly-11-ljt` | 类型：缺陷修复（P3，配置静默失效 + 小时级日志噪声）

## 目标

1. **写侧即拒**：autopilot 任务 create/update 的 `custom_fields` 复用共享校验
   `scene_custom_env.custom_env_error()`，命中保留前缀返回 400 并回显全部冲突变量名，禁止落库。
2. **保留语义收窄**：`is_reserved_name()` 由「任何 `GRAPE_` 前缀一律保留」收窄为
   「`GRAPE_` 前缀且不在 `_PLATFORM_MANAGED_ENV` 白名单」，让定时任务确实需要的
   `GRAPE_ANALYSIS_MODE` / `GRAPE_ORG_ID` / `GRAPE_MANAGED_BY` 可注入生效。
3. **存量清理**：注册式迁移一次性清掉 `autopilot_tasks.custom_fields` 与
   `devops_pipeline_scene.advanced_options.custom_fields` 中非法（非大写 / 空名 / 仍保留）条目。
4. **日志收敛**：执行侧注入循环不再逐条 WARNING，改为单条 `log.info` 汇总
   （`injected=N skipped=M names=...`）。

## 成功标准（可验证）

| # | 判据 | 验证方式 |
|---|------|---------|
| 1 | create/update 带 `GRAPE_API_TOKEN` → 400，响应含冲突名，DB 无该行 | `tests/mgmt/test_issue_4066_*` |
| 2 | 白名单三项可注入并覆盖到 `env_extra` | `tests/runtime/test_issue_4066_*` |
| 3 | `GRAPE_SESSION_TOKEN` / `GRAPE_API_TOKEN` / `GRAPE_WORK_BRANCH` / `BOT_*` / `USER_*` 仍被拒 | 同上 + 既有 `test_issue_1811_*` 回归 |
| 4 | 存量库跑迁移后非法 custom_fields 清零，合法项与 `TEAM_FLAG` 等保留 | 迁移单测 |
| 5 | 一次 autopilot 注入只产生 1 条 info（多字段含非法项时不刷 WARNING） | caplog 计数断言 |
| 6 | 既有测试全绿 | `pytest tests/mgmt tests/runtime tests/api -q`（相关子集） |

## Scope

**In Scope**

- `src/kernel/scene_custom_env.py`：新增 `_PLATFORM_MANAGED_ENV`，收窄 `is_reserved_name()`。
- `src/domains/mgmt/routes/autopilot.py`：create/update 写侧 `custom_fields` 校验（400 汇总回显）。
- `src/domains/runtime/engine/scheduler.py`：注入循环日志收敛为单条 info 汇总。
- `src/domains/runtime/pipeline/event_pipeline.py`：场景 custom_fields 注入同样遵循「平台已注入的值不被自定义覆盖」（白名单变量的安全边界）。
- `src/domains/mgmt/migrations/autopilot_custom_fields_cleanup.py` + `src/api/db_setup.py` 注册：存量清理。

**Out of Scope**

- 不改 autopilot 调度模型、任务存储结构、org_analysis 的「不再写 GRAPE_* 标记」既有决策。
- 不改场景 custom_fields 的合法变量语义（仅同步清理被丢弃项）。
- 不新增对外 API。

## 技术方案

### 1. 白名单收窄（`scene_custom_env.py`）

```python
_PLATFORM_MANAGED_ENV = frozenset({
    "GRAPE_ANALYSIS_MODE",   # 分析模式（all/single）
    "GRAPE_ORG_ID",          # 组织上下文
    "GRAPE_MANAGED_BY",      # 托管归属
})

def is_reserved_name(name):
    return ((name.startswith("GRAPE_") and name not in _PLATFORM_MANAGED_ENV)
            or name.startswith("BOT_") or name.startswith("USER_")
            or name in _RESERVED_ENV_NAMES)
```

- 白名单**只放开这三个精确名**，不是「所有 `GRAPE_` 都放过」；
- `_RESERVED_ENV_NAMES` 里的 `GRAPE_SESSION_TOKEN` / `GRAPE_WORK_BRANCH` / `GRAPE_SERVER_URL` 等仍在保留路径（前缀命中且不白名单）；
- `BOT_` / `USER_` 凭据命名空间不变。

### 2. 写侧校验（`routes/autopilot.py`）

新增模块级 `_validate_custom_fields(fields)`：

- 对每条 `{name,...}` 调 `custom_env_error(name)`；非 dict 忽略；
- 收集全部失败项，一次性 400：
  `{"code": 400, "message": "custom_fields 校验失败，冲突/非法变量名: A, B；<逐条原因>", "invalid_names": [...]}`
- `create_task` 在落库前调用；`update_task` 仅当 `body.custom_fields is not None` 时调用（部分更新语义）。

### 3. 日志收敛（`scheduler.py`）

`_execute_autopilot_agent` 的注入段改为累计 `injected` / `skipped` 两个列表，循环结束输出**一条**：

```
log.info("Autopilot exec %s: custom env injected=%d skipped=%d names=%s skipped_names=%s", ...)
```

- 非法/保留项只进 `skipped_names`，不再逐条 `warning`；
- 解析失败仍保留一条 warning（真正的异常）；
- 白名单名可注入；`fname` 已在 `env_extra`（平台注入）时不覆盖，计入 skipped（平台优先）。

### 4. 存量清理（注册式迁移 `202611240001`）

- 版本号 `202611240001`（当前最大注册号 `202611230012`，未占用）；
- `kind="cleanup"`，`domains=("mgmt",)`，`fn` 幂等；
- 扫描 `autopilot_tasks.custom_fields`：删除 `not is_upper_name(name) or is_reserved_name(name)` 的条目，
  空数组写 `''`（与写侧 `custom_fields_json` 空值口径一致）；
- 扫描 `devops_pipeline_scene.advanced_options` JSON 的 `custom_fields`：同样剔除非法项，保留其余键；
- `post_check`：残留非法条目数 == 0 才盖章；
- `fn` 内失败抛出（executor 约定「抛错=不盖章，下次启动重试」）。

### 5. 场景注入平台优先（`event_pipeline._inject_scene_env`）

`GRAPE_ORG_ID` 等已由平台按事件/组织上下文注入，白名单放开后必须防「任务配置覆盖平台上下文」：
循环内 `if name in env_extra: continue`（平台值优先），与 scheduler 同一口径。

## 关键决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 保留前缀处理 | 白名单 vs 强制改名 `AUTOPILOT_*` | 白名单三精确名 | 兼容存量配置意图，改动面最小；改名会破坏既有用户配置 |
| 写侧报错粒度 | 首条即返 vs 全量汇总 | 全量汇总 + `invalid_names` | 一次暴露全部问题，前端可直接标红 |
| 存量清理 | 在线逐条改写 vs 注册式迁移 | 注册式迁移（cleanup + post_check） | 与仓库既有迁移机制一致，可判定是否收敛 |
| 平台 vs 自定义优先级 | 自定义覆盖平台 vs 平台先行 | 平台优先（已存在键不覆盖） | `GRAPE_ORG_ID` 属平台上下文，不允许任务侧改写 |
| 注入日志 | 逐条 WARNING vs 单条 info | 单条 info 汇总 | 降噪且保留 skipped 名字可观测 |

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 白名单过宽 → 敏感变量被放行 | 高 | 显式枚举三精确名 + 敏感名单回归用例（`GRAPE_API_TOKEN`/`GRAPE_SESSION_TOKEN`/`GRAPE_WORK_BRANCH`） |
| 存量迁移误删有效配置 | 中 | 仅删「非大写 / 空名 / 仍保留」条目，`TEAM_FLAG` 类保留项有断言 |
| 写侧 400 影响既有合法任务更新 | 中 | 仅 `custom_fields` 提交时校验；合法大写变量行为不变，回归覆盖 |
| 迁移历史遗留字段（202611201700 已删三项名） | 低 | 本次迁移只按「当前保留规则」剔除，不复活已删配置 |