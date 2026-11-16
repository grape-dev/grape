# Task Breakdown: Issue #1208

- [x] T0 前置核对：T10 会话表双读（#868）已在基线合入（202611030005/6 + session_manager/chat 双读）→ 满足，已记录进 PR 描述
- [x] T1 RED：_selector_match 失败测试（组内 OR/组间 AND/空 selector/单维度单值/多维度混合/未知前缀）→ GREEN：匹配函数 + 202611050001 pool_selector 加列
- [x] T2 agent_tags 派生（engine:/scope:/scene:，注册+心跳链）+ get_tags 过滤冒号行（RED→GREEN）
- [x] T3 RED：入队三分叉 + 零命中 fail-closed 文案分流 → GREEN：get_scene_engine_selector/get_scene_engine_uids/候选集/单函数文案 + event_pipeline 挂点
- [x] T4 RED：claim 三级顺序 + uid 轨空值不误杀 → GREEN：service.py 六层守卫检查点对账（claim/claim_by_tag/_check_pool_engines_reachable/_check_stale_queued_tasks/_fail_task_pool_unreachable）
- [x] T5 保存端：SceneNodeInput 三模式字段 + 双非空 400 + L1 值域 400（合法值列表）+ 透明 uid 升格（scenes.py/agents.py）+ dashboard agent_engines 带 uid
- [x] T6 T11 存量 uid 升格 backfill（202611050002：幂等/分批/悬空跳过+报告/不生成 selector）
- [x] T7 前端：模式 radio（新场景默认按标签）+ 三组复选（engine:/范围/scene:）+ 命中预览分层 + uid 输出 + 徽标
- [x] T8 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR → Issue 验证明细回帖（#827 格式）→ A2A QA
