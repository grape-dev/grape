# bwrap 沙箱 UDS Bridge 并发性能修复 + BOLA global scope 放行 + hermes 路径修复

> **更新（bola-simplify-design）**：BOLA 已简化——system scope 废弃，统一用 global；scope_detail 删除；claim BOLA 拦截删除，宽松放行。详见 bola-simplify-design.md。

## 日期
2026-10-08

## 背景

codearts bwrap 沙箱模式下，每个 VOD 步骤耗时 39-111 秒，非 bwrap 模式 13 秒完成。
审计日志 NetworkProxy latency_ms 1-57ms（代理到目标快），但 codearts 日志端到端 10-20 秒/次。

## 根因分析

### 问题 1：_UnixSocketBridge 单线程串行瓶颈

**数据流**：
```
codearts(Bun) → TCP 127.0.0.1:8888 (stage2, in bwrap)
  → UDS socket → _UnixSocketBridge (host, UnixStreamServer)
  → TCP 127.0.0.1:{动态端口} (NetworkProxy, ThreadingTCPServer)
  → HTTP CONNECT → target
```

**根因**：`_UnixSocketBridge` 使用 `socketserver.UnixStreamServer`（单线程），
并发 UDS 连接串行处理。codearts 同时发起 heartbeat/collector/LLM 等多个 HTTPS 请求时，
每个 tunnel 在 bridge 层串行执行，后续连接排队等待。

**对照实验证据**（194 机器，2026-10-07）：

| 并发数 | 直连代理 | 单线程 bridge | 多线程 bridge |
|--------|----------|--------------|--------------|
| N=1    | 0.15s    | 0.16s        | 0.15s        |
| N=4    | 0.63s    | 1.49s        | 0.62s        |
| N=8    | 1.38s    | 4.40s (1超时) | 1.37s       |

- 单线程 bridge N=8 total=4.40s，max=1.08s，1 个超时失败
- 多线程 bridge N=8 total=1.37s，max=0.20s，无失败
- 多线程与直连代理持平

### 问题 2：BOLA 校验未放行 global scope

**根因**：`runtime.py` 的 `_check_task_access`、`runtime_claim`、`runtime_claim_by_tag`
三处 BOLA 校验只放行 `system` scope，`global` scope 的 worker token 认领任务后
task start/fail/state 全部返回 403 forbidden。

## 修复

### 修复 1：ThreadingUnixStreamServer（commit 7705b9fa）

**文件**：`src/agent/sandbox/isolation/network.py`

**改动**：
```python
# 修改前
self._server = socketserver.UnixStreamServer(self.socket_path, handler)

# 修改后
self._server = socketserver.ThreadingUnixStreamServer(self.socket_path, handler)
self._server.daemon_threads = True
```

**线程安全**：Handler 无共享状态，`proxy_port` 是只读闭包变量，
`handle()` 里的 `upstream`、`pipe`、`t` 都是 per-connection 局部变量。

### 修复 2：BOLA global scope 放行（commit 5a82f8e7）

**文件**：`src/api/routes/runtime.py`

**改动**（三处）：

1. `_check_task_access`：global scope 直接返回 task_user_id
2. `runtime_claim`：`if cred.scope not in ('system', 'global'):` 跳过 BOLA
3. `runtime_claim_by_tag`：同上

## 验证结果

**环境**：194 远程 worker（codearts bwrap 沙箱），115 主服务

**任务**：seq=9389，issue #46，prompt="回复一句话：你好"

**执行时间线**：
- 00:28:02 — sandbox 启动，codearts bwrap 启动
- 00:28:07 — stage2 第一个连接（codearts 开始发请求）
- 00:28:15 — opengw LLM 返回（status=200, 0.8s）
- 00:28:45 — codearts done: exit=0, text_len=44, stderr=''

**总耗时 43 秒**。

**codearts kernel log gap 分析**：
- 最大 GAP=3.0s（snap-access 503 重试）
- opengw LLM 调用 0.8s（之前 10-20s）
- stage2 8 个并发连接全部并行处理

**对比**：
| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 每步耗时 | 39-111s | 43s（含启动） |
| opengw LLM | 10-20s | 0.8s |
| 并发连接 | 串行排队 | 并行处理 |
| 任务结果 | text_len=0（stdout 缓冲） | exit=0, text_len=44 |

## 遗留问题

### codearts stdout 缓冲

codearts (Bun) 在管道模式下 stdout 块缓冲，`BUN_STDOUT_BUFFERING=none` 不是真实环境变量。
`raw=0 parsed=19` 说明 NDJSON 事件被解析但 text 类型事件未被 grape reader 收到。
text_len=44 可能通过其他途径获取。

**已验证的解决方案**：`script -q -c` 分配 PTY 强制行缓冲（194 手动验证有效）。
但未在完整 bwrap 沙箱链路中验证，暂未提交。

### worker hermes probe 阻塞

194 worker 注册了 codearts + hermes，hermes probe 失败后阻塞 ~15s。
临时解决方案：移走 hermes binary 只注册 codearts。
长期方案：worker 支持按引擎过滤注册（`RUNTIME_ENGINES` 环境变量）。

### 问题 3：hermes bwrap 沙箱 Python 路径缺失

**根因**：hermes binary (`/usr/local/bin/hermes`) 是 wrapper 脚本，exec 指向
`/usr/local/lib/hermes-agent/.hermes/bin/hermes`，后者硬编码了安装时的
Python 路径（如 `/root/.hermes/tools/python-3.14.7.../bin/python3`）。
worker 的 HOME 可能不同（如 `/tmp/remote-worker-home`），导致 `~/.hermes` 展开成
错误路径，hermes 在沙箱内找不到 Python。

**修复**（commit `82815020`）：从 hermes binary 脚本内容动态解析真实 Python 路径，
提取安装 HOME（如 `/root/.hermes`）加入 sandbox_ro_paths。
不假设 root——读 wrapper 脚本找 exec 指向的真实 binary，再从真实 binary 提取 python 路径。
任何用户安装都能正确解析。

**验证**：`_get_sandbox_ro_paths` 返回包含 `/root/.hermes`，
BWRAP binds 确认包含 `--ro-bind /root/.hermes`。

**遗留**：194 上 hermes python 环境依赖不完整（缺 `dotenv`、`ruamel` 等），
source-update 中断后未完成依赖安装。非沙箱模式下 `hermes acp` 可启动
（用旧依赖 + 跳过 source completion），bwrap 沙箱内因 `-I` isolated mode
不读 user site-packages 导致缺模块。需要修复 194 hermes 安装环境。

## 相关文件

- `src/agent/sandbox/isolation/network.py` — _UnixSocketBridge ThreadingUnixStreamServer
- `src/api/routes/runtime.py` — BOLA global scope 放行
- `src/agent/sandbox/backends/bwrap.py` — stage2 TCP→UDS 桥接
- `src/runtime/codearts.py` — codearts 引擎配置
- `src/runtime/hermes.py` — hermes sandbox_ro_paths 动态解析 Python 安装路径
- `docs/weekly-05/bwrap-sandbox-verification-report.md` — 之前的验证报告

## 相关 commit

- `7705b9fa` — fix: _UnixSocketBridge 单线程串行瓶颈改 ThreadingUnixStreamServer
- `5a82f8e7` — fix: BOLA global scope 放行——claim/claim-by-tag/_check_task_access
- `82815020` — fix: hermes bwrap 沙箱动态解析 binary 脚本里的 Python 安装路径
- `63b472c0` — fix: bwrap 禁用 OTEL 遥测导出（之前的前置修复）
