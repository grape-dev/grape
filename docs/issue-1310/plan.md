# Plan: Issue #1310 — 引擎显示名统一「引擎名（凭据名）」

## 方案
纯展示层改造 + 两处后端下拉数据源补字段。新增统一取值函数 `engineDisplayName(rt)`：
- 引擎名：`rt.display_name || rt.engine_name || rt.name || rt.instance_name`（dashboard 数据源 name=instance_name，故中间插入 engine_name）
- 凭据名：`rt.worker_name` trim；空 → i18n `common.unnamed_worker`（«未命名设备»），与 #701 workerDisplayName 同源
- 输出：`引擎名（凭据名）`（全角括号，对齐 Spec 文案）

## 关键数据源字段现状
- `GET /api/settings/agent-runtimes`（settings.py）：已有 `name/display_name/instance_name/worker_name/alias` → 前端直接可用
- `runtime-devices` 设备子列表 `runtimes`（settings.py _build_runtime_devices）：缺 `worker_name` → 需补（Workers chip 用）
- `GET /api/dashboard/...` agent_engines（dashboard.py）：`name` 被覆写为 instance_name、`cred_name` 实际是 owner_name → 补 `engine_name`（类型名）+ `worker_name`（凭据名）；`name` 保持 instance_name 不动（兼容现有消费）
- agents.py `pool`（_engines_by_ids）：缺 worker_name → 补（cid=0 回退 host_name，直接查 agent_runtimes.host_name）

## 测试策略（沿用仓库前端测试模式：提取逻辑镜像断言 + node 运行）
1. `tests/js/test_issue_1310_engine_display_name.js` — engineDisplayName 单测（正常/空凭据/占位/i18n 缺失兜底/本机直连 host_name/fallback 链）
2. `tests/js/test_issue_1310_engine_display_render.js` — runtimes.js 卡片标题/括号/复制值、Workers chip、详情弹窗标题、alias 编辑回写的镜像断言 + agents.js / autopilot.js / dashboard.js 下拉显示文本断言
3. 后端测试：`tests/test_issue_1310_engine_display_name.py` — dashboard agent_engines 含 engine_name/worker_name、agents 详情 pool 含 worker_name、runtime-devices 子列表含 worker_name、agent-runtimes 已有字段不变

## 实施顺序（TDD 红→绿分提交）
1. 红：worker-name.js engineDisplayName 单测（tests/js）— 当前函数不存在 → 红
2. 绿：worker-name.js 实现 engineDisplayName
3. 红：runtimes.js 展示位测试（镜像断言）— 旧逻辑不产生拼接名 → 红
4. 绿：runtimes.js 各展示位替换
5. 红：agents/autopilot/dashboard 下拉显示测试 — 红
6. 绿：agents.js/autopilot.js/dashboard.js + 后端 3 文件补字段
7. 回归：全量 python + node 测试