1|# Skill Egress 出站域名声明设计
2|
3|> 分支：feat/devbox-sandbox-integration
4|> 归一日期：2026-10-07
5|> 归一来源：docs/weekly-05/archive/skill-egress/ 下 3 份文档
6|
7|---
8|
9|## 1. 定位
10|
11|egress 是"skill 声明出站需求"的工程机制，不是"授权放行"。fail-closed 硬要求。
12|
13|原始文档：docs/weekly-05/archive/skill-egress/design-skill-egress-domains.md
14|
15|---
16|
17|## 2. 三层架构
18|
19|| Layer | 职责 | 实现 |
20||-------|------|------|
21|| Layer 1 | skill 声明（frontmatter egress 策略对象） | SKILL.md frontmatter |
22|| Layer 2 | 策略引擎审批（签名校验 + 管理员策略 + 拒绝规则 → per-task 策略） | PolicyEngine.apply() |
23|| Layer 3 | NetworkProxy 校验（域名+端口+IP/DNS/SNI + 方法/路径/大小 + 审计） | NetworkProxy._check_request() |
24|
25|---
26|
27|## 3. 拒绝规则
28|
29|### 3.1 两级拒绝
30|
31|- **绝对拒绝**：127.0.0.0/8 / ::1 / 元数据服务（169.254.169.254），永不可覆盖
32|- **默认拒绝**：内网段（10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16），管理员可例外
33|
34|### 3.2 关键设计决策
35|
36|- IPv4-mapped IPv6 归一化后匹配（防 curl -g -6 绕过）
37|- HTTPS CONNECT 隧道退化为域名+端口+IP+SNI 校验（不做 MITM）
38|- HTTP 标准转发做 L7 校验（method/path/body/redirect）
39|- SNI vs CONNECT host 一致性校验，SNI 不可见时拒绝
40|- max_body 流式计数，超限断连
41|- path_prefix 段边界匹配 + 路径规范化防穿越（单次解码不递归 + .. pop 回溯 + // 合并 + 去尾斜杠）
42|- HTTP/3 阻断（iptables UDP 443）
43|- 审计日志不可篡改（append-only + 落盘失败断连）
44|- 管理员策略 vs 管理员例外两套独立机制
45|- 紧急撤销：per-task 策略不自动失效，紧急情况 kill 任务
46|- 速率限制：per task per host 100 次/分钟 + 总出站 100MB
47|
48|---
49|
50|## 4. Phase 1+2 实施
51|
52|原始文档：docs/weekly-05/archive/skill-egress/impl-skill-egress-phase1-2.md
53|
54|### 4.1 设计
55|
56|- EgressPolicy 数据结构 + 最小策略引擎（Layer 2 进程内实现）
57|- NetworkProxy 接受策略对象 + _check_request 拆 4 函数
58|- HTTP L7 校验 + HTTPS SNI 校验 + 审计增强
59|- IPv4-mapped IPv6 归一化 + 拒绝列表分级
60|- Feature flag SANDBOX_EGRESS_ENABLED 默认关闭
61|
62|### 4.2 关键设计决策
63|
64|- Phase 1+2 不是安全边界（策略引擎不做 skill 签名校验），仅内部 skill + 管理员审核 + feature flag 显式开启
65|- bwrap 调 PolicyEngine.apply()，不直接解析 frontmatter（分层边界清晰）
66|- skill_digest 只覆盖 SKILL.md（Phase 3 升级为整包 digest）
67|- _check_request 拆 4 函数：_check_deny_lists / _check_admin_whitelist / _check_skill_egress / _check_l7
68|- admin 白名单命中前必须先过拒绝列表（不是后门）
69|- admin 白名单仅 per-task，不作为全局放行通道
70|- 拒绝列表分级：绝对拒绝 + 默认拒绝（Phase 1+2 无例外）
71|- IP 归一化用 ipaddress.ipv4_mapped
72|- path_prefix 段边界匹配 + 路径规范化防穿越（单次解码不递归）
73|- max_body 流式计数（chunked 按原始字节）
74|- redirect 最小改造（只读状态行判断 3xx）
75|
76|---
77|
78|## 5. Phase 3 整包 digest + 管理机制
79|
80|原始文档：docs/weekly-05/archive/skill-egress/impl-skill-egress-phase3.md
81|
82|### 5.1 整包 digest
83|
84|目录下所有文件按排序路径 + 内容 sha256，符号链接拒绝，单文件 10MB / 整包 100MB 上限。新增 content_hash_package 列（不改原 content_hash）。
85|
86|### 5.2 管理员策略
87|
88|| 策略 | 行为 |
89||------|------|
90|| auto_approve | 自动放行 egress |
91|| manual_approve | 拒绝 egress 声明，任务以无出站模式启动 |
92|| deny | 拒绝 |
93|
94|- 默认 auto_approve（过渡阶段，后续收紧 user/repo 为 manual_approve）
95|- manual_approve = 拒绝 egress 声明，任务以无出站模式启动（不做实时审批流）
96|- 策略引擎 DB 查询失败 → 返回 None → fail-closed → manual_approve（P2-1 修复后）
97|
98|### 5.3 管理员例外
99|
100|| scope | 审批强度 | TTL |
101||-------|---------|-----|
102|| task | 单人审批 | 单次 |
103|| skill | 单人审批 | 24h，绑定 digest |
104|| global | 双人审批 | 7d |
105|
106|- 无身份系统时 global scope 暂不开放（API 返回 403）
107|- rate_limits 只能由管理员策略设置，skill frontmatter 出现则拒绝
108|
109|### 5.4 速率限制
110|
111|- 滑动窗口 deque + per-task 内存计数
112|- per task per host 100 次/分钟 + 总出站 100MB
113|
114|### 5.5 fail-closed
115|
116|- 审计写入失败 → 断连（group commit 策略）
117|- 策略引擎 DB 查询失败 → 返回 None → fail-closed
118|
119|---
120|
121|## 6. 表结构
122|
123|### 6.1 egress_admin_policy
124|
125|```sql
126|CREATE TABLE IF NOT EXISTS egress_admin_policy (
127|    id            INTEGER PRIMARY KEY AUTOINCREMENT,
128|    skill_name    VARCHAR(191) NOT NULL,
129|    skill_scope   VARCHAR(64) NOT NULL DEFAULT 'official',
130|    policy        VARCHAR(32) NOT NULL,
131|    approved_by   VARCHAR(128),
132|    approved_at   TEXT,
133|    reason        TEXT,
134|    created_at    TEXT DEFAULT (datetime('now')),
135|    updated_at    TEXT DEFAULT (datetime('now')),
136|    UNIQUE(skill_name, skill_scope)
137|);
138|```
139|
140|### 6.2 egress_admin_exception
141|
142|```sql
143|CREATE TABLE IF NOT EXISTS egress_admin_exception (
144|    id            INTEGER PRIMARY KEY AUTOINCREMENT,
145|    host          VARCHAR(255) NOT NULL,
146|    host_type     VARCHAR(16) NOT NULL,
147|    ports         VARCHAR(256) NOT NULL,
148|    methods       VARCHAR(256) NOT NULL DEFAULT '["GET"]',
149|    scope         VARCHAR(16) NOT NULL,
150|    scope_target  VARCHAR(191) NOT NULL DEFAULT '',
151|    skill_digest  VARCHAR(128) NOT NULL DEFAULT '',
152|    ttl           INTEGER NOT NULL DEFAULT 0,
153|    approved_by   VARCHAR(256) NOT NULL,
154|    reason        TEXT,
155|    created_at    TEXT DEFAULT (datetime('now')),
156|    expires_at    TEXT,
157|    is_active     INTEGER NOT NULL DEFAULT 1,
158|    UNIQUE(host, host_type, ports, scope, scope_target)
159|);
160|```
161|
162|### 6.3 skill 表
163|
164|新增 `content_hash_package TEXT` 列（不改原 content_hash）。
165|
166|---
167|
168|## 7. 已废弃
169|
170|- egress_domains 简写（改为 egress 策略对象列表）
171|- Phase 1+2 的 SKILL.md-only digest（Phase 3 替换为整包 digest）
172|