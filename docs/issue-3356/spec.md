# Spec: Issue #3356 — /api/repos/member 缓存过期同步回源导致首屏偶发 3.5~5s

> Coding Agent | 基于 Issue Agent 已发布 Spec（评论 #已脱敏）执行 | 目标分支: weekly-mys | 开发分支: feat/issue-3356

## Objective
将 `/api/repos/member` 的缓存策略由「过期即同步全量回源」改为 **stale-while-revalidate**（先返旧缓存 + 后台异步刷新），并将多 provider 回源由串行改为**线程池并行**。

## Scope

### In Scope
- `src/domains/mgmt/routes/repos.py`
  - 缓存过期 / `refresh=1`：立即返回旧缓存，后台异步刷新；进程内 singleflight 防重复回源。
  - 多 provider 回源由 `for p in _providers` 串行改为 `ThreadPoolExecutor` 并行。
  - 首次无缓存：同步并行回源一次后返回。
  - 后台刷新失败：保留旧缓存并记 warning，等待下次触发重试。
- 新增测试覆盖缓存命中 / 过期回源 / `refresh=1` / 首次回源 / 后台失败降级 / singleflight。

### Out of Scope
- 前端本地缓存/上次响应先渲染（P1）。
- TTL 调大 / webhook 事件精准失效（P2）。
- 数据库与其他接口优化（已实测排除）。
- `repo_manager.py` 内部 `/user` + `/user/repos` 分页逻辑（作为并行任务单元被调用即可，无需改动）。

## Technical Approach
- 缓存读取：未过期直接返回；过期或 `refresh=1` 返回旧值并提交后台线程刷新。
- 后台刷新：`ThreadPoolExecutor` 并行调用各 provider 的 `with_oauth_token(user, p, _fetch)`，聚合并原子写回缓存。
- singleflight：进程内以 `(user_id, providers)` 为 key 维护刷新中 Event，重复请求只返回旧缓存不重复回源。
- 失败处理：后台刷新仅全部平台失败（含整轮超时）或 401 无结果时保留旧缓存并记日志、不写库；单个平台非 401 硬错误记为失败平台，健康平台结果照常合并落库。

## Acceptance Criteria
- [ ] 缓存过期后的首个请求返回旧缓存（用户侧 ~10ms 量级），后台完成刷新并写回。
- [ ] `refresh=1` 不再产生 3.5~5s 同步等待。
- [ ] 多 provider 回源并行化，回源总耗时 ≈ 最慢单平台耗时。
- [ ] 刷新期间接口正常返回仓库列表（非错误、非空列表）；刷新失败保留旧值并记日志。
- [ ] 首次无缓存场景不劣于现状（并行回源）。
- [ ] 新增测试覆盖缓存命中 / 过期回源 / `refresh=1` 三条路径；现有相关测试通过。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| 旧缓存短暂陈旧（≤ 刷新时长） | Low | 业务可接受 30 分钟级 TTL 陈旧，保留 `refresh=1` 语义 |
| 后台线程异常/泄漏 | Med | daemon 线程 + 全局异常捕获 + 失败保留旧缓存记日志 |
| 并发重复回源 | Med | 进程内 singleflight（刷新中 Event） |
| 并行下 VCS 限流 | Low | 并行度 = provider 数（上限 8），不改分页逻辑 |