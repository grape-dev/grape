# Issue #3749 任务拆解（Task）

> TDD：每个切片先红后绿，按提交可追溯。

## 切片 A：中继指标恢复 + 禁用态 503 打点

- [ ] A1（红）`tests/runtime/test_issue_3749_trace_relay_observability.py`：
  - `_relay_total` 非 None（复现重复注册导致指标整体失效）；
  - 禁用态两次 POST → registry 样本 `grape_trace_relay_requests_total{status="503"}` == 2；
  - 上游 200 转发 → 样本 `status="200"` +1。
- [ ] A2（绿）`src/domains/runtime/routes/runtime.py`：删尾部重复注册块（基线已删）；抽
  `_relay_count()`；禁用分支打 `status="503"`。

## 切片 B：启动自检

- [ ] B1（红）自检契约：空配置调用两次 → 仅一条 WARN（含 `OTLP_RELAY_TARGET`），返回
  `{"enabled": False, "upstream": "", "endpoint": ""}`；配置存在 → INFO 含 `<target>/v1/traces`；
  并断言 `main.py` / `grape_worker_server.py` 的 lifespan 接线存在。
- [ ] B2（绿）`trace_relay_selfcheck()` + 两个入口 lifespan 调用（`try/except` 不阻断启动）。

## 切片 C：部署清单与告警

- [ ] C1（红）部署契约测试：五个配置面含 `OTLP_RELAY_TARGET`，`deploy/alerts.yml` 含 503 告警规则。
- [ ] C2（绿）补齐 `.env.example`、`deploy/docker-compose.yml`、`deploy/systemd/grape-worker-server.service`、
  `scripts/grape-systemd-run.sh`、`start-worker.sh`、`deploy/alerts.yml`。

## 验收与交办

- [ ] 全量相关用例通过：`pytest tests/runtime/test_issue_3749_trace_relay_observability.py
  tests/runtime/test_issue_3699_trace_relay_noise.py tests/insight/test_pr1077_observability_endpoints.py`
- [ ] 自审查（5 轴 + 开源前置增量检查：黑名单拦截、敏感增量扫描）
- [ ] 建 PR（base=`arch-weekly-11-ljt`）→ Issue 评论 → A2A QA
- [ ] sre 承接：真机补 env、curl 中继、Tempo 抽查 `service.name=grape-worker` 根 span

## 复查（PR #1973 审查）补充

- [x] Required：`tests/runtime/test_issue_3749_trace_relay_observability.py` 与
  `test_issue_3749_trace_relay_deploy_config.py` 接入 `.github/workflows/windows-smoke.yml`
  的 `linux-backend` 门禁（独立 step；先例 #2133/#2245/#3167/#3490/#3699/#3758）。
- [~] Optional：中继观测实现抽到 `src/domains/runtime/otlp_relay.py` —— **rebase 后撤回**：
  新基线已把 `OTLP_RELAY_TARGET` 改为按请求现读 env（`_relay_target()`），抽出的模块会重新引入
  模块级 env 快照并使 `tests/runtime/test_trace_relay_env_and_metrics.py` 变红；禁用态 10 分钟
  限频状态也由请求路径与自检共用（基线用例 monkeypatch `runtime._relay_warn_ts`）。
  观测实现改为在 `routes/runtime.py` 中集中（`_relay_count()` + `trace_relay_selfcheck()`）。
- [x] Optional：自检新增 `/v1/traces` 后缀误配 WARN（拼成双后缀 → 上游 404 不命中 503 告警）。
- [x] Nit：自检文案不再硬编码 `/etc/grape/grape.env`，改为进程环境变量 / `.env` / systemd EnvironmentFile。
- [x] Nit：入口接线用例改 AST 断言（import + 实际调用 + try/except 包裹），注释提及不再能骗绿。

## rebase 到最新基线（2026-11-24）

- [x] `feat/issue-3749` rebase 到 `arch-weekly-11-ljt`（819f68ec2，PR 创建后基线前进 409 提交），
  冲突文件 `src/domains/runtime/routes/runtime.py` 按新语义重解：保留按请求读 env + 限频告警，
  叠加禁用态 503 打点与启动自检。
- [x] 基线已覆盖的修复不再重复提交：尾部重复 Counter 注册、`_relay_total` 静默降级。
- [x] 回归：`pytest tests/runtime/test_trace_relay_env_and_metrics.py
  tests/runtime/test_issue_3699_trace_relay_noise.py
  tests/runtime/test_issue_3749_trace_relay_observability.py
  tests/runtime/test_issue_3749_trace_relay_deploy_config.py` 全绿。
