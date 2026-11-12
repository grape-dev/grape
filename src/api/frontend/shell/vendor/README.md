# Vendored 第三方前端库

不走 CDN：内网/离线部署必须自持（见 docs/weekly-07/observability-overview-page-design.md §3）。

## echarts.min.js

- 包：`echarts@5.6.0`（Apache-2.0，见同目录 `echarts.LICENSE`）
- 来源：npm 官方包 `npm pack echarts@5.6.0` → `package/dist/echarts.min.js`
- sha256（前 16 位）：`bf4a223524e40b77`
- 用途：可观测性「总览」分区的图表（任务量与结果、Token 消耗、模型占比等）
- 升级方式：重新 `npm pack echarts@<ver>` 覆盖本文件并同步 LICENSE 与上面的 sha256/版本

_审计提示：本目录只放第三方产物，不修改其内容；如需补丁请以 wrapper 形式放在业务侧。_

- `faro-web-sdk.iife.js` — `@grafana/faro-web-sdk@2.13.0` IIFE bundle
  （jsdelivr `dist/bundle/faro-web-sdk.iife.js`，2026-11-04；
  sha256 `85741c45fd1fd487…`），浏览器端 RUM/日志/异常采集，
  上报至 faro.receiver（/faro/collect 反代，spa.py）
- `faro-web-tracing.iife.js` — `@grafana/faro-web-tracing@2.13.0` IIFE bundle
  （jsdelivr `dist/bundle/faro-web-tracing.iife.js`，2026-11-04；
  sha256 `8ca0f7dcc4d500d5…`），TracingInstrumentation（fetch 注入 traceparent）
- 升级方式：重新取同名 dist bundle 覆盖，并同步上面的版本与 sha256 前缀
