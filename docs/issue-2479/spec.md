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
   `insight.log_llm_usage` 锁 lib；**压测超阈值 14 项基线锁 lib**（QA 审查
   Required#1 裁决，见下「性能判据裁决」——空表新部署即与 bench-report
   落地结论一致，remote 由配置表显式放开）；public 原语本期无 remote 传输
   通道，基线 lib；其余契约方法（压测采样错误 5 项，无数据可裁决）默认 remote。
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

- [x] 配置表 DDL + 读写接口落地，缺行按内置基线取值，改表 ≤30s 自动生效（无需重启）
      （tests/kernel/test_gsb_route.py::test_hot_switch_within_ttl 实测 0.05s TTL 生效）
- [x] 逐接口压测报告：23 契约方法 + public 原语全量 lib/remote P95 增量表；
      remote 候选满足 P95 增量 ≤10ms 且相对增幅 ≤20%。
      **结果与裁决（QA 审查 Required#1）**：实测 remote 候选达标 0 项——本拓扑
      （loopback HTTP + sqlite 共库）lib 基线多在亚毫秒级，remote 固定传输成本
      约 1.3~4.7ms，绝对增量虽全部 ≤10ms，但相对增幅 >20% 全数不达标；
      按「不影响性能」前提，超阈值 14 项内置基线锁 lib（remote 由配置表
      gsb_provider_route 显式放开），采样错误 5 项（存量 schema 漂移，无数据）
      维持默认 remote。判定与代码事实一致（bench-report 判定列 = provider 基线）。
- [x] 热路径四项实测锁定 lib，有数据支撑（bench-report §1 lib-locked 行）。
- [x] 禁止矩阵守卫：runtime/insight 进程内 `get_service("mgmt")` 恒 Local；
      CI 静态扫描可红（含 public 原语清单归属标注）。
      （QA Required#4 修复：静态扫描 + 路由用例已接入 windows-smoke.yml linux-backend
      CI step，不再只挂 scripts/check-all.sh）
- [x] remote 超时 fail-fast：明确错误码、不回落 lib，metrics 可区分超时与业务错误。
      （QA Required#3 修复：per-method 超时改为 per-request 下发
      （`GsbClient.with_timeout` 线程本地覆盖 + `ServiceHttpClient.with_timeout`
      公开透传），无 per-method 粘滞，不再改共享客户端超时）
- [x] 回滚：配置表一键全量 lib，行为与改造前一致（无需改代码）
      （test_rollback_all_lib_matches_legacy_behavior）。
- [x] 现有测试通过，新增路由/热切换/矩阵/fail-fast/回滚测试覆盖
      （26 路由用例 + 2 存量口径更新；QA Required#2 修复：get_service 进程级
      代理句柄复用，per-call 构造成本消除，A/B 数据见 bench-report §3）。
