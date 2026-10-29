# Issue #471 P0 任务拆解

| # | 任务 | 类型 | 提交 |
|---|------|------|------|
| 1 | C1 红：_reregister 重发现引擎 + 注册空 ids 判定契约测试 | test | test(#471) |
| 2 | C1 绿：agent.py 注册判定 + _reregister 重发现/返回 engine 映射 + 调用方刷新 | feat | feat(#471) |
| 3 | C2 红：write_trust_settings + per-task settings.json 预写测试 | test | test(#471) |
| 4 | C2 绿：codearts.py 信任配置 + agent per-task/探测预写 | feat | feat(#471) |
| 5 | C2：install-worker.sh --engine + 信任合并 + 提示文案（脚本，无单测，shellcheck 目检） | feat | feat(#471) |
| 6 | E1 红：URL 拆分来源/回退/告警/消费端测试 | test | test(#471) |
| 7 | E1 绿：db.py 新函数 + config.py 双写 + 消费端切换 + health/main 暴露 | feat | feat(#471) |
| 8 | 文档：help/usage/faq.md 引擎与信任小节 + server-config.example.json + 任务记录 | docs | docs(#471) |
| 9 | QA Required 红：配置检查/告警纳入拆分项（只配拆分项不误报）+ runtime server_url 对齐 | test | test(#471) |
| 10 | QA Required 绿：db.py is_configured/warning 按面判定 + runtime.py _server_url 对齐 | fix | fix(#471) |
| 11 | QA S2/S3 红+绿：engine_by_rid 按 name 配对、重扫空沿用快照（含存量 mock 签名修复） | test+fix | test/fix(#471) |
| 12 | QA S4：install-worker.sh 复用 write_trust_settings + 防漂移注释 | fix | fix(#471) |
