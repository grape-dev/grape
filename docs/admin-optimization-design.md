# 运营管理台优化方案

> 分支：`feat/admin-optimization`（基于 dev `2284241a`）

---

## 需求 1：Runtime 引擎显示运行 IP 地址

### 问题

EngineManage.vue 引擎列表和卡片模式已显示：实例名/类型/版本/提供方/设备/范围/归属人/健康/心跳。但缺少运行 IP 地址。

### 根因

- `agent_runtimes` 表没有 IP 字段
- IP 在 `runtime_credentials.last_ip` 表里
- 后端 `RuntimeDeviceService.engineList()` 查了凭据（`credentialById`），但只取了 `deviceName` 和 `ownerName`，没取 `lastIp`
- `RuntimeCredentialRo` 实体已有 `lastIp` 字段（L49），只是没 put 到返回数据里
- 前端 EngineManage.vue 也没有 IP 列

### 改动点

#### 后端（1 处）

**文件**：`admin/backend/src/main/java/com/grape/admin/service/RuntimeDeviceService.java`

**engineList 方法**：在 `m.put("ownerName", ...)` 后加一行：
```java
m.put("lastIp", cred == null ? null : cred.getLastIp());
```

**engineDetail 方法**：在 `engine.put("ownerName", ...)` 后加一行：
```java
engine.put("lastIp", cred == null ? null : cred.getLastIp());
```

#### 前端（2 处）

**文件**：`admin/frontend/src/views/EngineManage.vue`

1. **列表模式表头**：在 `<th>最后心跳</th>` 前加 `<th>IP</th>`
2. **列表模式数据行**：在心跳 `<td>` 前加 `<td class="t3">{{ e.lastIp || '—' }}</td>`
3. **卡片模式 meta 区**：在归属 div 后加：
```html
<div class="ellipsis-cell" :title="e.lastIp || '—'">
  <span class="t3" style="display:inline-block;min-width:46px">IP</span> {{ e.lastIp || '—' }}
</div>
```

**文件**：`admin/frontend/src/views/EngineDetail.vue`

4. **详情页基本信息**：在 `{{ detail?.device || '' }}` 后面追加 ` · {{ detail?.lastIp || '' }}`

### 影响范围

- 只改 3 个文件（1 个 Java + 2 个 Vue）
- 不改数据库（字段已有）
- 不改 API 路径（只多返回一个字段）
- 不影响现有功能

---

## 需求 2：点击 Issue 跳转到 GitCode 仓库

### 问题

ReposManage.vue 仓库管理页有 A2A 时序弹窗，点击 Issue 只是展开/折叠（toggleIssue），没有跳转到 GitCode Issue 页面的入口。

### 现状

- `timelineRepo` 对象有 `repoKey`（格式 `gitcode:owner/repo`）和 `fullName`
- `timelineIssues` 数组每项有 `issueId`、`title`、`events`
- 已有 `repoUrl(r)` 函数生成 GitCode URL：`'https://gitcode.com/' + full`
- Issue 标题行 L110-113：点击整行触发 `toggleIssue(iss)` 展开/折叠

### 改动点

#### 前端（1 处）

**文件**：`admin/frontend/src/views/ReposManage.vue`

在 Issue 标题行的 `<code class="tl-issue">{{ iss.issueId }}</code>` 后面加一个 GitCode 链接图标：

```html
<code class="tl-issue">{{ iss.issueId }}</code>
<a v-if="timelineRepo.repoKey"
   :href="issueGitCodeUrl(timelineRepo, iss.issueId)"
   target="_blank"
   @click.stop
   style="color:var(--primary);font-size:12px;text-decoration:none;margin-left:4px"
   title="在 GitCode 中打开">↗</a>
<span class="t3 tl-title">{{ iss.title || '（无标题）' }}</span>
```

新增函数：
```javascript
function issueGitCodeUrl(repo, issueId) {
  const full = repo.fullName || (repo.repoKey || '').replace(/^gitcode:/, '')
  return 'https://gitcode.com/' + full + '/issues/' + issueId
}
```

### 设计说明

- `@click.stop` 阻止链接点击触发 toggleIssue 展开/折叠
- `target="_blank"` 新标签页打开
- 用 `↗` 图标表示外链，不占太多空间
- 复用已有 `repoUrl` 逻辑（`fullName || repoKey.replace('gitcode:','')`）
- 不改后端（数据已有，只是前端加链接）

### 影响范围

- 只改 1 个文件（ReposManage.vue）
- 不改后端
- 不影响现有展开/折叠功能

---

## 改动清单

| # | 文件 | 改动 | 类型 |
|---|------|------|------|
| 1 | RuntimeDeviceService.java | engineList + engineDetail 加 lastIp | 后端 |
| 2 | EngineManage.vue | 列表表头/数据行/卡片加 IP | 前端 |
| 3 | EngineDetail.vue | 详情页显示 IP | 前端 |
| 4 | ReposManage.vue | Issue 标题加 GitCode 跳转链接 | 前端 |

### 不改的

- 数据库（last_ip 字段已有）
- API 路径
- 后端 Controller（只改 Service 层返回数据）
- 现有功能逻辑

---

## 验证方式

1. `cd admin/backend && mvn compile` 编译通过
2. `cd admin/frontend && npm run build` 构建通过
3. 浏览器访问 EngineManage 页面：列表和卡片显示 IP 列
4. 浏览器访问 EngineDetail 页面：详情显示 IP
5. 浏览器访问 ReposManage → 点击 A2A 时序 → Issue 标题旁有 ↗ 链接 → 点击新标签打开 GitCode Issue
