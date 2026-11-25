# Plan: Issue #3508 实现方案

## 改动 1：后端 engine-status 结构化 + 500 修复（src/domains/mgmt/routes/scenes.py）
1. `_scene_engine_status` 扩展：对每个绑定引擎输出结构化条目
   `{id, name, health_score, reachability, reason, node}`：
   - reachability：复用唯一判定真源 `engine_health_ok`（health.py）→ reachable/unreachable；
     不可达时 reason 给「物理不可达（引擎状态：xxx）」；
   - health_score：`health_scorer.get_health_score`（表即缓存）；
   - 可达但不健康：reason 取结构化 `health_reason`（low_score / consecutive_failures /
     heartbeat_lost），不再仅凭 is_healthy 猜测为「健康分不足」；
   - 单引擎探测/查分异常降级为 reason=「健康数据暂不可用」，不中断整体。
2. 新增 `low_score_nodes`（存在可达但不健康引擎的节点），与 `offline_nodes`
   （全部引擎物理不可达）分列；覆盖多引擎混合场景；保留旧字段兼容。
3. `get_scene_engine_status` 路由：`get_pipeline_config` / 状态计算整体 try/except，
   异常返回 2xx + `{degraded: true, error}`（可读、可重试），不再裸 500。

## 改动 2：后端绑定修改校验候选范围 + 500 修复（同文件）
1. `update_scene` QA 节点提取改为安全访问器（dict/object 兼容，非 dict 不抛 AttributeError）。
2. `_validate_scene_engine_ids` 行解包加长度防护（row None / len<3 跳过）。
3. 新增 `_sanitize_scene_engine_ids`：落库前剔除非法（非整数）与不存在（无
   runtime_with_credential 行）的 engine id，保证保存的绑定真实生效；
   清洗后绑定为空但提交时非空（原绑定全部悬空/非法）→ 显式 400 提示重选：
   空绑定语义=广播，非 SRE 角色同样拦截，禁止静默清空为广播。

## 改动 3：前端 IssuesView（#tasks 创建弹窗）
- 选中场景后按需 `GET /scenes/{id}/engine-status`，据 offline_nodes / low_score_nodes
  渲染双指标告警（物理不可达 / 健康分不足），原因取 engines[].reason 汇总；
- 失败：可读错误提示 + 重试按钮（sceneHealth.retry），不白屏；
- 去掉已失效的 `s.all_offline` 依赖（with-pipeline 不再返回该字段）。

## 改动 4：前端 DashboardView（流程配置引擎区域）
- 新增 `sceneEngineHealth` 状态 + `loadSceneEngineHealth()`（detail 与 flowEdit 均加载，
  失败进 error 态 + 重试按钮）；
- 引擎徽章下增双指标行：每台绑定引擎显示 名称 + 可达性（物理不可达红/可达绿）+
  健康分（不足阈值黄），reason 以 title 提示可读展示；
- 引擎选择弹窗每行追加健康分与可达性标签：已绑定引擎取 engine-status；换绑候选引擎
  从 machine-health 拉健康分/原因 + agent_engines 状态派生可达性。

## 改动 5：回归用例
- Python `tests/api/test_issue_3508_scene_engine_health.py`：
  ① 物理不可达场景（离线引擎）→ engines[].reachability=unreachable + offline_nodes；
  ② 健康分不足场景（可达但低分）→ low_score_nodes + reason 含「健康分不足」；
  ③ get_pipeline_config 抛异常 → 2xx degraded + error 字段（不 500）；
  ④ update_scene 非 dict 节点不 500；非法/悬空 engine id 落库前被清洗。
- JS `tests/js/test_issue_3508_scene_engine_health.js`：静态断言两 Vue 文件包含
  engine-status 调用、双指标文案、重试按钮与错误兜底。

## 风险
- engine-status 新增字段向后兼容（只增不改）；探测循环单引擎异常已隔离；
- 前端改动限定在引擎健康区域与创建弹窗告警，不影响其他区域。
