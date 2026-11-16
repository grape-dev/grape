1|# Worker 执行与修复记录
2|
3|> 分支：feat/devbox-sandbox-integration
4|> 归一日期：2026-10-07
5|> 归一来源：docs/weekly-05/archive/fixes/ + docs/weekly-05/archive/verification/ 下文档
6|
7|---
8|
9|## 1. Worker 执行模式
10|
11|原始文档：docs/weekly-05/archive/issue-156/issue-156-agent-execution-modes.md
12|
13|### 1.1 两种执行模式
14|
15|| 模式 | 路径 | 沙箱初始化 |
16||------|------|-----------|
17|| 本地直连 | main.py → chat.py → stream_agent | bootstrap.py init_sandbox_components() |
18|| 远程 Worker | runtime/agent.py → claim → _execute_task → stream_agent | 同上（两个入口都调） |
19|
20|### 1.2 关键设计决策
21|
22|- Worker 默认开启沙箱（GRAPE_RUNTIME_MODE=worker 时 GRAPE_AGENT_SANDBOX 默认 on），主服务默认 off
23|- bwrap 降级 PREFERRED：有就隔离，没有裸跑+告警
24|- Worker 包打包 src/agent/ 整个目录（含 sandbox/、policy/、hermes_acp.py、executor.py）
25|- 决策权在 Worker，Agent 只感知（GRAPE_SANDBOX_MODE env 只读）
26|- LLM API 请求走 NetworkProxy 白名单直通（不写 external-api-audit，不触发 R1/R2）
27|- 业务 API 请求走 /api/proxy 中转（五维白名单 + token 注入 + 脱敏 + 审计）
28|
29|### 1.3 已废弃
30|
31|- seccomp 禁用（aarch64 SIGSEGV）→ bwrap namespace 隔离替代
32|- --share-net（Bun 不稳定）→ HTTP_PROXY 应用层代理
33|- cgroup 禁用（cgroup v1 非 root 不稳定）→ ulimit 兜底
34|
35|---
36|
37|## 2. Worker 全局配置下发
38|
39|原始文档：docs/weekly-05/archive/issue-156/issue-156-worker-config-sync.md
40|
41|### 2.1 设计
42|
43|主服务统一管理沙箱配置，Worker 心跳时自动获取（复用 /heartbeat 响应加 sandbox_config 字段 + config_version），不用重启。
44|
45|### 2.2 关键设计决策
46|
47|- 复用心跳不新建端点，config_version 版本单调递增，无变化返回空（省带宽）
48|- 安全策略：本地 env 只允许收紧（on→不能改 off，preferred→不能改 disabled，白名单只能删不能加）
49|- 远程不可达：保持上次已知良好 + TTL 60s；过期后安全默认（on + preferred + 空白名单）
50|- 远程返回无效配置（签名无效/版本回滚/解析失败）：立即安全默认 + 告警（不保持上次）
51|- extra_whitelist：远程白名单为上限，本地只能是其子集
52|- 审计路径不可关闭（空值 = 用默认路径）
53|- 值变化才覆盖（避免日志刷屏），原子更新失败回滚
54|
55|---
56|
57|## 3. P0/P1/P2 修复记录
58|
59|### 3.1 P0 修复
60|
61|原始文档：docs/weekly-05/archive/fixes/fix-p0-cloud-sandbox-bugs.md
62|
63|| Bug | 根因 | 修复 |
64||-----|------|------|
65|| A | pip3 install grape-cli（PyPI 没有此包） | 检查已存在跳过 + gitcode 源码安装 |
66|| B | 4 处错误路径调 __exit__() 导致 cleanup 设 _sandbox=None 后续 kill 失效 | 移除错误路径 __exit__ |
67|| C | __exit__ 报 state="running" 覆盖 kill 的 "killed" | _killed 标志 |
68|| D | run_long_cmd 丢弃 timeout + run_cmd 硬编码 60s | 加 timeout 参数 + 传递 |
69|
70|### 3.2 P1/P2 修复计划
71|
72|原始文档：docs/weekly-05/archive/fixes/p1-p2-fix-plan.md
73|
74|| ID | 问题 | 修复 |
75||----|------|------|
76|| P1-1 | devbox cmds.run 绕过重连 | 统一走 self.run_cmd() |
77|| P1-2 | LLM Key 泄漏到辅助命令 | run_command 加 merge_credentials 参数 |
78|| P1-3 | wait() 忽略 timeout | 传 timeout 给 SDK |
79|| P1-4 | RedactEngine fail-open | fail-closed 返回 [REDACTED] |
80|| P1-5 | DevBox 路径缺 chat 检测回调 | 传 on_message |
81|| P1-6 | PATH export 时序 | 调整顺序 |
82|| P1-7 | text_idle_timeout=60s 过短 | 非 chat 任务 300s |
83|| P2-2 | upload_skill 不检查 exit code | 检查 |
84|| P2-4 | reader EOF 误判 exit_code=0 | stopReason="error" |
85|| P2-7 | cat fifo 无 PID 管理 | 记录 PID |
86|
87|### 3.3 P2 设计（已全部实现）
88|
89|原始文档：docs/weekly-05/archive/fixes/p2-design.md
90|
91|- P2-1: _dispatch_execution 改造为 generator 只服务云沙箱
92|- P2-2: 租约（后随每环节新建废弃，列被删除）
93|- P2-3: 远程日志聚合：关键节点 report_progress 上报
94|- P2-4: install-cloud-worker.sh 一键部署（幂等）
95|- P2-5: 沙箱管理 API：GET /api/cloud-sandboxes + DELETE kill
96|
97|---
98|
99|## 4. 端到端验证 + Bug 修复
100|
101|原始文档：docs/weekly-05/archive/fixes/e2e-validation-and-bug-fixes.md
102|
103|| Bug | 严重性 | 根因 | 修复 |
104||-----|--------|------|------|
105|| 1 | P1 | 超时 cancel 时 await 日志丢失 | asyncio.shield + 同步日志 |
106|| 2 | P2 | _http_state_reporter fire-and-forget | 一期接受 |
107|| 3 | P2 | 错误路径 __exit__ 上报 running | 一期接受 |
108|| 4 | P2 | queue maxsize=256 | 一期接受 |
109|| 5 | P0 | 远程 worker prepare 查 DB 失败 | prepare() 新增参数从 task dict 获取 |
110|| 6 | P1 | A2A runtime_id 路由错误 | 待确认 |
111|| 7 | P0 | hermes acp --check 报 ACP 缺失 | pip install agent-client-protocol |
112|
113|---
114|
115|## 5. hermes ACP 修复
116|
117|### 5.1 临时目录权限
118|
119|原始文档：docs/weekly-05/archive/fixes/hermes-acp-homedir-fix.md
120|
121|/tmp 多用户权限冲突 → ~/.cache/hermes-acp-homes/（XDG Base Directory 规范）
122|
123|### 5.2 grape CLI PATH 丢失
124|
125|原始文档：docs/weekly-05/archive/fixes/grape-cli-path-fix-design.md
126|
127|三个根因：
128|1. grape wrapper 写到 /tmp/pylibs/bin 不在 _SANE_PATH
129|2. skill 文件不下发到 CubeSandbox
130|3. ACP env 注入缺失
131|
132|修复：
133|- 方案 D（主修复）：grape wrapper 写到 /usr/local/bin
134|- 方案 A（belt-and-suspenders）：同时写 ~/.bash_profile + ~/.profile
135|- skill_files 文本物化降级路径
136|- tirith_enabled: false（沙箱中 tirith 下载卡死）
137|- 超时诊断日志 dump err.log + out.jsonl
138|- CubeSandbox 到 grape 服务网络不通 → 安全组入方向 80 端口放行
139|
140|---
141|
142|## 6. 仓库信息内聚
143|
144|原始文档：docs/weekly-05/archive/fixes/repo-info-context-integration-design.md
145|
146|repo_url/bot_token/work_branch 在 executor 中散落式从 proc_env 取，_init_sandbox 的 _clone_repo 在 __enter__ 时这些值还没设。
147|
148|修复：内聚到 SandboxContext，build_context 时提取。_init_sandbox 从 ctx 读 repo 信息（不再依赖 configure_repo 后置设置）。
149|
150|---
151|
152|## 7. 场景引擎绑定失效
153|
154|原始文档：docs/weekly-05/archive/fixes/pool-engine-ids-fix.md
155|
156|A2A 交办走 pipeline_executor 路径，get_issue_scene 从 issue_state 表读 scene_id，但 issue_state 表 Phase C3 后已停写，scene_id 丢失导致 pool_engine_ids 为空。
157|
158|修复：get_issue_scene 增加 issue 表回退读 scene_id。
159|
160|---
161|
162|## 8. 遗留问题修复
163|
164|原始文档：docs/weekly-05/archive/fixes/remaining-fixes-design.md
165|
166|- grape-cli 安装命令 _pip/_py 未 shlex.quote → _pip_q/_py_q
167|- cloud_worker 调 client._request 私有 → get_templates() 公开方法
168|- is_cloud_sandbox hasattr 鸭子类型 → getattr
169|- upload_skill hasattr → getattr
170|
171|---
172|
173|## 9. Worker DB 访问改造
174|
175|原始文档：docs/weekly-05/archive/devbox-sandbox/design-worker-db-access-refactor.md + docs/weekly-05/archive/verification/worker-db-access-audit.md
176|
177|### 9.1 改造点
178|
179|| 文件 | DB 查询 | 改造方式 |
180||------|---------|---------|
181|| network.py _WhitelistCache | engine_sandbox_whitelist | GET /api/runtime/whitelist API 优先 + DB fallback |
182|| bwrap.py engine_sandbox_paths | engine_sandbox_paths | GET /api/runtime/sandbox-paths API 优先 + DB fallback |
183|| policy.py _get_db_content_hash | skill.content_hash_package | _fetch_skill_egress API（60s 缓存） |
184|| policy.py _match_admin_policy | egress_admin_policy | 同上 |
185|| policy.py _query_admin_exceptions | egress_admin_exception | 同上 |
186|
187|### 9.2 不需要改的
188|
189|- repository.py：manager.py `_is_local` 判断
190|- token.py：LocalWorkerServices vs RemoteWorkerServices
191|- llm_cred_selector.py：/env API 下发
192|- whitelist.py RuleCache：只在服务端 proxy.py
193|
194|### 9.3 白名单数据补充
195|
196|engine_sandbox_whitelist 表补充可信基础设施域名：
197|
198|| domain | 用途 |
199||--------|------|
200|| pypi.org | pip 安装依赖 |
201|| files.pythonhosted.org | pip 下载包 |
202|| gitcode.com | git 克隆 + API |
203|| github.com | git 克隆 + API |
204|
205|---
206|
207|## 10. 2026-10-07 审查修复
208|
209|| ID | 问题 | 修复 |
210||----|------|------|
211|| P2-1 | _match_admin_policy fail-closed → fail-open | auto_approve → manual_approve |
212|| P3-1 | DROP INDEX MySQL 不兼容 | information_schema 查后 ALTER TABLE DROP INDEX |
213|| P3-5 | 设计文档不一致 | 汇总表 + 章节号 + 改动清单修复 |
214|