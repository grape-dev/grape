# Issue #4182 Task

- [ ] ObservabilityView 删除 `/health/realtime` 与 `rtData`
- [ ] `fallbackOps()` 未知语义（tasks/runtimes/workers = null）
- [ ] overview 失败置 `opsFailedAt`，显示 `—` 与未更新提示
- [ ] 全前端确认无其它 `/health/realtime` 调用
- [ ] 新增回归测试（无 realtime 请求 / fallback unknown / 内网门禁保留）
- [ ] 前端缓存版本与 bundle 棘轮同步
- [ ] 前端构建 + lint + pytest 通过