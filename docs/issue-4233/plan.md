# Issue #4233 方案设计（Plan）

## 1. 测试 seam

| 切片 | seam | 说明 |
|------|------|------|
| 第三分支渲染 | `ReposView.vue` 模板源码断言 | 断言 `v-else` 为 disabled 按钮、无 click 处理 |
| 文案/提示 | `repos.card.no_permission_host` / `no_permission_host_title` | zh/en JSON 断言 |
| 禁用样式 | `.project-card-btn-disabled` | 断言 `cursor:not-allowed` 与 hover 无高亮 |
| 零回归 | `is_hosted` / `can_host` 分支源码断言 | 锁定原 click 与文案不变 |
| 凭据刷新 | `AccountView.refreshRepoCreds` + `ReposView` 挂载重拉 | 断言清缓存 + 静默重拉 `/repos/member` |

## 2. 模板改动（`ReposView.vue`）

第三分支（`v-else`）改为：

```vue
<button
  v-else
  class="project-card-btn project-card-btn-outline project-card-btn-disabled"
  :title="t('repos.card.no_permission_host_title')"
  disabled
  @click.stop
>
  {{ t('repos.card.no_permission_host') }}
</button>
```

- `disabled` 原生阻断 click/focus，`@click.stop` 兜底阻断冒泡到卡片外链。
- 移除 `hostNoPermission()`，避免死代码。

## 3. 样式改动

在 `ReposView.vue` scoped 样式新增：

```css
.project-card-btn-disabled,
.project-card-btn-disabled:hover {
  border-color: var(--hairline);
  background: transparent;
  color: var(--t3);
  opacity: 0.65;
  cursor: not-allowed;
}
```

## 4. 词条改动

`shell/locales/{zh,en}/repos.json` 的 `card` 下新增 `no_permission_host_title`：
- zh：`无托管权限：需为仓库 Maintainer 及以上角色，或使用仓库所有者凭据`
- en：`No hosting permission: requires the Maintainer role or higher ...`

## 5. 凭据刷新链路

- `AccountView.refreshRepoCreds()` 已清 `repo_member_cache` / `_clearCachedReposData` / `invalidateProjectsCache`。
- `ReposView` 带首屏种子挂载时执行 `loadProjects({ silent: true })` 重新拉 `/repos/member`；`goToRepos` 每次进入也直接请求 `/repos/member`。因此凭据变化后 `can_host` 会随最新列表更新。

## 6. 与在途 PR #2085 协调

PR #2085（#4172）已合入基线（`arch-weekly-11` → `weekly-mys`，提交 `9f234f12d`），其改动位于操作区头部与空态，本 Issue 仅改项目卡片第三分支，无冲突、不回退按钮顺序/文案。