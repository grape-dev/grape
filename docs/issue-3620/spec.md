# Issue #3620 — tko_ 接管凭据通过 git-credential 身份判定

> 需求来源：Issue #3620 Spec（评论 id=202578636）；#3619 为同缺陷重复单，已 closed，本实现合并参考、不重复修复。

## 目标

平台签发给跨环境接管者（Windows/macOS agent）的 `tko_`（kind=takeover）凭据可通过
`GET /api/repos/{owner}/{repo}/git-credential` 换取平台 git 凭据，打通「接管成功却交不回」
的最后一公里：clone → 开发 → push → request-finish 全链路。

## 现状（缺陷实据）

1. `src/domains/mgmt/routes/repos.py` `_repo_platform_token` 硬性要求
   `auth_type == "session_token"`，`tko_` 一律 403「仅支持会话级临时 token」。
2. `src/api/auth_caps.py` 能力矩阵中 `takeover.repo_session=True`，能力矩阵与端点判定脱节。
3. `src/api/git_repo_api.py` 的 `_repo_token` 已支持 `tk_`/`tko_`（含 repo_key 仓级校验 +
   接管会话 active 校验），git-credential 端点未对齐，形成同域两套口径。

## 范围

### In Scope
- `_repo_platform_token` 身份判定改由能力位 `repo_session` 驱动（`has_capability`），
  放行 `tk_`（worker）与 `tko_`（takeover）。
- `tko_` 仓级严格校验：`repo_key` 必须存在并与请求目标仓完整相等，跨仓 / 未绑定 403。
- `tko_` 接管会话仍须 `active`（finish/abort 吊销后二次校验拒绝），口径与 `_repo_token` 一致。
- `auth_token.py` 方式 C 注释同步：`tk_`/`tko_` verify 互不通用（kind 过滤）不等于能力位隔离。
- 回归测试：三凭据（tk_/tko_/grape_）边界 + tko_ 正向 / 越权拒绝。

### Out of Scope
- 不放宽 `tk_` / `grape_` 既有能力边界。
- 方案二（takeover_start bundle 随发一次性 git token）不做。
- 不改动 `tk_` 签发/下发通道与 `takeover_sessions` 表结构。

## 验收标准

- [x] `tko_` 且 repo_key 与请求仓库一致 + 会话 active → 200 返回平台凭据。
- [x] `tko_` 跨仓 / 未绑定 repo_key → 403；接管会话非 active → 403。
- [x] `tk_` 正向不受影响；`grape_`/JWT/internal 仍 403。
- [x] `x-token-scope` 维持 `session_only`（repo 域会话凭据族）。

## 非目标风险

鉴权面放宽由「能力位（仅 tk_/tko_）+ repo_key 严格相等 + 会话 active」三重约束收敛；
越权拒绝用例固定该边界。