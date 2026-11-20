# Spec: Issue #2236 组织上下文与知识库管理 UI

## Objective

把组织工作台的 context 占位页替换为可运营的「组织上下文 + 知识库」双子页，并让跨仓下派向导
可以勾选本次要携带的上下文与知识、预估注入大小，超限时阻断提交。

- 用户：owner/admin 管理（创建/编辑 draft、发布、废弃、归档），member 只读。
- 成功标准：三类上下文（brief/policy/decision）与组织知识全生命周期在 UI 可完成；下派携带
  内容与预览一致；member 看不到任何写入口；前端门禁用例通过且不回归既有组织工作台用例。

## Scope

### In Scope

- context Tab：
  - 子页 1「组织上下文」：brief / policy / decision 三区列表，字段 title、kind、
    `lifecycle_status`、version、owner、updated_at、valid_until。
  - 创建/编辑：标题、正文 Markdown、owner（成员选择）、valid_until；policy/decision 支持 key。
  - 发布/废弃二次确认；生命周期徽标；409 状态冲突提示；发布成功提示「旧 active 已自动降级」。
  - 子页 2「知识库」：kind/status/tag/关键词过滤，列表 name、kind、`lifecycle_status`、tags、
    owner、valid_until、last_reviewed_at；创建/编辑；发布/废弃/归档；已过期高亮。
  - 两子页均有空态/加载/错误态。
- 下派向导：
  - 仅加载 active 上下文与 active 知识；
  - 上下文按 brief/policy/decision 分组勾选；知识支持 tag/kind/关键词过滤并多选，显示引用条数；
  - 预览总大小/分项大小/引用条数；超过 `ORG_DISPATCH_CONTEXT_LIMIT_BYTES`（64KB）禁用提交；
  - dispatch payload 的 `context` 扩展为
    `{text, org_context_ids, knowledge_ids, knowledge_tags, estimated_bytes}`，无选择时保持纯字符串以兼容旧数据。
- 深链：`#organizations/<id>/context`、`#organizations/<id>/knowledge`（同时解析
  `#org=<id>&tab=context|knowledge` 形式），刷新后保持。
- 中英文 i18n；`tests/js/test_org_context_ui.js` 接入 windows-smoke 前端门禁。

### Out of Scope

- 后端接口/存储/发布逻辑（#2150 已合入），本单不新增后端接口。
- #2235 服务端预览/注入 API（未合入）：按客户端估算降级，接口就绪后仅替换数据源。
- 语义检索/向量搜索、跨组织共享、权限模型调整。

## Contract（复用 #2150 已合入接口）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/orgs/{id}/context?kind=&status=` | 列表，返回 `{contexts:[...]}` |
| POST | `/api/orgs/{id}/context` | 创建 draft（kind/key/title/content/owner_id/valid_until） |
| GET | `/api/orgs/{id}/context/{cid}` | 详情 |
| PUT | `/api/orgs/{id}/context/{cid}` | 更新（title/content/owner_id/valid_until） |
| POST | `/api/orgs/{id}/context/{cid}/publish` | 发布，自动降级同 key 旧 active |
| POST | `/api/orgs/{id}/context/{cid}/deprecate` | 废弃 |
| GET | `/api/orgs/{id}/knowledge?kind=&status=&tag=&q=` | 列表，返回 `{knowledge:[...]}`（无正文） |
| POST | `/api/orgs/{id}/knowledge` | 创建 draft |
| GET | `/api/orgs/{id}/knowledge/{kid}` | 详情（含正文，供预览） |
| PUT | `/api/orgs/{id}/knowledge/{kid}` | 更新 |
| POST | `/api/orgs/{id}/knowledge/{kid}/publish` | 发布，自动降级 |
| POST | `/api/orgs/{id}/knowledge/{kid}/deprecate` | 废弃 |
| POST | `/api/orgs/{id}/knowledge/{kid}/archive` | 归档（终态） |

状态读取 `lifecycle_status`（draft/active/deprecated/archived），查询参数用 `status`。

## Commands

```bash
node tests/js/test_org_context_ui.js
node tests/js/test_org_workbench.js
```

## Acceptance Criteria

- [ ] brief/policy/decision 三区可查看、创建、编辑（draft）、发布、废弃，状态/版本正确展示
- [ ] 发布后旧 active 自动降级提示正确；409 状态冲突有明确提示
- [ ] 知识可按 kind/status/tag/关键词过滤，可创建/编辑/发布/废弃/归档，过期项高亮
- [ ] 下派向导可选 active 上下文与知识，预览总大小/分项/引用条数，超限禁用提交
- [ ] dispatch payload 含 `org_context_ids`、`knowledge_ids`、`knowledge_tags` 且兼容旧字符串 context
- [ ] member 不渲染任何写操作入口；403 时降级只读并提示
- [ ] Tab 深链刷新后保持；zh/en 文案齐全（键集合一致）；空态/加载/错误态可用
- [ ] `node tests/js/test_org_context_ui.js` 通过并接入 windows-smoke；既有组织工作台用例不回归