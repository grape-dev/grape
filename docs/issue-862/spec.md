# Spec: Issue #862 — 场景市场体验 domain-swarm 偶发 504 修复

> 基准分支：`weekly-mys` | 上游 Spec：Issue #862 issue agent 评论（VOD 核验 + spec 设计）

## Objective

消除场景市场「体验」domain-swarm（market_id=19，模板 jiuwenswarm-develop）全新建仓路径的偶发 504：全流程实测 ≈30s 撞 `scenes.py` 体验接口 #357 P1-C 引入的 30s 超时闸。

## 现状（VOD 已核验）

- `src/api/routes/scenes.py:889` `asyncio.wait_for(create_from_template, timeout=30)`，超时抛 `HTTPException(504)`。
- 实测主控 108：浅克隆 ~3s + git add ~3s + force push ~16s + 建仓/托管 API ≈ 30s 压线；模板工作区 ≈126MB / 4000+ 文件。
- 504 为假死：`run_in_executor` 线程不受 `wait_for` 取消影响，后台继续完成 push/托管；用户重试命中「仓库已存在」秒过。
- #357 的结构性风险（外呼占事件循环 / db_lock 长持）已由 P1-A~P1-E 修复，30s 闸现为误伤。

## Scope

### In Scope（本 PR，M1 止血）
- P0：`timeout=30 → 45`（提升为命名常量 `EXPERIENCE_TEMPLATE_CREATE_TIMEOUT`），并更新注释说明依据。
- 约束核对：外层 nginx 300s、前端 300s 均覆盖 45s；内层 git 超时（clone 120s / push 180s）不变。
- 回归测试：锁定超时值为 45（防回退到 30）。

### Out of Scope（M2 按排期另立 PR）
- P1-a 模板仓瘦身（`jiuwenswarm-develop` 模板的 docs/ 52MB、resources/ 16MB 移出模板）——模板仓侧变更。
- P1-b 体验改异步（task_id + expProgress 轮询）——涉及前后端契约改造。
- 双主同步部署与重启 main —— SRE 部署侧动作。

## Acceptance Criteria

- [ ] 代码：体验模板创建超时 30→45，具名常量 + 注释依据（#862 / #357 P1-C）。
- [ ] 测试：`tests/test_issue_862_experience_timeout.py` 断言常量值为 45 且路由源使用该常量。
- [ ] 全新建仓路径体验 domain-swarm 多次实测无 504（部署后由 SRE/验收回归执行）。
- [ ] 体验期间其他用户接口延迟无显著劣化（对照 api_latency_log 同时间窗）。
