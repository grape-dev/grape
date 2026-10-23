# 仓库状态一致性修复

> **实现状态声明**：已实现。41/41 测试通过。涉及前端 account.js / app.js / api.js / repos.js / settings.js / issues.js / feedback.js 逻辑修复 + index.html 版本号递增。
>
> **重构铁律（AI 协作必读）**：优化重构不得破坏现有功能逻辑——此为绝对约束，优先级高于一切代码整洁目标。任何 AI 修改本文件涉及的代码时，必须保证执行路径等价、条件守卫保留、行为模拟测试不被破坏。详见设计文档第 0 章「重构铁律」。

## 一、总览

### 1.1 背景

用户在账户页解绑 GitHub 或 GitCode 平台后，仓库列表不可见，必须重新登录才能恢复。经代码审计确认该问题存在，根因在前端解绑回调未清理 `sessionStorage.currentRepo` 和内存缓存 `_cachedReposData`。

深度审计进一步发现这是一个系统性的「多层仓库状态缺乏同步清理」设计缺陷：解除托管、组织切换、issues.js 跳转、新建仓库、多标签页同步等操作同样存在状态不一致问题。同时发现 feedback.js 存在独立的 `currentRepo` 调用 bug。全部一并修复。

### 1.2 需求范围

| 编号 | 需求 | 类型 | 影响文件 |
|------|------|------|---------|
| R1 | 解绑平台后清理过期的 `sessionStorage.currentRepo` | Bug 修复 | account.js |
| R2 | 解绑平台后引导用户回到仓库选择页 + 刷新角色 | 体验优化 | account.js |
| R3 | 解绑平台后清理内存缓存 `_cachedReposData` | Bug 修复 | account.js + app.js |
| R4 | 解除托管后清理 `localStorage.repo_member_cache` | Bug 修复 | repos.js + settings.js |
| R5 | 组织切换后清理内存缓存 `_cachedReposData` | Bug 修复 | app.js |
| R6 | issues.js 设置 `currentRepo` 时补全 `repoKey` 字段 | Bug 修复 | issues.js |
| R7 | `_forceSyncRepos` / `refreshRepos` / `submitCreateRepo` 成功后清理内存缓存 `_cachedReposData` | Bug 修复 | app.js + repos.js |
| R8 | 多标签页 localStorage 变化同步清理 `sessionStorage.currentRepo` | Bug 修复 | api.js |
| R9 | feedback.js 使用 `app.currentRepo()` 替代 `window._currentRepo` | Bug 修复 | feedback.js |
| R10 | 修改的 JS 文件在 index.html 中递增 `?v=` 版本号 | 部署保障 | index.html |

> **R7 范围说明**：R7 覆盖所有在仓库选择页修改仓库列表但未清 `_cachedReposData` 的操作路径——`_forceSyncRepos`（app.js）、`refreshRepos`（repos.js）、`submitCreateRepo`（repos.js）。`syncRepos`（repos.js）在仓库管理页执行不影响 `_cachedReposData`，不纳入。

### 1.3 不在范围

- 后端 `unbind_provider` 逻辑（已正确清理 `user_repos` 缓存，无需改动）
- 后端 `delete_external_identity` 逻辑（已正确软删除 `external_identities`，无需改动）
- `/repos/member` 和 `/repos/managed` 路由逻辑（无需改动）
- dashboard 403 自愈逻辑（已有独立实现 app.js:1449-1471，本次不改动）
- 403 全局处理（已有独立设计文档 `r10-403-global-handling-design.md`）
- 全局架构重构「多层仓库状态统一清理机制」（后续评估）

---

## 二、问题分析

### 2.1 前端仓库状态全量盘点

Grape 前端是 SPA 架构，维护多层仓库状态：

| 层级 | 存储位置 | 用途 | 生命周期 |
|------|---------|------|---------|
| L1 | `sessionStorage.currentRepo` | 当前选中仓库（含 `fullName`、`owner`、`name`、`repoKey`） | 页签级（刷新保留，关闭清除） |
| L2 | `localStorage.repo_member_cache` | `/repos/member` 响应缓存 | 持久化（跨页签共享） |
| L3 | app.js 闭包变量 `_cachedReposData` | 语言切换 `_rerenderReposView()` 轻量重渲染 | 内存级（页面刷新重置） |
| L4 | `localStorage.user.bindings` | 已绑定平台列表 | 持久化（跨页签共享） |
| L5 | `localStorage.current_org_id` | 当前组织 | 持久化（跨页签共享） |
| L6 | DOM `userRole.textContent` | 顶栏角色显示（管理员/成员） | DOM 级 |
| L7 | DOM `repoSelect.options` | 侧边栏仓库下拉 | DOM 级 |

### 2.2 根因本质：多层仓库状态缺乏同步清理

各操作的清理完整性矩阵：

| 操作 | 位置 | L1 currentRepo | L2 repo_member_cache | L3 _cachedReposData | L4 bindings | L6 userRole |
|------|------|:-:|:-:|:-:|:-:|:-:|
| **解绑平台** | account.js | ❌ **R1** | ✅ | ❌ **R3** | ✅ | ❌ **R2** |
| **解除托管（repos.js）** | repos.js:625 | ✅ wasCurrent | ❌ **R4** | ❌ | - | - |
| **解除托管（settings.js）** | settings.js:64 | ✅ wasCurrent | ❌ **R4** | ❌ | - | - |
| **组织切换** | app.js:652 | ✅ 更新或清除 | ✅ | ❌ **R5** | - | - |
| **新建仓库/fork** | repos.js:790 / templates.js:732 | - | ✅ | ❌ **R7** | - | - |
| 绑定成功 | account.js:765 | ❌（跳 goToRepos 间接刷新） | ✅ | ❌（间接刷新） | ✅ | ❌（间接刷新） |
| OAuth 登录 | app.js:2567 | ✅ | ✅ | N/A（页面刷新） | ✅ | ✅ |
| logout | app.js:2387 | ✅ | ✅ | ❌（showLogin 重置） | ✅ | N/A |

此外：
- issues.js 三处跳转设置 `currentRepo` 缺 `repoKey` 字段（R6）
- 多标签页 localStorage 变化未同步清理当前标签页的 `sessionStorage.currentRepo`（R8）
- feedback.js 使用不存在的 `window._currentRepo` 而非 `app.currentRepo()`（R9）

### 2.3 解绑平台根因定位

**问题文件**：`src/api/static/js/pages/account.js`，函数 `_accountUnbindProvider`

**涉及两条代码路径**：

1. 普通解绑路径（account.js:986-1000）
2. 确认后解绑路径（account.js:1009-1021）

两条路径在解绑成功后均执行了以下操作：

| 步骤 | 操作 | 状态 |
|------|------|------|
| 1 | 更新 `localStorage.user.bindings` | ✅ 已做 |
| 2 | 清除 `localStorage.repo_member_cache` | ✅ 已做 |
| 3 | 调 `initSidebarRepos()` 刷新侧边栏 | ✅ 已做 |
| 4 | 检查并清理 `sessionStorage.currentRepo` | ❌ 缺失（R1） |
| 5 | 引导用户回到仓库选择页 | ❌ 缺失（R2） |
| 6 | 清理内存缓存 `_cachedReposData` | ❌ 缺失（R3） |
| 7 | 刷新角色 `loadProfile()` | ❌ 缺失（R2） |

**后端已正确处理**（无需改动）：`delete_external_identity` 软删除 status=0 + 清空 token、`DELETE FROM user_repos WHERE repo_key LIKE '{provider}:%'`、`get_user_identities` 只查 `status IN (1, 4)`、`/repos/member` 只拉 `_bound_providers`、`check_repo_access` → 403。

### 2.4 解绑影响链路

`sessionStorage.currentRepo` 未清理时，用户从 account 页返回后有三种路径：

**路径 A：通过「返回工作台」返回（半自愈，403 闪烁）**——`_accountGoBack()` → `currentRepo()` truthy → `nav('dash')` → 403 → 403 自愈清 currentRepo → 最终跳仓库选择页。用户看到 403 错误闪烁。

**路径 B：通过「返回仓库大厅」返回（正常）**——`goToRepos()` 不检查 currentRepo，直接调 `/repos/member`。

**路径 C：通过 popstate/用户菜单导航到非 dash 视图（完全卡死）**——`nav('pipe')` 等 → 403 → 这些视图的 catch 只有 `pc.innerHTML = error`，没有 403 自愈 → 完全卡死。

各视图 403 处理对比：

| 视图 | 403 自愈？ |
|------|:-:|
| dash | ✅ 清 currentRepo + 切仓库/goToRepos |
| pipe/vod/issue/coding/qa/sre/chat | ❌ 只有 `pc.innerHTML = error` |

### 2.5 解除托管根因（R4）

`_doUnhost`（repos.js:625）和 `unhostFromSettings`（settings.js:64）在解除托管后：✅ 检查 `wasCurrent` 清 currentRepo、✅ 调 `/repos/member` 刷新，但 ❌ 未清 `localStorage.repo_member_cache`。

**影响**：`wasCurrent` 时 currentRepo 被清 → null → `nav()` 守卫调 `loadRepos()` → 先读旧 `repo_member_cache`（含已解除托管的仓库）→ 短暂渲染脏数据。

### 2.6 组织切换根因（R5）

`switchOrg`（app.js:652）：✅ 清 `repo_member_cache`、✅ 更新 currentRepo，但 ❌ 未清 `_cachedReposData`。

**影响**：`selectRepo` 路径不调 `goToRepos()`，`_cachedReposData` 持有旧组织仓库列表。用户切换语言 → `_rerenderReposView()` 用旧数据渲染 → 显示旧组织仓库。

### 2.7 issues.js repoKey 缺失根因（R6）

issues.js 三处跳转函数设置 `currentRepo` 时缺失 `repoKey` 字段：

| 函数 | 位置 | 当前代码 |
|------|------|---------|
| `jumpToPipeline` | issues.js:545-546 | `{owner, name, fullName}` — 无 repoKey |
| `_jumpToNodeEvent` | issues.js:571-572 | `{owner, name, fullName}` — 无 repoKey |
| `_jumpToPipePage` | issues.js:586-587 | `{owner, name, fullName}` — 无 repoKey |

`t.repo_key` 本身是 `github:owner/name` 或 `gitcode:owner/name`，但 `replace(/^(github|gitcode):/, '')` 把前缀去掉后只用于拆分 owner/name，没把原始 `t.repo_key` 存入 `repoObj.repoKey`。

**影响**：`nav()` 中 `_rk.indexOf('github:') === 0` 判断失败 → GitHub 仓库显示 GitCode 图标和链接。

### 2.8 新建仓库/fork 后 _cachedReposData 脏数据根因（R7）

`_forceSyncRepos`（app.js:752-763）在新建仓库/模板 fork 后被调用：
- ✅ 调 `/repos/sync` → 更新 `localStorage.repo_member_cache`
- ✅ 调 `renderReposFromData(repos)` 渲染列表
- ❌ 未清 `_cachedReposData`

**影响**：新建仓库后用户在仓库选择页切换语言 → `_rerenderReposView()` 用旧 `_cachedReposData`（不含新仓库）渲染 → 新仓库从列表中消失。

`_forceSyncRepos` 与解绑不同：解绑是「减少」仓库（旧缓存多出已解绑仓库），新建是「增加」仓库（旧缓存少了新仓库）。但根因相同——L3 未同步清理。

### 2.9 多标签页 localStorage 同步根因（R8）

api.js:54-67 的 `storage` 事件监听只处理 `token` 变化：

```js
window.addEventListener('storage', (e) => {
  if (e.key === 'token') { /* ... */ }
});
```

Tab A 解绑 GitHub → 清 Tab A 的 `sessionStorage.currentRepo` + `localStorage.repo_member_cache` + `localStorage.user.bindings`。Tab B 的 `sessionStorage.currentRepo` 仍是旧值（sessionStorage 页签级隔离），且 `localStorage` 已被 Tab A 修改。

Tab B 的 `currentRepo()` 仍返回过期的 GitHub 仓库 → `nav('dash')` → 403 → dashboard 403 自愈兜底（但非 dash 视图会卡死）。

**影响**：Tab B 用户体验差——dashboard 403 闪烁或非 dash 视图完全卡死。

### 2.10 feedback.js currentRepo 调用 bug 根因（R9）

feedback.js:328-333 检查 `window._currentRepo`——但 `currentRepo` 是 app.js 闭包内函数，不在 `window` 对象上：

```js
if (window._currentRepo && typeof window._currentRepo === 'function') {
  var r = window._currentRepo();  // 永远不会执行——window._currentRepo 不存在
} else if (window._currentRepo) {
  var r2 = window._currentRepo;  // 永远不会执行——window._currentRepo 不存在
}
```

**影响**：feedback.js 提交反馈时 `repoKey` 永远为空字符串，反馈不带 repo_key 关联信息。

### 2.11 为什么重新登录能恢复

OAuth 登录流程（app.js:2566-2570, 2596-2599）清理了 `sessionStorage.currentRepo` 和 `localStorage.repo_member_cache`。随后 `_restoreNavAfterLogin()` → `currentRepo()` null → `loadRepos()` → 正常加载。页面刷新也重置 `_cachedReposData`。

---

## 三、场景与场景用例

### 场景 1：选中 GitHub 仓库，解绑 GitHub，通过「返回工作台」回到 dashboard

**初始状态**：用户已绑定 GitHub + GitCode，`sessionStorage.currentRepo` = `{repoKey:"github:octocat/my-repo",...}`

#### 用例 1.1：解绑 GitHub 后点「返回工作台」（当前缺陷——半自愈，403 闪烁）

1. 用户进入 account 页 → 点击 GitHub「解绑」→ 弹窗确认
2. 后端执行解绑，返回 `{bindings: ["gitcode"], affected_repos: 1}`
3. 前端更新 bindings、清 `repo_member_cache`、调 `initSidebarRepos()`
4. `sessionStorage.currentRepo` 未清理 → 用户点「返回工作台」→ `nav('dash')` → 403 → 自愈
5. **用户看到 403 错误页面闪烁**

#### 用例 1.2：解绑 GitHub 后直接跳仓库选择页（修复后预期）

1-3. 同用例 1.1
4. 前端检查 `currentRepo().repoKey` 以 `"github:"` 开头 → 匹配 → 清除 currentRepo
5. 前端清除 `_cachedReposData`（R3）+ 调 `loadProfile()` 刷新角色（R2）
6. 前端调 `goToRepos()` → `/repos/member` → 显示 GitCode 仓库列表
7. **用户直接看到仓库选择页，无 403 闪烁，顶栏角色已刷新**

### 场景 2：选中 GitCode 仓库，解绑 GitCode

#### 用例 2.1：当前缺陷——半自愈，403 闪烁

1-3. 同场景 1 对应步骤
4. `currentRepo` 未清理 → 403 → 自愈 → **403 闪烁**

#### 用例 2.2：修复后——直接跳仓库选择页

1-3. 同用例 2.1
4. `currentRepo().repoKey` 以 `"gitcode:"` 开头 → 匹配 → 清除
5. 清 `_cachedReposData` + `loadProfile()` + `goToRepos()`
6. **用户看到 GitHub 仓库列表，无 403 闪烁**

### 场景 3：选中 GitCode 仓库，解绑 GitHub

#### 用例 3.1：当前行为——无缺陷（GitCode 未解绑，currentRepo 有效）

1. 用户解绑 GitHub → currentRepo 仍是 GitCode 仓库 → `nav('dash')` → 正常
2. **用户无感知**

#### 用例 3.2：修复后——不清理 currentRepo，但仍清理 _cachedReposData

1-2. 同用例 3.1
3. `currentRepo().repoKey` 以 `"gitcode:"` 开头 → 不匹配 `"github"` → 不清理 currentRepo
4. 不跳转（用户继续在 account 页）
5. 仍清理 `_cachedReposData`（R3 无条件）+ 调 `loadProfile()` 刷新角色（R2）
6. **用户无感知，继续使用 GitCode 仓库**

### 场景 3b：选中 GitHub 仓库，解绑 GitCode

**初始状态**：用户已绑定 GitHub + GitCode，`sessionStorage.currentRepo` = `{repoKey:"github:octocat/my-repo",...}`

#### 用例 3b.1：当前行为——无缺陷（GitHub 未解绑，currentRepo 有效）

1. 用户解绑 GitCode → currentRepo 仍是 GitHub 仓库 → `nav('dash')` → 正常
2. **用户无感知**

#### 用例 3b.2：修复后——不清理 currentRepo，但仍清理 _cachedReposData

1-2. 同用例 3b.1
3. `currentRepo().repoKey` 以 `"github:"` 开头 → 不匹配 `"gitcode"` → 不清理 currentRepo
4. 不跳转（用户继续在 account 页）
5. 仍清理 `_cachedReposData`（R3 无条件）+ 调 `loadProfile()` 刷新角色（R2）
6. **用户无感知，继续使用 GitHub 仓库**

### 场景 4：仅绑定一个平台，解绑后无平台绑定

#### 用例 4.1：修复后预期

1. 用户解绑 GitHub → `bindings` = `[]`
2. `currentRepo().repoKey` 匹配 → 清除 + 清 `_cachedReposData` + `loadProfile()`
3. `goToRepos()` → `bindings.length === 0` → 显示新手引导页
4. **用户看到新手引导页**

### 场景 5：二次确认后解绑

#### 用例 5.1：当前缺陷——半自愈

1. 后端返回 409 `{need_confirm: true, warehouses: [...]}`
2. 用户确认 → `API.del('/auth/bind/github?confirm=true')` → 成功
3. currentRepo 未清理 → 403 → 自愈 → **403 闪烁**

#### 用例 5.2：修复后——直接跳仓库选择页

1-2. 同用例 5.1
3. `currentRepo().repoKey` 匹配 → 清除 + 清 `_cachedReposData` + `loadProfile()` + `goToRepos()`
4. **用户直接看到 GitCode 仓库列表**

### 场景 6：解绑后通过 popstate 导航到非 dash 视图（完全卡死）

#### 用例 6.1：当前缺陷——完全卡死

1. 用户解绑 GitHub → currentRepo 未清理
2. 用户按浏览器后退 → `nav('pipe')` → 403 → pipe catch 只有 `pc.innerHTML = error`
3. **完全卡死**

#### 用例 6.2：修复后——不卡死

1. 用户解绑 → R1 清 currentRepo + R2 跳 `goToRepos()`
2. 用户在仓库选择页选 GitCode 仓库 → `switchRepo` → currentRepo 更新
3. `nav('pipe')` → GitCode 仓库 → 正常加载
4. **用户正常使用**

### 场景 7：解绑后语言切换触发脏数据渲染（R3）

#### 用例 7.1：当前缺陷——脏数据

1. 用户解绑 GitHub → `_cachedReposData` 仍持有含 GitHub 仓库的旧数据
2. 用户在仓库选择页切换语言 → `_rerenderReposView()` 用旧数据渲染
3. **显示已解绑平台的仓库**

#### 用例 7.2：修复后——正确渲染

1. 用户解绑 → R3 清 `_cachedReposData` = null
2. 用户切换语言 → `_rerenderReposView()` 检查 null → fallback 到 `goToRepos()`
3. `goToRepos()` 调 `/repos/member` → 只返回 GitCode 仓库
4. **正确渲染**

### 场景 8：解除托管后 loadRepos 短暂渲染旧缓存（R4）

#### 用例 8.1：当前缺陷

1. 用户解除托管当前仓库 A → `wasCurrent` 清 currentRepo ✅
2. 未清 `repo_member_cache` ❌ → `loadRepos()` 读旧缓存 → 含仓库 A → 短暂渲染脏数据
3. 后台 `/repos/member` 返回新数据 → 仓库 A 消失
4. **用户短暂看到已解除托管的仓库 A**

#### 用例 8.2：修复后

1. 解除托管 → 清 `repo_member_cache` ✅（R4）
2. `loadRepos()` 读缓存为 null → 直接调 `/repos/member`
3. **正确渲染，不含仓库 A**

### 场景 9：组织切换后语言切换渲染旧组织仓库（R5）

#### 用例 9.1：当前缺陷

1. 用户切换到组织 B → `switchOrg` 清 `repo_member_cache` ✅、更新 currentRepo ✅
2. `_cachedReposData` 仍持有组织 A 数据 ❌
3. 用户切换语言 → `_rerenderReposView()` 用旧数据渲染
4. **显示旧组织仓库**

#### 用例 9.2：修复后

1. `switchOrg` 清 `repo_member_cache` ✅、更新 currentRepo ✅、清 `_cachedReposData` ✅（R5）
2. 用户切换语言 → `_cachedReposData` 为 null → fallback `goToRepos()`
3. **显示组织 B 仓库**

### 场景 10：从任务详情跳转流水线，GitHub 仓库显示 GitCode 图标（R6）

#### 用例 10.1：当前缺陷

1. 用户点击「跳转流水线」→ `jumpToPipeline()` 设置 currentRepo 不含 repoKey
2. `nav('pipe')` → `_rk = ''` → `_isGithub = false` → GitCode 图标
3. **GitHub 仓库显示 GitCode 图标和链接**

#### 用例 10.2：修复后

1. `jumpToPipeline()` 设置 currentRepo 含 `repoKey: t.repo_key`（R6）
2. `nav('pipe')` → `_rk = "github:octocat/my-repo"` → `_isGithub = true`
3. **GitHub 仓库正确显示 GitHub 图标和链接**

### 场景 11：新建仓库后语言切换，新仓库消失（R7）

**初始状态**：用户在仓库选择页，`_cachedReposData` 持有当前仓库列表

#### 用例 11.1：当前缺陷

1. 用户新建仓库 `new-project` → `submitCreateRepo` 调 `/repos/sync` → 更新 `repo_member_cache` ✅
2. `renderRepoSelection(repos)` 渲染含新仓库的列表 ✅
3. 但 `_cachedReposData` 未更新 ❌ → 仍持有不含 `new-project` 的旧数据
4. 用户切换语言 → `_rerenderReposView()` 用旧 `_cachedReposData` 渲染
5. **新仓库 `new-project` 从列表中消失**

#### 用例 11.2：修复后

1. 新建仓库 → `/repos/sync` 成功 → `renderReposFromData(repos)` 渲染 ✅
2. `_forceSyncRepos` 清 `_cachedReposData` ✅（R7）→ 下次语言切换时 fallback 到 `goToRepos()`
3. 用户切换语言 → `goToRepos()` 调 `/repos/member` → 含新仓库
4. **新仓库 `new-project` 正常显示**

### 场景 12：多标签页解绑同步（R8）

**初始状态**：用户在 Tab A 和 Tab B 同时打开 Grape，两个标签页都选中 GitHub 仓库

#### 用例 12.1：当前缺陷——Tab B 卡死

1. Tab A 解绑 GitHub → 清 Tab A `sessionStorage.currentRepo` + `localStorage.repo_member_cache` + `localStorage.user.bindings`
2. Tab B 的 `sessionStorage.currentRepo` 仍是旧值（sessionStorage 页签级隔离）
3. Tab B 的 `localStorage` 已被 Tab A 修改，但 Tab B 不知道
4. Tab B 用户点「工作台」→ `currentRepo()` 返回过期 GitHub 仓库 → 403 → 闪烁或卡死
5. **Tab B 用户体验差**

#### 用例 12.2：修复后——Tab B 自动同步

1. Tab A 解绑 GitHub → 清 localStorage 各项
2. Tab B 收到 `storage` 事件（R8）→ 检测 `repo_member_cache` 或 `user.bindings` 变化
3. Tab B 检查 `currentRepo().repoKey` 是否仍有效（后端 `/repos/managed` 验证 或 检查 `bindings` 是否含该 provider）
4. 如果 `bindings` 不再含 currentRepo 的 provider → 清 `sessionStorage.currentRepo` + 清 `_cachedReposData` + 调 `goToRepos()`
5. **Tab B 自动跳到仓库选择页，无 403 闪烁**

### 场景 13：提交反馈不带 repo_key（R9）

#### 用例 13.1：当前缺陷

1. 用户选中仓库 `octocat/my-repo`
2. 用户提交反馈 → feedback.js:328 检查 `window._currentRepo` → 不存在 → `repoKey = ''`
3. `API.post('/feedback', {repo_key: '', ...})` → 反馈不带仓库关联
4. **反馈无法关联到具体仓库**

#### 用例 13.2：修复后

1. 用户选中仓库 `octocat/my-repo`
2. 用户提交反馈 → feedback.js 调 `app.currentRepo()` → 返回 `{owner:"octocat", name:"my-repo", ...}`
3. `repoKey = "octocat/my-repo"` → `API.post('/feedback', {repo_key: 'octocat/my-repo', ...})`
4. **反馈正确关联到仓库**

### 场景 14：JS 文件修改后浏览器缓存不失效（R10）

**初始状态**：用户浏览器已缓存旧版 JS 文件

#### 用例 14.1：当前缺陷——版本号未递增

1. 开发者修改了 `account.js`（新增 `_accountCleanupAfterUnbind` 函数）
2. `index.html` 中 `account.js?v=42` 未递增
3. 用户浏览器看到 `?v=42` 与缓存一致 → 使用旧版 JS
4. **用户解绑平台后仍出现 403 闪烁——所有 R1/R2/R3 修复不生效**

#### 用例 14.2：修复后——版本号递增

1. 开发者修改了 `account.js` → `index.html` 中递增为 `account.js?v=43`
2. 用户浏览器看到 `?v=43` 与缓存 `?v=42` 不一致 → 请求新文件
3. 用户解绑平台 → R1/R2/R3 修复生效
4. **用户直接看到仓库选择页，无 403 闪烁**

### 场景 15：全部 7 个文件的版本号验证（R10）

**初始状态**：基线 weekly-08 的 index.html 版本号

#### 用例 15.1：修复后预期——7 个文件版本号全部递增

| 文件 | 基线 | 修复后 | 验证方法 |
|------|------|--------|----------|
| `api.js` | ?v=30 | ?v=31 | index.html 源码检查 |
| `app.js` | ?v=184 | ?v=185 | index.html 源码检查 |
| `account.js` | ?v=42 | ?v=43 | index.html 源码检查 |
| `feedback.js` | ?v=8 | ?v=9 | index.html 源码检查 |
| `issues.js` | ?v=117 | ?v=118 | index.html 源码检查 |
| `repos.js` | ?v=61 | ?v=62 | index.html 源码检查 |
| `settings.js` | ?v=33 | ?v=34 | index.html 源码检查 |

1. 打开 `src/api/static/index.html`
2. 检查 7 个改动文件的 `?v=` 值全部大于基线值
3. **全部递增 → 浏览器缓存失效 → 修复生效**

---

## 四、修复需求

### R1：解绑平台后清理过期的 `sessionStorage.currentRepo`

**触发条件**：`_accountUnbindProvider` 成功回调，解绑 API 返回成功后。

**规则**：检查 `currentRepo()` 的 `repoKey` 是否以 `{provider}:` 开头。如果是，清除 `sessionStorage.currentRepo`。

**覆盖范围**：普通解绑路径 + 确认后解绑路径。

**不清理的情况**：`repoKey` 不匹配被解绑平台（场景 3），不清除。

### R2：解绑平台后引导用户回到仓库选择页 + 刷新角色

**触发条件**：R1 清理了 `sessionStorage.currentRepo` 后。

**行为**：
1. 调 `loadProfile()` 刷新顶栏角色（L6 `userRole.textContent`）——解绑后托管仓库变化，角色可能从「管理员」变为「成员」
2. 调 `goToRepos()` 引导用户回到仓库选择页

**不引导的情况**：R1 未清理（场景 3），不跳转。但仍调 `loadProfile()` 刷新角色——因为解绑可能影响托管仓库列表（即使当前仓库未变，角色可能变化）。

### R3：解绑平台后清理内存缓存 `_cachedReposData`

**触发条件**：解绑 API 返回成功后，**无条件**清理。

**规则**：通过 app.js 暴露的 `_clearCachedReposData()` 方法将 `_cachedReposData` 置 null。

**无条件清理的理由**：即使 currentRepo 不属于被解绑平台（场景 3），`_cachedReposData` 中的仓库列表仍可能包含已解绑平台的仓库。

### R4：解除托管后清理 `localStorage.repo_member_cache`

**触发条件**：`/repos/remove` API 返回成功后。

**覆盖范围**：repos.js `_doUnhost` + settings.js `unhostFromSettings`。

**规则**：`localStorage.removeItem('repo_member_cache')`。

### R5：组织切换后清理内存缓存 `_cachedReposData`

**触发条件**：`switchOrg` 中清除 `localStorage.repo_member_cache` 的同时。

**规则**：`_cachedReposData = null`（switchOrg 在 app.js 闭包内部，直接赋值）。

### R6：issues.js 设置 `currentRepo` 时补全 `repoKey` 字段

**覆盖范围**：`jumpToPipeline`、`_jumpToNodeEvent`、`_jumpToPipePage` 三处。

**规则**：`repoObj.repoKey = t.repo_key`（原始值，含 `github:` / `gitcode:` 前缀）。

### R7：仓库列表变更后清理内存缓存 `_cachedReposData`

**触发条件**：在仓库选择页修改仓库列表的操作成功后。

**覆盖范围**（三个操作路径）：
- `_forceSyncRepos`（app.js:752）——绑定新平台/模板 fork 后强制同步
- `refreshRepos`（repos.js:493）——用户在仓库选择页点「刷新」按钮
- `submitCreateRepo`（repos.js:790）——用户在仓库选择页新建仓库

**不纳入的操作**：
- `syncRepos`（repos.js:485）——在仓库管理页执行，调 `renderRepos()` 而非 `renderRepoSelection`，不在仓库选择页，不影响 `_cachedReposData`
- `loadRepos`（app.js:727）——通常在 `currentRepo()` 为 null 时被 `nav()` 守卫调用，不在仓库选择页
- `goToRepos`（app.js:1078）——成功时已更新 `_cachedReposData`（app.js:1081），无需清理

**规则**：
- `_forceSyncRepos` 在 app.js 闭包内，直接 `_cachedReposData = null`
- `refreshRepos` 和 `submitCreateRepo` 在 repos.js 中，通过 `app._clearCachedReposData()` 清理

**原因**：`_cachedReposData` 供语言切换 `_rerenderReposView()` 使用。这些操作修改了仓库列表但不清 `_cachedReposData`，语言切换时用旧缓存渲染 → 新仓库消失或旧仓库残留。

### R8：多标签页 localStorage 变化同步清理 `sessionStorage.currentRepo`

**触发条件**：api.js `storage` 事件监听中，检测到 `repo_member_cache` 或 `user.bindings` 被其他标签页修改。

**规则**：
1. 当 `e.key === 'repo_member_cache'` 且 `e.newValue` 为 null（被清除）时，说明其他标签页执行了解绑/解除托管等操作
2. 检查当前标签页的 `sessionStorage.currentRepo` 是否仍有效——通过 `localStorage.user.bindings` 判断 currentRepo 的 provider 是否仍在 bindings 中
3. 如果 currentRepo 的 provider 不在 bindings 中 → 清 `sessionStorage.currentRepo` + 清 `_cachedReposData` + 调 `goToRepos()`

**实现位置**：api.js:54 的 `storage` 事件监听中，扩展处理 `repo_member_cache` 和 `user` 键。

### R9：feedback.js 使用 `app.currentRepo()` 替代 `window._currentRepo`

**触发条件**：feedback.js 提交反馈时获取当前仓库。

**规则**：将 `window._currentRepo` 调用改为 `app.currentRepo()`：

```js
// 修复前
if (window._currentRepo && typeof window._currentRepo === 'function') { ... }

// 修复后
var r = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
if (r) repoKey = (r.owner || '') + '/' + (r.name || '');
```

### R10：修改的 JS 文件在 index.html 中递增 `?v=` 版本号

**触发条件**：任何 JS 文件有代码改动后，必须在 `index.html` 中递增该文件的 `?v=` 查询参数。

**覆盖范围**（本次修改的 7 个文件）：

| 文件 | 基线 ?v= | 递增后 ?v= |
|------|----------|------------|
| `api.js` | 30 | 31 | ✅ 已递增 |
| `app.js` | 184 | 185 | ✅ 已递增 |
| `pages/account.js` | 42 | 43 | ✅ 已递增 |
| `pages/feedback.js` | 8 | 9 | ✅ 已递增 |
| `pages/issues.js` | 117 | 118 | ✅ 已递增 |
| `pages/repos.js` | 61 | 62 | ✅ 已递增 |
| `pages/settings.js` | 33 | 34 | ✅ 已递增 |

**规则**：在 `src/api/static/index.html` 中，将每个改动文件对应的 `<script src="...?v=N">` 标签的 `N` 值递增 1。已全部实施。

**原因**：Grape 前端通过 `?v=` 查询参数控制浏览器缓存。JS 文件代码改动后如果不递增版本号，浏览器使用旧缓存，所有修复不生效。这是部署的必要保障，不是可选步骤。

**不纳入的文件**：`index.html` 本身通过 `Cache-Control: no-cache` 提供，无需版本号。CSS 文件本次无改动。

---

## 五、验证清单

| 验证项 | 场景 | 预期结果 |
|--------|------|----------|
| V1 | 选中 GitHub 仓库 + 解绑 GitHub + 点「返回工作台」 | currentRepo 清除，跳仓库选择页，无 403 闪烁，显示 GitCode 仓库 |
| V2 | 选中 GitCode 仓库 + 解绑 GitCode + 点「返回工作台」 | currentRepo 清除，跳仓库选择页，无 403 闪烁，显示 GitHub 仓库 |
| V3 | 选中 GitCode 仓库 + 解绑 GitHub | currentRepo 不清除，用户无感知继续使用 GitCode 仓库 |
| V3b | 选中 GitHub 仓库 + 解绑 GitCode | currentRepo 不清除，用户无感知继续使用 GitHub 仓库 |
| V4 | 选中 GitHub 仓库 + 解绑 GitHub（仅绑定一个平台） | currentRepo 清除，跳转新手引导页 |
| V4b | 选中 GitCode 仓库 + 解绑 GitCode（仅绑定一个平台） | currentRepo 清除，跳转新手引导页 |
| V5 | 二次确认后解绑 | 确认后 currentRepo 清除，直接跳仓库选择页，无 403 闪烁 |
| V6 | 解绑后通过 popstate 导航到 pipe/vod 等非 dash 视图 | 不卡死（R1+R2 已清理并跳转） |
| V7 | 解绑后侧边栏仓库下拉列表 | 已解绑平台的仓库消失，剩余平台正常显示 |
| V8 | 解绑后切换语言 → 项目列表渲染 | 不显示已解绑平台仓库（_cachedReposData 已清理） |
| V9 | 解绑后 goToRepos() 加载项目列表 | /repos/member 只返回剩余平台仓库 |
| V10 | 选中 GitCode 仓库 + 解绑 GitHub + 切换语言 | _cachedReposData 被清理（R3 无条件） |
| V10b | 选中 GitHub 仓库 + 解绑 GitCode + 切换语言 | _cachedReposData 被清理（R3 无条件） |
| V11 | 解除托管当前仓库后 loadRepos 渲染 | 不短暂显示已解除托管的仓库（repo_member_cache 已清理，R4） |
| V12 | 解除托管非当前仓库后 | 仓库列表正确更新 |
| V13 | 组织切换后切换语言 → 项目列表渲染 | 不显示旧组织仓库（_cachedReposData 已清理，R5） |
| V14 | 从任务详情跳转流水线，GitHub 仓库 | 顶栏显示 GitHub 图标和链接（repoKey 已补全，R6） |
| V15 | 从任务详情跳转事件详情，GitCode 仓库 | 顶栏显示 GitCode 图标和链接（repoKey 已补全，R6） |
| V16 | 从任务详情跳转流水线，GitHub 仓库 + 解绑 GitHub | R1 正确匹配并清理（因 R6 补全了 repoKey） |
| V17 | 新建仓库后切换语言 → 项目列表渲染 | 新仓库不消失（_cachedReposData 已清理，R7） |
| V18 | 模板 fork 后切换语言 → 项目列表渲染 | 新仓库不消失（R7 _forceSyncRepos 路径） |
| V18b | 仓库选择页点「刷新」按钮后切换语言 | 仓库列表正确渲染（R7 refreshRepos 路径） |
| V18c | 仓库选择页新建仓库后切换语言 | 新仓库不消失（R7 submitCreateRepo 路径） |
| V19 | 解绑后顶栏角色显示 | 角色已刷新（loadProfile 已调用，R2） |
| V20 | 选中 GitCode 仓库 + 解绑 GitHub + 顶栏角色 | 角色已刷新（R2 即使不跳转也调 loadProfile） |
| V20b | 选中 GitHub 仓库 + 解绑 GitCode + 顶栏角色 | 角色已刷新（R2 即使不跳转也调 loadProfile） |
| V21 | Tab A 解绑 GitHub → Tab B 点工作台 | Tab B 自动清理 currentRepo 并跳仓库选择页（R8） |
| V22 | Tab A 解绑 GitHub → Tab B 点流水线 | Tab B 不卡死（R8 已清理 currentRepo） |
| V22b | Tab A 解绑 GitCode → Tab B 点流水线 | Tab B 不卡死（R8 对 GitCode 同样生效） |
| V23 | 提交反馈时 repo_key 字段 | 正确携带当前仓库的 owner/name（R9） |
| V24 | 选中 GitHub 仓库 + 提交反馈 | repo_key = "owner/name"（R9） |
| V24b | 选中 GitCode 仓库 + 提交反馈 | repo_key = "owner/name"（R9 对 GitCode 同样生效） |
| V25 | 修改的 7 个 JS 文件在 index.html 中 ?v= 值全部递增 | 浏览器缓存失效，修复生效（R10） |
| V26 | 未修改的 JS 文件 ?v= 值不变 | 不触发不必要的缓存失效（R10 精确性） |
