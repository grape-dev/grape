# Issue #1932 方案设计（Plan）

## 改动点（单处渲染）
`src/api/static/js/pages/runtimes.js` → `renderWorkers()`（L725）：

- 现状：`${rt.alias || engineDisplayName(rt)}` → 渲染 `codearts（ecs-xxx） v26.8.8 ✓`
- 目标：`${rt.alias || engineBaseName(rt)}` → 渲染 `codearts v26.8.8 ✓`

## engineBaseName(rt)（新增局部 helper，置于 renderWorkers 前）
与 `engineDisplayName()`（worker-name.js，#1310）的引擎名取值链保持一致：
`rt.display_name || rt.engine_name || rt.name || rt.instance_name`（trim）；
全空时回退 `engineDisplayName(rt)`（此时其值为凭据名/占位符，本身无括号），保证不出现空 chip。

## 不改动
- 公共 `engineDisplayName()` / `workerDisplayName()`（worker-name.js）。
- Engines 子页卡片（L674/L687）、引擎详情弹窗（L1439-1440）、alias 回写段、排序/筛选、跳转。

## 测试方案（TDD）
1. 新增 `tests/api/js/test_issue_1932_workers_engine_chip.js`（沙箱执行 runtimes.js，同 #1310 测试模式）：
   - 红：chip 无括号、仅 `引擎名 v版本 ✓`；多项 ` · ` 分隔；alias 优先；openEngineDetail 保留；
   - 回归：renderEngines / _renderEngineDetail 仍显示「引擎名（凭据名）」。
2. 更新 `tests/api/js/test_issue_1310_runtimes_display.js` 检查点 6：workers chip 期望由「引擎名（凭据名）」改为「引擎名」（#1932 口径收敛仅作用于该 chip）。
3. 运行：`node tests/api/js/test_issue_1932_workers_engine_chip.js` 与 `node tests/api/js/test_issue_1310_runtimes_display.js`。

## 风险与影响
纯展示字符串变更，无数据/接口影响；唯一调用面为 workers 设备卡片承载引擎行。
