1|# 沙箱体系架构设计
2|
3|> 分支：feat/devbox-sandbox-integration
4|> 归一日期：2026-10-07
5|> 归一来源：见各节"原始文档"标注
6|
7|---
8|
9|## 1. 威胁模型与安全需求
10|
11|### 1.1 威胁模型
12|
13|六类威胁（原始文档：docs/weekly-05/archive/devbox-sandbox/sandbox-requirements.md）：
14|
15|| 威胁 | 描述 |
16||------|------|
17|| 文件泄露 | Agent 读取敏感配置（.env、configs/、data/、~/.grape） |
18|| 数据破坏 | Agent 修改/删除非工作区文件 |
19|| 横向移动 | Agent 访问其他仓库/issue 的工作目录 |
20|| 资源耗尽 | Agent 占满磁盘/内存/CPU |
21|| 凭证窃取 | Agent 从环境变量/文件读取 API key/token |
22|| 数据外传 | Agent 通过网络将敏感数据发送到外部 |
23|
24|### 1.2 三级隔离
25|
26|| 级别 | 实现 | 隔离边界 |
27||------|------|---------|
28|| 无沙箱 | NullSandbox | 无隔离（GRAPE_AGENT_SANDBOX=off） |
29|| bwrap 进程级 | BwrapSandbox (--unshare-user --unshare-uts --unshare-net) | 进程级（多任务共享宿主机） |
30|| 云沙箱 MicroVM 级 | CubeSandbox / DevBox | MicroVM 级（每环节新建，独立文件系统） |
31|
32|### 1.3 七道安全防线
33|
34|1. **隔离**：bwrap 文件系统白名单 / 云沙箱 MicroVM 隔离
35|2. **凭证**：NullCredentialStrategy（凭据不进沙箱）/ envs 注入（一期）/ L7 代理（二期）
36|3. **网络**：--unshare-net + NetworkProxy 白名单 / 云沙箱 iptables
37|4. **命令**：PolicyManager 命令预检（check_command）
38|5. **脱敏**：RedactEngine 输出侧令牌化（10 类敏感数据格式）
39|6. **销毁**：沙箱 cleanup 释放资源 + tmpfs 丢弃
40|7. **fail-closed**：required 模式不可用=拒绝；optional+strict+不可用=拒绝；optional+compatible+不可用=裸跑+审计
41|
42|### 1.4 安全需求关键决策
43|
44|- cloud_worker 不直连 DB——所有 DB 操作走 HTTP API（原始文档：sandbox-requirements.md）
45|- 每环节新建沙箱，不复用沙箱实例
46|- LLM Key 加密链路：用户配置→AES-256-GCM 加密存 DB→/env API 解密→注入沙箱
47|- kill 先查状态，已不存在的沙箱直接返回成功
48|- 一期所有 provider 共享命令校验/输出脱敏/fail-closed 三道防线；凭证和网络控制二期统一到 L7 代理
49|
50|---
51|
52|## 2. 沙箱 OOP 架构
53|
54|### 2.1 核心接口
55|
56|原始文档：docs/weekly-05/archive/issue-156/issue-156-p0-oop-design.md + issue-156-sandbox-oop-design.md
57|
58|七个组件 Protocol（均加 @runtime_checkable）：
59|
60|| Protocol | 职责 | 实现 |
61||----------|------|------|
62|| Sandbox | 沙箱实例——wrap 返回 list[str]，context manager 管生命周期 | BwrapSandbox / NullSandbox / CloudSandbox |
63|| SandboxProvider | 抽象工厂——创建沙箱实例 | BwrapProvider / CubeSandboxBackend / DevBoxBackend |
64|| SandboxManager | 策略决策 + Provider 缓存 + 持有全局依赖 | sandbox_manager（模块级默认实例） |
65|| NetworkIsolation | 组合件——网络隔离 | NetworkProxy（bwrap）/ Null（云沙箱） |
66|| ResourceLimiter | 半组合件——资源限制（cgroup/ulimit） | NullResourceLimiter（cgroup v1 非 root 不稳定，已禁用，ulimit 兜底） |
67|| AuditListener | 外部依赖——审计日志 | FileAuditLogger / NullAuditLogger |
68|| CredentialStrategy | 注入参数——凭据保护 | NullCredentialStrategy（凭据不进沙箱），P2 预留 SentinelStrategy（AES-256-GCM sentinel + TLS MITM 替换） |
69|
70|关键设计决策（精确引用原始文档）：
71|
72|- **context manager 强制化**：`__enter__` 启动资源（失败抛异常），`wrap()` 纯构造可多次调用，`__exit__` 清理
73|- **Null Object 模式**：NullSandbox 消除调用方 if None 分支；disabled/降级时返回空实现
74|- **cleanup 顺序**：先停网络（切断逃逸通道）→ 再释放 cgroup → 审计记录 stopped
75|- **依赖方向单向无环**：types/api/errors ← credential/isolation/observability ← backends ← manager
76|- **build_bwrap_argv 是纯函数**，detect.py 主要被 manager.py import（bwrap.py 也直接 import detect 部分函数）
77|- **SandboxManager 默认构造允许**（Null 默认值轻量，不打开文件/不生成密钥），生产环境必须显式构造覆盖
78|- **GRAPE_AGENT_SANDBOX=off 环境变量保留**（紧急关闭），返回 NullSandbox
79|- **并发安全**：Manager 单例只读、Sandbox per-task 实例不共享、Provider 缓存首次探测后只读
80|
81|### 2.2 SandboxMode 三态
82|
83|| Mode | DB 映射 | 行为 |
84||------|---------|------|
85|| REQUIRED | optional+strict | 不可用时拒绝执行（fail-closed） |
86|| PREFERRED | optional+compatible | 不可用时裸跑+审计告警 |
87|| DISABLED | disabled | 不启用沙箱 |
88|
89|### 2.3 状态机
90|
91|PREPARED → ACTIVE（__enter__）→ IDLE（exec exit）→ CLEANED（cleanup）
92|
93|- ACTIVE → TIMEOUT_KILLED（TTL 30min）→ CLEANED
94|- ACTIVE → FAILED（crash）→ CLEANED
95|- cleanup 幂等：IDLE/TIMEOUT_KILLED/FAILED 都可清理
96|- wrap() 在 CLEANED 后抛 RuntimeError
97|
98|### 2.4 沙箱门面
99|
100|`src/agent/sandbox/__init__.py` 导出（原始文档：issue-156-p0-oop-design.md）：
101|
102|```python
103|from .api import Sandbox, SandboxProvider
104|from .types import SandboxMode, AlertSeverity, WhitelistRule, CompiledRule
105|from .errors import SandboxUnavailableError
106|from .manager import SandboxManager
107|from .alert import AlertEngine, NullAlertEngine, Alert
108|from .redact import RedactEngine, NullRedactEngine, RedactResult
109|from .whitelist import (
110|    normalize_path, match_path_prefix, match_whitelist,
111|    compile_rules, get_rules, invalidate_cache, load_rules_from_db,
112|    RuleCache,
113|)
114|sandbox_manager = SandboxManager()
115|```
116|
117|调用方只允许 `from src.agent.sandbox import sandbox_manager`，禁止 import 内部路径（backends/isolation）。
118|
119|### 2.5 bootstrap.py 初始化
120|
121|`init_sandbox_components()` 在 main.py 和 runtime/agent.py 两个入口都调用（原始文档：issue-156-agent-execution-modes.md）：
122|
123|- 返回 `(sandbox_audit, external_api_audit)` 两个 FileAuditLogger 实例
124|- 注入 FileAuditLogger → sandbox_manager（审计日志）
125|- 注入 AlertEngine → sandbox_manager（告警引擎）
126|- 注入 NullSeccompProgram → sandbox_manager（seccomp 暂时禁用）
127|- 注入 PolicyManager(detector=RuleBasedDetector, guard=RegexCommandGuard, audit=FileAuditLogger)
128|- executor._audit = FileAuditLogger
129|- 幂等：重复调用返回已创建的实例
130|- SchemaState 降级状态机：启动时 ensure 失败 → mark_degraded → 60s 节流懒重试
131|
132|---
133|
134|## 3. bwrap 文件系统隔离
135|
136|### 3.1 白名单模型
137|
138|原始文档：docs/weekly-05/archive/issue-156/issue-156-sandbox-design.md + issue-156-sandbox-whitelist-design.md
139|
140|bwrap `--unshare-user --unshare-uts --unshare-net` 隔离 user/uts/net namespace，只挂载白名单路径：
141|
142|- **基础系统（ro）**：/usr、/lib、/lib64、/bin、/sbin 等
143|- **引擎运行时（ro）**：~/.opencode、~/.codeartsdoer/installers、~/.hermes 等
144|- **引擎缓存（rw）**：~/.cache/opencode、~/.codex 等
145|- **工位 cwd（rw + chdir）**：保持 /proc/{pid}/cwd == cwd
146|- **/tmp（tmpfs）**：隔离，任务结束丢弃
147|- **hermes overlay**：~/.cache/hermes-acp-homes/{md5(cwd)[:12]} 单独 rw-bind（不割断 state.db 续传）
148|
149|关键设计决策：
150|- 白名单配置分两层：基础系统路径（BWRAP_RO_SYSTEM / BWRAP_RO_ENGINE_COMMON）在 config.py，引擎专用路径（sandbox_ro_paths / sandbox_rw_paths）在 AgentRuntime 基类属性，各引擎子类 override
151|- DB 表 `engine_sandbox_paths` 存额外路径（动态追加）
152|- `.git/hooks` 挂载到 /dev/null 屏蔽（防 git hooks 持久化恶意代码）
153|- `--die-with-parent`：grape 进程退出时沙箱内进程一并回收
154|- `--chdir {cwd}` + `--bind {cwd} {cwd}`：保持 /proc/{pid}/cwd 不变
155|
156|### 3.2 白名单配置表
157|
158|原始文档：issue-156-sandbox-whitelist-design.md
159|
160|```sql
161|CREATE TABLE IF NOT EXISTS engine_sandbox_paths (
162|    id            INTEGER PRIMARY KEY AUTOINCREMENT,
163|    backend_name  TEXT NOT NULL,          -- '*' = 全局，或引擎名
164|    path          TEXT NOT NULL,
165|    access        TEXT NOT NULL DEFAULT 'ro',  -- ro/rw
166|    created_at    TEXT DEFAULT (datetime('now')),
167|    updated_at    TEXT DEFAULT (datetime('now')),
168|    UNIQUE(backend_name, path)
169|);
170|```
171|
172|注意：原始设计文档（issue-156-sandbox-whitelist-design.md）定义了 engine_name/resolved_path/enabled/remark 列，但代码实现简化为 backend_name/path/access + created_at/updated_at。以代码为准。
173|
174|### 3.3 引擎路径自动解析
175|
176|原始文档：docs/weekly-05/archive/devbox-sandbox/bwrap-engine-path-resolution-design.md
177|
178|远程 worker 上 opencode/codex 引擎在 bwrap 沙箱内 exit=127（命令找不到），根因是软链目标路径不在白名单。
179|
180|`resolve_engine_runtime_paths(binary)` 纯函数：
181|- 动态解析软链真实路径 + 安装前缀
182|- 只检查 `bin/` 不检查 `lib/`
183|- fallback 用 found_prefix 标志（不靠 cur 值反推），5 层跑满时挂 real 本身
184|- 安全验证：isabs + len>1 + not startswith("//")
185|- 缓存放 tuple 返回 list 拷贝，不缓存负结果允许重试
186|
187|### 3.4 bwrap 沙箱配置表
188|
189|原始文档：issue-156-sandbox-design.md
190|
191|```sql
192|CREATE TABLE IF NOT EXISTS engine_sandbox_config (
193|    id            INTEGER PRIMARY KEY AUTOINCREMENT,
194|    runtime_id    INTEGER NOT NULL,
195|    engine_name   TEXT NOT NULL,
196|    sandbox_mode  TEXT NOT NULL DEFAULT 'required',
197|    degrade_policy TEXT DEFAULT 'compatible',
198|    created_at    TEXT DEFAULT (datetime('now')),
199|    updated_at    TEXT DEFAULT (datetime('now')),
200|    UNIQUE(runtime_id, engine_name)
201|);
202|```
203|
204|注意：
205|- 代码中 sandbox_provider 列通过 `_ensure_column` 动态添加（不在初始 DDL 中）
206|- ttl_seconds / idle_timeout_seconds 在原始设计文档（issue-156-p1-1-p1-3-design.md）中设计，但代码中**未实现**
207|
208|---
209|
210|## 4. 网络隔离
211|
212|### 4.1 --unshare-net + UDS 桥接
213|
214|原始文档：docs/weekly-05/archive/issue-156/issue-156-network-isolation-design.md
215|
216|从 --share-net 升级到 --unshare-net + UDS 桥接。沙箱内独立网络命名空间，raw socket 的 connect() 返回 ENETUNREACH（内核级强制）。
217|
218|架构：
219|1. bwrap --unshare-net 创建独立网络命名空间
220|2. stage2.py 在沙箱内把 UDS 翻译成标准 TCP localhost（Agent 的 HTTP_PROXY=http://127.0.0.1:8888）
221|3. UDS 连接到宿主机 NetworkProxy
222|4. NetworkProxy 按白名单转发请求
223|
224|关键设计决策：
225|- 不用 seccomp：--unshare-net 已内核级强制，stage2 需要 AF_INET 在 loopback 监听
226|- stage2 必需：Agent HTTP 客户端不一定支持 UDS 代理，stage2 翻译 TCP→UDS
227|- --unshare-net 下 loopback 可用但无外部路由
228|- codearts 代理链路：grape bwrap 设 HTTP_PROXY → codearts init_proxy_fetch 替换 fetch → getProxyConfig 读 env → fetchWithProxy → Bun fetch proxy 参数 → stage2 → UDS → NetworkProxy → 白名单
229|
230|### 4.2 验证判据三件套
231|
232|1. 任务结果正确（exit=0 + text_len > 0）
233|2. 审计日志 `sandbox_access/allow provider=bwrap`
234|3. NetworkProxy `network_decision/allow`
235|
236|### 4.3 已废弃
237|
238|- --share-net 方案（--unshare-net + stage2 完全有效）
239|- seccomp（aarch64 SIGSEGV）→ bwrap namespace 隔离替代
240|- cgroup（cgroup v1 非 root 不稳定）→ ulimit 兜底
241|- kioslaverc 文件（envProxy 优先于 getSystemProxy）
242|- per-task config 预写 proxy 相关配置
243|
244|---
245|
246|## 5. NetworkProxy 白名单
247|
248|### 5.1 五维白名单
249|
250|原始文档：docs/weekly-05/archive/issue-156/issue-156-p2-unified-design.md
251|
252|从 domain 一维扩展到五维（domain + method + path_prefix + scheme + port）：
253|
254|```sql
255|CREATE TABLE IF NOT EXISTS engine_sandbox_whitelist (
256|    id            INTEGER PRIMARY KEY AUTOINCREMENT,
257|    backend_name  TEXT NOT NULL,          -- '*' = 全局
258|    domain        TEXT NOT NULL,
259|    method        TEXT NOT NULL CHECK(method IN ('GET','POST','PUT','DELETE','PATCH')),
260|    path_prefix   TEXT NOT NULL DEFAULT '*',
261|    scheme        TEXT NOT NULL DEFAULT 'https' CHECK(scheme IN ('http','https')),
262|    port          INTEGER NOT NULL DEFAULT 443 CHECK(port BETWEEN 1 AND 65535),
263|    created_at    TEXT DEFAULT (datetime('now')),
264|    updated_at    TEXT DEFAULT (datetime('now')),
265|    UNIQUE(backend_name, domain, method, scheme, port)
266|);
267|```
268|
269|注意：原始设计文档（issue-156-p2-unified-design.md）的 UNIQUE 包含 path_prefix（六维），但代码实现中 UNIQUE 不含 path_prefix（五维唯一约束，path_prefix 有 DEFAULT '*'）。path_prefix 作为匹配维度但不在唯一键中。以代码为准。
270|
271|关键设计决策：
272|- CONNECT 请求只校验 domain + port（HTTPS 隧道内容不可见），path 校验在 P2-3 API 中转层做
273|- method 拆单值一行（不逗号分隔），path_prefix 末尾 * 通配（不支持中间通配）
274|- whitelist（旧域名列表）保留向后兼容，whitelist_rules（五维规则）优先使用
275|- match_whitelist 只给 proxy.py 用，NetworkProxy 白名单和 proxy.py 白名单是两套独立策略
276|
277|### 5.2 白名单三层合并
278|
279|原始文档：issue-156-agent-execution-modes.md
280|
281|基础层（grape host + 127.0.0.1）∪ DB 层（engine_sandbox_whitelist 官方域名）∪ 引擎层（backend.llm_api_domains 自动推导）
282|
### 5.3 Worker DB 访问改造

原始文档：docs/weekly-05/archive/devbox-sandbox/design-worker-db-access-refactor.md + docs/weekly-05/archive/verification/worker-db-access-audit.md

> 详细的改造点和改造方式见 worker-execution-and-fixes.md §9
286|
287|Worker 进程 DB 直接访问改为 API 优先 + DB fallback：
288|
289|| 文件 | DB 查询 | 改造方式 |
290||------|---------|---------|
291|| network.py _WhitelistCache | engine_sandbox_whitelist | GET /api/runtime/whitelist API 优先 + DB fallback |
292|| bwrap.py engine_sandbox_paths | engine_sandbox_paths | GET /api/runtime/sandbox-paths API 优先 + DB fallback |
293|| policy.py _get_db_content_hash | skill.content_hash_package | _fetch_skill_egress API 统一获取（60s 缓存） |
294|| policy.py _match_admin_policy | egress_admin_policy | 同上 |
295|| policy.py _query_admin_exceptions | egress_admin_exception | 同上 |
296|
297|不需要改的（确认正确）：
298|- repository.py：manager.py `_is_local` 判断
299|- token.py：LocalWorkerServices vs RemoteWorkerServices
300|- llm_cred_selector.py：/env API 下发
301|- whitelist.py RuleCache：只在服务端 proxy.py
302|
303|### 5.4 _match_admin_policy fail-closed 语义
304|
305|原始文档：外部审查报告 P2-1（2026-10-07 修复）
306|
307|API/DB 失败时返回 `"manual_approve"`（拒绝 egress），不是 `"auto_approve"`。匹配旧代码 fail-closed 行为和 docstring 声明。
308|
309|---
310|
311|## 6. P2-3 通用 API 中转
312|
313|原始文档：docs/weekly-05/archive/issue-156/issue-156-p2-unified-design.md §P2-3
314|
315|### 6.1 设计
316|
317|外部业务 API 走 proxy.py 五维白名单中转。`POST /api/proxy/{domain}/{path:path}` 端点，grape 后端用真实 token 请求目标 API，HTTPS 不需要 TLS MITM。
318|
319|token 来源：chat 用 USER_GITCODE_TOKEN，devops 用 BOT_GITCODE_TOKEN，通用从 DB credentials 查。
320|
321|### 6.2 task_scoped_token 机制
322|
323|原始文档：issue-156-p2-unified-design.md §P2-3
324|
325|```sql
326|CREATE TABLE IF NOT EXISTS task_proxy_tokens (
327|    id           INTEGER PRIMARY KEY AUTOINCREMENT,
328|    task_id      TEXT NOT NULL,
329|    token_hash   TEXT NOT NULL,
330|    backend_name TEXT NOT NULL,
331|    created_at   TEXT DEFAULT (datetime('now')),
332|    expires_at   TEXT,
333|    UNIQUE(task_id, backend_name)
334|);
335|```
336|
337|task 启动时签发 proxy token，沙箱内用 token 调 /api/proxy，任务结束撤销。
338|
339|### 6.3 proxy.py 14 条安全控制
340|
341|1. DNS rebinding 防护：用 domain 连接（不直连 IP），连接后二次校验 socket 对端 IP
342|2. headers 白名单（只允许安全 headers 透传）
343|3. 禁重定向（3xx 不自动跟随）
344|4. 请求体上限 1MB
345|5. 响应体上限 10MB
346|6. gzip 炸弹防护
347|7. 超时（连接+读取）
348|8. 并发限制（per task）
349|9. 循环检测（防 grape→proxy→grape 循环）
350|10. path 归一化：unquote 一次 + 拒绝残留 % + .. pop 回溯 + // 合并 + 去尾斜杠
351|11. 转发用原始 path，校验用归一化 path
352|12. CompiledRule 规则编译时归一化一次
353|13. 审计统一写 external-api-audit.jsonl
354|14. match_whitelist 只给 proxy.py 用，NetworkProxy 白名单和 proxy.py 白名单是两套独立策略
355|
356|### 6.4 P2-4/P2-5 管理 API
357|
358|- P2-4: 审计查询 API（GET /api/admin/sandbox-audit/{task_id}），放独立 sandbox_admin.py 路由
359|- P2-5: 白名单管理 CRUD API，权限模型用 admin scope（复用 _require_admin）
360|
361|### 6.5 TokenManager 和 RuleCache
362|
363|原始文档：issue-156-p2-unified-design.md §OOP 优化
364|
365|- TokenManager：封装 task_scoped_token 生命周期（签发/校验/失效 + DB 操作），构造时注入 db + db_lock，向后兼容模块级函数
366|- RuleCache：封装白名单规则缓存（load/compile/invalidate），替代模块级 _whitelist_cache dict
367|
368|---
369|
370|## 7. 输出脱敏
371|
372|### 7.1 RedactEngine
373|
374|原始文档：docs/weekly-05/archive/issue-156/issue-156-p1-8-redact-design.md
375|
376|两层互补：
377|1. **精确值 redact**：注入的 secret_values 列表精确替换为 [REDACTED]
378|2. **正则模式令牌化**：10 类敏感数据格式匹配替换为 ***REDACTED***
379|
380|10 类检测模式：OpenAI Key / GitCode PAT / AWS AK / AWS SK / 华为云 AK / 私钥 / JWT / DB 连接串 / 通用密码 / SSH 路径
381|
382|关键设计决策：
383|- fail-open 策略——令牌化失败保留原文 + log.warning，不阻断主流程
384|- NullRedactEngine 默认值，生产环境显式注入
385|- hermes_acp.py 的 _enqueue 是所有输出唯一入口，在此处加 redact 覆盖所有事件类型
386|- 审计按 task 聚合——任务结束时汇总 redact 统计写一条审计
387|- RedactResult.matches 含类型/预览前 20 字符/位置供审计
388|
389|---
390|
391|## 8. 告警引擎
392|
393|### 8.1 AlertEngine
394|
395|原始文档：docs/weekly-05/archive/issue-156/issue-156-p1-5-p1-9-alert-engine-design.md
396|
397|单条审计合法 ≠ 行为序列合法。AlertEngine 在任务结束后（BwrapSandbox.cleanup 中）扫描完整审计序列，匹配 8 条规则生成 Alert。
398|
399|P1-5 组合攻击检测 5 条：
400|- R1 数据外泄（敏感文件读+外部 POST 60s 内→CRITICAL）
401|- R2 高频请求（同 host 60s >20→HIGH）
402|- R3 非标端口（→LOW）
403|- R4 命令预检拦截（→HIGH）
404|- R5 配置漂移（P1 默认关闭）
405|
406|P1-9 高风险操作 3 条：
407|- R6 CRITICAL（force push/DROP TABLE/rm -rf /，精确匹配根路径排除构建目录）
408|- R7 HIGH（force delete/hard reset/TRUNCATE/rm .env）
409|- R8 MEDIUM（rollback/revert）
410|
411|关键设计决策：
412|- R4 和 R6/R7/R8 互斥：后者匹配则不重复报 R4
413|- R6 命令归一化：剥 sudo/env/timeout/nohup/exec 前缀 → 压缩空白 → 词元级判断
414|- Alert.fingerprint 去重（hash(rule + task_id + 关键字段)）
415|- cleanup 中异常隔离——告警扫描失败不影响资源清理
416|- admin 通知 P1 只审计 + log.error，SSE 推送留 P2
417|
418|---
419|
420|## 9. 云沙箱平台抽象
421|
422|### 9.1 多沙箱平台抽象层
423|
424|原始文档：docs/weekly-05/archive/devbox-sandbox/multi-sandbox-abstraction-design.md
425|
426|bwrap/DevBox/CubeSandbox 三平台可插拔抽象层。CloudSandbox 继承 Sandbox Protocol，新增 `run_command()`/`bind_credential()`/`capabilities` 等云特有契约。CloudSandboxBase 公共基类处理 DB 记录/refresh/kill 模板方法。
427|
428|关键设计决策：
429|- CloudSandbox 继承 Sandbox（不是并列独立协议），executor 按 sandbox.execution_mode 属性分叉（注意：这是 Sandbox 对象的属性，不是已废弃的 agent_tasks.exec_mode DB 列）
430|- 基类不放陷阱方法（不用 NotImplementedError），wrap() 移到 bwrap 实现类，run_command() 移到 CloudSandbox 子协议
431|- 凭证绑定与命令执行解耦：bind_credential() 绑定，run_command() 不接收凭证参数；CredentialMode 枚举（ENVS/VAULT/PROXY）
432|- DevBox 一期 ENVS 模式，CubeSandbox VAULT 模式（L7 代理注入，Key 不进沙箱）
433|- 统一 cloud_sandboxes 表 + provider 字段区分平台
434|- NullSandbox 主动选择才走 NONE；被动降级绝对禁止（云不可达必须报错）
435|- is_available 拆成 is_configured()（本地同步）+ health()（TTL 缓存 30s）
436|- run_command() 永远返回 ExecHandle，不返回 CommandResult|ExecHandle 联合类型
437|
### 9.2 模板路由

原始文档：docs/weekly-05/archive/devbox-sandbox/sandbox-template-design.md

> 引擎注册改造详见 cloud-sandbox-engine.md §1.4
441|
442|统一 sandbox_templates 表（DevBox/CubeSandbox 共用），引擎→模板→provider 路由。
443|
444|关键设计决策：
445|- provider 是模板属性，不是 worker 身份——一个通用 worker + 模板路由
446|- 沙箱复用已移除：每环节新建，原因是不需要共享工作区+不可信代码污染风险+预装模板消除启动成本
447|- 模板校验只在创建/编辑时（创建临时沙箱验证→立即 kill），不做定时健康检查
448|- exec_mode 路由彻底移除，改为模板路由（exec_mode 字段在代码中完全不存在，连兼容性保留都没有）
449|- scope 统一为 'global'（不是 'system'）—— system scope 已废弃，global 即 system
450|- cloud_base __enter__ 直接 create+init（移除 connect 复用），__exit__ no-op（kill 由 cloud_worker finally 处理）
451|- CubeSandbox 拆分为 Provider + Instance（和 DevBox 对齐）
452|- 沙箱创建限流：设计文档中 CubeSandbox 3 个、DevBox 5 个，但代码中未实现限流
453|- idle reaper TTL 600s（后改为 1800s）
454|- fencing token 用 agent_tasks.attempts
455|- 异常恢复：沙箱可丢弃+状态外置+重试即新建，不"找回沙箱继续执行"
456|
457|### 9.3 架构演进（已废弃方案）
458|
459|原始文档：docs/weekly-05/archive/devbox-sandbox/devbox-sandbox-integration-design.md + devbox-p0-dispatch-design.md + cloud-worker-design.md
460|
461|以下方案已废弃，保留历史参考：
462|
463|1. **exec_mode 路由 → 模板路由**：从 claim 按 exec_mode 过滤改为引擎选模板+模板定 provider
464|2. **沙箱复用 → 每环节新建**：不复用沙箱实例，和 bwrap 路径一致
465|3. **scope='cloud_worker' → scope='global'**：统一系统级凭据范围（system 已废弃，global 即 system）
466|4. **租约机制废弃**：每环节新建+claim 原子抢单已足够隔离（leased_by/leased_at 列已删除）
467|
468|---
469|
470|## 10. cloud_base DB 职责解耦
471|
472|原始文档：docs/weekly-05/archive/devbox-sandbox/cloud-base-db-decouple-design.md
473|
474|cloud_base.py 从直接 import session_manager 改为接受 state_reporter 回调函数。
475|
476|关键设计决策：
477|- state_reporter 回调：cloud_worker 传 HTTP 版（async），manager.prepare 传 DB 版
478|- get_llm_credentials 从基类删除，LLM 凭据统一外部注入 _llm_env
479|- _query_db 删除（每环节新建后不需要查复用记录）
480|- cloud_base 零 DB 依赖
481|
482|已删除：_query_db / _write_db / _update_db / _is_remote_worker / get_llm_credentials
483|
484|---
485|
## 11. 云沙箱生命周期

原始文档：docs/weekly-05/archive/devbox-sandbox/design-sandbox-lifecycle-2026-09-29.md

> 云沙箱平台抽象详见 §9，引擎注册详见 cloud-sandbox-engine.md
489|
490|### 11.1 问题清单（6 个已修复）
491|
492|| # | 问题 | 根因 | 修复 |
493||---|------|------|------|
494|| 1 | cubesandbox 在沙箱列表看不到 | state_reporter fire-and-forget 请求被 GC 吞 | 直接 await |
495|| 1b | 同 issue 多沙箱 INSERT 被吞 | UNIQUE KEY (issue_id, repo_key) 冲突 | 改为 sandbox_id |
496|| 1c | 失联沙箱资源不释放 | 新沙箱创建时旧沙箱未标记 | 标记 stale |
497|| 1d | prepare 失败时沙箱状态不上报 | except 块无 state report | kill 残留 + 上报 killed |
498|| 3 | idle reaper 参数不合理 | 600s 偏短 + 不扫 creating | 1800s + 扫 creating |
499|| 4 | 任务取消/终止时云沙箱不清理 | cancel 链路只杀引擎不 kill 沙箱 | cancel_task kill 沙箱 |
500|
501|### 11.2 text 事件分片修复
502|
503|text 事件缓冲合并（step_finish 不触发 flush，ACP end_turn 场景 text 合并）
504|
505|### 11.3 补充修复
506|
507|原始文档：docs/weekly-05/archive/devbox-sandbox/design-supplementary-fixes-2026-09-29.md
508|
509|- P1-8: finally 中 sync kill() 阻塞事件循环 → asyncio.to_thread 包装
510|- P2-13: finally sync __exit__() 阻塞 → 同上
511|- P2-14: fire-and-forget 的 running 报告后到覆盖 killed → UPDATE state='killed' + WHERE state != 'killed'
512|- P2-15: 未知事件 debug 级看不到 → warning
513|
514|---
515|
## 12. 表结构一览

> 基线：main 分支。以下为本需求（issue-156 + devbox-sandbox-integration）整体合并到 main 时需要的表改动。

### 12.1 新建表（10 个）

| 表 | 用途 | DDL 位置 |
|----|------|---------|
| cloud_sandboxes | 云沙箱实例记录 | db_setup.py L1014 |
| sandbox_templates | 沙箱模板管理 | db_setup.py L1048 |
| sandbox_provider_config | Provider 凭据服务端管理 | db_setup.py L506 |
| user_llm_credentials | 用户 LLM 凭据（AES-256-GCM 加密） | db_setup.py L1033 |
| engine_sandbox_paths | 引擎沙箱白名单路径 | db_setup.py L933 |
| engine_sandbox_config | 引擎沙箱配置（mode/degrade） | db_setup.py L944 |
| engine_sandbox_toggle | 引擎维度沙箱开关（global/hermes/opencode/codex/codearts） | engine-sandbox-toggle-design |
| engine_sandbox_whitelist | 五维白名单规则 | db_setup.py L957 |
| task_proxy_tokens | task 级代理 token | db_setup.py L971 |
| egress_admin_policy | 管理员 egress 策略 | db_setup.py L982 |
| egress_admin_exception | 管理员 egress 例外 | db_setup.py L995 |

### 12.2 修改已有表（_ensure_column 动态添加列）

| 表 | 新增列 | 来源 |
|----|--------|------|
| agent_runtimes | +binary/provider/isolation_level/provider_name/bwrap_available | issue-156-sandbox-design |
| agent_runtimes | +runtime_type/runtime_info | engine-registration-design |
| agent_runtimes | +skill_scan_result/tags | issue-156 |
| agent_runtimes | runtime_info JSON agent_type→engine_type（数据迁移） | llm-credential-multi-provider |
| runtime_credentials | +last_ip/owner_name/load_avg/spec | issue-156 |
| runtime_credentials | +source（system/user），-scope_detail（已删除） | bola-simplify-design |
| runtime_credentials | +node_id（_seed_cloud_worker_key 中应用层管理） | cloud-sandbox-registration-optimization |
| runtime_credentials | owner_id 语义修正（scope=user 存数字 ID） | bola-fix |
| cloud_sandboxes | +kill_attempts/created_by/task_id/stage/attempt/expire_at | step9-11/sandbox-lifecycle |
| user_llm_credentials | +engine_type + UNIQUE(user_id,engine_type) | llm-credential-multi-provider |
| user_llm_credentials | +key_version | step9-11 |
| engine_sandbox_config | +sandbox_provider（_ensure_column 动态添加） | multi-sandbox-abstraction |
| skill | +content_hash_package | impl-skill-egress-phase3 |

### 12.3 设计文档有但代码未实现

| 表 | 列 | 状态 |
|----|-----|------|
| engine_sandbox_config | ttl_seconds/idle_timeout_seconds | issue-156-p1-1-p1-3 设计，代码未实现 |
| agent_tasks | claim_skip_cloud | claim-predicate-design 设计，代码中注释级实现（非 DB 列） |
| agent_tasks | exec_mode | devbox-p0-dispatch 设计，已废弃，代码中完全不存在 |
553|
554|---
555|
## 13. 已知问题

> 2026-10-07 全分支深度审查发现 17 BUG + 18 遗漏 + 10 体验级。
> 修复方案详见：docs/weekly-05/audit-bugfix-design.md

### 13.1 已修复

| ID | 严重性 | 描述 | 状态 |
|----|--------|------|------|
| P2-1 | P2 | _match_admin_policy fail-closed | ✅ FIXED 2026-10-07 |
| P3-1 | P3 | DROP INDEX MySQL 兼容性 | ✅ FIXED 2026-10-07 |

### 13.2 审查 BUG（修复方案见 audit-bugfix-design.md）

#### 合并阻塞（6 个）

| ID | 严重性 | 文件 | 描述 | 状态 |
|----|--------|------|------|------|
| BUG-1 | P1 | issues.py:250-285 | _group_events_by_issue 定义 _sel 未使用，SQL 缺列 | ✅ FIXED |
| BUG-2 | P1 | conftest.py | 未设置 GRAPE_DB_ENCRYPTION_KEY | ✅ FIXED |
| BUG-4 | P0 | proxy.py:204,238 | SSRF TOCTOU 竞态，DNS rebinding | ✅ FIXED |
| BUG-8 | P0 | executor.py:1158 | 云沙箱路径缺少命令预检 | ✅ FIXED |
| BUG-12 | P0 | settings.py:51-52 | 回显华为云 AK/SK 明文 | ✅ FIXED |
| BUG-13 | P1 | event_pipeline.py:1954 / runtime.py:1175 | cloud_sandboxes 状态机不一致 DELETE vs UPDATE | ✅ FIXED |

#### 建议修复（11 个）

| ID | 严重性 | 文件 | 描述 | 状态 |
|----|--------|------|------|------|
| BUG-3 | P2 | cubesandbox.py:150 | _do_kill 无条件 return True | ✅ FIXED |
| BUG-5 | P2 | devbox.py:308 | _clone_repo 缺少幂等检查 | ✅ FIXED |
| BUG-6 | P2 | cubesandbox.py:392 | _configure_grape_cli 直接用 cmds.run | ✅ FIXED |
| BUG-7 | P2 | cubesandbox.py:337 | upload_skill 不检查 exit_code | ✅ FIXED |
| BUG-9 | P2 | cubesandbox.py | 缺少 send_stdin/close_stdin | ✅ FIXED |
| BUG-10 | P2 | devbox_dns.py | DNS override 无清理 | ✅ FIXED |
| BUG-11 | P3 | seccomp.py:206 | BPF sock_filter 用 signed i | ✅ FIXED |
| BUG-14 | P2 | network.py:1083 | _WhitelistCache fail-open | ✅ FIXED |
| BUG-15 | P1 | bwrap.py:594 | _SAFE_TOKENS 允许云 AK/SK 进沙箱 | ⏸️ 暂不修复（业务需要） |
| BUG-16 | P2 | network.py:560 | admin 白名单无 body 限制 | ✅ FIXED |
| BUG-17 | P2 | cubesandbox.py:77 | __init__ 不接收 work_branch | ✅ FIXED |

### 13.3 遗漏级（后续处理）

| ID | 描述 | 状态 |
|----|------|------|
| 遗漏-2 | ENGINE_REGISTRY 无大小写容错 | ✅ FIXED |
| 遗漏-3 | CubeSandbox run_cmd 不接受 envs | ✅ FIXED |
| 遗漏-4 | _run_long_command 不清理临时文件 | 📋 后续 |
| 遗漏-5 | _do_write_file/_do_read_file 死代码 | 📋 后续 |
| 遗漏-6 | 设计文档内部矛盾（每环节新建 vs A2A复用） | 📋 后续 |
| 遗漏-7 | _kill_cloud_sandbox_on_issue_close 未传 config | 📋 后续 |
| 遗漏-8 | devbox 路径系统性缺失 8 个安全组件 | 📋 后续 |
| 遗漏-9 | worker 离线后 running 任务无重调度 | 📋 后续 |
| 遗漏-10 | issue 关闭 kill 沙箱不含 stale 状态 | 📋 后续 |
| 遗漏-11 | 云沙箱静默超时 300s 硬编码 | 📋 后续 |
| 遗漏-12 | proxy-token backend 推导恒返回 opencode | 📋 后续 |
| 遗漏-13 | CubeSandboxInstance read_file_bytes 二进制支持 | 📋 后续 |
| 遗漏-14 | _do_run_command 返回原始 SDK 结果 | 📋 后续 |
| 遗漏-15 | bwrap 路径缺少 DNS override 清理对称性 | 📋 后续 |
| 遗漏-16 | redact.py 脱敏正则覆盖不全 | 📋 后续 |
| 遗漏-17 | token.py 无过期 token 清理机制 | 📋 后续 |
| 遗漏-18 | sandbox_admin 无操作审计日志 | 📋 后续 |

### 13.4 原有已知问题（未修复，不阻塞）

| ID | 严重性 | 描述 | 状态 |
|----|--------|------|------|
| R117 | P2 | _read_new 全文件读取 O(n²) | SDK 无 range read |
| R118 | P2 | readline 进程退出最后一行 | 已改善未完全修复 |
| R119 | P2 | DevBox _clone_repo 幂等性 | → BUG-5 修复中 |
| R120 | P2 | configure_repo 不失效 ctx 缓存 | 当前无功能影响 |
| R121 | P2 | run_long_cmd 不传 envs | → 遗漏-3 修复中 |
| R122 | P2 | SandboxContext 同名类冲突 | sandbox_context.py vs engine_strategies.py |
| R123 | P2 | CubeSandbox run_command 签名 vs Protocol | 位置参数 vs 关键字参数 |
| R124 | P2 | CubeSandbox capabilities 缺 INTERACTIVE_STDIN | supports_pty=True 但未声明 |
| — | P2 | codearts OOM (Bun 75GB 虚拟地址空间) | 待 cgroup v2 或加内存 |
| — | P3 | _fetch_skill_egress API 401 静默 fallback | 可能掩盖 auth 配置问题 |
| — | P3 | _fetch_skill_egress 冗余 API 调用 | 60s 缓存缓解 |
| — | P3 | DDL 去 DEFAULT 后列可空 | 读路径需 NULL 处理 |
574|# Hermes bwrap 沙箱 HOME 重构（2026-10-08 验证通过）

> 分支：feat/devbox-sandbox-integration
> 验证日期：2026-10-08
> 前置文档：§3 bwrap 文件系统隔离 + §6.1 实测修复记录

## 问题背景

hermes ACP 在 bwrap 沙箱（`--unshare-net`）中 `stopReason=refusal`，result 为空。经 strace + 源码分析定位到 4 层根因，逐层修复后验证通过。

## 根因分析与修复

### 根因 1：`~/.hermes` RW 导致 binary 脚本被改坏

**现象**：hermes source-update 修改 `~/.hermes` 下的 binary 脚本 Python 路径，导致 hermes 无法启动。

**修复**：`~/.hermes` 整个 RO 挂载（`_get_sandbox_ro_paths` 加回 `~/.hermes`），`sandbox_rw_paths = ()`。source-update 失败只打 warning 不退出（hermes_bootstrap.py:553-559）。

**文件**：`src/runtime/hermes.py`

### 根因 2：`installs/` 需要 RW（activate_dependencies 写 lock）

**现象**：`HERMES_DISABLE_LAZY_INSTALLS=1` 跳过 `prepare_launch` 的 source-update，但 `activate_dependencies` 仍需写 `installs/<key>/.install.lock`。installs 是 symlink 到 RO 的 `~/.hermes/installs`，写失败 → `SystemExit(1)`。

**修复**：`~/.hermes/installs` 单独加到 `sandbox_rw_paths`。bwrap 先 `--ro-bind /root/.hermes`（整个 RO），再 `--bind /root/.hermes/installs`（子目录 RW 覆盖）。

**文件**：`src/runtime/hermes.py`（`sandbox_rw_paths = ("~/.hermes/installs",)`）

### 根因 3：`HERMES_HOME` 环境变量未在 bwrap `--setenv` 里设置

**现象**：hermes binary 脚本启动时 `os.environ.get('HERMES_HOME')` 为空，fallback 到 `get_default_hermes_root()` → `Path.home() / ".hermes"` → `/root/.hermes`（RO），写 `logs/agent.log` 报 EROFS。

**根因**：`_build_env` 设了 `HOME` 但没设 `HERMES_HOME`。hermes binary 脚本从环境变量读 `HERMES_HOME`，不从 `.env` 读（`.env` 在 ACP adapter 里加载，但 binary 脚本先执行）。

**修复**：`_build_env` 里显式设 `env["HERMES_HOME"] = env["HOME"]`（沙箱 HOME 路径）。

**文件**：`src/agent/sandbox/backends/bwrap.py` `_build_env()`

### 根因 4：`_build_overlay_home` 非目录文件 symlink 到 RO 源导致 EROFS

**现象**：`auth.lock`、`state.db.fts_rebuild.lock` 等文件被 symlink 到 `~/.hermes/`（RO），写操作报 EROFS。

**修复**：`_build_overlay_home` 的 symlink 逻辑加 `if not os.path.isdir(src): continue`——非目录文件不 symlink，让 hermes 自己在可写的 HERMES_HOME 里创建。

**文件**：`src/agent/hermes_acp.py` `_build_overlay_home()`

### 根因 5：`session/resume` 用旧 session ID 发 prompt 导致 refusal

**现象**：沙箱 HOME 清理后旧 session ID 失效。`session/resume` 返回 warning（不是 error），hermes 内部创建新 session，但 response 里不返回新 session ID。grape 的 `_extract_session_id` 返回空，fallback 到旧 session ID 发 prompt → `session not found` → `stopReason=refusal`。

**修复**：`session/resume` 成功后检查返回的 session ID 是否有效（非空且不等于旧 ID），无效则走 `session/new`。

**文件**：`src/agent/hermes_acp.py` `stream_execute()`

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/runtime/hermes.py` | `~/.hermes` 恢复 RO + `sandbox_rw_paths = ("~/.hermes/installs",)` + 删除 `sandbox_rw_paths_dynamic` property |
| `src/agent/sandbox/backends/bwrap.py` | `_build_env` 加 `HERMES_HOME` + `HERMES_DISABLE_LAZY_INSTALLS=1` + per-task 沙箱 HOME + `sandbox_rw_paths_dynamic` 改回 `sandbox_rw_paths` |
| `src/agent/hermes_acp.py` | `_build_overlay_home` 非目录文件不 symlink + `session/resume` 后检查 session ID 有效性 + `_WRITABLE` 加 `backups` + stderr 截断 500→5000 |
| `src/runtime/agent.py` | Worker 依赖自检 + `RUNTIME_ENGINES` 引擎过滤 |
| `requirements-worker.txt` | 加 `requests` + `pyyaml` |

## 验证结果

| 检查项 | 结果 |
|--------|------|
| bwrap 启动 | ✅ `sandboxed via bwrap` |
| ACP 初始化 | ✅ `hermes ACP initialized` |
| session/new | ✅ session 创建成功 |
| LLM 调用 | ✅ opengw API status=200 |
| 输出回复 | ✅ result 非空（"已按前序 Agent 指定在 Issue #46 发布评论「你好」"） |
| 任务完成 | ✅ status=completed, exit=0 |
| 沙箱清理 | ✅ `cleaned sandbox home` |

## 设计决策

| 决策 | 理由 |
|------|------|
| `~/.hermes` 整个 RO + `~/.hermes/installs` 子目录 RW | installs 写 lock 必须 RW；其余 RO 防 source-update 改坏 binary |
| `HERMES_DISABLE_LAZY_INSTALLS=1` | 跳过 source-update（沙箱内 installs 已有依赖，不需要重装） |
| `HERMES_HOME` 在 `_build_env` 显式设置 | hermes binary 脚本从 env 读 HERMES_HOME，不从 .env 读 |
| 非目录文件不 symlink | `auth.lock`、`state.db.fts_rebuild.lock` 等文件需要可写 |
| `session/resume` 后检查 session ID | 沙箱 HOME 清理后旧 session ID 失效，需要 fallback 到 session/new |
