# Spec: Issue #513 — DELETE /api/devstage/environments/{不存在} 返回 502 + 云厂商错误码（应 404）

> 依据 Issue Agent 已发布的 Spec（Issue #513 评论，2026-10-29），本文件为 coding 执行用精简版。
> 同根因 Issue #413（QA 回归提单，spec-ready，两次执行因平台引擎故障中断未产出 PR）——本次实现一次覆盖两单、互为验证；仓库已核验无 #413 分支/commit/PR，无重复开发风险。

## Objective
删除不存在的 DevStage 环境时，`DELETE /api/devstage/environments/{devstage_id}` 返回 **404 统一错误包络**（`{"code":404,"message":...}`），不再返回 502 并回传上游华为云错误码（实测 `HD.97420111 parameter invalid`）与内部文案。

## Scope
- `src/api/routes/devstage.py` `delete_environment()`（L314）：
  - DevStageError 分类映射——「资源不存在」→ 404 统一包络（不含上游码/文案）；「上游真实故障」→ 维持 502 透传（现有行为）；
  - 修复前置 `_list_envs`（查询环境名用于凭据清理）失败被吞导致「不存在」信号丢失的问题：列表查询成功但目标 id 不在列表中 → 直接 404（不发起注定失败的删除调用，同时也不做凭据清理——环境不存在即无凭据可清）。
- `src/im/devstage_client.py` `_request()`（L94-121）：错误分类暴露——DevStageError 增加 `not_found` 布尔属性，`error_code != "0000"` 时按最小白名单判别「资源不存在」特征（`HD.97420111 parameter invalid`，删除/查询单个不存在资源场景）。
- `tests/test_devstage_delete_404_513.py`（新增）：原子用例 `delete_missing_environment_is_404`（`-m atomic`）+ 上游真实故障维持 502 的对应用例；`pytest.ini` 注册 `atomic` marker（仓库当前无该 marker）。

## Out of Scope
- devstage 其它接口（list/create/start/stop）同类语义批量清理（#383/#385 已单独跟进，本次不动）。
- 前端错误展示适配（统一包络本就符合现有 `detail.code` 约定）。

## 判别策略（决策记录）
- 主判别 = 上游错误码白名单（`HD.97420111`，删除不存在环境实测码），避免额外上游查询；
- 辅判别 = 删除前 `_list_envs` 结果（列表可枚举成功时，id 不在列表 → 404，跳过删除调用）；
- 白名单未命中且列表不可用 → 维持 502（宁可保守 502 也不误报 404）。

## Acceptance Criteria
- [ ] `DELETE /api/devstage/environments/does-not-exist-xyz` → 404 `{"code":404,"message":...}`，不含 `HD.*` 上游码；
- [ ] 上游真实故障（网络/5xx/白名单外错误码）仍返回 502；
- [ ] 原子用例 `pytest -m atomic -k delete_missing_environment_is_404` 通过（新增用例并注册 marker）；
- [ ] 现有测试全通过（重点回归 tests/test_devstage_filter.py、tests/test_devstage_admin_config_470.py）。

## 验证
- 单测全部本地跑（pytest tests/ 相关文件）；
- 真实上游 404/502 行为属云侧联调，由 QA 审查阶段复核。
