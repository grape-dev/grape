# Spec: Issue #2479 — GSB remote/lib 按接口方法并存路由（默认偏好 remote）

> 需求文档（依据 Issue #2479 及其 Spec 评论固化）| 开发基线分支：`arch-weekly-11-ljt`

## Objective

把 GSB provider 从 `GRAPE_PROVIDER_MODE` 全局一刀切，改为「按接口方法/原语粒度
remote/lib 并存路由 + 默认偏好 remote + 禁止矩阵兜底」，在不引入可测性能退化的前提下
让跨域调用优先走 remote，并满足三库三机独立部署目标态。

## 需求要点（In Scope）

1. **按方法路由**：路由决策按两级 key——契约 `service.method`（23 个：mgmt 7 /
   runtime 9 / insight 7）与 public 原语 `domain.primitive`（mgmt 41 个可调用、
   runtime 68 个可调用，其中数据原语 39 + 66，探活口 `available`/`health` 不参与路由）。
2. **DB 路由配置表**（复用服务注册表同库）：`route_key`、`mode(remote/lib)`、
   `timeout_ms`、`version`、`updated_at`、`updated_by`；进程内按 version+TTL 缓存，
   改表 ≤30s 不重启生效；配置表读取失败退回上一次已知配置。
3. **内置基线**（配置表缺行时）：热路径四项
   `mgmt.verify_session_token` / `insight.log_obs` / `insight.log_api_latency` /
   `insight.log_llm_usage` 锁 lib；public 原语本期无 remote 传输通道，基线 lib；
   其余契约方法默认 remote。
4. **禁止矩阵**：`runtime|insight → mgmt` 全部 lib（含 mgmt 7 个契约方法 + mgmt
   public 全部原语）；运行时守卫（runtime/insight 进程内 `get_service("mgmt")`
   恒 Local，不依赖配置表）+ CI 静态扫描双守卫。`mgmt → runtime|insight` 允许 remote。
5. **remote 超时 fail-fast**：明确错误码（`GSB_REMOTE_TIMEOUT` 等）、不回落 lib；
   metrics status 标签区分「remote 超时 / 不可达」与「业务错误」。
6. **性能判据**：逐接口 lib/remote P95 增量表（同进程、同参数、同数据量，各采样
   ≥200 次）；remote 候选须同时满足 P95 增量 ≤10ms 且相对增幅 ≤20%。
7. **回滚**：配置表一键全量 lib 后行为与「未开启 remote」一致，无需改代码。

## Out of Scope

- 不改 `GRAPE_DOMAIN_LIB` / `GRAPE_DSN_*` 连接层语义。
- 不改跨域写「目标域 public 原语 + 幂等意图 + 排水补偿」三原则，不拆组合写。
- 不为 public 原语新增 remote 传输通道（本期原语 remote 化仅到路由 key 登记，
  实际传输待后续 Issue；报告中标注 lib-locked）。
- 不新增服务发现/传输层基础设施（沿用既有 GsbClient 与 DB 注册表）。

## 全局开关语义（向后兼容口径）

`GRAPE_PROVIDER_MODE` 保持为部署级总开关：

- 未设置（或非 `remote`）→ 一律 Local（存量单机/共库行为，零回归）；
- `= remote` → 进入按方法路由：矩阵守卫 → 配置表 → 内置基线（默认偏好 remote）。

该口径下「配置表一键全量 lib」即等价回滚：即使 `GRAPE_PROVIDER_MODE=remote`，
全部 key 配置为 lib 后行为与改造前未开 remote 时一致。

## 验收标准（对照 Spec）

- [ ] 配置表 DDL + 读写接口落地，缺行按内置基线取值，改表 ≤30s 自动生效（无需重启）。
- [ ] 逐接口压测报告：23 契约方法 + public 原语全量 lib/remote P95 增量表；
      remote 候选满足 P95 增量 ≤10ms 且相对增幅 ≤20%。
- [ ] 热路径四项实测锁定 lib，有数据支撑。
- [ ] 禁止矩阵守卫：runtime/insight 进程内 `get_service("mgmt")` 恒 Local；
      CI 静态扫描可红（含 public 原语清单归属标注）。
- [ ] remote 超时 fail-fast：明确错误码、不回落 lib，metrics 可区分超时与业务错误。
- [ ] 回滚：配置表一键全量 lib，行为与改造前一致（无需改代码）。
- [ ] 现有测试通过，新增路由/热切换/矩阵/fail-fast/回滚测试覆盖。
