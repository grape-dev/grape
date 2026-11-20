# Spec: Issue #1208 Worker 标签化调度（selector，精简定稿版）

> 需求权威源 = Issue #1208 正文 §2（定稿 Spec，D1~D20 全部闭环）。本文件为开发落地视角的摘录与映射，不改动任何裁定。

## 1. 目标

场景 node 的引擎绑定支持三模式（同 node 互斥，保存端双非空拒绝）：
- **selector 模式（新场景默认）**：`engine_selector={"tags":[...]}` 按标签特征圈池，扩容新引擎自动进池。
- **uid 模式**：`engine_runtime_uids` 逐台手选（#646 uid 轨）。
- **id 模式（legacy）**：`engine_runtime_ids` 存量只读兼容；保存时透明升格 uid。

与 #658 role_tags 正交：#658 管"会不会"（claim 硬拒），selector 管"是不是"（特征圈池，入队过滤）。

## 2. 代码映射（实现落点，基于 arch-optimization 基线）

| Spec §2 | 现状锚点 | 改动 |
|---|---|---|
| §2.1 nodes_json engine_selector / engine_runtime_uids | `src/api/routes/scenes.py` SceneNodeInput(:1704)、`src/api/pipeline_config.py` get_scene_engine_binding(:814) | 模型加两字段；binding 读侧 uids 优先回落 ids |
| §2.1 agent_tasks.pool_selector TEXT NULL | `src/api/db_setup.py`（迁移号段 20261105xxxx） | 幂等加列迁移（MySQL TEXT 无 DEFAULT，#627 教训） |
| §2.1 agent_tags 派生 engine:/scope:/scene: | `src/api/routes/runtime.py` register(:224)/heartbeat(:463)、engine_capabilities 行式标签 | 派生冒号标签行（注册/心跳写）；platform:/worker: 数据先行暂缓消费，label: 预留 |
| §2.2 匹配语义（组内 OR + 组间 AND，无 op） | `src/runtime/engine_select.py` | 新增 `_selector_match`（D14 原式） |
| §2.3 入队链 | `src/api/event_pipeline.py`(:2644 get_scene_pool_engine_ids 挂点) | 新增 `get_scene_engine_selector` + 入队三分叉 + 零命中 fail-closed 文案分流（单函数） |
| §2.4 claim 六层守卫 | `src/runtime/service.py` claim_by_tag(:1684)、_check_pool_engines_reachable(:37)、_check_stale_queued_tasks(:3904)、_fail_task_pool_unreachable(:140) | ③级 selector 检查插入（能力>角色>selector>名单>身份>隔离）；统一日志前缀 `claim guard [level]`；uid 轨空值不误杀（#1011 既有） |
| §2.5 前端 | `src/api/static/js/pages/dashboard.js` _openEngineModalImpl(:1731) | 模式 radio（新场景默认按标签）+ 三组复选（UI 标签「范围」）+ 命中预览分层标注 + D11 uid 输出 |
| §2.6 保存端三层 | `src/api/routes/scenes.py` _validate_scene_engine_ids(:1739)（服务端权威）+ `src/api/routes/agents.py`(:446 PUT) | L1 硬阻断 400（未知前缀文案="未实现的维度，自定义请用 label: 前缀"）；L2 前端 confirm 不阻断；徽标复用 engine-status |
| §2.7 T11 存量 uid 升格 | `src/api/db_setup.py` backfill 迁移 | 幂等/分批；ids 有效→uids+清 ids；悬空跳过+报告；不生成 selector |

## 3. 关键语义

- **入队三分叉判据**：`engine_selector` 非空 → selector 路径（落 pool_selector，pool_engine_ids 置空）；`engine_runtime_uids` 非空 → uid 池路径；否则 legacy id 路径（行为零变化）。
- **匹配**：`_selector_match(selector, engine_tags)`：按冒号前缀分组，组内 OR、组间 AND、不勾的组不约束；空 selector 恒 True。
- **fail-closed 文案分流（单函数）**：配置死 = "无引擎满足标签 X——去改场景"（引擎从来没带过该标签）；瞬时死 = "命中引擎当前均不在线，可能正在重启——稍后 redispatch 重试"；redispatch 重校验（重派不信旧判断）。
- **健康尺子**：与 #259 同一把尺子（status=online + 心跳新鲜 + healthy + probe ok）。
- **L1 值域**：engine:/scope: 固定硬编码校验（engine 取引擎类型名值域、scope∈{global,user,repo}）；scene 动态查 scene_market 活跃行（空回退 default）。
- **agent_tags 派生存储**（D4）：capabilities 行式标签 + `get_tags` 过滤冒号行（`NOT LIKE '%:%'`）；`all` 为唯一平铺保留字。

## 4. 兼容性红线（§4.1 对账）

- id 路径代码不动，分叉判据=selector 非空 → 存量 id 场景行为逐字节不变（stash 基线对照）。
- #658 角色守卫在 selector 之前；SRE scope 过滤在 selector 之后叠加，语义不变。
- selector 任务两池列均空 → claim 空值守卫放行（验收 4 专项）。
- `all` 广播语义不变。
- 存量 worker 协议零改动（claim 仍按 runtime_id 寻址）。

## 5. 明确不做（§2.8）

表达式/affinity/权重/SRE 专属 selector/角色维度标签/selector 引用 uid 匹配/资源规格维度/op 字段。
