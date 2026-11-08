# Plan: Issue #1477 — API Key“最近使用时间”UTC+8 格式化

## 切片拆解（TDD，一竖切片一红一绿）

### 切片 1：公共格式化函数 `fmtTimestamp8`（api.js）
- 测试 seam：`node tests/js/test_issue_1477_apikey_time_format.js` 用 `new Function` sandbox 从 `api.js` 源码提取 `fmtTimestamp8` 纯函数直接断言（复用 `tests/js/test_issue_1229_efficiency_board.js` 的 sandbox 加载模式）。
- 红：断言 秒级/毫秒级/ISO/带偏移字符串/0/空/非法输入 的输出。
- 绿：`api.js` 新增 `fmtTimestamp8`（固定 `Asia/Shanghai`，`YYYY-MM-DD HH:mm:ss`）。

### 切片 2：settings.js API Keys 卡片接入
- 测试 seam：同一 node 测试文件以 sandbox 加载 `settings.js`，mock `API.get` 返回含 秒级时间戳/None 的 keys，断言 `#sApiKeys` innerHTML 含格式化时间与 `—` 占位。
- 红 → 绿：`loadApiKeys` 渲染行接入 `fmtTimestamp8`。

### 切片 3：account.js API Keys Tab 接入
- 测试 seam：同上加载 `account.js`，mock `API.get`，断言 `#accApiKeyList` innerHTML。
- 红 → 绿：`_accountLoadApiKeys` 接入。

### 切片 4：后端契约回归（Python）
- `tests/test_api_key_lifecycle.py` 补 1 用例：直接插 DB 秒级时间戳后 `GET /api/settings/api-keys`，断言 `last_used_at` 归一为 UTC ISO、`created_at` 幂等归一、空值保持 `None`（接口契约护栏，展示层的前置）。

### 切片 5：缓存版本号
- `index.html`：`api.js?v=31→32`、`settings.js?v=36→37`、`account.js?v=48→49`（原子变更，随实现提交）。

## 提交计划
1. `test(#1477): API Key 最近使用时间格式化 先写失败测试`（切片 1-3 node 测试）
2. `feat(#1477): API Key 最近使用时间按 UTC+8 格式化 + 空值占位`（api.js + settings.js + account.js + index.html）
3. `test(#1477): api-keys 接口时间归一化回归`（切片 4）

## 回归
- `node tests/js/test_issue_1477_apikey_time_format.js`
- `python3 -m pytest tests/test_api_key_lifecycle.py tests/test_time_utils.py -q`（如存在 time_utils 测试）
