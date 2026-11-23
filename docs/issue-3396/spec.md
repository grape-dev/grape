# Spec: Issue #3396 — sre/chat 引擎调度限制放开（arch-weekly-11 版本）

> 基线：arch-weekly-11-zj @ 8e4f961ea（初版 9c4c770b8，已按新分层布局移植：`src/domains/*` + `src/kernel/*`、前端 Vue）。同款需求 #3384 在 weekly-11 线并行推进，本 Issue 为 arch 基线落地载体，行号一律以符号名 grep 定位。

## Objective

在 arch 基线落地 5 项改动——放开 SRE/Chat 引擎调度限制，并以 `engine_capabilities` 角色标签守卫防止官方机器被越权调度：

1. **删 SRE 禁广播**：删除 `_sre_no_broadcast`（engine_select.py）及 pick_engine / get_scene_pool_engine_ids 中的全部调用分支，SRE 任务进入通用引擎选择链（global 池广播放开）。
2. **chat 补进标签值域四处联动**：后端互斥规则 `AGENT_TAGS`（src/domains/runtime/routes/runtime.py `_apply_tag_rules`）；前端 `_RT_TAG_OPTIONS` / `_RT_TAG_COLORS` / `_rtTagLabel`、`onEdTagChange` 5→6（src/api/frontend/app/src/views/RuntimesView.vue）、`sceneAvailableRoles` 默认表；配套对账扫描测试锁定前后端值域一致。
3. **认领侧 #658 守卫口径统一到 `engine_capabilities`**：`_filter_by_role` / `_member_role_tags` / `_role_no_candidates_detail` / `_candidate_reject_reasons` / `service.claim_by_tag` 全部改读 engine_capabilities；`engine_role_tags` 降级为注册透传解析（守卫不再消费）；空标签 fail-closed。
4. **角色标签写权限收紧**：新增 `require_runtime_owner_or_admin`（src/api/deps.py）；`PUT /runtimes/{id}/tags` 仅引擎凭据所有者（user 本人 / org owner+admin）与平台管理员可改，仓库管理员禁改；本机直连（credential_id=0）仅平台管理员可改；读接口保持全员可读。
5. **Chat 兜底角色过滤**：两条兜底路径（`resolve_local_engine` global 回退、`_resolve_chat_runtime_id` 末级兜底）保留，但候选必须过 chat 角色过滤（勾了 chat 或 all），无合规候选给出明确提示，不静默调度。

## 关键设计决策（4 个裁决点已裁定）

| 决策 | 选择 | 理由 |
|---|---|---|
| 标签事实源 | `engine_capabilities` | UI 勾选即生效，无需重启/重注册（A4） |
| 空标签语义 | fail-closed | 手动清空标签的引擎两侧均不可调度（A7） |
| 仓库管理员写权限 | 禁改 | 可改面 = 凭据所有者（user 本人 / org owner+admin）+ 平台管理员（A5） |
| 官方机器初始态 | 无声明补 all；有声明按 role_tags 播种 | 存量回归不被误伤（A8），`tags_user_set=1` 防心跳覆盖；显式声明 role_tags 的机器（特殊场景，如 only-sre）升级后保持收窄，不被补成 all（架构守护 M1 整改） |

## Acceptance Criteria

- [ ] A1 SRE 任务可被 global 池中勾了 sre/all 的引擎认领（去限制生效）
- [ ] A2 未勾 sre 也未勾 all 的引擎，派发+认领两侧都不可调度（决策日志 `role_no_candidates` 留痕）
- [ ] A3 UI 可勾/不勾 chat；6 角色全选自动折叠为全能；保存后心跳不覆盖（`tags_user_set=1`）
- [ ] A4 认领侧以 engine_capabilities 为准，UI 改标签无需重启/重注册即时生效，role_tags 列改值不再影响认领
- [ ] A5 权限矩阵：非所有者/仓库管理员 PUT tags → 403；所有者/平台管理员 → 200
- [ ] A6 Chat 兜底只落勾了 chat/all 的引擎，全不勾时明确提示不静默
- [ ] A7 手动清空标签的引擎任何角色两侧均不可调度（fail-closed）
- [ ] A8 存量回归：未手工设标签的引擎（注册自动补 all）行为与基线一致不被误伤
- [ ] A8b 声明兼容：注册带 `role_tags=['sre']`（特殊场景 only-sre 机器）→ 能力行播种为 `sre`（不补 all），派发/认领两侧仅放行 sre；存量行 `role_tags` 非空且仅有自动补的 `all` → 再注册/心跳改写为声明集合；`tags_user_set=1` 与既有非 all 角色行一律不覆盖
- [ ] 现有测试全绿；新增测试覆盖新功能；5 个 commit 各自可独立 revert（回滚改动④须连带回滚改动③）
- [ ] arch 基线专项：无 weekly-11 专属假设残留（行号/分支相关断言）

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| 拆禁广播后官方机器被越权认领 | High | 守卫先行（改动③先于改动①提交）；`role_no_candidates` 留痕 |
| 口径切换导致认领面变化 | Med | A8 存量回归 + 对账扫描 |
| 前后端标签值域不同步 | Med | 四处联动清单固定 + 对账扫描测试兜底 |
| 权限收紧误伤现有使用者 | Med | A5 权限矩阵测试；读接口保持全员可读 |
