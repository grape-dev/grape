# Spec: Issue #2121 — 体验建仓不进托管栏 / 解除托管后找不回

> Source: Issue #2121 body + Issue Agent Spec (comment id=200205213) | Base branch: weekly-mys

## Objective
修复场景中心体验建仓后仓库不进「已托管」栏、解除托管后无法找回的问题，统一
`user_repos` 失效入口，并让 host 失败可见、可重试。

## Why
`user_repos` 读缓存 TTL 30 分钟，而 `host_repo` / `create_from_template` /
`remove_managed_repo` 三处写操作都不失效缓存；同时体验接口把 `host_repo` 失败
降级为普通日志后照常返回成功，形成孤儿仓库。

## Scope
- 后端：`src/user/db.py` 新增缓存失效入口；`src/api/routes/repos.py` 写后失效 +
  `/repos/member?refresh=1`；`src/api/routes/templates.py`、`src/api/routes/scenes.py`
  体验路径返回 `{full_name, hosted, host_error}`。
- 前端：`scene-market.js` 体验/切换仓库后强制刷新；`repos.js` 解除托管后刷新且
  未托管空态保留同步入口。
- 测试：缓存失效、`refresh=1` 绕过缓存、默认仍命中缓存。

## Acceptance
- 体验建仓成功后 ≤10s 出现在「已托管」栏。
- 解除托管后立即进入「未托管」表并可一键重新托管。
- host 失败有明确提示，不再误报成功。
- `refresh=1` 立即生效；不带参数仍走 30 分钟缓存。
- 现有测试通过 + 新增测试覆盖。
