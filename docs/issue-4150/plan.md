# Issue #4150 方案设计（Plan）

## 1. 测试 seam（编码前先定）

| 切片 | 公开 seam | 说明 |
|------|-----------|------|
| 断言 A（关联仓注入） | `_execute_agent_inner` → `stream_devops_agent(env_extra=...)` | 以替身依赖驱动**真实** `_execute_agent_inner`，捕获传给 executor 的 env；不走私有拼串 |
| 断言 B（失败 errors 落库） | `_execute_agent_inner` → `service.update_task_status(status="failed", errors=...)` | 同上，捕获终态写库调用参数（生产同一调用点） |
| AST 反模式 | 源码 AST（`src/**/*.py`） | 按函数自身作用域（不进入嵌套函数）判定「无别名局部 `import json` 且此前已用 `json.`」 |

选择理由：缺陷就发生在 `_execute_agent_inner` 内联代码块，最小可信 seam 是该函数与
「executor env」「任务终态写库」两个既有边界；无需为测试重构生产代码（保持 -1 行改动面）。

## 2. 实现

```python
# src/domains/runtime/engine/direct_executor.py  L975-977（修改前）
                        from src.kernel.session_manager import db, db_lock
                        import json          # ← 删除（模块级 L14 已有）
                        with db_lock:
```

- 仅删除该行；函数内 L523 `import json as _json_lr_dr`、L772 `import json as _json`
  带别名不遮蔽，保留。
- 删除后 L408 / L927 的 `json.xxx` 解析回模块级 `json`，两处 `UnboundLocalError` 消失；
  `except`/`log.warning` 外壳保留（仍保护其它解析异常，修 bug 不扩大面）。

## 3. 回归测试设计（`tests/runtime/test_issue_4150_local_json_shadow.py`）

- `TestNoLocalJsonShadow`：函数级「无局部 import json」+ 模块级 `json` 可解析 + `src/` 全量 AST 扫描清零。
- `TestLinkedReposEnvInjection`：
  - `_read_scene_linked_repos` 替身返回 1 个关联仓 → 断言 env 含 `GRAPE_LINKED_REPOS` 且
    `json.loads(env[...]) == 关联仓列表`；断言 `_inject_linked_repo_tokens(env, 同一 JSON)` 被调用；
  - 无关联仓 → 不注入该变量（不误注入空串）。
- `TestFailedErrorsPersist`：
  - done 事件 `exit_code=1, errors=["boom..."]`、`task_id` 非空 → 断言 `update_task_status`
    收到 `status="failed"` 且 `errors` 非 None、JSON 解码含 boom；
  - 既有 `_task_errors_json` 记录（`TRANSIENT_RETRY`）→ 断言落库内容为「既有 + 新增」两条（合并不覆盖）。

依赖替身经 `sys.modules` + 父包属性双写（`from pkg import sub` 读父包属性），
DB 用「无行」假库（本用例只校验写库参数），`db_lock` 用 `threading.RLock`。

## 4. 红/绿证据

修改前（仅提交测试）：6 failed / 2 passed —— env 缺 `GRAPE_LINKED_REPOS`、
`_inject_linked_repo_tokens` 未调用、`errors=None`（KeyError）与 AST 命中 L978。
删除局部 import 后：8 passed。

## 5. 回归面

- `tests/runtime`、`tests/api/test_result_post_policy.py`、`tests/kernel/test_token_injection_r20.py`、
  `tests/runtime/test_issue_{596,528,699,2514,1660,482}*` 与 `origin/arch-weekly-11-ljt` 基线逐条比对失败集：
  **一致，无新增失败**（基线存量失败与本改动无关）。
- `make lint`（import OK + F821 ratchet 0/1 + async-blocking 253/273）通过。