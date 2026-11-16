# Spec: 沙箱列表迁移至运行驾驶舱（Observability）——删云沙箱页面前置 2/2

- 日期：2026-10-29
- 状态：spec 定稿待开工
- 背景（用户规划终局）：去掉左侧「云沙箱」入口与沙箱平台列表页。前置第一步（platform-entry-
  migration，76c68ae32 已实施）已把平台管理操作迁到 Provider 详情页+Worker 设备卡片。
  本 spec 是第二步：把最后一个残留——**沙箱列表 tab**——迁到**运行驾驶舱**（用户裁定），
  迁完后云沙箱页只剩平台详情（深链进入），可安全删除。
- 用户裁定：宿主 = 运行驾驶舱（Observability 页）。

## 1. 迁移目标位置

```
运行驾驶舱（#observability）
├─ 总览
├─ 设备与引擎
│   ├─ 设备
│   ├─ 引擎
│   └─ 沙箱 ←（新子 Tab）
└─ 任务与日志
```

沙箱列表本质是执行资源观测（生命周期/失败原因/Event 回链），与驾驶舱"设备与引擎"
分区语义同族。

## 2. 迁移内容（从 sandbox.js 整块搬运）

| 组件 | 来源 | 去处 |
|---|---|---|
| 表格（Sandbox ID/Event 链接/平台/模板/引擎/State/初始化/创建人/时间/最后刷新） | sandbox.js sandboxes tab | observability.js 新子 Tab |
| 筛选器（Event 号精确/任务 ID、平台下拉、状态下拉含 setup_failed） | 同上 | 同上 |
| 失败原因弹窗（openSandboxError + 复制按钮） | 同上 | 同上（Vue 组件 → 驾驶舱是原生 JS，改函数式渲染） |
| 分页 | 同上 | 复用 observability 分页样式 |
| 数据端点 `GET /runtime/cloud-sandboxes` | 不动 | 直接调用 |

### 平台筛选下拉的数据源

现从 sandbox.js 的 `providers`（页面内状态）取——迁移后 observability 页没有该数据，
改为**进入沙箱子 Tab 时拉取** `/settings/sandbox-providers?page=1&page_size=100`（轻量，
仅取 id+name 做下拉；失败降级为仅"全部平台"）。

## 3. 改动清单

| 文件 | 改动 |
|---|---|
| observability.js | 新增「沙箱」子 Tab（设备与引擎分区）：表格+筛选+分页+失败弹窗 ~150 行 |
| app.js | 左侧导航：**删「云沙箱」入口**（VIEWS/noRepoViews/_nonRepoViews 移除 sandbox；导航文案删除） |
| sandbox.js | 删 sandboxes tab（switchTab 分支/loadSandboxes/筛选/表格/分页/弹窗数据段）；保留：providers tab（过渡）+ Provider 详情视图 + 深链路由（`#sandbox/provider/{id}` 保留——Worker 设备卡片入口） |
| i18n zh/en | 驾驶舱词条（沙箱 Tab 名/空态等）+ 删云沙箱导航词条 |

**注意**：`#sandbox` 路由本身保留（详情深链还在用），删的只是**左侧导航入口和
列表 tab 默认视图**。直接访问 `#sandbox`（无 /provider 参数）→ 显示平台列表
（存量入口，无导航可达；后续真要彻底删再评估无凭据 Provider 的可达性）。

## 4. 验收

1. 运行驾驶舱「设备与引擎」分区出现「沙箱」子 Tab：表格/筛选（Event/平台/状态）/
   分页/失败原因弹窗（setup_failed ⚠️ 点击）全部可用
2. Event 列链接跳转事件详情正常；平台筛选下拉含全部平台
3. 左侧导航无「云沙箱」入口；Worker 设备卡片「详情」按钮 → `#sandbox/provider/{id}`
   平台详情仍正常（模板操作/沙箱查看按钮跳驾驶舱沙箱 Tab）
4. 云沙箱页直接 hash 访问不再崩溃（平台列表兜底视图）
5. `viewSandboxes`（详情页「查看沙箱实例」按钮）→ 改跳 `#observability`（沙箱子 Tab，
   可带平台筛选参数 `#observability/sandboxes?provider=38`——按驾驶舱现有 URL 惯例）
6. 回归：node --check ×2 + 相关 pytest + 渲染冒烟

## 5. 改动量

- observability.js ~150 行（大头）
- sandbox.js 净删 ~120 行
- app.js ~3 行
- i18n ~6 词条
- 预估半天
