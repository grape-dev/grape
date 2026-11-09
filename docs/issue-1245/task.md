# Issue #1245 任务拆解

- [x] 新增 scene_precheck 核心校验与配置读取
- [x] 新增 configs/scene-precheck.json
- [x] API create_task_via_issue 拦截接入
- [x] webhook issue_opened 失败路径接入（failed + 评论）
- [x] 单测：6 reason_code + 3 豁免 + fail-open + enforce=false
- [x] 单测：API 400 + problems[] + trace_id 且不调用建单
- [ ] 全量回归/自审查后建 PR