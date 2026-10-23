# Spec：修复运行驾驶舱「设备与引擎」页 runtime 列表搜索（Issue #436）

## 目标

修复运行驾驶舱「设备与引擎」页 runtime 列表搜索功能：输入设备名（或关键词）能正确过滤并返回匹配的 runtime 记录。

## 现状与根因分析（2026-10-22 实测排查）

**排查过程**：
- 生产站（www.grapedev.cn）与仓库（功能分支 / origin/main）代码逐字节一致（md5 `d52000ad…`）；
- 后端 `/api/dashboard/ops/overview` 返回的 `runtimes.list[].device_name` 字段有值且全 ASCII（生产库 94 台设备验证）；
- `_filterRuntimes`（observability.js:1080）过滤字段含 `name/device_name/version/mode`，纯逻辑复现「搜设备名」在正常路径下能命中。

**可复现缺陷**（与「搜设备名直接没一个匹配」吻合）：

| # | 缺陷 | 复现 | 现象 |
|---|------|------|------|
| R1 | `_rtDeviceFilter` 残留失效设备名：设备下拉选中的设备后来失联/吊销（7 天过滤）后不在 `_devices` 列表，`<select>` 无匹配 option 回显第一项「所有设备」，但变量值仍为旧设备名，过滤条件 `_dev !== _rtDeviceFilter` 对所有行成立 | jsdom/逻辑复现：匹配数 0 | 搜索任何词、甚至清空搜索框都 0 匹配——「直接没一个匹配」；且 UI 显示"所有设备"造成用户以为没有过滤 |
| R2 | 搜索词未 `trim()`：前后空格（输入法/复制粘贴常见）直接参与 `includes` 匹配 | 逻辑复现：搜 `" build "` 匹配数 0 | 看起来「搜了设备名没一个匹配」 |
| R3 | 空状态文案复用 `no_matching_log`（"无匹配日志"），语义错误 | 静态检查 observability.js:1654 | 无匹配时提示误导（验收标准要求空状态提示） |

## 期望行为

- 搜索框输入设备名/关键词 → 列表按引擎名/设备名/版本/模式字段过滤并展示匹配项
- 搜索词自动忽略首尾空格
- 设备下拉残留失效值时自动失效（不产生"幽灵过滤"），UI 与实际过滤状态一致
- 无匹配时展示专用空状态提示「无匹配引擎」
- 清空搜索框后恢复完整列表
- 搜索不引起页面报错或渲染异常

## 验收标准

- [ ] 输入已知设备名能返回匹配 runtime 项
- [ ] 输入带首尾空格的设备名能返回匹配项
- [ ] 设备下拉残留失效设备名时，搜索仍正常（幽灵过滤被清除）
- [ ] 无匹配时显示空状态提示（专用文案，非"无匹配日志"）
- [ ] 清空搜索恢复全列表
- [ ] 页面无报错、渲染正常

## 优先级 / 类型

P1 · bug · 前端搜索过滤逻辑修复（`src/api/static/js/pages/observability.js` 单文件），无外部依赖

## 影响范围

- 文件：`src/api/static/js/pages/observability.js`（`_filterRuntimes` / `renderOverview` / `_rerenderRuntimeTable` / `updateOverview` 四处共用过滤路径）
- 不涉及后端与数据库
