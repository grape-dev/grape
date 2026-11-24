# Issue #4150 需求文档（Spec）

## 1. 目标

修复 `src/domains/runtime/engine/direct_executor.py` 中 `_execute_agent_inner` 函数体内
局部 `import json`（原 L978，无别名）对模块级 `json`（L14）的遮蔽，消除其之前两处
`json.xxx` 必抛的 `UnboundLocalError`。

## 2. 现状（缺陷机理）

Python 编译期见到函数体内任意位置的 `import json`，即把 `json` 判为该函数**局部名**；
因此在 import 语句**之前**的 `json.xxx` 取到的局部名尚未绑定 → `UnboundLocalError:
cannot access local variable 'json' where it is not associated with a value`。本函数内两处：

| 位置 | 代码 | 后果 |
|------|------|------|
| L408 | `env_extra["GRAPE_LINKED_REPOS"] = json.dumps(_linked_repos, ...)` | 被外层 `except Exception: pass` 静默吞掉 → direct 路径**永不注入** `GRAPE_LINKED_REPOS`；紧随其后的 `_inject_linked_repo_tokens`（#466 关联仓 token 分发）一并跳过且**无日志**（#242 相关） |
| L927 | `_fail_errors_json = json.dumps(_merged)` | 仅留 WARNING `errors merge for seq=... failed` → failed 任务的 `agent_tasks.errors` 不落库，#494 失败归因双写失效 |

生产日志近 24h 出现 8 次 `Exec agent: errors merge for seq=... failed: cannot access
local variable 'json' where it is not associated with a value`，与上述机理吻合。

## 3. 期望行为

- 删除函数内多余的局部 `import json`，函数内所有 `json.xxx` 解析回模块级 `import json`；
- direct 路径恢复注入 `GRAPE_LINKED_REPOS`（含关联仓 token 分发）；
- `new_status=failed` 且 done 事件带 errors 时，`agent_tasks.errors` 正常落库（合并而非覆盖）。

## 4. 范围

### In Scope
- `src/domains/runtime/engine/direct_executor.py`：删除局部 `import json`（-1 行）。
- 回归测试 ×3 组：
  - AST 扫描：`src/` 无「函数内无别名局部 `import json` 且此前已用 json」反模式；
  - 断言 A：含 linked_repos 的 direct 任务 env 必含 `GRAPE_LINKED_REPOS`；
  - 断言 B：`new_status=failed` 且 done.errors 非空 → `update_task_status` 带非 None `errors`。

### Out of Scope
- 同文件 L523 `import json as _json_lr_dr` / L772 `import json as _json`：带别名，不遮蔽，不动。
- `_read_scene_linked_repos` / `_inject_linked_repo_tokens` / `update_task_status` 自身行为改造。
- worker（非 direct）路径的 errors 落库链路；#637 界面侧失败摘要展示。
- 生产日志计数归零验证（`{service_name="grape-worker"} |= "errors merge for seq="`）由 sre/发布窗口执行。

## 5. 验收标准

- [x] 局部 `import json` 已删除；`src/` AST 扫描反模式清零。
- [x] 含 linked_repos 的 direct 任务 env 含 `GRAPE_LINKED_REPOS`（内容为关联仓列表 JSON）。
- [x] `new_status=failed` 且 done.errors 非空时 `update_task_status` 收到非 None `errors`，且保留既有诊断记录（合并）。
- [x] 相关测试通过（`tests/runtime` 等既有失败与基线一致，未新增）。
- [ ] 部署后生产日志 `errors merge for seq=` 计数为 0（sre/发布窗口）。

## 6. 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 注入恢复后唤醒此前被静默跳过的多仓路径存量问题 | Med | 注入路径本有 `log.warning` 兜底；灰度观察 direct 多仓任务日志，异常按 #242/#466 既有链路处理 |
| `_post_result_to_issue` 失败分支的 `RESULT_POST_FAILED` 追加首次真正可达 | Low | 该分支逻辑独立完整（读-合并-截断-写回），回归用例覆盖失败归因链路 |