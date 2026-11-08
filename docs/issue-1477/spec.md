# Spec: Issue #1477 — 用户设置 API Key 页面“最近使用时间”显示为东八区具体时间

## 需求
- 用户设置 → API Key 页面的“最近使用时间”由裸时间戳/原始字符串改为人类可读具体时间，固定按东八区（UTC+8）渲染，格式 `YYYY-MM-DD HH:mm:ss`。
- 未使用（`last_used_at` 为 `None` / 0 / 空）显示占位（本仓既有空态字符 `—`），不出现 `1970-01-01` 或异常值。
- 与页面其他时间字段（`created_at`，同接口同一格式化链路）口径一致。

## 现状（代码事实）
- 后端 `GET /api/settings/api-keys`（`src/api/routes/settings.py:586`）已通过 `normalize_event_time` 归一化：
  - `created_at` → UTC ISO 字符串（带 `+00:00`）；
  - `last_used_at` → 非空时归一为 UTC ISO，空值返回 `None`。
  - 存储层 `api_tokens.last_used_at`：使用时写入 `str(_now())`（秒级 Unix 时间戳字符串，`src/api/auth_token.py:229`），SQLite DDL 默认 `''`。
- 前端两处 API Keys 渲染直接裸拼 `k.last_used_at`（裸时间戳/ISO 原文）：
  - `src/api/static/js/pages/settings.js:294`（`loadApiKeys`，设置页 API Keys 卡片）
  - `src/api/static/js/pages/account.js:1317`（`_accountLoadApiKeys`，用户中心 API Keys Tab）
- 公共时间工具：`src/api/static/js/api.js:5` 的 `fmtLocalTime(utcStr)` —— UTC ISO / 带偏移字符串 → `YYYY-MM-DD HH:mm:ss`，但按**浏览器本地时区**渲染，非固定 UTC+8。

## 范围
- In：上述两处“最近使用时间”展示层格式化 + 空值占位；`api.js` 新增公共 UTC+8 时间戳格式化工具（时间戳秒/毫秒自动判别、ISO 字符串兼容、0/空返回占位）。
- Out：后端接口/存储格式变更（服务端保持原始数据）；其他页面时间口径改造；`fmtLocalTime` 既有行为改动（避免波及全站其他调用方）。

## 技术方案
1. `api.js` 新增 `fmtTimestamp8(ts)`：
   - 空值（`''`/`null`/`undefined`）→ `''`（占位由调用方用 `||` 兜底为 `—`，与现有空态字符一致）；
   - 纯数字或数字字符串 → 按量级自动判别秒/毫秒（`< 10^11` 视为秒），`0`/负值返回 `''`；
   - ISO / 带时区字符串 → 解析为绝对时刻（含 `±HH:MM` 偏移换算，复用 `fmtLocalTime` 同源逻辑）；
   - 固定用 `toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false})` 取 UTC+8 各时间分量，拼为 `YYYY-MM-DD HH:mm:ss`；
   - 解析失败 → 原样返回（不破坏数据，不抛异常）。
2. `settings.js` / `account.js` API Keys 行渲染：`created_at` 与 `last_used_at` 统一走 `fmtTimestamp8`；`last_used_at` 空时渲染 `—`（不再条件省略整段，与“创建”并排展示，占位语义清晰）。
3. `index.html` 对 `api.js`/`settings.js`/`account.js` 的缓存版本号 `?v=N` 各 +1（仓库既有缓存失效惯例）。

## 验收标准映射
| 验收 | 实现 |
|------|------|
| YYYY-MM-DD HH:mm:ss 具体时间 | `fmtTimestamp8` 输出固定该格式 |
| 固定 UTC+8、跨环境一致 | `toLocaleString` 显式 `timeZone:'Asia/Shanghai'`，不依赖宿主时区 |
| 0/空值占位、无 1970 异常值 | 0/负/空 → `''` → 调用方渲染 `—` |
| 与其他时间字段口径一致 | 同接口 `created_at` 一并走 `fmtTimestamp8` |
| 测试 | 新增 `tests/js/test_issue_1477_apikey_time_format.js`（纯函数级）+ `tests/test_api_key_lifecycle.py` 补接口时间归一化用例 |

## 风险
- `Intl` 的 `timeZone:'Asia/Shanghai'` 在现代浏览器/Node 全量支持，无 1970 前历史时区歧义（中国 1988 后无夏令时，API Key 时间戳均为近期时刻，无边界风险）。
- 不改 `fmtLocalTime`，其他页面零影响。
