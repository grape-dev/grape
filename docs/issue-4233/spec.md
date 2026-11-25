# Spec: Issue #4233 — can_host=false 仍渲染可点击「加入并托管→」

> Coding Agent 实现基线 | 目标分支 weekly-mys | 2026-11-25

## Objective
- 仓库卡片在用户无托管权限（`can_host=false`）时直接渲染禁用态按钮，并在按钮自身说明无权限，消除「看起来可点、点了才报错」。
- 成功标准：无权限卡片按钮 disabled、不可点击、文案自解释；有权限/已托管分支行为与外观零回归。

## Scope
### In Scope
- `src/api/frontend/app/src/views/ReposView.vue` 项目卡片第三分支（`v-else`）disabled 化。
- 复用词条 `repos.card.no_permission_host` 作为按钮文案，新增 `repos.card.no_permission_host_title` 作为 `title` 权限说明。
- 禁用态样式：`cursor: not-allowed`、hover 无实心高亮、`disabled` 阻断 click 与请求。
- 凭据变更后列表重取（缓存失效 + 挂载后静默重拉 `/repos/member`），避免误挡刚获权限用户。

### Out of Scope
- 后端 `can_host` 权限计算逻辑与权限口径（见 #4246）。
- 托管确认弹窗文案（见 #4232）。
- PR #2085 已涉及的按钮顺序/范围切换文案/空态跳转（该改动已在基线 `weekly-mys` 中）。

## Technical Approach
1. `ReposView.vue` 第三分支去掉 `@click.stop="hostNoPermission"`，改为 `disabled` + `:title` + 禁用类，文案换为 `repos.card.no_permission_host`。
2. 新增 scoped 样式 `.project-card-btn-disabled`：`border-color: var(--hairline)`、`background: transparent`、`color: var(--t3)`、`opacity: .65`、`cursor: not-allowed`，并覆盖 `:hover` 保持无高亮。
3. 删除死代码 `hostNoPermission()`（无其它调用点）。
4. zh/en `repos.json` 同步新增 `no_permission_host_title`。

## Acceptance Criteria
- [x] `can_host=false` 卡片按钮为 disabled：点击无 toast、无请求、无 pointer 手势、hover 无高亮。
- [x] 按钮文案直接表明无权限（`repos.card.no_permission_host`），`title` 说明需 Maintainer 及以上权限 / 使用仓库所有者凭据。
- [x] 回归：`can_host=true` 仍走 `hostAndSelect`；`is_hosted=true` 仍走 `selectRepo`，外观与行为不变。
- [x] 凭据刷新清缓存且列表在挂载后静默重拉 `/repos/member`，`can_host` 随之更新。
- [x] 与 PR #2085 无冲突（其改动已合入基线 `weekly-mys`）。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 禁用后凭据刷新未重取导致误挡有权限用户 | Med | 缓存失效 + 挂载静默重拉，测试覆盖 |
| 与 #2085 同文件冲突 | Med | 已确认 #2085 合入基线，仅改第三分支 |
| 移除 `hostNoPermission` 影响其他调用 | Low | 全局检索确认无引用 |

## Open Questions
- 无