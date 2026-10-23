# Plan：修复 runtime 列表搜索（Issue #436）

## 方案设计

全部改动集中在 `src/api/static/js/pages/observability.js`，围绕三个根因：

### F1：清除幽灵设备过滤（R1 主因）

`_filterRuntimes` 无法感知当前数据源的有效设备集合；在**渲染入口**（`renderOverview` / `_rerenderRuntimeTable` / `updateOverview` 共用的排序后数据）校验 `_rtDeviceFilter`：

- 在 `_filterRuntimes(runtimes)` 内部：若 `_rtDeviceFilter` 不在 `runtimes` 的 `device_name` 集合中，视为无设备过滤（不 return false）。
- 该修复对三条渲染路径（renderOverview 全量、_rerenderRuntimeTable 搜索重画、updateOverview 局部更新）统一生效，因为三者都调用 `_filterRuntimes`。
- select 的 selected 回显逻辑（1642 行）无需改：`_rtDeviceFilter` 失效后不匹配任何 option，浏览器自然回显「所有设备」，UI 与实际状态一致。

### F2：搜索词 trim（R2）

`_q = _rtSearch.trim().toLowerCase()`；trim 后为空串则跳过搜索过滤（`if (_q)`），保证「输入全是空格」等价于「无搜索」。

### F3：空状态专用文案（R3）

- 新增 i18n 键 `observability.no_matching_engine`（zh：`无匹配引擎`；en：`No matching engines`）。
- runtime 表空状态（`renderOverview` 1654 行与 `_rerenderRuntimeTable` 1236 行）使用新键；错误日志/事件视图的 `no_matching_log` 用法保持不变。

## 任务拆解

| # | 任务 | 验证 |
|---|------|------|
| T1 | `_filterRuntimes`：trim 搜索词 + 幽灵设备过滤失效 | Node 逻辑复现脚本断言 |
| T2 | 空状态文案 `no_matching_engine` + 两处替换 | i18n 键存在性检查（check-i18n-keys 思路：zh/en 双语对齐） |
| T3 | 语法校验 `node --check` | 通过 |
| T4 | jsdom DOM 级回归：搜设备名命中、空格命中、幽灵过滤清除、无匹配空态、清空恢复 | 复现脚本全绿 |
| T5 | 提交 + 推送 + PR | MR 评审 |

## 测试策略

- 复用排查期搭建的复现脚本（`/tmp/hypo_verify.js` 逻辑级 + jsdom DOM 级），修复后重跑并扩展断言；
- 断言不强软化：搜索命中数、空态文案、恢复全列表数量均为精确断言。
