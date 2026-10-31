# Tasks: Issue #573

- [x] 代码实勘：定位三处 coding 门控注入块与 AGENT_NAME worker 缺口
- [x] 编写 spec.md / plan.md
- [ ] event_pipeline.py 新增 `_resolve_bot_username` / `_inject_bot_agent_env`
- [ ] 三处注入点统一替换（enqueue / direct / build_task_env）
- [ ] scene_custom_env.py 保留集加 AGENT_NAME
- [ ] 新增测试并回归
- [ ] 建 PR + Issue 评论（含技能资产文档侧降级说明）
