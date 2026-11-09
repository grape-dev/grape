# Spec: Issue #475 — Agent Runtime 引擎列表异常状态直接可见 + 非健康状态帮助提示

> 来源：Issue #475 Spec Document（issue agent 发布，VOD 跨流水线复核确认 ready）。本文件为开发落位副本。

## Objective
- Agent Runtime 页面（`src/api/static/js/pages/runtimes.js`）引擎列表在**不点开标签页/详情弹窗**的情况下直接可见异常状态标识。
- 每个非健康状态旁提供帮助小按钮（? 图标），悬浮展示按状态类型区分的处理建议（复用页面已有 `data-tooltip` 悬浮样式）。
- 用户：工作台前端使用者（引擎管理员/运维）。

## Scope
### In Scope
- `renderEngines` 引擎卡片：非健康（offline / recently_lost / about_to_gc / 健康分过低 / health_heartbeat_alive=false / health_consecutive_failures>0）时在 `_healthBadge` 旁渲染 ? 帮助按钮，并对异常态卡片做红色描边强化。
- `renderWorkers` 设备卡片：`_healthBadge` 非健康旁追加同一帮助按钮。
- 新增 `_healthHelpHint(rt)` 状态→建议 i18n key 映射、`_helpBtn(key)` 渲染函数。
- locales `src/api/static/locales/{zh,en}/runtimes.json` 新增 `runtimes.health.hint.*` 双语文案。

### Out of Scope
- 后端健康数据采集/接口改动（字段前端已可用）。
- 健康分计算逻辑、重置健康分功能调整。
- 引擎详情弹窗内部信息重构。

## Acceptance Criteria
- [ ] 存在非健康引擎时，Engines 列表卡片不点开任何标签页/详情即可见醒目异常标识
- [ ] 每个非健康状态旁有帮助小按钮，悬浮展示对应处理建议（探活连续失败 → 建议登录机器定位）
- [ ] 健康引擎不显示异常强化标识与帮助按钮
- [ ] Workers 设备卡片非健康状态同样有帮助按钮
- [ ] zh/en 双语文案齐全，无写死中文
- [ ] 现有功能（详情弹窗、重置健康分、筛选排序分页）不回归
