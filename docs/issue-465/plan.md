# Plan: Issue #465

## 根因
dashboard.py ops/overview 构造 runtimes 行时，远端引擎 `device_name` 直接取 `rc.device_name`（边端自报 OS 主机名），而 Workers 卡片标题取凭据自定义名 `rc.name`，两页显示不一致。

## 方案（展示层最小改动，单一数据源）
1. runtimes SELECT 增加 `rc.name AS rc_name` 字段。
2. 行构造处：`cid==0` → `_dev_name = rhost or "本机直连"`（不变）；远端 → `_dev_name = rc_name or rdev or ""`（name 优先，空回退 device_name）。
3. 前端字段名 `device_name` 不变，observability.js 渲染与 `_filterRuntimes` 筛选自动一致（核对无需改动）。

## 测试 seam
公开接口 `get_ops_overview(repo="", user=...)` 返回的 `runtimes.list[].device_name`——测试通过真实 db 构造凭据+引擎行断言取值（复用 test_issue_417 测试基建）。

## TDD 步骤
- 红：新增 tests/test_issue_465_runtime_device_name.py，断言远端引擎 device_name 取 rc.name、空 name 回退 device_name、本机直连取 host_name → 先对现状变红（现状取 device_name）。
- 绿：修改 dashboard.py 使测试通过；跑相关回归测试。

## 追加修复（用户澄清后，SRE 已根因定位）
PR #650 合入后用户反馈：同页上方 **Worker 设备卡片**仍显示原始设备名。

- 根因：observability.js `_workerCardHTML` 标题渲染 `wk.device_name || wk.name`（原始名优先），与 PR #650 修复的 runtimes 列 `rc.name` 优先口径相反。
- 方案：卡片标题显示名改为 `name 优先、空回退 device_name`；`_sortedWorkers` 排序键同步换为同一显示名口径（新增 `_workerDisplayName` helper，避免两处内联逻辑漂移）。
- 后端 workers 块（dashboard.py:340-357）`name=rc.name`、`device_name=rc.device_name` 字段本就齐备，无需后端改动。
- 关联字段核查结论（不改，记录依据）：
  - runtimes 设备筛选下拉（`:1635` 取 `rt.device_name`）：后端字段未变，与 runtimes 表列同源，口径自动一致。
  - `_devIpMap`/`_timelineDevIpMap`（`:2286-2314`/`:2350-2353`）：IP 映射按 `w.device_name` 键匹配引擎名前缀，保留原始字段不受影响。
  - 本机直连 worker（id=0，name=device_name=hostname）不受影响。
- TDD：红 tests/js/test_issue_465_worker_card_name.js（卡片标题自定义名/空回退不空名/本机不变/排序口径一致/渲染全链路/排序稳定 6 断言，对未修复代码 3 红）→ 绿实现 → 回归 observability 全量 js。
