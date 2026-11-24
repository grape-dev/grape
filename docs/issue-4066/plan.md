# Plan: Issue #4066 — 写侧校验 / 白名单收窄 / 日志收敛 / 存量清理

> 基线：`arch-weekly-11-ljt`。改动点 5 处，测试 4 个文件。

## 1. `src/kernel/scene_custom_env.py`（先改，被下游依赖）

| 符号 | 改动 |
|------|------|
| `_PLATFORM_MANAGED_ENV` | 新增 `frozenset({"GRAPE_ANALYSIS_MODE","GRAPE_ORG_ID","GRAPE_MANAGED_BY"})`，docstring 说明「平台前缀下允许自定义注入的白名单」 |
| `is_reserved_name()` | `name.startswith("GRAPE_")` → `name.startswith("GRAPE_") and name not in _PLATFORM_MANAGED_ENV` |
| `custom_env_error()` | 不变（复用 `is_reserved_name`，白名单自动生效） |

模块 docstring 规则 2 同步改为「`GRAPE_*` 前缀（白名单除外）」。

## 2. `src/domains/mgmt/routes/autopilot.py`

新增模块级 helper（放在 `_row_to_task` 之前，便于单测直接 import）：

```python
def _validate_custom_fields(fields) -> None:
    """写侧校验 custom_fields（Issue #4066）：非法/保留名一次性 400 汇总回显。"""
```

- 逐条 `custom_env_error`，收集 `(name, err)`；
- 任一失败 → `HTTPException(400, {"code":400, "message": "...冲突/非法变量名: A, B；<原因>…", "invalid_names":[...]})`；
- `create_task`：校验调用放在 `check_repo_access` 之后、`custom_fields_json = json.dumps(...)` 之前；
- `update_task`：`if body.custom_fields is not None: _validate_custom_fields(body.custom_fields)`，放在 `updates = body.model_dump(...)` 之前。

读侧 `_row_to_task` 的保留过滤保持不变（现在白名单三项不再被过滤，属预期）。

## 3. `src/domains/runtime/engine/scheduler.py`

`_execute_autopilot_agent` 内 custom_fields 注入段（现 558-571 行）：

```python
_task_custom_fields = task.get("custom_fields") or ""
if _task_custom_fields:
    _injected, _skipped = [], []
    try:
        _cf_list = json.loads(...)
        from src.kernel.scene_custom_env import is_upper_name, is_reserved_name
        for _f in _cf_list or []:
            _fname = (_f.get("name") or "").strip() if isinstance(_f, dict) else ""
            if (not _fname or not is_upper_name(_fname) or is_reserved_name(_fname)
                    or _fname in env_extra):
                _skipped.append(_fname); continue
            env_extra[_fname] = str(_f.get("value", "")); _injected.append(_fname)
    except Exception as _e:
        log.warning(...)
    log.info(f"Autopilot exec {exec_id}: custom env injected={len(_injected)} "
             f"skipped={len(_skipped)} names={','.join(_injected)} "
             f"skipped_names={','.join(_skipped)}")
```

- `_fname in env_extra` 即「平台已注入优先」（`REPO_FULL_NAME`/`ACCOUNT` 等本就在 env，属保留名，双保险）；
- 一次执行**一条 info**，无论是否含 skipped。

## 4. `src/domains/runtime/pipeline/event_pipeline.py`

`_inject_scene_env` 场景 custom_fields 循环：`if name in env_extra: continue`（平台上下文优先），
避免白名单 `GRAPE_ORG_ID` 覆盖平台按事件注入的组织上下文。

## 5. 注册式迁移 `202611240001`

新文件 `src/domains/mgmt/migrations/autopilot_custom_fields_cleanup.py`：

| 符号 | 语义 |
|------|------|
| `cleanup_invalid_custom_fields()` | 清理 `autopilot_tasks.custom_fields` + `devops_pipeline_scene.advanced_options.custom_fields` |
| `verify_custom_fields_clean()` | `post_check`：残留非法条目数 == 0（表缺失视为通过） |
| `MIGRATION` | `version=202611240001`, `kind="cleanup"`, `domains=("mgmt",)` |

`src/api/db_setup.py` 末尾追加登记（字面量 version/name/kind，供 `test_migration_track_guard.py` 解析）：

```python
from src.domains.mgmt.migrations.autopilot_custom_fields_cleanup import (
    cleanup_invalid_custom_fields as _fn_autopilot_cf_cleanup,
    verify_custom_fields_clean as _post_autopilot_cf_cleanup,
)
register_migration(version="202611240001", name="autopilot-custom-fields-cleanup",
                   kind="cleanup", fn=_fn_autopilot_cf_cleanup,
                   domains=("mgmt",), post_check=_post_autopilot_cf_cleanup)
```

幂等：每轮只按当前规则过滤；无非法项时不做 UPDATE。

## 测试（TDD：先红后绿）

| 文件 | 覆盖 |
|------|------|
| `tests/kernel/test_issue_4066_platform_managed_env.py` | 白名单三项非保留；`GRAPE_*` 其余仍保留；`BOT_`/`USER_`/显式集合不变；`custom_env_error` 口径 |
| `tests/mgmt/test_issue_4066_autopilot_custom_fields.py` | create/update 保留名 400 + `invalid_names` 回显 + 未落库；白名单名可保存；`TEAM_FLAG` 正常；update 不带 custom_fields 不受影响 |
| `tests/runtime/test_issue_4066_autopilot_env_inject_log.py` | 单条 info 汇总（injected/skipped/names）；白名单三项进入 `env_extra`；敏感名被跳过且无逐条 WARNING；平台已注入键不被覆盖 |
| `tests/mgmt/test_issue_4066_custom_fields_cleanup.py` | autopilot 行清理、scene advanced_options 清理、保留项不动、幂等两跑、post_check |

## 测试 seam 声明

- 写侧：`ap.create_task(body, user)` / `ap.update_task(task_id, body, user)`（既有测试同款直接调用 + monkeypatch `db`）；
- 执行侧：`scheduler._execute_autopilot_agent`（复用 `test_issue_1811_*` 的依赖 patching + 捕获 `env_extra`），日志用 `caplog`；
- 迁移：`cleanup_invalid_custom_fields()` 直调（既有 `_fn_strip_org_analysis_reserved_fields` 测试同款）。

## 执行顺序

1. 白名单（kernel）→ 2. 写侧校验（mgmt route）→ 3. 日志收敛（runtime）→ 4. event_pipeline 平台优先 →
5. 迁移 + 注册 → 6. 测试全绿 → 7. 自审查/pre-pr → 8. PR。