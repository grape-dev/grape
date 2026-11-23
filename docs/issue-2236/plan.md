# Plan: Issue #2236 组织上下文与知识库管理 UI

## 决策摘要

| 决策 | 选择 | 理由 |
|------|------|------|
| 状态字段 | 展示/流转读 `lifecycle_status`，查询参数用 `status` | #2150 API 实际返回 `lifecycle_status` |
| active 唯一冲突 | 前端不做阻断，采用后端「发布自动降级旧 active」；仅 409 时提示 | 与已合入后端语义一致，避免误报 |
| 预览数据源 | 客户端按 UTF-8 字节估算 | #2235 未合入，不阻塞本单；payload 结构保持兼容 |
| 权限显隐 | `org.role` owner/admin 写，member 只读；403 兜底降级 | org 详情未返回 repo 角色，且不新增接口 |
| 深链格式 | `#organizations/<id>/<context|knowledge>`，兼容解析 `tab=` 形式 | 与 app.js `#view/param` 路由兼容，刷新可恢复 |
| 知识正文获取 | 列表不含正文，预览按选中 id 逐条 `get`，并发 4 + 缓存 | 控制 N+1，重复预览不重复请求 |
| payload 兼容 | 有选择时 `context` 为 dict，无选择保持 `''` | 后端 `CreateDispatchBody.context: dict|str`，旧数据可读 |

## 模块改动

| 文件 | 改动 |
|------|------|
| `src/api/static/js/api.js` | 新增 `API.orgContext` / `API.orgKnowledge`；`localizeOrgError` 增补上下文/知识错误码 |
| `src/api/static/js/app.js` | `_pushHistory` 保留 `#organizations/<id>/<sub>` 后缀，子页深链刷新不丢 |
| `src/api/static/js/pages/organizations.js` | context Tab 双子页、生命周期徽标、权限显隐、下派选择与预览 |
| `src/api/static/locales/zh/organizations.json` | `lifecycle.*`、`context.*`、`knowledge.*`、`dispatch.*`、`errors.*` 文案 |
| `src/api/static/locales/en/organizations.json` | 同上英文文案（键集合与 zh 一致） |
| `tests/js/test_org_context_ui.js` | 新增 node 源断言 + 纯函数用例 |
| `.github/workflows/windows-smoke.yml` | 前端步骤追加 `node tests/js/test_org_context_ui.js` |

## 关键函数（测试 seam）

页面层为单文件 + 顶层函数（既有模式），测试 seam 取「纯函数 + 源码断言」：

- `_orgCanEditContent(org)` / `_orgCanPublish(org)`：权限显隐单一入口。
- `_orgLifecycle(status)` / `_orgLifecycleBadge(status)`：生命周期归一与徽标。
- `_orgIsExpired(validUntil, today)`：过期判定（可注入 today，避免时钟依赖）。
- `_orgUtf8Bytes(text)` / `_orgFormatBytes(n)`：UTF-8 字节统计与展示。
- `_orgBuildDispatchContext(input)`：下派预览与 payload 纯函数（上下文/知识/补充文本 → 分项字节、引用、超限）。
- `_orgDispatchPayloadContext(preview)`：预览结果 → dispatch payload，空选择返回 `''`。
- `_orgParseContentTab(hash)` / `_orgHashForContentTab(orgId, sub)`：深链读写。
- `_orgSelectableContexts(list)` / `_orgSelectableKnowledge(list)`：只允许 active 进入向导。
- `_orgContextActions(ctx, org)` / `_orgKnowledgeActions(item, org)`：写入口渲染（member 返回只读）。

## 渲染结构

```
_orgSetTab('context')
  └─ _loadOrgContext()                    # 并行拉 contexts + knowledge
      └─ _orgContentShell()               # 子页切换 + 容器
          ├─ _renderContextBody()         # brief/policy/decision 三区
          │   └─ _orgOpenContextEditor / _orgConfirmPublishContext / _orgConfirmDeprecateContext
          └─ _renderKnowledgeBody()       # 过滤条 + 列表
              ├─ _orgRenderKnowledgeList()
              └─ _orgOpenKnowledgeEditor / _orgConfirmPublishKnowledge / ...
```

## 下派向导

`_orgOpenDispatchWizard` 渲染目标仓库 + 补充说明 + 上下文勾选 + 知识过滤/勾选 + 预览区；
选择变化 → `_orgOnDispatchSelectionChange()` → 拉取缺失知识正文（并发 4、缓存）→
`_orgRenderDispatchPreview()` 更新总大小/分项/引用并切换提交按钮 disabled。
`_createOrgDispatch()` 用同一纯函数生成 `context`，超限或二次校验失败时阻断并提示。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 知识列表无正文，预览需逐条 detail（N+1） | 选中上限由 UI 勾选自然限制 + 并发 4 + 正文缓存 |
| 前端权限显隐与后端 repo admin 口径不完全一致 | 403 统一走 `localizeOrgError` 并提示只读 |
| 改 `_orgSetTab` 影响既有 Tab | 仅将 context 分支从占位替换为 `_loadOrgContext()`，其余分支不动 |
| 深链与 app.js `#view/param` 冲突 | 仅追加第 3 段，不改 view/param 解析 |