# Spec: Issue #1310 — Agent 运行时界面引擎显示名统一为「引擎名+凭据名」

> 来源：Issue #1310 评论区 Spec Document（issue agent 已评级 ready），本文件为开发落地引用。

## 目标
Agent 运行时相关界面（Engines 列表卡片、引擎详情弹窗、Workers 承载引擎 chip、各引擎绑定/配置下拉）的引擎显示名统一为「引擎名（凭据名）」，不再回退显示设备固有名称派生的 instance_name（如 opencode-worker-demo-0003）。

## 关键口径（与 Spec Document 一致）
- 引擎名：`rt.display_name || rt.name`（agent-runtimes 出口 name 已 to_display）
- 凭据名：`rt.worker_name`（= runtime_credentials.name，与 #701 workerDisplayName 同源）；空 → i18n `common.unnamed_worker`（未命名设备）
- 本机直连（credential_id=0）无凭据名：沿用 `worker_name=host_name` 兜底
- 有 alias 优先 alias，括号/副标题显示「引擎名（凭据名）」
- option value / instance_name / resolve_instance_id 等底层标识逻辑不变
- 复制值、排序、搜索关键字同步适配（保留 instance_name 兼容匹配）

## 改动清单（Diff Scope）
| 文件 | 改动 |
|---|---|
| src/api/static/js/worker-name.js | 新增 engineDisplayName(rt)，抽出共用占位 helper |
| src/api/static/js/pages/runtimes.js | L196/197 排序、L523 type 派生兜底、L642/655/656 卡片标题/括号/复制、L693 Workers chip、L1407/1408 弹窗标题、L1740/1747/1750 alias 编辑回写 |
| src/api/static/js/pages/agents.js | L130 绑定下拉显示文本改 engineDisplayName（value 保持 instance_name） |
| src/api/static/js/pages/autopilot.js | L633 搜索 hay 增补显示名、L647 引擎条目主/副标题去 instance_name |
| src/api/static/js/pages/dashboard.js | L1872 搜索 hay 增补、L1882 引擎条目显示改 engineDisplayName（副行去 device 固有名称） |
| src/api/routes/agents.py | pool 补 worker_name 字段（凭据名；cid=0 回退 host_name） |
| src/api/routes/dashboard.py | agent_engines 补 engine_name（引擎类型名）+ worker_name（凭据名；cid=0 回退 host_name） |
| src/api/routes/settings.py | _build_runtime_devices 设备子列表 runtimes 补 worker_name（凭据名；cid=0 回退 host_name），供 Workers 承载引擎 chip 拼接 |

## 验收
- 全部展示位显示「引擎名（凭据名）」，无设备固有名称残留
- 凭据名空 → 「未命名设备」；本机直连 → host_name
- 底层标识（option value / instance_name / resolve_instance_id）不变
- 现有前端 + 后端测试全绿；新增 engineDisplayName 单测与渲染位覆盖