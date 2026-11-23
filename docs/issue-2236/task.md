# Task Breakdown: Issue #2236 组织上下文与知识库管理 UI

> 每个竖切片先写失败用例（test 提交），再落最小实现（feat 提交）。

## Slice 1 — API 封装 + 错误码 + i18n 骨架

- [ ] `tests/js/test_org_context_ui.js`：断言 `API.orgContext` / `API.orgKnowledge` 方法齐全、
      路径正确、新错误码可本地化、zh/en 键集合一致（红）
- [ ] `api.js`：新增 `orgContext`（list/get/create/update/publish/deprecate/remove）与
      `orgKnowledge`（list/get/create/update/publish/deprecate/archive/remove），统一 `_orgCatch`
- [ ] `localizeOrgError` 增补 `INVALID_CONTEXT_KIND` / `INVALID_NAME` / `INVALID_CONTENT` /
      `CONTENT_TOO_LARGE` / `SENSITIVE_CONTENT_REJECTED` / `INVALID_STATUS_TRANSITION` / `PUBLISH_BUSY`
- [ ] zh/en `organizations.json` 补齐 lifecycle/context/knowledge/dispatch/errors 文案

## Slice 2 — 上下文 / 知识双 Tab

- [ ] 测试：生命周期归一/徽标、过期判定、权限显隐（member 无写按钮）、深链解析（红）
- [ ] `_orgSetTab('context')` → `_loadOrgContext()`，子页 context/knowledge
- [ ] 上下文三区列表 + 创作/编辑弹窗 + 发布/废弃二次确认 + 409 提示
- [ ] 知识过滤（kind/status/tag/q 防抖）+ 列表 + 创建/编辑 + 发布/废弃/归档 + 过期高亮
- [ ] 空态/加载/错误态；深链 `#organizations/<id>/<sub>` 刷新保持

## Slice 3 — 下派向导选择与预览

- [ ] 测试：`_orgBuildDispatchContext` 分项/超限、`_orgDispatchPayloadContext` 兼容空选择、
      active-only 选择器（红）
- [ ] 向导加载 active 上下文/知识，勾选、知识过滤、引用条数
- [ ] 客户端 UTF-8 估算预览（总大小/分项/引用），超限禁用提交
- [ ] dispatch payload 携带 `org_context_ids` / `knowledge_ids` / `knowledge_tags` / `estimated_bytes`

## Slice 4 — 门禁与回归

- [ ] `.github/workflows/windows-smoke.yml` 前端步骤追加 `node tests/js/test_org_context_ui.js`
- [ ] `node tests/js/test_org_workbench.js` 回归通过
- [ ] 自审查（5 轴 + pre-pr 增量检查）→ 建 PR → A2A QA

## 验收核对

- [ ] 三类上下文全生命周期可用
- [ ] 知识过滤/生命周期/过期高亮可用
- [ ] 下派预览与超限阻断正确，payload 字段齐全
- [ ] member 无写入口；403 降级只读
- [ ] 深链、zh/en、空/载/错态齐备；node 用例通过并进门禁