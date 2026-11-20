1|# 云沙箱引擎与注册设计
2|
3|> 分支：feat/devbox-sandbox-integration
4|> 归一日期：2026-10-07
5|> 归一来源：见各节"原始文档"标注
6|
7|---
8|
9|## 1. 引擎注册体系
10|
11|### 1.1 _BACKENDS 单一数据源
12|
13|原始文档：docs/weekly-05/archive/devbox-sandbox/design-codearts-cubesandbox-engine-2026-09-30.md §9
14|
15|`_BACKENDS`（executor.py）是引擎注册的唯一事实源。新增引擎只需：
16|1. 在 `_BACKENDS` 注册（executor.py）
17|2. 继承 `AgentRuntime` 实现 Runtime 子类（声明 binary/protocol/pip_package/probe_command/kill_subcommand/bwrap_overlay_fn/prepare_task_env 等属性）
18|3. 如需云沙箱支持：注册 `ENGINE_REGISTRY`（engine_strategies.py）+ 新增 PTY wrapper
19|
20|自动支持：发现/探测/探活/校验/kill/bwrap 挂载/per-task 环境准备，无需修改 agent.py/executor.py/engine_probe.py/service.py/event_pipeline.py/user/db.py 等通用代码。
21|
22|### 1.2 五个小 Protocol
23|
24|原始文档：design-codearts-cubesandbox-engine §3.6
25|
26|| Protocol | 方法 | 用途 |
27||----------|------|------|
28|| AgentBackend | 基础协议 | 所有 backend 必须满足 |
29|| ProtocolAware | `protocol: str` | "acp" / "ndjson" 协议分叉 |
30|| CloudBackendProvider | `cloud_backend()` | 云沙箱路径 backend 替换 |
31|| PTYAware | `pty_wrapper_name()` / `pty_needs_stdin_payload()` / `pty_use_ndjson_common()` / `pty_prepare_env()` | PTY 分派 |
32|| SessionStaleDetector | `detect_session_stale(done)` | session 失效检测 |
33|
34|executor 用 isinstance 按需查询，9 处 if-else 改为能力查询。
35|
36|### 1.3 backend_for_engine
37|
38|从硬编码前缀匹配改为查 `_BACKENDS` key（按 key 长度降序匹配）：
39|
40|```python
41|def backend_for_engine(engine_name: str | None = "") -> str:
42|    name = (engine_name or "").lower()
43|    for backend_name in sorted(_BACKENDS, key=len, reverse=True):
44|        if name.startswith(backend_name):
45|            return backend_name
46|    return ""  # 不 fallback 到 opencode，空串表示不支持的引擎
47|```
48|
49|### 1.4 引擎注册改造
50|
51|原始文档：docs/weekly-05/archive/devbox-sandbox/engine-registration-design.md
52|
53|cloud_worker 注册时按 agent_type + provider 组合注册多个引擎实例。name=`{agent_type}:{provider}`。runtime_info JSON 存全量信息（engine_type/provider/template_id/preinstalled）。
54|
55|关键设计决策：
56|- credential_id 就是 device_id（复用现有架构，不加新表/字段）
57|- agent_runtimes 加 runtime_type + runtime_info 两列
58|- runtime_type: local/cloud/user；scope 保持 global/user/repo 不变
59|- 模板刷新：心跳时查模板列表对比 runtime_info，变化才更新（60s）
60|- get_task JOIN agent_runtimes 返回通用 task dict
61|- 池任务匹配保持基线逻辑 pool_agent_type != ''（不做精确匹配）
62|
63|### 1.5 已废弃
64|
65|- _PROBE_COMMANDS 字典（从 backend.probe_command 获取）
66|- _KNOWN 静态白名单（从 _BACKENDS 推导）
67|- discover_backends 的 known 字典（从 _BACKENDS 遍历）
68|- cursor 引擎占位（无 Runtime 实现，移除）
69|
70|---
71|
72|## 2. 云沙箱注册机制优化
73|
74|原始文档：docs/weekly-05/archive/devbox-sandbox/design-cloud-sandbox-registration-optimization.md
75|
76|### 2.1 认证层重构
77|
78|CredentialContext dataclass 替代 _get_credential_scope_and_user：一次 SELECT 取 6 字段（id/scope/owner_name/owner_id/repo_key/source），22 个端点消费，消除重复查表。scope_detail 已删除。
79|
80|关键设计决策：
81|- 删除 _get_credential_scope_and_user，逻辑内聚到认证层
82|- _expand_templates 统一模板展开（cubesandbox 优先排序 + preinstalled 默认空字符串）
83|- 增量注册：只注册 runtime_info 变化的条目，删除的标 offline
84|- 心跳退避：连续失败 ≥3 次指数退避，上限 600s，成功立即恢复
85|- node_id 列：auto-seed 行带节点标识，控制台行 node_id=''，不建 DB 唯一约束（MySQL 不支持 partial index），应用层保证唯一
86|- cloud_worker enabled 配置：cloud-worker.json + CLOUD_WORKER_ENABLED 环境变量
87|- RUNTIME_SKIP_LOCAL_REGISTER 改名 CLOUD_WORKER_HANDLES_LOCAL（旧名兼容+deprecation 日志）
88|
89|### 2.2 key 文件丢失恢复
90|
91|原始文档：design-cloud-sandbox-registration-optimization §2.7
92|
93|修复矩阵：
94|
95|| 场景 | DB 行 | key 文件 | 行为 |
96||------|-------|---------|------|
97|| 首次启动 | 无 | 无 | 生成 + INSERT 带 node_id |
98|| 正常运行 | 有 | 有 | return（跳过） |
99|| 文件丢失 | 有 | 无 | 生成新 token → UPDATE hash → 写新文件 |
100|| 文件在 DB 无 | 无 | 有 | return（跳过）——可能手动轮换过 / DB 被重置 |
101|| 集群新节点 | 无 | 无 | INSERT 新行带新 node_id |
102|| name 被改名 | 有 | 有 | return（按 node_id 精确命中） |
103|| node_id 丢失重生 | 无(新) | 无 | 孤儿清理同 name 旧行 + INSERT 新行 |
104|
105|关键设计决策：
106|- 按 node_id 精确定位（不按 name——name 可改，node_id 是唯一标识）
107|- node_id 来源：GRAPE_NODE_ID 环境变量 > /etc/machine-id > 自生成 UUID 存 /var/lib/grape/node-id
108|- name 带节点标识：`cloud-local-{node_id[:12]}`（保持 UNIQUE 约束下集群不冲突）
109|- 孤儿清理：检测同 name 但 node_id 不同的旧行 → 标记 revoked
110|
111|### 2.3 已废弃
112|
113|- engines 字段（cloud-worker.json + _load_config）
114|- _get_credential_scope_and_user 函数
115|- gateway_ip / gateway_skip_tls 从 cloud-worker.json 迁移到 DB extra_config（devbox.py 仍读取使用，配置来源变了不是删除）
116|
117|---
118|
119|## 3. Provider 凭据服务端下发
120|
121|原始文档：docs/weekly-05/archive/devbox-sandbox/design-provider-config-server-dispatch.md
122|
123|### 3.1 DB 表设计
124|
125|```sql
126|CREATE TABLE IF NOT EXISTS sandbox_provider_config (
127|    id INTEGER PRIMARY KEY AUTOINCREMENT,
128|    provider VARCHAR(64) NOT NULL UNIQUE,
129|    endpoint VARCHAR(512) NOT NULL DEFAULT '',
130|    api_key_enc TEXT,
131|    extra_config TEXT,
132|    enabled INTEGER DEFAULT 1,
133|    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
134|    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
135|    updated_by VARCHAR(64) DEFAULT ''
136|);
137|```
138|
139|公共列（endpoint / api_key_enc）+ extra_config JSON 私有字段混合模型。
140|
141|### 3.2 关键设计决策
142|
143|- provider 配置全局共享（不按节点区分）
144|- api_key 加密存储（enc:v3），服务端下发时解密为明文
145|- ssl_cert_content 明文存 DB（CA 证书是公开的），cloud_worker 落盘后设 REQUESTS_CA_BUNDLE
146|- cloud_worker 启动时从服务端 API 拉取（含加密 api_key 解密），心跳时刷新
147|- provider-configs 缓存加密（crypto.encrypt 加密 api_key 后写缓存文件）
148|- 迁移后删除本地配置文件（避免明文凭据残留）
149|- get_backend 不再静默 fallback → UnknownBackendError → 任务 fail
150|- backend_for_engine 不再 fallback 到 opencode → 返回空串
151|
152|### 3.3 已废弃
153|
154|- configs/cubesandbox-config.json（迁移到 DB 后删除）
155|- configs/devbox-config.json（同上）
156|- cloud-worker.json 的 engines 字段（已废弃删除）
157|- cloud_worker main() 的 gateway 初始化代码（已删除，DevBoxProvider 内部处理）
158|- get_backend 静默 fallback 到 opencode 的行为
159|
160|注意：gateway_ip / gateway_skip_tls **没有删除**，只是配置来源从 cloud-worker.json 迁移到 DB extra_config，devbox.py 仍读取使用。
161|
162|---
163|
164|## 4. 多引擎 LLM 凭据
165|
166|原始文档：docs/weekly-05/archive/devbox-sandbox/design-llm-credential-multi-provider-2026-09-30.md
167|
168|### 4.1 设计
169|
170|/env 端点按 engine_type 两级查询（引擎专属→通用兜底）。user_llm_credentials 加 engine_type 列 + UNIQUE(user_id, engine_type)。
171|
172|关键设计决策：
173|- 一引擎一凭据 UNIQUE(user_id, engine_type)，provider 降级为描述性属性
174|- 'common' 作通用标识（不用空串）
175|- 从源头改 runtime_info JSON 字段名 agent_type→engine_type（消除与基线 agent_tasks.agent_type 业务角色的语义冲突）
176|- build_context() 去掉 or task.agent_type fallback
177|- env_var 统一为 {ENGINE}_API_KEY（HERMES_API_KEY / OPENCODE_API_KEY / CODEARTS_API_KEY）
178|- UPSERT 按 user_id + engine_type 唯一
179|- select_llm_cred 公共函数（/env 端点 + repository 共用）
180|- service.py claim() 池匹配恢复基线逻辑（删掉 runtime_info 读取 + 精确匹配 bug）
181|
182|### 4.2 E2E 验证 Bug 修复
183|
184|| Bug | 根因 | 修复 |
185||-----|------|------|
186|| Bug 1 | executor.py agent_type→engine_type 遗漏（TypeError） | 修复 |
187|| Bug 2 | CubeSandbox 缺 CODEARTS_CLI_AK/SK | _inject_extra_env 注入 dummy |
188|| Bug 3 | codearts DB migration 覆盖 apiKey（enc:v3 解密失败） | 用 provider.env 字段绕过 config 文件 apiKey |
189|| Bug 4 | bwrap _SAFE_TOKENS 未同步 env_var 改名 | 补 HERMES/OPENCODE/CODEARTS_API_KEY |
190|| Bug 5 | DevBox grape token 未传 | envs 参数传递 |
191|| Bug 6 | hermes overlay 路径不一致 | hermes_overlay_path() 公共函数 |
192|
193|### 4.3 已废弃
194|
195|- runtime_info JSON 字段名 "agent_type"（改为 "engine_type"）
196|- build_context() 的 or task.agent_type fallback
197|- env_var 旧名（HERMES_OPENAI_API_KEY / HERMES_CUSTOM_API_KEY 等统一为 HERMES_API_KEY）
198|
199|---
200|
201|## 5. CodeArts 引擎 CubeSandbox 支持
202|
203|原始文档：docs/weekly-05/archive/devbox-sandbox/design-codearts-cubesandbox-engine-2026-09-30.md
204|
205|### 5.1 CodeartsInstaller
206|
207|模板预装验证（不 fallback 安装——无公开安装源）：
208|- 验证 codearts 已预装（`codearts --version`）
209|- 如果未预装，报错
210|- 设置 PATH（写 ~/.bashrc + ~/.bash_profile + ~/.profile）
211|- models-cache.json 存在性检查（缺失时 warn 但不 fail）
212|
213|### 5.2 CodeartsConfigurator
214|
215|读改写（不整文件覆盖），三层兼容：
216|1. 镜像有 codearts_cli.json 且可解析 → 读改写（只改 apiKey + baseURL，保留镜像的 provider/models/lsp）
217|2. 镜像有 codearts_cli.json 但解析失败 → fallback 整文件写
218|3. 镜像无 codearts_cli.json → fallback 整文件写
219|
220|apiKey 用 `{env:CODEARTS_API_KEY}` 引用环境变量（Issue #156 enc:v3 陷阱防护）。
221|
222|### 5.3 enc:v3 陷阱
223|
224|原始文档：docs/weekly-05/archive/issue-156/issue-156-codearts-enc-v3-fix.md
225|
226|codearts 启动时 `migratePlainTextApiKeys` 把明文 apiKey 加密为 enc:v3。enc:v3 加解密用 hostname:username 做 KDF identity（scryptSync 派生 key），Bun 1.2 的 GCM final() 在 bwrap user namespace 下失败。
227|
228|修复方案演进：
229|1. 最初：用 Python hashlib.scrypt + AESGCM 解密 enc:v3 apiKey，通过 AI_GATEWAY_API_KEY 环境变量注入
230|2. 更正：实际根因是 USER 环境变量缺失导致 identity 不一致
231|3. 最终：codearts_cli.json 中不写明文 apiKey，用 `{env:CODEARTS_API_KEY}` 引用环境变量
232|
233|### 5.4 codearts_wrapper.py
234|
235|PTY 桥接 wrapper，用绝对路径 `~/.codeartsdoer/installers/codearts` 启动。与 opencode_wrapper 共享 `_ndjson_common.py`（emit + convert_event + run_once + emit_done）。
236|
237|### 5.5 Bun 兼容性
238|
239|- stdout 块缓冲：BUN_STDOUT_BUFFERING=none + BUN_STDERR_BUFFERING=none
240|- OOM：Bun 预留 75GB 虚拟地址空间，7.2GB 内存机器触发 OOM（待修复）
241|- 503：codearts AK/SK 是 dummy 值，华为云 auth 返回 503，codearts 降级到 free version 用 opengw provider
242|
243|### 5.6 方案G per-task 数据目录
244|
245|原始文档：design-codearts-cubesandbox-engine §10
246|
247|CodeArtsRuntime.prepare_task_env() 创建 per-task 独立数据目录 `/var/cache/codearts-tasks/{tid}/cli-data/`，预写权限配置 + sandbox/config.json。
248|
249|云沙箱路径不需要方案G（MicroVM 级隔离 + 模板预装权限配置）。
250|
251|### 5.7 模板信息
252|
253|| 项 | 值 |
254||----|-----|
255|| 模板 ID | <template-id-codearts> |
256|| 别名 | codearts |
257|| 预装 | codearts 26.8.8 x86_64 + git 2.39.5 + Python 3.12.12 + models-cache.json |
258|
259|---
260|
261|## 6. CubeSandbox 集成
262|
263|原始文档：docs/weekly-05/archive/devbox-sandbox/cubesandbox-design.md
264|
265|### 6.1 关键设计决策
266|
267|- 用 CubeSandbox 官方 SDK 不用 E2B 原厂 SDK（协议不兼容）
268|- SDK 钉死 cubesandbox==0.7.0 + e2b==2.15.2（新版走 /v2/sandboxes 会 405）
269|- commands.run 走 gRPC stream 有 60s deadline，长命令用 nohup+轮询 /tmp/_cmd_done 标记
270|- PTY timeout 是会话生命周期不是命令超时；用 `; exit` 确保命令结束后 shell 退出
271|- hermes 和 opencode 完全解耦：各自 wrapper 脚本，不共享代码路径
272|- fencing token 409 重试：只在 status in (dispatched,running) 时用 current_attempts 重试一次
273|- 双标记不收敛修复：worker complete 是权威信号，self_reported 缺失时直接走 complete_task
274|
275|### 6.2 Bug 修复
276|
277|| Bug | 根因 | 修复 |
278||-----|------|------|
279|| Bug-2 | BOLA 仓库权限校验缺失 | GET/DELETE 加 check_repo_access |
280|| Bug-3 | grape config set-token 缺失 | 补 set-token 命令 |
281|| Bug-4 | git clone token 明文嵌 URL | 改用 GIT_ASKPASS |
282|| Bug-5 | crypto.py InvalidTag 未 catch | 显式 catch 转 ValueError |
283|| Bug-6 | _do_kill None 安全 | _sandbox is None 返回 False |
284|| Bug-new-2 | DevBox 同步 GIT_ASKPASS | credential helper 改 ASKPASS |
285|
286|---
287|
288|## 7. DevBox ACP Transport
289|
290|原始文档：docs/weekly-05/archive/devbox-sandbox/devbox-acp-transport-design.md
291|
292|hermes acp 在 DevBox 沙箱内 exit=0 但 text_len=0——stream_stdout 走 hwtunnel 事件流，重连后只收新事件丢字节。
293|
294|重构为：
295|- stdin 走 SDK send_stdin（gRPC 直连可靠）
296|- stdout 重定向到文件后用 run_command 增量读取（不走 hwtunnel）
297|
298|ACPTransport Protocol（write/readline/close）：
299|- PipeTransport（bwrap）：select+os.read 实现真超时
300|- DevBoxTransport：文件 stdout + base64 无损传输 + kill -0 健康检查
301|
302|---
303|
304|## 8. 文件物化
305|
306|原始文档：docs/weekly-05/archive/devbox-sandbox/design-file-materializer-2026-09-29.md
307|
308|桥接模式（Bridge）：SkillMaterializer / KnowledgeMaterializer（物化逻辑）× FileSink（写入方式）两个正交维度独立变化。
309|
310|关键设计决策：
311|- Materializer 不碰 IO，只调 sink.write_file / sink.upload_skill
312|- 占位符替换在各自 materialize() 内部完成
313|- skill 失败中断，knowledge 失败不中断（best-effort）
314|- 只抽象写不抽象读（有意取舍）
315|- 删除 executor.py 的 skill OBS 下载 + {{SKILL_DIR}} 替换，统一由 DefaultMaterializer 处理
316|
317|---
318|
319|## 9. 云沙箱设备与本机直连引擎分离
320|
321|原始文档：docs/weekly-05/archive/devbox-sandbox/design-runtime-device-separation-2026-09-30.md
322|
323|cloud_worker 与本机 agent 同机部署时，设备列表错误混入本机引擎。根因是 device_name 相同（都是本机 hostname）。
324|
325|修复：合并条件收紧为 `if target_device and target_device.get("credential_id") == 0`。本机引擎（credential_id=0）走 else 分支创建独立设备。
326|
327|---
328|
329|## 10. BOLA 任务分配权限
330|
331|原始文档：docs/weekly-05/archive/devbox-sandbox/design-bola-fix-2026-09-30.md
332|
333|### 10.1 三级校验
334|
335|| scope | 校验方式 |
336||-------|---------|
337|| system+global | 放行 |
338|| repo | 校验 task.repo_key == credential.repo_key |
339|| user | 校验 task.user_id == credential.owner_id |
340|
341|### 10.2 关键设计决策
342|
343|- owner_id 统一只存用户数字 ID（scope=user），repo_key 有独立字段
344|- claim 保持"先分配再回滚"机制（选项 A，改动小）
345|- runtime_register 回写 + db_setup migration 双重修复历史数据
346|- 13 个 task_id 端点统一加 _check_task_access 校验
347|- claim 不在应用层 skip——SQL 查询层加 user_id 过滤
348|- scope='global' 当前等于全量无界（一期单租户可接受）—— system scope 已废弃
349|
350|### 10.3 security-fix-design
351|
352|原始文档：docs/weekly-05/archive/fixes/security-fix-design.md
353|
354|- P0-1: runtime_type='cloud' 自声明获取系统信任 → 删除 cloud runtime 放行分支，系统级信任只认 scope='global'
355|- P0-2: fencing_token 默认不强制 → 翻转为默认强制，过渡期用 GRAPE_FENCING_TRANSITION=true
356|- P0-3: claim_by_tag 缺 fencing_token → 补设
357|- P1-1: sandbox-templates GET 对任意 rta_ 放行 → 校验 source='system'
358|
359|---
360|
361|## 11. Fencing Token
362|
363|原始文档：docs/weekly-05/archive/devbox-sandbox/fencing-token-design.md
364|
365|用 agent_tasks.attempts 作为 fencing token，claim 时原子递增，外部写操作带 token，主服务校验是否最新。
366|
367|关键设计决策：
368|- 用 attempts 而非独立字段（已有字段、天然单调递增、A2A 串行）
369|- 递增点统一到 claim（去掉 sweeper 回退递增）
370|- report_progress 不校验（日志允许旧 attempt 写）
371|- 空值过渡期放行+日志，未来收紧为 400
372|- fencing 校验集中在 cloud_worker 宿主侧，不注入沙箱环境变量
373|- bwrap 路径不需要 fencing（本机沙箱随 worker 进程终止）
374|
375|---
376|
377|## 12. Claim Predicate
378|
379|原始文档：docs/weekly-05/archive/devbox-sandbox/claim-predicate-design.md
380|
381|Cloud Worker Claim 前置校验——Predicate 链 + 防死循环。用户没配 LLM Key 时不应分配给云沙箱，在 claim 时校验而非创建沙箱后才发现。
382|
383|关键设计决策：
384|- ClaimPredicateChain 短路逻辑，可插拔策略链
385|- LlmCredentialPredicate 只校验 runtime_type='cloud' 的任务
386|- DB 查询失败 fail-open（不拦），用户确实没配 Key fail-closed（拒绝）
387|- 防死循环：predicate 拒绝时写 claim_skip_cloud 时间戳，5 分钟过期
388|- runtime_type 推断：cred_scope='user' 且未显式传 → 设为 'user'
389|
390|---
391|
392|## 13. 远程 Worker DB 查询解耦
393|
394|原始文档：docs/weekly-05/archive/fixes/fix-remote-worker-sandbox-db-query.md
395|
396|### 13.1 prepare() DB 查询解耦
397|
398|SandboxContext 纯数据 dataclass，build_context 从 task dict 提取（不查 DB）。
399|
400|关键设计决策：
401|- WorkerServices Protocol + 工厂函数，is_local 显式参数（不从 URL 每次猜）
402|- LocalWorkerServices: asyncio.to_thread 包装同步 DB 调用
403|- RemoteWorkerServices: HTTP API 调主服务
404|- cloud_worker 走 prepare() 统一安全策略
405|- 清理顺序：kill（需活连接）→ __exit__（关连接）→ revoke token
406|- kill() 加入 Sandbox Protocol（-> bool），BwrapSandbox/NullSandbox 空操作返回 True
407|
408|### 13.2 已废弃
409|
410|- _select_provider / _select_cloud_provider（@deprecated，保留供旧路径和测试）
411|- repository.py query_task_context / query_runtime_info / query_sandbox_template（@deprecated 死代码）
412|- make_db_state_reporter（cloud_worker 用 _http_state_reporter 替代）
413|
414|---
415|
416|## 14. codex 沙箱本地网关
417|
418|原始文档：docs/weekly-05/archive/fixes/fix-codex-sandbox-local-gateway.md
419|
420|### 14.1 问题
421|
422|codex api2codex bind 127.0.0.1，bwrap --unshare-net 隔离导致 Connection failed。
423|
424|### 14.2 修复
425|
426|- local_gateway 窄机制：backend 加 local_gateway property，codex 从 config.toml 动态解析回环端点
427|- NO_PROXY 摘除全部回环等价写法，端口级白名单覆盖 127.0.0.0/8 全段
428|- HTTP 代理转发时完整 URL 转路径（修复 codex 404）
429|- fail-closed：非回环地址 → NetworkStartupError
430|- 所有 worker 用 RUNTIME_SERVER_URL override GRAPE_SERVER_URL
431|- 环境变量白名单改黑名单（默认全部透传，只排 GIT_SSH_COMMAND）
432|- WORKER_MODE 环境变量优先于 _IS_LOCAL_WORKER 推断
433|- 主控自己调自己走 http://127.0.0.1:{_local_port()}
434|
435|---
436|
437|## 15. agent.py 模块化拆分
438|
439|原始文档：design-codearts-cubesandbox-engine §11
440|
441|agent.py 从 2420 行拆分为 5 个模块 + agent.py 保留 1416 行：
442|
443|| 模块 | 行数 | 内容 |
444||------|------|------|
445|| worker_probe.py | 275 | 引擎探测 |
446|| worker_metrics.py | 259 | 指标收集 |
447|| worker_skills.py | 143 | skill 扫描 |
448|| worker_cleanup.py | 200 | task 清理 + kill_stale_engine |
449|| worker_upgrade.py | 187 | 升级 |
450|| agent.py | 1416 | worker 初始化 / backend_for_engine / 流式执行 / _execute_task / run_loop / main() |
451|
452|升格符号（4 个，改调用方 import 路径）：
453|- _kill_stale_engine → kill_stale_engine
454|- _scan_local_skills → scan_local_skills
455|- _scan_local_skills_with_detail → scan_local_skills_with_detail
456|- _run_loop → run_loop
457|
458|---
459|
460|## 16. 审查问题修复
461|
462|### 16.1 P0 修复（audit-fix-design）
463|
464|原始文档：docs/weekly-05/archive/fixes/audit-fix-design.md
465|
466|10 P0 + 15 P1 + 18 P2，只修复本分支引入的代码，不碰基线。
467|
468|关键修复：
469|- P0-1: 明文 API key 写日志 → 删除 log 行
470|- P0-3: git credential 明文落盘 → env credential helper
471|- P0-6: CloudSandboxBase 缺 Protocol 契约 → 移到子协议
472|- P0-9: 租约列存在但未实现 → 删除 leased_by/leased_at
473|- P0-10: DDL 缺 attempt + key_version → 补列
474|- P1-2: attempts 可变负数 → CASE WHEN
475|
476|### 16.2 职责耦合修复
477|
478|原始文档：docs/weekly-05/archive/fixes/responsibility-decouple-design.md + responsibility-decouple-design-v3.md
479|
480|- cloud_worker 直接访问 sandbox._llm_env/_sandbox_id/_do_kill → set_llm_env/sandbox_id/kill 公共方法
481|- 跨模块调 _dispatch_execution/_backend_for_engine → 改公开名 + 兼容别名
482|- manager.py 22 处 DB 操作散落 → SandboxRepository DAO 层
483|- executor 直接设 sandbox._repo_url 等 5 个私有属性 → configure_repo() 公共方法
484|- factory(None,None,None,None) 可能崩 → 传 Null 实现
485|
486|### 16.3 2026-10-07 审查修复
487|
488|- P2-1: _match_admin_policy fail-closed 语义修复（auto_approve → manual_approve）
489|- P3-1: DROP INDEX MySQL 兼容性（information_schema 查后 ALTER TABLE DROP INDEX）
490|- P3-5: 设计文档一致性修复
491|