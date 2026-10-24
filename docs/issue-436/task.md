# Task：修复 runtime 列表搜索（Issue #436）

## 执行记录

| # | 任务 | 状态 |
|---|------|------|
| T1 | `_filterRuntimes`：搜索词 trim + 幽灵设备过滤失效 | ✅ observability.js:1080 |
| T2 | 空状态文案 `no_matching_engine`（zh/en）+ 两处替换 | ✅ observability.js:1242,1660 + locales |
| T3 | 语法校验 + 缓存版本号 bump（v=67→68, I18N.init 35→36） | ✅ |
| T4 | 仓库固化测试 tests/js/test_issue_436_runtime_search.js（14 用例） | ✅ 14/14 |
| T5 | 既有测试回归（anomaly_page_size / observability_i18n / events_flow / page_size_162） | ✅ 无回归（page_size_162 的 7 失败为既有 harness 缺陷 stripRepoPrefix，与本次无关，stash 验证一致） |

## 排查结论（根因）

- R1（主因）：设备下拉残留失效设备名（设备失联/吊销后被后端过滤出列表），`<select>` 回显「所有设备」但 `_rtDeviceFilter` 仍持旧值 → 任何搜索（含清空）都 0 匹配——「搜设备名直接没一个匹配」。
- R2：搜索词未 trim，首尾空格全灭。
- R3：空状态文案复用「无匹配日志」。

生产验证：www.grapedev.cn 部署代码与仓库逐字节一致（md5 d52000ad…），后端 device_name 字段正常（94 台设备全 ASCII），排除后端/部署因素。

## 修复方式

`_filterRuntimes` 单点修复（三条渲染路径共用）：设备过滤值不在当前数据源设备集合中时视为未选择；搜索词 trim 后为空则不过滤。空状态专用 i18n 键。
