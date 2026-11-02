# Issue #501 设计文档：项目场景页全链路分页 + 引擎探测按需化

> Issue #501 | 分支 feat/scene-list-pagination
> 状态：已实现，测试全绿（tests/test_scene_list_pagination.py + tests/test_issue_404_create_task_offline_hint.py）

## 1. 问题概述

场景数量增长后无任何分页保护，全链路线性膨胀：

| # | 环节 | 问题 |
|---|------|------|
| P1 | `GET /api/scenes` | 全量返回，无 page/page_size/total |
| P2 | `GET /api/scenes/with-pipeline` | 全量返回 + **N 场景 × M 节点**引擎在线探测（拉取即全量预探测） |
| P3 | 前端项目场景页 | 全量渲染卡片，无分页；30s 自动刷新反复付全量成本 |
| P4 | 需求中心弹窗 | 5 个只要「场景名+ID」的下拉框也在为 N×M 探测买单 |

核心浪费：with-pipeline 的 9 个前端调用点中，只有需求中心弹窗 1 个真需要引擎探测结果。

## 2. 方案（分页 + 探测按需化，分到 SQL 层）

### 2.1 场景清单（改动的代码单元）

| 单元 | 改动 |
|------|------|
| `src/api/pipeline_config.py` `_query_scenes_from_db` | 加可选 limit/offset：SQL `LIMIT/OFFSET`（SQLite/MySQL 通用），`ORDER BY id` 稳定排序；不带 limit 全量（其余 9 个共享调用点零改动） |
| `src/api/pipeline_config.py` `_count_scenes_from_db` | 新增：COUNT 查询取 total，不拉 nodes_json |
| `src/api/pipeline_config.py` `get_scenes` | 透传可选 limit/offset，签名向后兼容 |
| `src/api/routes/scenes.py` `list_scenes` | page/page_size 可选 Query 参数；total=COUNT；带 page 时 SQL 分页 |
| `src/api/routes/scenes.py` `list_scenes_with_pipeline` | 同上；enrich（skill display name）只处理当前页；**移除 offline_nodes/sre_unbound/all_offline 三字段** |
| `src/api/routes/scenes.py` `get_scene_engine_status` | 新增 `GET /scenes/{scene_id}/engine-status`：单场景按需探测，节点经 `get_pipeline_config` 单行查询（DB-first + 60s 缓存） |
| `src/api/routes/scenes.py` `_scene_engine_status` | 新增 helper：探测语义与原 with-pipeline 内联版逐行一致（见 §3） |
| `src/api/static/js/app.js` | dash 加载 /scenes 带 page/page_size；透传 `_scenesTotal`；切换仓库页码归位 |
| `src/api/static/js/pages/dashboard.js` | 数量角标用跨页 total；渲染 `_renderScenePager` 分页条；with-pipeline 与 /scenes 同页参数；场景创建后刷新带页参数 |
| `src/api/static/js/pages/issues.js` | 创建弹窗：下拉只取场景名；onchange + 默认选中项按需调 engine-status 显示警告条（stale guard） |
| `help/api/06-scenes.md` | with-pipeline 出参更新；新增 engine-status 端点文档块 |

### 2.2 关键设计决策（含被否决的备选）

- **SQL 分页 vs 路由层内存切片**：必须 SQL 层。内存切片只省渲染/探测，DB 全量读 + 每行 nodes_json 反序列化照旧，30s 自动刷新反复付全量成本——是把问题挪到 DB 层而非消除。
  - 方言顾虑不成立：LIMIT/OFFSET 双方言通用（该项目的双方言坑只在 PRAGMA/SHOW COLUMNS DDL 探测）。
  - 页边界漂移顾虑不成立：`UNIQUE(scene_id, repo_key)` 约束保证无重复行（Python 层 seen_ids 去重是迁移期防御残留，实数据零重复）。
- **探测按需化 vs 保留在 with-pipeline**：移出。受影响的消费方只有需求中心弹窗 1 个，同分支同步改造，无空窗。N×M → 1×M。
- **engine-status 取节点**：`get_pipeline_config`（单行查询 + 60s 缓存），不再全量拉场景列表遍历找 id。

## 3. 场景用例

### 3.1 后端行为用例（tests/test_scene_list_pagination.py）

| 用例 | 断言 |
|------|------|
| T1-01 不带 page 全量 | 返回 25 条 + total=25（向后兼容） |
| T1-02 page=1&page_size=20 | 20 条、total=25、首条 pg-001、无 pg-021（SQL 分页正确切片） |
| T1-03 page=2&page_size=20 | 余数 5 条 |
| T1-04 page 越界 | 空列表、total 仍 25 |
| T2-01 with-pipeline 分页 | 10 条 + total + 每条含 pipeline |
| T2-02 with-pipeline 全量 | 25 条 + total |
| T2-03 探测字段移除 | 响应每条不含 offline_nodes/sre_unbound/all_offline |
| T2-04 engine-status 可达 | 单场景返回 scene_id + 空节点降级不标记 |

### 3.2 引擎探测语义用例（tests/test_issue_404_create_task_offline_hint.py，#404 语义随 #501 迁移到新接口）

| 用例 | 断言 |
|------|------|
| 全灭场景 | all_offline=True、offline_nodes=[coding, qa] |
| pool 健康 | 不进 offline_nodes |
| 无绑定节点 | 广播语义，不参与判定 |
| enabled=False | 跳过 |
| SRE 空绑定 | sre_unbound=True、all_offline=True（「未选机器」话术） |
| 探测异常 | 降级不标记（不挡创建入口） |
| 场景不存在 | all_offline=False |

### 3.3 前端契约用例（源码断言，沿用 test_issue_372 先例）

| 用例 | 断言 |
|------|------|
| T3-01 | app.js /scenes 请求带 &page=、有 _sceneListPage 状态 |
| T3-02 | dashboard.js 有 _renderScenePager，函数体用 sceneTotal 计算页数 + _sceneGotoPage 翻页 |
| T3-03 | 数量角标用 ${sceneTotal}（跨页总数） |
| T3-04 | with-pipeline 请求带 &page=（与 /scenes 页对齐） |
| T3-05 | 切仓库页码归位（_sceneListRepo） |
| T4-01 | 弹窗 onchange 联动 _updateSceneOfflineWarning、调 /engine-status |
| T4-02 | 下拉选项不再挂 ⚠️ 预标记 / opt._all_offline 属性 |
| T4-03 | 异步探测 stale guard（sel.value !== sceneId） |

## 4. 影响访问与兼容性说明

**A. 对现有 API 调用方：零**
- page/page_size 全可选；不带 page 时两个列表接口返回与现状一致，仅多 total（纯增量）
- get_scenes 不带 limit 全量语义不变，9 个共享调用点（prompt_builder / dashboard / issues_center / user.db / scene_sync / 测试）零改动

**B. 唯一破坏性变更：with-pipeline 移除 offline_nodes/sre_unbound/all_offline**
- 受影响消费方只有需求中心弹窗（issues.js:1631 一处），同分支同步改造，无空窗
- 弹窗行为变化：⚠️ 从下拉选项预标改为选中后警告条（含默认选中项主动探测一次，不弱于原行为）

**C. 不受影响（明确排除）**
- DB schema 零改动；探测函数 get_scene_pool_engine_ids 逻辑不动，只改调用时机和范围
- 场景市场 /scenes/market、场景 CRUD、流水线页、需求列表/详情、webhook 链路均不涉及

**D. 性能（正向）**
- 场景页打开：DB 只读当前页行 + COUNT；N×M 探测消失
- 需求中心开弹窗：零探测，选中场景时 1×M

## 5. 验证记录

- 两个用例集 70 passed（含 test_issue_479_scene_label / test_linked_repos 回归）
- tests/test_scene_repo_model.py 与 tests/test_api_docs_sync.py 的 5 个失败经 git stash 基线对比确认为 weekly-09 固有，与本分支无关

## 5. 深审修正（2026-11-01，全量走查 + 3001 端口真机实测）

| 级别 | 问题 | 修正 | 验证 |
|------|------|------|------|
| P1 | engine-status 对不存在场景静默回退 pipelines.json default，误报 sre_unbound=true | cfg.fallback 或 scene_id 不匹配 → 直接返回 all_offline=False | 实测 /scenes/nosuch/engine-status 三字段全 False；t5_01 |
| P2 | 创建场景后停在第 1 页看不到新卡片（新场景 id 最大在末页） | 创建成功后按 total 计算末页，非末页则重拉一次跳末页 | t5_05（源级契约） |
| P3 | engine-status 只认显式 repo 参数，与列表的会话级解析（#534）不一致 | 改用 _resolve_scene_repo_key(repo, user) | t5_03 + 实测 |
| 优化3 | 翻页走 app.nav('dash') 整页重载 | loadDash 挂 window._sceneReload，分页条局部重载，nav 兜底 | t5_04 |
| 优化4 | 前端 scopeTag 依赖 with-pipeline 的 scope 字段（历史上从未返回，死代码） | with-pipeline 每项补 scope='repo' | 实测 scope=['repo']；t5_02 |

分页数学真机复验：25 条种子数据，无 page=25 全量、page1/2/3@10=10/10/5、越界=空、total 恒 25、页边界 id 连续无漂移。修正后 28 用例全绿（test_scene_list_pagination.py 21 + test_issue_404 7）。

## 6. 场景用例矩阵（需求 → 用例 → 代码）

| # | 场景 | 用例 | 用例代码 |
|---|------|------|----------|
| S1 | 项目场景页打开（无 page 参数，老调用方） | 全量返回 + total，向后兼容 | tests/test_scene_list_pagination.py::TestScenesListPagination::test_t1_01 |
| S2 | 场景卡片翻页 | page/page_size SQL 分页，页边界连续 | test_t1_02 / test_t1_03 |
| S3 | 翻到不存在的页 | 空数组 + total 恒定 | test_t1_04 |
| S4 | 全部翻完汇总 | 拼接==全量，无缺无重无漂移 | TestPagingIntegrity::test_t1_05 |
| S5 | 他人软删场景后刷新 | is_active=0 不进列表也不进 total | test_t1_06 |
| S6 | 多仓库数据并存 | 本仓列表/total 不泄漏他仓 | test_t1_07 |
| S7 | page_size=1 最小边界 | 每页 1 条，末页/越界正确 | test_t1_08 |
| S8 | 卡片技能链分页拉取 | with-pipeline 同参数同切片 | TestWithPipelinePagination::test_t2_01/02 |
| S9 | with-pipeline 不再全量探测 | off 三字段已移出 | test_t2_03 |
| S10 | 弹窗选中场景按需探测 | engine-status 可达、空节点降级 | test_t2_04；探测语义细节见 test_issue_404_create_task_offline_hint.py 7 用例（all_offline/部分离线/无绑定/禁用/SRE 未选机器/异常降级/未知场景） |
| S11 | 选中已被删除的场景 id | 不回退 default 误报（P1 修复） | TestDeepReviewFixes::test_t5_01 |
| S12 | 下拉显示仓库级标签 | with-pipeline scope 字段 | test_t5_02 |
| S13 | engine-status 无显式 repo | 会话级解析与列表一致（P3 修复） | test_t5_03 |
| S14 | 翻页交互 | 局部重载不整页切换 | test_t5_04；前端请求带 page：test_t3_01 |
| S15 | 创建场景后可见性 | 跳末页（P2 修复） | test_t5_05 |
| S16 | 数量角标跨页总数 | sceneTotal 而非当前页条数 | test_t3_03 |
| S17 | 切换仓库 | 页码归位防越界 | test_t3_05 |
| S18 | 他人删场景后当前页越界 | 空页自动收敛末页（全流程审查补漏） | TestFullFlowReview::test_t3_06 |
| S19 | 分页 enrich 数据一致性 | 分页/全量同场景 nodes 一致 + display_name 补齐 | TestPagingIntegrity::test_t2_05 |
| S20 | 弹窗异步探测竞态 | stale guard 防串台 | test_t4_03 |

覆盖统计：分页套件 27 用例 + 探测语义套件 7 用例 = 34 用例全绿；旧调用方（autopilot.js / knowledge.js 不带 page 全量、issues_center 服务端 get_scenes 全量）逐点核验兼容。

## 7. 场景检索（Issue #501 增量，2026-11-01 第二轮）

### 7.1 方案

- 后端：`/scenes` 与 `/scenes/with-pipeline` 新增可选 `q`（name/display_name/description 三字段 LIKE，max_length=100）与 `sort`（id 稳定序默认 / latest=created_at DESC,id DESC）；`total` 为命中数。COUNT 与列表两处 WHERE 片段强制同步（实现纪律点）；老库缺 display_name 列时两处同步降级为 name/description 两字段
- 前端：搜索框放在场景中心卡片头部（cd-h 行，标题同视野——不是分页条旁），带 🔍 图标、placeholder、✕ 清除；输入防抖 300ms → 局部重载（window._sceneReload，不整页切换）；回车立即检索；输入/清除/切仓页码归 1；切仓重置搜索词
- 焦点保持：重渲染（防抖检索/30s 自动刷新/翻页）重建 DOM 会打断输入——渲染前记录焦点态、渲染后恢复并光标置尾
- 空态区分：检索无命中显示「无匹配场景 + 清除搜索」，不误显「暂无场景→创建」引导

### 7.2 场景用例矩阵增量

| # | 场景 | 用例 | 用例代码 |
|---|------|------|----------|
| S21 | 按名称子串检索 | 命中数/列表/total 一致 | TestSceneSearch::test_t6_01 |
| S22 | 按 display_name / description 检索 | 中文/英文字段命中 | test_t6_02 |
| S23 | 检索无命中 | 空列表 + total=0（前端显示无匹配空态） | test_t6_03 |
| S24 | 检索 + 分页组合 | 命中 6 条分 4+2，页内不重不漏 | test_t6_04 |
| S25 | with-pipeline 同 q 语义 | 与 /scenes 同命中集同 total | test_t6_05 |
| S26 | 按创建时间倒序 | sort=latest 逆序，默认 id 序不受影响 | test_t6_06 |
| S27 | 搜索框位置/交互契约 | 头部位置、防抖、局部重载、页码归位、焦点保持、空态区分、切仓重置、with-pipeline 同 q | test_t6_07 |

覆盖统计更新：分页+检索套件 34 用例 + 探测语义套件 7 用例 = 41 用例全绿。

### 7.3 全流程审查补漏（第二轮）

| 问题 | 修正 | 验证 |
|------|------|------|
| q 在 loadDash 闭包外取值——30s 自动刷新/_sceneReload 复用旧 q，静默丢搜索词 | q/sort 移入 loadDash 函数体内（每次调用取最新值） | test_t6_08（源级回归锁） |

真机实测（3001 端口 + 8 条种子）：q 命中 name/display_name/description 三字段、q=zzz 零命中、q+page 组合切片正确、sort=latest 倒序、with-pipeline 与 /scenes 同 q 同命中、sort 非法值 422 拦截。老调用方默认参数行为不变（T1/T2 全部原用例通过）。

## 8. 缓存版本回退事故（全流程审查第三轮发现并修复）

**现象**（www.grapedev.cn 实测）：项目场景页点「添加场景」报 `window._branchQS is not a function`（dashboard.js:2006）、点引擎按钮报 `openEngineModal is not defined`。

**根因**：代码已修（00466993a 已把 _branchQS hoist 到顶层、openEngineModal 在创建模式渲染时赋值），但 a53e097ca（登录提示修复）把 index.html 里 dashboard.js 缓存版本从 v=102 **回退到 v=101**。hoist 之前的老用户浏览器缓存着 v=101 的旧文件，URL 未变永不重拉 → 线上报的是旧代码的错。纯缓存版本回退，非逻辑回归。

**修复**：本分支改过的前端文件全部升到未用过的版本号——dashboard.js 101→103（101/102 均曾被缓存）、app.js 210→211（分页/检索/局部重载）、issues.js 123→124（弹窗按需探测）；app_cache_ver 46→47（刷新 locales，新增检索词条生效）。回归锁：t7_01/t7_02。

## 9. 深审第四轮（家族扫描，2026-11-01）

### 9.1 确认有问题并修复

| # | 问题 | 修法 | 用例 |
|---|------|------|------|
| F1 | 搜索防抖游离提交强拉视图：输入后 300ms 内切走页面，挂起的 _sceneSearchCommit 走 app.nav('dash') 把用户拽回项目场景页 | 非 dash 视图时只静默更新搜索状态（切回自然带上），不再导航 | TestDeepReviewRound4::test_t8_02 |
| F2 | renderAgentCards 死代码（5235 字符，无调用者，内含第二处 openEngineModal 赋值+引擎按钮，制造"看似活代码"错觉） | 整段删除，注释残留清零 | test_t8_03 |
| F3 | openEngineModal 仅在渲染函数体内赋值（_branchQS 事故同构：函数中途抛错 → 赋值不执行 → 点按钮 undefined，线上已被点名） | 顶层定义 + 渲染函数只写 window._engineModalCtx 上下文注册表；上下文缺失静默降级 toast | test_t8_01 |

### 9.2 场景用例矩阵增量

| # | 场景 | 用例 | 用例代码 |
|---|------|------|----------|
| S28 | 创建/编辑模式中途异常后点引擎按钮 | 处理器仍存在（顶层），不再 undefined | test_t8_01 |
| S29 | 搜索输入后 300ms 内切换页面 | 不被强拉回 dash，搜索状态保留 | test_t8_02 |
| S30 | 死代码不复活 | renderAgentCards 引用计数为 0 | test_t8_03 |

### 9.3 有风险无实锤（记录不动手，修复收窄原则）

- 21 个 onclick 处理器仍是"渲染函数内赋值"模式（_saveCreateScene/_toggleFlowEdit/_confirmDeleteScene 等）：同函数耦合、无复现实例，全量 hoist 改动面大收益不明，留观察清单；再现 undefined 类报错时按 F3 模式逐个 hoist
- 缓存版本号降级实例 3 个（events.js/autopilot.js/tokens.css）：降号=URL 变=浏览器重拉，判定无害；真正的有害形态是"同 v 复用不同内容"（历史约 200 例，多在废线）
- 机制性根治建议（另立 issue）：index.html 版本号自动化（?v= 用 git/内容 hash 生成，人不再手维护）+ CI 守卫"部署线上同 (file,v) 不同内容即 fail"

dashboard.js 内容本轮再变，缓存版本 103→104。覆盖统计（程序化对账）：分页+检索+深审套件 40 用例 + 探测语义套件 7 用例 = 47 用例全绿。

## 10. A+C 方案实施（2026-11-01，第三轮评审拍板）

### 10.1 A：项目场景页单一数据来源

问题：进 dash 视图 3 个并行请求中，/scenes 与 /scenes/with-pipeline 查同表同页数据，nodes_json 反序列化×2、DB 读×2、HTTP×2，30s 自动刷新每轮双倍付费（历史演进痕迹：列表先有、技能链后挂）。

方案：loadDash 只打 `/dashboard` + `/scenes/with-pipeline`，同一响应产出卡片数据（_scenes）+ 技能链映射（_pipelines）；dashboard.js 有现成映射直接消费、无映射兜底自取（防御）；创建成功刷新同改。前置：with-pipeline 补 `icon` 字段。/scenes 保留给 autopilot/knowledge 老调用方与 minimal 下拉（C）。

收益：每次进页/自动刷新少 1 请求 1 次 DB 读；响应面纯增量（icon），无字段删除。

### 10.2 C：下拉轻量化

需求中心创建/复制弹窗只要场景名/图标，原走 with-pipeline 全量拉 nodes_json。改为 `/scenes?repo&fields=minimal`——路由层透传 minimal，`_query_scenes_from_db` 跳过 nodes_json 反序列化（列仍 SELECT 保索引稳定）；total 与名称字段面不变。

### 10.3 场景用例矩阵增量

| # | 场景 | 用例 | 用例代码 |
|---|------|------|----------|
| S31 | with-pipeline 承载卡片字段 | icon 存在且空串兜底 | TestSingleSourceAndMinimal::test_t9_01 |
| S32 | 下拉 minimal 不带 nodes | nodes 空 + 名称字段完整 + total 不变 | test_t9_02 |
| S33 | 前端单一来源 | loadDash 产 _pipelines、dashboard 不重拉、创建刷新同源 | test_t9_03（t3_01 同步收紧：loadDash 内不得残留 /scenes 调用） |
| S34 | 弹窗走 minimal | 两个下拉 fields=minimal、不再调 with-pipeline | test_t9_04 |

缓存版本：dashboard 104→105、app 211→212、issues 124→125。覆盖统计：分页+检索+深审+AC 套件 44 用例 + 探测语义 7 = 51 全绿。

### 10.4 深审五：DOM 级局部刷新（用户复审补强）

前情：A 方案做到了"数据层局部重载"（不走 app.nav 视图切换），但渲染层仍是 renderDashboard 整页 innerHTML 重建——hero/市场卡片/搜索框全部重排。

本节：_renderSceneCards 增加局部分支——页面 DOM 已是场景页（#dash-scenes 存在）且列表非空时，30s 自动刷新/翻页/搜索重载只更新四个动态区域（卡片行 #dash-scenes、角标 #sceneCountBadge、分页条 #scenePagerWrap、hero 统计 dashStatActive/Failed/Total），不整页重建；搜索框/焦点/滚动天然保持。首次进入（DOM 未建）、空态（含检索无命中）、创建页（DOM 已换）自动回退全量渲染。缓存版本 dashboard 105→106，用例 S35 = test_t9_05。

覆盖统计：分页+检索+深审+AC 套件 45 用例 + 探测语义 7 = 52 全绿。

### 10.5 全流程审查收尾（深审五补漏 2 项）

| # | 问题 | 修法 | 用例 |
|---|------|------|------|
| F4 | 删除模式挂起时 30s 局部刷新会重建卡片行，冲掉行上叉号/选中态（全量重建时代同样存在，局部刷新顺势堵住） | 局部分支加 `!window._dashDeleteMode` 守卫；模式状态提升 window 级；视图切回（全量渲染）自动复位防脏状态 | test_t9_06 |
| F5 | 删除场景后刷新只拉 /dashboard 不带场景列表——renderDashboard 拿不到 _scenes 误渲染"暂无场景"空态页（weekly-09 原有缺陷，删除一个场景页面变空） | 改为 A 方案同款双请求（/dashboard + with-pipeline 同页同 q），附 _pipelines；删后 total 收缩越界由末页收敛逻辑接管 | test_t9_07 |

场景矩阵增量：S35 局部刷新四区域（t9_05）、S36 删除模式保活与复位（t9_06）、S37 删除后列表正确回显（t9_07）。
已知取舍：局部刷新不更新 host-banner/hero 标题（仅统计值）——banner 变化罕见且属告警类，全量渲染仍会带上。

覆盖统计（程序化对账）：分页+检索+深审+AC 套件 47 用例 + 探测语义 7 = 54 全绿。缓存版本 dashboard 106→107。

## 11. 评审遗留提醒收口（2026-11-01，同事评审后）

| 提醒 | 处置 | 用例 |
|------|------|------|
| LIMIT/OFFSET int() 内插与参数化风格不一致 | 改为 `LIMIT ? OFFSET ?` 占位符传参（int() 强转保留为双保险，双方言通用） | TestReviewFollowups::test_t10_01 |
| q 过滤 WHERE 片段在 _count 与 _query 两处人工同步 | 抽公共构建器 `_scene_search_where(q, cols)`，两处共用（列集合：query 行内探测 / count 进程级缓存，降级口径一致）；后续改检索字段只动一处 | test_t10_02 |
| test_issue_479_scene_label 2 失败 | 基线 weekly-09 同样失败，既有问题非本分支引入（评审方独立确认，结论一致） | 无需处置 |

覆盖统计：分页+检索+深审+AC+评审收口套件 49 用例 + 探测语义 7 = 56 全绿；MySQL 方言守卫套件 89 用例复验通过。

## 12. 评审遗留修复：test_openapi 4 个基线失败（就地修在本分支）

根因均为"代码演进、测试常量未跟上"（非本分支引入，但按评审建议顺手修复）：

| 失败 | 根因 | 修法 |
|------|------|------|
| test_contract_version | 契约已升 2.2.0，断言仍是 2.1.0 | 断言改 2.2.0 |
| test_public_ops | EXPECTED_PUBLIC 停在 236 项，实际公开端点 301（auth/handoff/a2a 等新端点未回填） | 从活契约程序化重生成全集（301 项，含本分支 scenes_engine_status） |
| test_x_token_scope | 组织隔离（0c8b6b5df/d7caf6201）将 /api/skills、/api/dashboard/ops/overview、/api/issues/mine、/api/issues/{owner}/{repo} POST 改为 require_org_member（get_current_user_or_api_key 链）→ dual；测试样本表仍按 user_only 断言 | 四端点迁至 dual 样本，注释注明来源提交 |
| test_three_channels | #343 username 一致性校验：测试用假用户名 u{uid} 签 JWT → SESSION_INVALID；且 mine 已 dual 化（tk_ 应 200 非 403） | _user() 返回 (id, username) 原样签发；断言改 200 |

验证：test_openapi 8/8 全绿；场景三套件回归全绿。

## 13. 设计评审 D1 修复：i18n 语义一致性

openEngineModal 降级 toast 原借用 common.load_failed（"加载失败"），与 I18N 未加载时 fallback "请先进入场景编辑页" 语义不符。新增专属 key `dashboard.scene_engine_ctx_missing`（zh/en 双语），代码改用专属 key；i18n 完整性回归锁 t11_01/t11_02（新功能 5 个 key 双语齐备 + 不得借用语义不符 key）。缓存版本 dashboard 107→108。

评审其余判定认同不改：D2 硬编码色（存量约定不溯及，文件级重构另立项）、A3 性能数据（本 PR 为净优化，dev 实测过）、B2 默认值（20/100/300 均合理默认）。

## 14. 交付总账（收官对账）

**用例总账（程序化计数）**：test_scene_list_pagination.py 51 用例（T1 分页 8 / T2 with-pipeline 5 / T3 前端分页 6 / T4 弹窗按需 3 / T5 深审一 5 / T6 检索 8 / T7 缓存版本 2 / T8 深审四 3 / T9 A+C+局部刷新 7 / T10 评审收口 2 / T11 i18n 完整性 2——collect-only 51）+ 探测语义套件 7 + openapi 契约 8 + MySQL 方言守卫 89 = **155 用例全绿**（实跑 155 passed）。

**真机实测**：dev 3000 实例，真实 API 创建 26 场景 → 分页/检索/排序/单一来源/minimal/engine-status/handoff-targets 全矩阵 18 项验证 → 真实 DELETE 清理回原状。

**遗留事项清单（均已决策，非 pending）**：
1. dashboard.js 硬编码色令牌化（评审 D2）——文件级重构，另立项，不溯及存量（推荐：weekly-10 前后专项）
2. 深分页 keyset 化——当前百级量级不需要，触发条件：单仓场景 >5000 条（监控项非待办）
3. with-pipeline 三字段移除的仓外消费方——help/api 已声明 breaking change，合并公告中提示（一次性动作）

**发布状态**：单提交，MR #788（feat/scene-list-pagination → weekly-09）open、mergeable=True、0 issue/close 字样，等 CI + 维护者 approve。
