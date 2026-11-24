# Issue #4082 需求文档（Spec）

## 1. 现象与证据

- 生产 Loki 近 1 小时：`GET /api/runtime/runtimes/0/tags` **203 次、100% 401**，来自 **15 台主机**；
  UA 为 `curl/7.79.1`，upstream=8083（worker runtime 进程），属 worker 自带探活脚本。
- 伴随同类 401：`GET /api/tasks/current` 50 次、`POST /api/runtime/register` 24 次；
  另有 12 条 `GET /api/runtime/runtimes/13445/tags` 401（已撤权 runtime 的同类探活）。
- 平台侧表现：这些 runtime「在线但不具备任何标签」，引擎 `role_tags/scene_tags` 过滤只能靠兜底；
  探活无终止条件、无退避、无 ERROR 级告警（仅 nginx warn + worker 一条被吞的 warning）。
- 归因：worker 侧 `runtime_id` 缺失/非法时 URL 模板拼成 `/runtimes//tags` 归一为 `0`，
  且注册回包未强制回填真实 id——**注册态缺失被静默降级成 `runtime_id=0` 探活**，
  与鉴权无关（同一 token 对 8083 其它端点可用）。与 #3426（服务端选型侧 `id=0` 判死）方向相反，
  本条只做客户端探活守卫 + 服务端参数校验。

## 2. 目标

消除 worker 以 `runtime_id=0` 探活 `/api/runtime/runtimes/{id}/tags` 恒 401 的静默失败，
让「已注册但无标签 / 未注册」成为**显式可观测**状态，而不是长期 401 噪声与空引擎标签。

## 3. 范围

### In Scope

1. worker 探活/心跳：`runtime_id` 缺失、为 0、负数、非数字时**不发请求**，
   落结构化错误 `RUNTIME_ID_MISSING`（含 host/场景/注册态），并按退避重试注册。
2. 注册回包强制回填真实 id：非法 id（含 0）不再作为 `runtime_ids` 兜底，注册视为失败 → 退避重试。
3. 服务端 `/api/runtime/runtimes/{runtime_id}/*` 非法 id（`0`/`abc`/负数/非正整数）统一返回
   400 `invalid runtime_id`（不再落到 401/422），且不进入鉴权分支。
4. 新增 `grape_runtime_tags_probe_failure_total{host,reason}` 指标（reason=id_missing /
   unauthorized / not_found / no_tags），并在 `deploy/alerts.yml` 增加连续失败告警。
5. 存量 15 台主机补回真实 `GRAPE_RUNTIME_ID`（运维执行，本条仅提供排查/核对入口）。

### Out of Scope

- 不改鉴权模型 / token 校验逻辑（本条非权限问题）。
- 不处理 #3426 服务端选型侧 `runtime_id=0 判死`。
- 不重构 registry 存储结构。

## 4. 验收标准

- [ ] `runtime_id` 缺失/非法时 worker 不再发出 `/runtimes/0/tags` 请求，
      输出结构化 `RUNTIME_ID_MISSING` 并退避重试注册。
- [ ] 注册回包必须回填真实 id，代码中不存在以 0 兜底的路径。
- [ ] `/runtimes/0/tags`、`/runtimes/abc/tags` 返回 400 `invalid runtime_id`（非 401），原因可读。
- [ ] 探活失败有按 host 的 `grape_runtime_tags_probe_failure_total{reason}` 与连续失败告警，
      可区分未注册 / 无标签 / 无权限。
- [ ] 存量 15 台主机补回真实 runtime id 后，指标与 401 日志归零（sre 执行验证）。
- [ ] 新增测试覆盖：id 缺失不发请求、服务端 400、标签成功读取三条路径；现有测试全通过。