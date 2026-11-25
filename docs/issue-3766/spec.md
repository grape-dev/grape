# Spec: Issue #3766 — 注入的 GRAPE_SERVER_URL 指向 admin 前端导致业务 API 404

> 目标分支：weekly-mys（以 `$GRAPE_WORK_BRANCH` 为准）

## Objective
- 修正任务环境注入的 `GRAPE_SERVER_URL`：注入前校验其确实命中业务 API（`/api/issues`、`/api/git-repo` 等），不再把 admin 前端（nginx → grape-admin 8081）地址注入给 Agent。
- 价值：消除 issue/coding/qa 同族任务的 `/api/issues` 301→404 与手动 `--base-url` 自救，消除 P1 级全链路阻塞。
- 用户：所有 grape Agent 任务环境及其技能（issue-cli / coding-cli / qa-cli 等）。
- 成功标准：以注入地址直接执行 `grape issues detail` / `grape issues comments list` 返回 2xx JSON；注入链路对错误地址可自动回退或显式阻断，并留下可审计告警。

## Scope
### In Scope
- `src/kernel/prompt_builder.py` 业务基址解析：显式配置与远程 runtime 一并纳入业务 API 可达性校验。
- 新增 `/api/issues/mine` 端点探测：识别 admin 前端的 301→404 接管签名，作为 health/openapi 之后的兜底判据（裸 `/api/issues` 未注册，不能作为探测端点）。
- 注入取值修正：不可用候选按候选链回退到可用业务基址（`task_business_api_url` → 本机业务端口 → `server_base_url` → 显式 `worker_connect_url`）。
- 注入配置守护：无可用候选时 `error` 告警；`strict` 模式阻断注入；`off` 保留旧行为。
- `configs/server-config.example.json` / `help/usage/agent-environment-variables.md` 配置说明补充。

### Out of Scope
- grape-admin 前端自身改造。
- 技能侧 `--base-url` 自救逻辑增强（grape-skills 仓库）。
- 历史同因反馈单（仓库与编号已脱敏）逐单处理。
- 生产 nginx 配置的实际下发（本仓库只维护 `deploy/nginx-grape*.conf` 模板与代码侧守护）。

## Technical Approach
1. 新增状态码感知探测原语 `_http_json_status`，区分「业务 API 鉴权挑战（401/403 JSON）」与「前端接管（301/302/404/HTML）」。
2. 新增 `_probe_business_issues`：`/api/issues/mine`（业务 router 真实注册路由）返回 2xx JSON 或 401/403 鉴权结构视为业务 API；301→404 等判为不可用；作为 `_probe_business_api` 的第三级兜底。
3. `_resolve_business_base` 改为候选链 + 探测：`warn`（默认）返回首个通过者，全部失败回退运维声明值并 `error` 告警；`strict` 抛 `BusinessApiGuardError` 阻断；`off` 保留 Issue #1731 旧语义。
4. 远程 runtime 不加入本机回环候选，避免把 `127.0.0.1` 注入远端任务。
5. `get_url_split_warning` 增加 `task_business_api_url` 回环告警，便于运维修正注入配置。

### 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 校验范围 | A. 仅本机；B. 仅显式；C. 所有 runtime | C | #3766 证明显式/远程同样可能被网关转到 admin |
| 探测判据 | A. 仅 health；B. health+openapi；C. 再加 `/api/issues/mine` | C | 直接覆盖 Skill 实际调用端点与 301→404 签名 |
| 失败处理 | A. 静默使用；B. 回退+告警；C. 直接阻断 | B 默认 + C 可选 | 默认保连通性并显式告警，高风险环境可 `strict` 阻断 |

## Acceptance Criteria
- [ ] 注入地址指向 admin 前端（`/api/issues` 301→404）时，不再被采用；自动回退到可用业务基址并记录 `error` 告警。
- [ ] 远程 runtime 候选链不含本机回环地址。
- [ ] `GRAPE_BUSINESS_API_GUARD=strict` 在无可用候选时阻断注入；`off` 保持 #1731 旧行为。
- [ ] `/api/issues/mine` 301/302/404/HTML 判为不可用，2xx JSON 与 401/403 鉴权挑战判为可用。
- [ ] `task_business_api_url` 指向回环时 `get_url_split_warning` 给出告警。
- [ ] 新增测试覆盖以上路径，相关现有测试通过。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 注入地址校验影响全部 Agent 连通性 | High | 默认 `warn` 失败回退不阻断；`off` 可一键回滚；`strict` 仅显式启用 |
| 服务端到公网地址 NAT 回环不通导致探测误判 | Med | 探测失败回退到运维声明值且不改写；不因此丢失任务；告警提示核对网关 |
| 探测引入派发延迟 | Med | 沿用 60s 进程内缓存与 1s 总超时；同一基址多次派发不重复探测 |