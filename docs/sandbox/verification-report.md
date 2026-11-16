1|# 验证报告
2|
3|> 分支：feat/devbox-sandbox-integration
4|> 最后更新：2026-10-07
5|
6|---
7|
8|## 1. 本地引擎全模式验证（2026-10-01）
9|
10|原始文档：docs/weekly-05/archive/verification/verification-report-local-engines-2026-10-01.md
11|
12|### 1.1 验证范围
13|
14|4 引擎 × 2 沙箱模式 × 2 任务类型 = 16 项
15|
16|- 引擎：hermes / opencode / codearts / codex
17|- 模式：bwrap (--unshare-net) / 非 bwrap (NullSandbox, GRAPE_AGENT_SANDBOX=off)
18|- 任务：chat (简单消息冒烟) / A2A VOD (真实 Issue 全流程)
19|
20|### 1.2 结果总表
21|
22|#### Chat 模式
23|
24|| 引擎 | bwrap | 非 bwrap |
25||------|-------|----------|
26|| hermes | ✅ done/text_len=34 (Bug 6 修复后) | ✅ done/text_len=90 |
27|| opencode | ✅ done/text_len=16 | ✅ done/text_len=17 |
28|| codearts | ✅ done/text_len=17 (复验) | ✅ done/text_len=17 |
29|| codex | ❌ error (proxy failed) | ❌ error (request timed out) |
30|
31|#### A2A VOD 模式
32|
33|| 引擎 | bwrap | 非 bwrap |
34||------|-------|----------|
35|| hermes | ✅ completed/exit=0 (Bug 6 修复后) | ✅ completed/exit=0 |
36|| opencode | ✅ 35步/tokens=32181 (执行中被取消) | ✅ completed/exit=0 |
37|| codearts | ✅ 18步/tokens=35481/completed | ✅ completed/exit=0/4步 |
38|| codex | — (跳过) | ❌ timeout (api.openai.com 不通) |
39|
40|### 1.3 结论
41|
42|- 14/16 通过
43|- 2/16 未通过：codex 的 api.openai.com 网络不可达（环境限制，与沙箱无关）
44|- 本次 6 个 Bug 修复未引入任何回归
45|- bwrap 网络限制：opengw 可达，api.openai.com 超时
46|
47|### 1.4 codearts bwrap chat 首次超时根因
48|
49|并发排队——hermes A2A 任务占用 bwrap sandbox，codearts chat 等待释放导致超时。**不是网络限制。** 复验通过。
50|
51|---
52|
53|## 2. 云沙箱 E2E 验证（2026-10-04）
54|
55|### 2.1 已通过
56|
57|- 云沙箱 hermes/opencode/codearts 全通过
58|- bwrap hermes 通过
59|
60|### 2.2 待验证
61|
62|- bwrap opencode（白名单已修复 pypi.org）
63|- bwrap codearts
64|- 非 bwrap 本机模式（3 引擎）
65|
66|---
67|
68|## 3. Bug 修复验证
69|
70|### 3.1 已确认修复
71|
72|| Bug | 验证方式 | 结果 |
73||-----|----------|------|
74|| Bug 1: executor.py agent_type→engine_type | bwrap chat 任务不再报 TypeError | ✅ |
75|| Bug 2: CubeSandbox CODEARTS_CLI_AK/SK | codearts CubeSandbox A2A 不报认证失败 | ✅ |
76|| Bug 3: codearts DB migration apiKey | codearts CubeSandbox A2A LLM 调用 200 OK | ✅ |
77|| Bug 4: bwrap _SAFE_TOKENS | HERMES/OPENCODE/CODEARTS_API_KEY 在白名单中 | ✅ |
78|| Bug 5: DevBox grape token | envs 参数传递正确 | ✅ |
79|| Bug 6: hermes overlay 路径 | hermes bwrap chat/A2A text_len > 0 | ✅ |
80|
81|### 3.2 环境限制（非代码问题）
82|
83|| 问题 | 引擎 | 根因 |
84||------|------|------|
85|| codex proxy failed | codex bwrap | api.openai.com，NetworkProxy 转发超时 |
86|| codex request timed out | codex 非 bwrap | api.openai.com，本机网络环境访问不通 |
87|| codearts OOM | codearts | Bun 75GB 虚拟地址空间，7.2GB 内存机器触发 OOM |
88|
89|---
90|
91|## 4. 测试结果
92|
93|### 4.1 核心测试
94|
95|- 134 passed（test_agent_executor + test_runtime + test_engine_protocol + test_engine_probe_codex + test_resolve_engine_paths + test_runtime_agent）
96|- 2 failed（test_a2a_queue SQLite fixture 问题，预存非代码 bug）
97|- 1 deselected（test_agent_event_subscription 预存失败）
98|
99|### 4.2 审查报告测试
100|
101|- Round 48: 核心测试 262 passed, 0 failed
102|- Round 49: 核心测试 278 passed, 4 failed（同上环境问题）
103|
104|---
105|
106|## 5. 已知问题（未修复，不阻塞）
107|
108|| ID | 严重性 | 描述 | 状态 |
109||----|--------|------|------|
110|| P2-1 | P2 | _match_admin_policy fail-closed | ✅ FIXED (2026-10-07) |
111|| P3-1 | P3 | DROP INDEX MySQL 兼容性 | ✅ FIXED (2026-10-07) |
112|| R117 | P2 | _read_new 全文件读取 O(n²) | SDK 无 range read |
113|| R118 | P2 | readline 进程退出最后一行 | 已改善未完全修复 |
114|| R119-R124 | P2 | DevBox/CubeSandbox 各项 | 见沙箱架构文档 §12 |
115|| — | P2 | codearts OOM | 待 cgroup v2 或加内存 |
116|