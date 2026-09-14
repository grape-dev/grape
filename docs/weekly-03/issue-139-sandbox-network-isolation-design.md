# Issue #139 设计方案：网络出口策略 + 资源限额 + 访问审计

> Issue：https://gitcode.com/grape-dev/grape/issues/139
> 父 Issue：#64 支持任务执行沙箱隔离
> 分支：`feat/issue-139-sandbox-network-isolation`（基于 `weekly-03`）
> 日期：2026-09-14（v11 — codex worker 实测验证 + /opt/node 修复 + get_stats 接入）

---

## 一、参考实现比对

### 1.1 三个参考仓库

| 仓库 | 语言 | 核心能力 | 对应本 Issue |
|------|------|----------|-------------|
| anthropics/sandbox-runtime | TypeScript | bwrap --unshare-net + socat Unix socket 桥接 + HTTP/SOCKS 代理 + seccomp + DNS 地址校验 + 凭据遮蔽 + 违规通知 + hop-by-hop header 剥离 + 请求走私防护 + CONNECT 超时/半关闭 + 代理认证 | #1 网络隔离 + #3 审计 |
| john221wick/deepagents-sandbox | Python | bwrap --clearenv --cap-drop ALL --disable-userns + cgroup v2 + 降级 + 环境预检 + 工作区路径逃逸防护 + OOM 检测 | #2 资源限额 |
| msaad00/agent-bom | Python | 代理层 JSONL 审计 + 哈希链 + RotatingAuditLog + policy/reason + HMAC key 轮转 + evidence 分层脱敏 + Prometheus 指标 | #3 审计 |

### 1.2 逐项对比与采纳决策

| 设计点 | 参考实现 | 采纳 | 原因 |
|--------|----------|------|------|
| 网络打通方式 | Anthropic: --bind Unix socket | ✅ | 不需要 veth/nsenter/iptables，零 sudo，隔离更彻底 |
| 代理 env 变量 | Anthropic: 8+ 个全覆盖 | ✅ | 不同引擎的 HTTP 客户端认不同变量名 |
| 域名匹配 | Anthropic: 通配符 *.example.com | ✅ | 子域名管理更灵活 |
| DNS 解析后地址校验 | Anthropic: resolved-address-guard.ts | ✅ | 防 DNS 重绑定到内网/元数据接口 |
| 沙箱内 DNS 环境 | deepagents: 挂载 resolv.conf | ✅ | 不挂则 Agent DNS 解析失败 |
| CA 证书 / TLS 信任链 | Anthropic: 确保 /etc/ssl 挂载 | ✅ | 不挂则 HTTPS 验证失败 |
| cgroup 降级策略 | deepagents: warn+跳过 | ✅ | Worker 环境差异不应拒绝启动 |
| cgroup context manager | deepagents: __enter__/__exit__ | ✅ | 异常时保证清理 |
| OOM 检测 | deepagents: exit_code==137 | ✅ | 区分 OOM 和正常崩溃 |
| 环境预检 | deepagents: detect.py | ✅ | Worker 环境差异提前发现 |
| 审计哈希链 | agent-bom: prev_hash/record_hash | ✅ | 防篡改 |
| 审计轮转 | agent-bom: RotatingAuditLog 100MB | ✅ | 防磁盘撑满 |
| 审计 policy+reason | agent-bom: 结构化字段 | ✅ | 直接回答为什么放行/拒绝 |
| 代理进程健康监控 | Anthropic: exit/error 事件 | ✅ | 崩溃后快速失败而非 hang |
| 违规通知 | Anthropic: 结构化 403 body | ✅ | Agent 可感知拒绝原因 |
| trap EXIT 清理 | Anthropic: trap 'kill %1' EXIT | ✅ | 桥接进程不残留 |
| 主进程信号处理 | Anthropic: registerCleanup | ✅ | SIGTERM 时清理沙箱资源 |
| 凭据 env 遮蔽 | Anthropic: --unsetenv | ✅ | Agent 读不到真实 token |
| **hop-by-hop header 剥离** | Anthropic: stripHopByHop | ✅ **v4 新增** | 防请求走私 |
| **请求走私防护** | Anthropic: CL/TE 检查 | ✅ **v4 新增** | Content-Length + Transfer-Encoding 共存时拒绝 |
| **CONNECT 超时 + 半关闭** | Anthropic: 30s 超时 + EOF 检测 | ✅ **v4 新增** | 防 hang + 线程泄漏 |
| **ECONNRESET 捕获** | Anthropic: socket.on('error') | ✅ **v4 新增** | 防代理进程崩溃 |
| **HTTP 转发完整实现** | Anthropic: http-proxy.ts | ✅ **v4 新增** | 原方案只有注释没有实现 |
| **bwrap --cap-drop ALL** | deepagents: executor.py:158 | ✅ **v4 新增** | 阻止 capability 逃逸 |
| **bwrap --disable-userns** | deepagents: executor.py:157 | ✅ **v4 新增** | 阻止嵌套 namespace 逃逸 |
| **git over SSH 禁止** | Anthropic: GIT_SSH_COMMAND | ✅ **v4 新增** | SSH 不走代理，禁止外联 |
| **审计 path 脱敏** | agent-bom: evidence 分层 | ✅ **v4 新增** | URL 查询参数含 token |
| **审计 latency/status** | agent-bom: metrics | ✅ **v4 新增** | 调试可见性 |
| **端口分配实现** | Anthropic: listen-in-range.ts | ✅ **v4 新增** | 原方案只有函数名 |
| **敏感 env 扫描** | deepagents: --clearenv 启发 | ✅ **v4 新增** | 发现未遮蔽的敏感变量 |
| symlink 攻击防护 | Anthropic: findSymlinkInPath | ❌ 后续 | 复杂度高，bwrap ro-bind 已限制写入 |
| seccomp BPF 防绕过 | Anthropic: apply-seccomp | ❌ 后续 | 需编译 C 二进制 |
| TLS 终止代理 (MITM) | Anthropic: tls-terminate-proxy | ❌ 后续 | 隐私敏感，按需启用 |
| 并发清理计数器 | Anthropic: activeSandboxCount | ❌ | grape task_id 唯一，无共享资源 |
| SOCKS5 代理 | Anthropic: mux-proxy | ❌ | grape 不用 SSH，HTTP CONNECT 够用 |
| 代理认证 token | Anthropic: proxyAuthToken | ❌ 后续 | 白名单已限制出站，认证是纵深防御 |
| 审计 HMAC key 轮转 | agent-bom: 多 key 列表 | ❌ 后续 | 单 key 够用 |
| Prometheus 指标 | agent-bom: blocked_total | ❌ 后续 | grape 已有 /metrics |

---

## 二、环境现状（代码级 + 环境级验证）

### 2.1 当前 bwrap 配置

```
src/agent/sandbox.py:53
opts = [bwrap, "--die-with-parent", "--unshare-all", "--share-net"]
```

### 2.2 Agent 子进程需要访问的外部服务（白名单清单）

| 类别 | 域名 | 用途 | 来源 |
|------|------|------|------|
| Git 平台 | gitcode.com | git clone/push | repogit.py |
| Git 平台 API | api.gitcode.com | Issue/PR CRUD | executor.py |
| Git 平台 | raw.gitcode.com | ZIP 下载 | docs |
| Git 平台 | github.com | github git 操作 | repogit.py |
| Git 平台 API | api.github.com | GitHub API | vcs/github.py |
| 模型 API | opengw.clouddeveloper.club | hermes/opencode 主模型 | ~/.hermes/config.yaml |
| 模型 API | api.deepseek.com | deepseek 模型 | ~/.hermes/config.yaml |
| 模型 API | api.modelarts-maas.com | 华为云 ModelArts | configs/ |
| 模型 API | api.openai.com | OpenAI API（codex 默认端点） | codex CLI |
| 包管理 | registry.npmmirror.com | npm registry | ~/.npmrc |
| 包管理 | pypi.tuna.tsinghua.edu.cn | pip 源 | worker_package.py |
| 对象存储 | obs.cn-north-4.myhuaweicloud.com | OBS | storage/obs.py |
| grape API | 127.0.0.1:3000 或公网 | A2A / tasks | agent.py |

### 2.3 环境工具可用性

| 工具 | 可用 | 说明 |
|------|------|------|
| bwrap 0.4.1 | ✅ | 支持 --unshare-net / --unshare-pid / --bind / --cap-drop / --disable-userns |
| cgroup v1 | ✅ | memory/cpu/pids 子系统独立挂载 |
| sudo | ✅ | yangaiwu NOPASSWD |
| /tmp 可写 | ✅ | Unix socket 文件存放 |
| /etc/ssl/certs | ✅ | CA 证书 bundle |
| /etc/pki/tls/certs | ✅ | RHEL/openEuler CA 证书 |
| socat | ❌ | 用 Python 标准库替代 |

### 2.4 凭据注入现状与遮蔽计划

| 凭据 | 注入方式 | Agent 可见 | P0 遮蔽 | 原因 |
|------|----------|-----------|---------|------|
| BOT_GITCODE_TOKEN | env | ✅ | ❌ 不遮蔽 | QA/SRE skill 直调 api.gitcode.com 用 $BOT_GITCODE_TOKEN，遮蔽后 401。skill 未迁移到 grape API（/api/repos/...）。靠网络白名单限制（只能连 gitcode.com） |
| USER_GITCODE_TOKEN | env | ✅ | ❌ 不遮蔽 | 同上 |
| BOT_GITHUB_TOKEN | env | ✅ | ❌ 不遮蔽 | 同上 |
| BOT_TOKEN | env（别名） | ✅ | ❌ 不遮蔽 | 同上 |
| HUAWEICLOUD_SDK_AK/SK | env | ✅ | ❌ 不遮蔽 | QA/SRE terraform plan/apply 必须直连华为云 OBS + provider API，无等价 API 包装层。靠网络白名单限制（只能连 obs.cn-north-4.myhuaweicloud.com） |
| CODEARTS_CLI_AK/SK | env | ✅ | ❌ 不遮蔽 | codearts 引擎需要 AK/SK 认证 |
| GRAPE_API_TOKEN | env | ✅ | ❌ 保留 | Agent 需调 grape API，权限有限 |

**P0 阶段结论**：不遮蔽任何凭据。网络白名单是唯一的安全边界 — Agent 即使有 token，也只能连白名单内的域名。

**凭据泄露监控（审计层补偿）**：不遮蔽不等于无感知。代理审计层增加凭据泄露检测规则：
- 代理转发请求时，扫描 URL 查询参数和请求头中是否包含已知 token 值（BOT_GITCODE_TOKEN / USER_GITCODE_TOKEN / BOT_TOKEN 的实际值）
- 如果检测到，审计记录标记 `credential_leak: true` + `leaked_token: "BOT_GITCODE_TOKEN"`，并升级为高危事件
- 这不能阻止泄露（token 已在 env 中），但能在事后发现 — 比完全无感知强
- 实现方式：prepare_sandbox 时读取被注入的 token 值，传给代理做字符串匹配

**后续**：skill 迁移到 grape API 后（Issue #45 的 /api/repos/...），恢复 GitCode token 遮蔽；华为云 AK/SK 需实现代理层凭据注入后再遮蔽。代理层凭据注入可参考 phantom token 模式 — 真实凭证不进入沙箱 env，代理在出站请求时动态注入 Authorization 头。

---

## 三、方案设计

### 3.1 总体架构

```
grape 主进程（executor.py）
  │
  ├─ prepare_sandbox(task_id, cwd, backend_name)
  │    │
  │    ├─ 1. detect_sandbox_env() — 环境预检 + 敏感 env 扫描
  │    │
  │    ├─ 2. CgroupSlice(task_id) — cgroup v1（memory + cpu + pids）
  │    │     └─ 不可用则降级
  │    │
  │    ├─ 3. SandboxProxy(task_id) — Python HTTP 代理
  │    │     ├─ 监听 TCP 127.0.0.1:{port}（端口 0 自动分配）
  │    │     ├─ CONNECT 隧道（30s 握手超时 + 半关闭检测 + ECONNRESET 捕获）
  │    │     ├─ HTTP 转发（完整实现 + hop-by-hop 剥离 + CL/TE 走私检查）
  │    │     ├─ 白名单过滤（通配符 + DNS 地址校验）
  │    │     ├─ JSONL 审计（哈希链 + 轮转 + policy/reason + latency/status + path 脱敏）
  │    │     ├─ 违规通知（结构化 403 JSON body）
  │    │     └─ 崩溃监控（exit 事件 → kill Agent）
  │    │
  │    ├─ 4. UnixSocketBridge — Unix socket → TCP 代理端口
  │    │
  │    └─ 5. 构造 bwrap 命令
  │         ├─ --unshare-net（完全隔离网络）
  │         ├─ --unshare-pid --proc /proc（PID 隔离）
  │         ├─ --new-session --die-with-parent（加固）
  │         ├─ --cap-drop ALL（加固，bwrap 0.4.1 不支持 --disable-userns 已去掉）
  │         ├─ --bind socket_path（挂入 Unix socket）
  │         ├─ --ro-bind /etc/ssl/certs + /etc/pki（CA 证书）
  │         ├─ --symlink /dev/null /etc/shadow（遮蔽敏感文件）
  │         ├─ --setenv HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/GRPC_PROXY/NO_PROXY/...
  │         │    └─ NO_PROXY 只含 ::1（移除 localhost/127.0.0.1，让 grape API 走代理）
  │         ├─ --setenv GIT_SSH_COMMAND=/bin/false（禁止 SSH 外联）
  │         ├─ --setenv GRAPESOCK socket_path
  │         ├─ --unsetenv（P0 不遮蔽任何凭据 — 见 §2.4）
  │         └─ -- /bin/sh -c 'python3 bridge.py & trap "kill %1" EXIT; exec {agent_cmd}'
  │
  ├─ bwrap 沙箱子进程
  │    ├─ 完全隔离网络（无网卡，lo 可用）
  │    ├─ Unix socket 可见（--bind）
  │    ├─ 沙箱内 Python 桥接: localhost:3128 → Unix socket → 宿主代理
  │    ├─ 受 cgroup 限制（CPU/memory/pids）
  │    ├─ 敏感 env 已遮蔽 + SSH 已禁止 + capabilities 已丢弃
  │    └─ CA 证书可用
  │
  └─ cleanup_sandbox()
       ├─ kill 代理 + 桥接进程
       ├─ 删 Unix socket 文件
       ├─ 销毁 cgroup 子组
       └─ 注册到 terminate_active_agents()（SIGTERM/SIGINT 时清理）
```

### 3.2 网络隔离实现

#### 3.2.1 代理 env 变量生成

```python
# src/agent/proxy.py

PROXY_ENV_KEYS = [
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "GRPC_PROXY", "grpc_proxy",
]

NO_PROXY_VALUES = ",".join([
    "::1",
    # 注意：不排除 localhost/127.0.0.1 — grape API 在 127.0.0.1:3000，
    # 必须走代理才能从沙箱到达宿主的 3000 端口（--unshare-net 后 localhost 是沙箱内 loopback）
])

def generate_proxy_envs(proxy_port: int = 3128) -> list[tuple[str, str]]:
    """生成 bwrap --setenv 参数对。"""
    envs = []
    proxy_url = f"http://localhost:{proxy_port}"
    for key in PROXY_ENV_KEYS:
        envs.append((key, proxy_url))
    envs.append(("NO_PROXY", NO_PROXY_VALUES))
    envs.append(("no_proxy", NO_PROXY_VALUES))
    # git 代理认证方式（采纳 Anthropic sandbox-utils.ts:552）
    envs.append(("GIT_CONFIG_PARAMETERS", "'http.proxyAuthMethod=basic'"))
    # 禁止 git over SSH（SSH 不走 HTTP 代理）
    envs.append(("GIT_SSH_COMMAND", "/bin/false"))
    return envs

def get_dynamic_whitelist(base_whitelist: list[str]) -> list[str]:
    """动态白名单：基础白名单 + GRAPE_SERVER_URL 的地址。
    Agent 调 grape API（127.0.0.1:3000 或公网 IP）需要走代理，必须加入白名单。

    注意：localhost:3000 的流量会经过代理审计。这是预期行为 —
    本机 grape API 调用虽然可信，但审计可发现 Agent 异常的 API 调用模式
    （如大量 /api/repos/ 请求可能表明 token 泄露后被批量操作）。
    如果审计噪音过多，可通过 SANDBOX_AUDIT_SKIP_LOCALHOST=1 跳过 localhost 审计。
    """
    wl = list(base_whitelist)
    server_url = os.environ.get("GRAPE_SERVER_URL", "") or os.environ.get("RUNTIME_SERVER_URL", "")
    if server_url:
        from urllib.parse import urlparse
        parsed = urlparse(server_url)
        host = parsed.hostname or ""
        port = parsed.port
        if host and host not in wl:
            # 白名单匹配是 host 级别（不含端口），代理转发时用 host:port 连接
            wl.append(host)
        # 如果是 localhost/127.0.0.1，也加入（代理需要放行才能转发到宿主 3000）
        if host in ("localhost", "127.0.0.1") and "127.0.0.1" not in wl:
            wl.append("127.0.0.1")
    # 本机 Worker 的 grape API 地址始终加入
    if "127.0.0.1" not in wl:
        wl.append("127.0.0.1")
    return wl
```

#### 3.2.2 域名白名单匹配

```python
def match_domain(host: str, pattern: str) -> bool:
    """支持: example.com / *.example.com / example.com:443。
    拒绝过宽模式: *.com / *"""
    if pattern.startswith("*."):
        suffix = pattern[2:]
        if "." not in suffix or suffix.startswith(".") or suffix.endswith("."):
            return False
        return host == suffix or host.endswith("." + suffix)
    host_base = host.split(":")[0]
    pattern_base = pattern.split(":")[0]
    return host_base == pattern_base
```

#### 3.2.3 DNS 解析后地址校验

```python
import ipaddress, socket

DENIED_ADDRESS_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
]

CLOUD_METADATA_ADDRESSES = [
    "100.100.100.200",   # Alibaba Cloud
    "168.63.129.16",     # Azure WireServer
    "192.0.0.192",       # Oracle Cloud
]

def check_resolved_address(host: str, port: int) -> tuple[bool, str]:
    """DNS 解析后校验 IP。防止 DNS 重绑定到内网/元数据。"""
    try:
        ip = ipaddress.ip_address(host)
        return _check_ip(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"
    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        ok, reason = _check_ip(ip)
        if not ok:
            return False, f"resolved to {ip_str}: {reason}"
    return True, "ok"

def _check_ip(ip) -> tuple[bool, str]:
    for network in DENIED_ADDRESS_RANGES:
        if ip in network:
            return False, f"address in denied range {network}"
    for addr in CLOUD_METADATA_ADDRESSES:
        if str(ip) == addr:
            return False, "cloud metadata address"
    return True, "ok"
```

#### 3.2.4 hop-by-hop header 剥离 + 请求走私防护（v4 新增，采纳 Anthropic parent-proxy.ts:55-65 + request-filter.ts:107-113）

```python
# 采纳 Anthropic HOP_BY_HOP 集合
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade",
}

def strip_hop_by_hop_headers(headers: dict) -> dict:
    """剥离 hop-by-hop headers，防止代理指令泄露到上游。"""
    return {k: v for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS}

def check_request_smuggling(headers: dict) -> tuple[bool, str]:
    """检查 Content-Length + Transfer-Encoding 共存（请求走私原语）。
    采纳 Anthropic request-filter.ts:107-113。"""
    has_cl = any(k.lower() == "content-length" for k in headers)
    has_te = any(k.lower() == "transfer-encoding" for k in headers)
    if has_cl and has_te:
        return False, "Content-Length and Transfer-Encoding both present (smuggling risk)"
    return True, "ok"
```

#### 3.2.5 审计 path 脱敏（v4 新增，采纳 agent-bom evidence 分层）

```python
import re

SENSITIVE_QUERY_PARAMS = re.compile(
    r"(token|key|secret|password|access_token|private_token|ak|sk)=([^&]+)",
    re.IGNORECASE,
)

def sanitize_path(path: str) -> str:
    """脱敏 URL 查询参数中的 token/key/secret。"""
    def redact(m):
        return f"{m.group(1)}=[REDACTED]"
    return SENSITIVE_QUERY_PARAMS.sub(redact, path)
```

#### 3.2.6 Python HTTP CONNECT 代理（完整实现）

```python
# src/agent/proxy.py

import http.server, socketserver, threading, json, socket, time
from datetime import datetime, timezone

CONNECT_TIMEOUT = 30  # 秒，采纳 Anthropic CONNECT_TIMEOUT_MS
TUNNEL_IDLE_TIMEOUT = 300  # 秒，隧道空闲超时

class WhitelistProxyHandler(http.server.BaseHTTPRequestHandler):
    _task_id: str = ""
    _whitelist: list[str] = []
    _audit_log = None
    _proxy_port: int = 0

    def do_CONNECT(self):
        host, port = self._parse_target(self.path)
        start_time = time.monotonic()
        decision = self._check_request(host, port)

        if not decision["allowed"]:
            self._audit("CONNECT", host, self.path, decision, start_time, 403)
            body = json.dumps({
                "error": "blocked", "policy": "denied",
                "reason": decision["reason"], "host": host,
            }).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # CONNECT 隧道 — 30 秒握手超时（采纳 Anthropic parent-proxy.ts:44）
        try:
            upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
            upstream.settimeout(0)  # 握手完成后清除超时
        except Exception as e:
            self._audit("CONNECT", host, self.path,
                       {"allowed": True, "reason": f"connect failed: {e}"}, start_time, 502)
            self.send_error(502, f"Failed to connect to {host}: {e}")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()
        self._audit("CONNECT", host, self.path,
                   {"allowed": True, "reason": "whitelist match"}, start_time, 200)

        # 双向隧道 — 带超时 + ECONNRESET 捕获 + 半关闭检测（采纳 Anthropic http-proxy.ts:280-340）
        self._tunnel(self.connection, upstream)

    def do_GET(self):
        self._proxy_http_method("GET")
    def do_POST(self):
        self._proxy_http_method("POST")
    def do_PUT(self):
        self._proxy_http_method("PUT")
    def do_DELETE(self):
        self._proxy_http_method("DELETE")
    def do_PATCH(self):
        self._proxy_http_method("PATCH")
    def do_HEAD(self):
        self._proxy_http_method("HEAD")

    def _proxy_http_method(self, method):
        """HTTP 代理转发（完整实现）。
        采纳 Anthropic http-proxy.ts 的转发逻辑。"""
        start_time = time.monotonic()

        # 解析目标 URL
        if self.path.startswith("http://") or self.path.startswith("https://"):
            from urllib.parse import urlparse
            parsed = urlparse(self.path)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
        else:
            # 相对路径 — 从 Host header 取
            host = self.headers.get("Host", "").split(":")[0]
            port = int(self.headers.get("Host", "").split(":")[1]) if ":" in self.headers.get("Host", "") else 80
            path = self.path

        decision = self._check_request(host, port)
        if not decision["allowed"]:
            self._audit(method, host, self.path, decision, start_time, 403)
            body = json.dumps({
                "error": "blocked", "policy": "denied",
                "reason": decision["reason"], "host": host,
            }).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 请求走私检查（v4 新增）
        smuggling_ok, smuggling_reason = check_request_smuggling(dict(self.headers))
        if not smuggling_ok:
            self._audit(method, host, self.path,
                       {"allowed": False, "reason": smuggling_reason}, start_time, 400)
            self.send_error(400, smuggling_reason)
            return

        # 剥离 hop-by-hop headers（v4 新增）
        forward_headers = strip_hop_by_hop_headers(dict(self.headers))
        # 修正 Host header
        forward_headers["Host"] = f"{host}:{port}" if port != 80 else host

        # 连接目标
        try:
            upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except Exception as e:
            self._audit(method, host, self.path,
                       {"allowed": True, "reason": f"connect failed: {e}"}, start_time, 502)
            self.send_error(502, f"Failed to connect: {e}")
            return

        # 转发请求
        try:
            request_line = f"{method} {path} HTTP/1.1\r\n"
            header_lines = "".join(f"{k}: {v}\r\n" for k, v in forward_headers.items())
            upstream.sendall((request_line + header_lines + "\r\n").encode("utf-8"))

            # 转发 body（如果有）
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                upstream.sendall(body)

            # 读取响应并转发
            self._forward_response(upstream)
            self._audit(method, host, self.path,
                       {"allowed": True, "reason": "whitelist match"}, start_time, 200)
        except Exception as e:
            self._audit(method, host, self.path,
                       {"allowed": True, "reason": f"forward error: {e}"}, start_time, 502)
        finally:
            upstream.close()

    def _forward_response(self, upstream):
        """从上游读取 HTTP 响应并转发给客户端。"""
        # 读取响应行 + headers
        response_line = self._read_line(upstream)
        self.wfile.write(response_line.encode("utf-8"))

        headers_done = False
        while not headers_done:
            line = self._read_line(upstream)
            self.wfile.write(line.encode("utf-8"))
            if line == "\r\n" or line == "\n":
                headers_done = True

        self.end_headers()

        # 转发 body
        while True:
            data = upstream.recv(65536)
            if not data:
                break
            self.wfile.write(data)

    @staticmethod
    def _read_line(sock):
        """逐行读取（直到 \\r\\n）。"""
        line = b""
        while True:
            char = sock.recv(1)
            if not char:
                break
            line += char
            if line.endswith(b"\n"):
                break
        return line.decode("utf-8", errors="replace")

    def _check_request(self, host, port):
        """三级检查：白名单 → DNS 地址校验。"""
        for pattern in self._whitelist:
            if match_domain(host, pattern):
                break
        else:
            return {"allowed": False, "reason": "domain not in whitelist"}

        addr_ok, addr_reason = check_resolved_address(host, port)
        if not addr_ok:
            return {"allowed": False, "reason": f"DNS address denied: {addr_reason}"}

        return {"allowed": True, "reason": "whitelist match"}

    def _parse_target(self, path):
        if ":" in path:
            host, port_str = path.rsplit(":", 1)
            return host, int(port_str)
        return path, 443

    def _audit(self, method, host, path, decision, start_time, status_code):
        """写 JSONL 审计记录。v4 新增 latency_ms + status_code + path 脱敏。"""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": self._task_id,
            "method": method,
            "host": host,
            "path": sanitize_path(path),  # v4 脱敏
            "policy": "allowed" if decision["allowed"] else "denied",
            "reason": decision["reason"],
            "status_code": status_code,  # v4 新增
            "latency_ms": round((time.monotonic() - start_time) * 1000),  # v4 新增
        }
        if self._audit_log:
            self._audit_log.write(record)

    def _tunnel(self, client, upstream):
        """双向隧道转发 — 带超时 + ECONNRESET 捕获 + 半关闭检测。
        采纳 Anthropic http-proxy.ts:280-340 的设计。"""
        sockets = [client, upstream]
        # 设置隧道空闲超时（v4 新增）
        for s in sockets:
            s.settimeout(TUNNEL_IDLE_TIMEOUT)

        def pipe(src, dst, peer_closed):
            """单向转发。src → dst。"""
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        # 半关闭：对端 EOF，关闭另一端（采纳 Anthropic http-proxy.ts:287-289）
                        peer_closed.set()
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except Exception:
                            pass
                        break
                    dst.sendall(data)
            except socket.timeout:
                # 隧道空闲超时（v4 新增）
                pass
            except (ConnectionResetError, BrokenPipeError, OSError):
                # ECONNRESET 捕获（采纳 Anthropic http-proxy.ts:269-277）
                peer_closed.set()
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        import threading as _t
        peer_closed = _t.Event()
        t1 = _t.Thread(target=pipe, args=(client, upstream, peer_closed), daemon=True)
        t2 = _t.Thread(target=pipe, args=(upstream, client, peer_closed), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # 确保两端都关闭
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass

    def log_message(self, format, *args):
        pass  # 审计走 _audit
```

#### 3.2.7 Unix socket 桥接

```python
class UnixSocketBridge(threading.Thread):
    """宿主侧: Unix socket → TCP 代理端口（替代 socat）。"""

    def __init__(self, socket_path: str, proxy_port: int):
        super().__init__(daemon=True)
        self.socket_path = socket_path
        self.proxy_port = proxy_port
        self._server = None

    def run(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = socketserver.UnixStreamServer(
            self.socket_path, self._make_handler())
        self._server.serve_forever()

    def _make_handler(self):
        proxy_port = self.proxy_port
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    upstream = socket.create_connection(
                        ("127.0.0.1", proxy_port), timeout=10)
                except Exception:
                    return
                def pipe(src, dst):
                    try:
                        while True:
                            data = src.recv(65536)
                            if not data:
                                break
                            dst.sendall(data)
                    except Exception:
                        pass
                    finally:
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except Exception:
                            pass
                t = threading.Thread(target=pipe, args=(self.request, upstream), daemon=True)
                t.start()
                pipe(upstream, self.request)
                t.join()
        return Handler

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
```

#### 3.2.8 沙箱内桥接启动脚本

```python
def _build_in_sandbox_script(agent_cmd: str, socket_path: str) -> str:
    """生成沙箱内启动脚本：桥接 + trap + Agent。
    采纳 Anthropic linux-sandbox-utils.ts:840-860 的 trap EXIT 模式。"""
    # 沙箱内 Python 桥接：TCP-LISTEN:3128 → Unix socket
    # bwrap --unshare-net 后 lo 接口可用（bwrap 默认启用 lo）
    bridge_code = (
        "import socketserver,socket,threading\n"
        f"SOCK='{socket_path}'\n"
        "class H(socketserver.BaseRequestHandler):\n"
        "  def handle(self):\n"
        f"    u=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        f"    u.connect(SOCK)\n"
        "    def p(s,d):\n"
        "      try:\n"
        "        while 1:\n"
        "          b=s.recv(65536)\n"
        "          if not b:break\n"
        "          d.sendall(b)\n"
        "      except:pass\n"
        "      finally:\n"
        "        try:d.shutdown(socket.SHUT_WR)\n"
        "        except:pass\n"
        "    t=threading.Thread(target=p,args=(self.request,u),daemon=True)\n"
        "    t.start()\n"
        "    p(u,self.request)\n"
        "    t.join()\n"
        "socketserver.TCPServer(('127.0.0.1',3128),H).serve_forever()\n"
    )
    script = (
        f"python3 -c '{bridge_code}' &\n"
        "trap 'kill %1 2>/dev/null' EXIT\n"
        f"exec {agent_cmd}"
    )
    return script
```

### 3.3 资源限额 + bwrap 加固

#### 3.3.1 CgroupSlice（采纳 deepagents 降级 + context manager + OOM）

```python
# src/agent/cgroup.py

class CgroupSlice:
    """cgroup v1 子组管理（memory + cpu + pids）。
    降级策略：不可用时 warn + 跳过。"""

    def __init__(self, task_id, mem_limit_mb=0, cpu_quota=0, max_pids=0):
        self._name = f"grape-sandbox-{task_id[:12]}"
        self._mem_limit = mem_limit_mb
        self._cpu_quota = cpu_quota
        self._max_pids = max_pids
        self._created = False
        self._has_sudo = shutil.which("sudo") is not None

    def create(self) -> bool:
        if not self._has_sudo:
            log.warning("cgroup: sudo not available, resource limits disabled (degraded)")
            return False
        try:
            # 内存限额：0 = 自动取宿主机 50%
            mem_limit = self._mem_limit
            if mem_limit == 0:
                mem_limit = self._auto_mem_limit()
                if mem_limit > 0:
                    log.info(f"cgroup: auto memory limit = {mem_limit}MB (50% of host)")
            self._mkdir("/sys/fs/cgroup/memory/" + self._name)
            if mem_limit > 0:
                self._write("/sys/fs/cgroup/memory/" + self._name + "/memory.limit_in_bytes",
                           str(mem_limit * 1024 * 1024))
            self._mkdir("/sys/fs/cgroup/cpu,cpuacct/" + self._name)
            if self._cpu_quota > 0:
                self._write("/sys/fs/cgroup/cpu,cpuacct/" + self._name + "/cpu.cfs_quota_us",
                           str(self._cpu_quota * 1000))
                self._write("/sys/fs/cgroup/cpu,cpuacct/" + self._name + "/cpu.cfs_period_us", "100000")
            if self._max_pids > 0:
                self._mkdir("/sys/fs/cgroup/pids/" + self._name)
                self._write("/sys/fs/cgroup/pids/" + self._name + "/pids.max", str(self._max_pids))
            self._created = True
            return True
        except Exception as e:
            log.warning(f"cgroup: create failed, resource limits disabled: {e}")
            self.destroy(force=True)
            return False

    def attach_pid(self, pid):
        if not self._created:
            return
        for subsys in ("memory", "cpu,cpuacct"):
            path = f"/sys/fs/cgroup/{subsys}/{self._name}/tasks"
            if os.path.exists(path):
                try:
                    self._write(path, str(pid))
                except Exception:
                    pass
        if self._max_pids > 0:
            path = f"/sys/fs/cgroup/pids/{self._name}/tasks"
            if os.path.exists(path):
                try:
                    self._write(path, str(pid))
                except Exception:
                    pass

    def destroy(self, *, force=False):
        if not self._created:
            return
        for subsys in ("memory", "cpu,cpuacct", "pids"):
            path = f"/sys/fs/cgroup/{subsys}/{self._name}"
            if os.path.isdir(path):
                try:
                    self._sudo("rmdir", path)
                except Exception:
                    pass
        self._created = False

    def __enter__(self):
        self.create()
        return self
    def __exit__(self, *args):
        self.destroy()

    def _sudo(self, *args):
        subprocess.run(["sudo", *args], check=True, capture_output=True, timeout=10)
    def _mkdir(self, path):
        self._sudo("mkdir", "-p", path)
    def _write(self, path, value):
        self._sudo("bash", "-c", f"echo {value} > {path}")

    @staticmethod
    def _auto_mem_limit() -> int:
        """读取宿主机总内存，返回 50%（MB）。读取失败返回 0（不限）。"""
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                        return max(512, total_kb * 1024 // (1024**3) // 2)  # 最低 512MB
        except Exception:
            pass
        return 0
```

#### 3.3.2 bwrap 加固（v4 新增，采纳 deepagents executor.py:149-160）

```python
# src/agent/sandbox.py — build_bwrap_cmd 中新增

# v4 加固（采纳 deepagents executor.py:149-160）
opts += ["--cap-drop", "ALL"]       # 丢弃所有 capabilities
# 注：bwrap 0.4.1 不支持 --disable-userns（Unknown option），已去掉
# 注：不加 --clearenv（会破坏引擎运行时依赖的 env）
# 注：不加 --unshare-user --uid 1000（grape worktree 属主是当前用户，uid 映射可能导致权限问题）
```

### 3.4 审计实现（采纳 agent-bom 哈希链 + 轮转 + v4 增强）

```python
# src/agent/audit.py

import hashlib, hmac, json, os, threading
from pathlib import Path

class RotatingAuditLog:
    """JSONL 审计日志 + 轮转 + 哈希链。
    v4 新增：verify_chain 方法。"""

    MAX_BYTES = 100 * 1024 * 1024
    MAX_ROTATED = 5
    _chain_state: dict[str, str] = {}
    _lock = threading.Lock()

    def __init__(self, path: str, hmac_key: str = ""):
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._hmac_key = self._resolve_key(path, hmac_key)

    def _resolve_key(self, path, explicit_key):
        if explicit_key:
            return explicit_key.encode("utf-8")
        sidecar = path + ".chainkey"
        if os.path.exists(sidecar):
            with open(sidecar) as f:
                return f.read().strip().encode()
        key = os.urandom(32).hex().encode()
        fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, key)
        os.close(fd)
        return key

    def write(self, record: dict):
        with self._lock:
            prev_hash = self._chain_state.get(self._path, "")
            payload = dict(record)
            payload["prev_hash"] = prev_hash
            record_hash = self._compute_hash(payload)
            payload["record_hash"] = record_hash
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._append(line)
            self._chain_state[self._path] = record_hash

    def _compute_hash(self, payload):
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hmac.new(self._hmac_key, data, hashlib.sha256).hexdigest()

    def _append(self, line):
        current = os.path.getsize(self._path) if os.path.exists(self._path) else 0
        if current > self.MAX_BYTES:
            self._rotate()
        with open(self._path, "a") as f:
            f.write(line)

    def _rotate(self):
        for i in range(self.MAX_ROTATED - 1, 0, -1):
            old = f"{self._path}.{i}"
            new = f"{self._path}.{i+1}"
            if os.path.exists(old):
                os.rename(old, new)
        if os.path.exists(self._path):
            os.rename(self._path, f"{self._path}.1")

    def verify_chain(self) -> tuple[bool, str]:
        """验证哈希链完整性。"""
        if not os.path.exists(self._path):
            return True, "no log file"
        prev_hash = ""
        with open(self._path) as f:
            for i, line in enumerate(f, 1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False, f"line {i}: invalid JSON"
                stored_prev = record.get("prev_hash", "")
                stored_hash = record.get("record_hash", "")
                if stored_prev != prev_hash:
                    return False, f"line {i}: prev_hash mismatch"
                payload = {k: v for k, v in record.items() if k != "record_hash"}
                computed = self._compute_hash(payload)
                if computed != stored_hash:
                    return False, f"line {i}: record_hash mismatch"
                prev_hash = stored_hash
        return True, "chain intact"
```

### 3.5 环境预检 + 敏感 env 扫描（v4 新增敏感 env 扫描）

```python
# src/agent/detect.py

import os, re, shutil, platform
from dataclasses import dataclass

@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    reason: str

def check_platform():
    if platform.system() == "Linux":
        return CheckResult("platform", True, "Running on Linux")
    return CheckResult("platform", False, f"Not Linux")

def check_bwrap():
    path = shutil.which("bwrap")
    if path:
        return CheckResult("bwrap", True, f"Found at {path}")
    return CheckResult("bwrap", False, "not found")

def check_sudo():
    if shutil.which("sudo"):
        return CheckResult("sudo", True, "available")
    return CheckResult("sudo", False, "not found")

def check_cgroup():
    for subsys in ("memory", "cpu,cpuacct"):
        if not os.path.isdir(f"/sys/fs/cgroup/{subsys}"):
            return CheckResult("cgroup", False, f"{subsys} not mounted")
    return CheckResult("cgroup", True, "cgroup v1 available")

def check_tmp():
    try:
        with open("/tmp/.grape-test", "w") as f:
            f.write("x")
        os.unlink("/tmp/.grape-test")
        return CheckResult("tmp", True, "/tmp writable")
    except Exception as e:
        return CheckResult("tmp", False, str(e))

def check_ssl():
    for p in ("/etc/ssl/certs", "/etc/pki/tls/certs"):
        if os.path.isdir(p):
            return CheckResult("ssl", True, f"CA certs at {p}")
    return CheckResult("ssl", False, "no CA certs")

# v4 新增：敏感 env 变量扫描
SENSITIVE_ENV_PATTERN = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|AK|SK)", re.IGNORECASE
)

def scan_sensitive_env(unset_list: list[str]) -> list[str]:
    """扫描当前 env 中含敏感关键词但未列入遮蔽清单的变量。
    返回 warning 列表（不阻断启动）。"""
    warnings = []
    for key in os.environ:
        if SENSITIVE_ENV_PATTERN.search(key) and key not in unset_list:
            # 排除已知安全变量
            if key in ("GRAPE_API_TOKEN", "HTTP_PROXY", "HTTPS_PROXY",
                       "ALL_PROXY", "NO_PROXY", "GRPC_PROXY",
                       "DEVOPS_EVENT_TYPE", "AGENT_NAME",
                       "GIT_CONFIG_PARAMETERS", "GIT_SSH_COMMAND"):
                continue
            warnings.append(key)
    return warnings

HARD_FAILURES = {"platform", "bwrap"}

def detect_sandbox_env(unset_list=None) -> tuple[bool, list[CheckResult], list[str]]:
    """返回 (can_proceed, check_results, env_warnings)。"""
    results = [check_platform(), check_bwrap(), check_sudo(),
               check_cgroup(), check_tmp(), check_ssl()]
    hard_fails = [r for r in results if not r.passed and r.name in HARD_FAILURES]
    env_warnings = scan_sensitive_env(unset_list or []) if unset_list else []
    return len(hard_fails) == 0, results, env_warnings
```

### 3.6 凭据遮蔽（P0 不遮蔽）

P0 阶段不遮蔽任何凭据 — 见 §2.4 凭据注入现状与遮蔽计划。

网络白名单是唯一安全边界：Agent 即使有 token/AK/SK，也只能连白名单内的域名。
- GitCode token → 只能连 gitcode.com / api.gitcode.com（grape 托管的仓库）
- 华为云 AK/SK → 只能连 obs.cn-north-4.myhuaweicloud.com
- GRAPE_API_TOKEN → 只能连 grape API（127.0.0.1:3000 或公网）

```python
# src/agent/sandbox.py

# P0 阶段：空列表，不遮蔽任何凭据
# 后续 Issue（skill 迁移到 grape API 后）恢复遮蔽
CREDENTIAL_ENV_VARS: list[str] = []

def _build_unsetenv_args() -> list[str]:
    """生成 bwrap --unsetenv 参数。P0 阶段返回空列表。"""
    args = []
    for var in CREDENTIAL_ENV_VARS:
        args += ["--unsetenv", var]
    return args
```

### 3.7 代理进程健康监控

```python
# src/agent/proxy.py — SandboxProxy

class SandboxProxy:
    """管理代理进程 + Unix socket 桥接的生命周期。"""

    def __init__(self, task_id, whitelist, audit_log):
        self._task_id = task_id
        self._proxy_port = 0
        self._server = None
        self._bridge = None
        self._socket_path = f"/tmp/grape-{task_id[:12]}.sock"
        self._whitelist = whitelist
        self._audit_log = audit_log
        self._on_crash = None

    def start(self) -> dict:
        # 绑定 port 0 让 OS 分配空闲端口（v4：采纳 Anthropic listen-in-range 思路）
        handler = WhitelistProxyHandler
        handler._task_id = self._task_id
        handler._whitelist = self._whitelist
        handler._audit_log = self._audit_log

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self._proxy_port = self._server.server_address[1]
        proxy_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        proxy_thread.start()

        self._bridge = UnixSocketBridge(self._socket_path, self._proxy_port)
        self._bridge.start()

        return {"proxy_port": self._proxy_port, "socket_path": self._socket_path}

    def on_crash(self, callback):
        self._on_crash = callback

    def stop(self):
        if self._bridge:
            self._bridge.stop()
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def is_alive(self):
        return self._server is not None
```

### 3.8 主进程信号处理

```python
# src/agent/executor.py

_active_sandboxes = []
_active_sandboxes_lock = threading.Lock()

def register_sandbox(sb_ctx):
    with _active_sandboxes_lock:
        _active_sandboxes.append(sb_ctx)

def unregister_sandbox(sb_ctx):
    with _active_sandboxes_lock:
        if sb_ctx in _active_sandboxes:
            _active_sandboxes.remove(sb_ctx)

def terminate_active_agents(grace_seconds=5):
    """服务停机时终止所有 agent 子进程 + 清理沙箱资源。"""
    with _active_sandboxes_lock:
        sandboxes = list(_active_sandboxes)
        _active_sandboxes.clear()
    for sb in sandboxes:
        try:
            sb.cleanup()
        except Exception:
            pass
    # 原有 Agent 进程清理逻辑不变
    with _active_procs_lock:
        procs = list(_active_procs)
        _active_procs.clear()
    for proc in procs:
        try:
            _terminate_proc(proc, "shutdown")
        except Exception:
            pass
```

### 3.9 sandbox.py — build_bwrap_cmd 完整改造

```python
# src/agent/sandbox.py

def build_bwrap_cmd(cwd, backend_name="", sandbox_ctx=None):
    """构造 bwrap 命令。sandbox_ctx 有值时启用网络隔离。"""
    if not sandbox_enabled():
        return None
    bwrap = _bwrap_bin()
    if not bwrap or not cwd or not os.path.isdir(cwd):
        return None

    from src.config import (BWRAP_RO_SYSTEM, BWRAP_RO_ENGINE_COMMON,
                           BWRAP_RO_ENGINE, BWRAP_RW_ENGINE)

    # 基础参数
    opts = [bwrap, "--die-with-parent", "--new-session"]

    # v4 加固（采纳 deepagents）
    opts += ["--cap-drop", "ALL", "--disable-userns"]

    if sandbox_ctx and sandbox_ctx.net_enabled:
        # 网络隔离模式
        opts += ["--unshare-net", "--unshare-pid", "--proc", "/proc"]
        # 挂入 Unix socket
        opts += ["--bind", sandbox_ctx.socket_path, sandbox_ctx.socket_path]
        # CA 证书
        if os.path.isdir("/etc/ssl/certs"):
            opts += ["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"]
        if os.path.isdir("/etc/pki"):
            opts += ["--ro-bind", "/etc/pki", "/etc/pki"]
        # 遮蔽敏感文件（v4 新增）
        opts += ["--symlink", "/dev/null", "/etc/shadow"]
        # 代理 env 变量
        for key, value in generate_proxy_envs(3128):
            opts += ["--setenv", key, value]
        opts += ["--setenv", "GRAPESOCK", sandbox_ctx.socket_path]
        # 凭据遮蔽
        opts += _build_unsetenv_args()
    else:
        # 原有模式（--share-net）
        opts += ["--unshare-all", "--share-net"]

    # 文件系统白名单（原有逻辑不变）
    for p in BWRAP_RO_SYSTEM:
        if p and os.path.isdir(p):
            opts += ["--ro-bind", p, p]
    # ... 引擎运行时 + cwd ...

    # 沙箱内启动脚本（网络隔离模式）
    if sandbox_ctx and sandbox_ctx.net_enabled:
        agent_cmd = " ".join(opts_remaining)  # 原 agent 命令
        script = _build_in_sandbox_script(agent_cmd, sandbox_ctx.socket_path)
        opts += ["--", "/bin/sh", "-c", script]
    else:
        opts.append("--")

    return opts
```

---

## 四、改动清单

| # | 文件 | 改动 | 新增/修改 |
|---|------|------|-----------|
| 1 | `src/agent/detect.py` | 环境预检 + 敏感 env 扫描 | 新增 |
| 2 | `src/agent/cgroup.py` | cgroup v1 子组（降级 + context manager + OOM） | 新增 |
| 3 | `src/agent/audit.py` | RotatingAuditLog（哈希链 + 轮转 + verify_chain） | 新增 |
| 4 | `src/agent/proxy.py` | HTTP 代理（白名单 + DNS 校验 + hop-by-hop 剥离 + 走私检查 + CONNECT 超时/半关闭 + HTTP 转发 + path 脱敏 + 审计 + 崩溃监控） + UnixSocketBridge + env 生成 | 新增 |
| 5 | `src/agent/sandbox.py` | build_bwrap_cmd 改造（--unshare-net + --cap-drop ALL + --disable-userns + --bind socket + CA + --symlink shadow + --unsetenv + --unshare-pid + prepare/cleanup） | 修改 |
| 6 | `src/agent/executor.py` | prepare/cleanup + OOM 检测 + terminate_active_agents + register_sandbox | 修改 |
| 7 | `src/runtime/agent.py` | Worker 端同步 | 修改 |
| 8 | `src/config.py` | 白名单配置 + cgroup 限额 + 审计路径 + HMAC key + 凭据清单 + DB 降级 | 修改 |
| 9 | `src/api/db_setup.py` | sandbox_whitelist 表 DDL + 索引（v7 新增） | 修改 |
| 10 | `tests/test_sandbox_net.py` | 验收用例 1-28 | 新增 |

---

## 五、配置项

```python
# config.py 新增

GRAPE_SANDBOX_NET = os.environ.get("GRAPE_SANDBOX_NET", "off").lower() in ("1","true","on","yes")

SANDBOX_WHITELIST = [d.strip() for d in os.environ.get("GRAPE_SANDBOX_WHITELIST",
    "gitcode.com,api.gitcode.com,raw.gitcode.com,"
    "github.com,api.github.com,"
    "opengw.clouddeveloper.club,api.deepseek.com,"
    "api.modelarts-maas.com,"
    "registry.npmmirror.com,"
    "pypi.tuna.tsinghua.edu.cn,"
    "obs.cn-north-4.myhuaweicloud.com"
).split(",") if d.strip()]

GRAPE_SANDBOX_MEM_LIMIT_MB = int(os.environ.get("GRAPE_SANDBOX_MEM_LIMIT_MB", "0"))  # 0=自动（宿主机内存 50%）
GRAPE_SANDBOX_CPU_QUOTA = int(os.environ.get("GRAPE_SANDBOX_CPU_QUOTA", "0"))  # 0=不限制
GRAPE_SANDBOX_MAX_PIDS = int(os.environ.get("GRAPE_SANDBOX_MAX_PIDS", "0"))  # 0=不限制

# cgroup 内存限额策略：
# - 显式设值（如 4096）→ 用该值
# - 0（默认）→ 自动取宿主机总内存的 50% 作为宽松硬上限
#   原因：完全不限（0）意味着单个失控 Agent 可耗尽宿主内存拖垮全机；
#   设固定值又因四个引擎（bun/Python）内存特性差异大而难以合理；
#   50% 是折中 — 单 Agent 不会拖垮全机，同时足够宽松不会误杀正常操作。
#   实施时读取 /proc/meminfo MemTotal 计算。

SANDBOX_AUDIT_LOG = os.environ.get("SANDBOX_AUDIT_LOG", "")
SANDBOX_AUDIT_HMAC_KEY = os.environ.get("SANDBOX_AUDIT_HMAC_KEY", "")

SANDBOX_CREDENTIAL_ENV_VARS = [v.strip() for v in os.environ.get("SANDBOX_CREDENTIAL_ENV_VARS", "").split(",") if v.strip()]
# P0 默认空 — 不遮蔽任何凭据。后续 skill 迁移到 grape API 后恢复。

# 白名单 DB 表 + 5 分钟缓存（v7 新增）
# 生产环境通过 DB 表 sandbox_whitelist 管理白名单，支持动态增删无需重启。
# 环境变量 SANDBOX_WHITELIST_FALLBACK 仅用于首次启动 / DB 不可用时降级。
SANDBOX_WHITELIST_FALLBACK = [
    d.strip() for d in os.environ.get("GRAPE_SANDBOX_WHITELIST",
        "gitcode.com,api.gitcode.com,raw.gitcode.com,"
        "github.com,api.github.com,"
        "opengw.clouddeveloper.club,api.deepseek.com,"
        "api.modelarts-maas.com,"
        "registry.npmmirror.com,"
        "pypi.tuna.tsinghua.edu.cn,"
        "obs.cn-north-4.myhuaweicloud.com"
    ).split(",") if d.strip()]
SANDBOX_WHITELIST = SANDBOX_WHITELIST_FALLBACK  # 兼容
```

---

## 六、降级策略

| 组件 | 检测 | 不可用时 | 影响 |
|------|------|----------|------|
| bwrap | detect.py | 硬失败 | Agent 无沙箱运行 |
| platform | detect.py | 硬失败 | — |
| /tmp 可写 | detect.py | 硬失败 | — |
| cgroup | detect.py + sudo | warn + 跳过限额 | Agent 无资源限制 |
| CA 证书 | detect.py | warn + 跳过 | HTTPS 验证可能失败 |
| 代理端口 | OS 分配 | warn + 跳过网络隔离 | 回退 --share-net |
| 审计日志 | 路径可写 | warn + 跳过审计 | 网络隔离仍生效 |

---

## 七、回滚策略

| 开关 | 环境变量 | 默认 | 关闭后 |
|------|----------|------|--------|
| 网络隔离 | GRAPE_SANDBOX_NET | on | 回退 --share-net（设 off 关闭） |
| 文件系统隔离 | GRAPE_AGENT_SANDBOX | on | 回退无 bwrap |
| 内存限额 | GRAPE_SANDBOX_MEM_LIMIT_MB | 0（不限制） | 设具体值启用 |
| CPU 限额 | GRAPE_SANDBOX_CPU_QUOTA | 0 | 保持 0 |
| 审计 | SANDBOX_AUDIT_LOG | 默认路径 | /dev/null |
| 凭据遮蔽 | SANDBOX_CREDENTIAL_ENV_VARS | 空（不遮蔽） | 后续恢复 |

---

## 八、白名单 DB 表 + 5 分钟缓存（v7 新增）

### 8.1 表结构

```sql
CREATE TABLE IF NOT EXISTS sandbox_whitelist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,                        -- 域名或通配符（如 *.gitcode.com）
    scope       TEXT NOT NULL DEFAULT 'global',       -- global / repo / engine
    scope_value TEXT NOT NULL DEFAULT '',             -- scope=repo 时为 owner/repo；engine 时为引擎名
    enabled     INTEGER NOT NULL DEFAULT 1,           -- 1=启用 0=禁用
    description TEXT NOT NULL DEFAULT '',             -- 说明（为什么加这个域名）
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (domain, scope, scope_value),              -- 同域名可跨 scope，但同 scope+value 不可重复
    CHECK (scope = 'global' OR scope_value != '')    -- scope=repo/engine 时 scope_value 不能为空
);
CREATE INDEX IF NOT EXISTS idx_whitelist_scope ON sandbox_whitelist(scope, scope_value, enabled);
CREATE INDEX IF NOT EXISTS idx_whitelist_domain ON sandbox_whitelist(domain, enabled);
```

### 8.2 scope 设计

| scope | 含义 | scope_value | 场景 |
|-------|------|-------------|------|
| global | 所有 Agent 任务都放行 | 空 | gitcode.com / 模型 API / pip / npm |
| repo | 特定仓库的 Agent 才放行 | owner/repo | 某仓库需要连特定私有 API |
| engine | 特定引擎才放行 | opencode/hermes/codearts/codex | codearts 需要 snap-access.cn-north-4 |

### 8.3 查询 + 缓存

```python
# src/agent/sandbox.py

_whitelist_cache: list[str] | None = None
_whitelist_cache_ts: float = 0
_whitelist_cache_lock = threading.Lock()
_WHITELIST_CACHE_TTL = 300  # 5 分钟

def _get_whitelist_from_db(backend_name: str = "", repo_key: str = "") -> list[str]:
    """从 DB 查询白名单（5 分钟缓存）。

    查询条件：enabled=1 AND (
        scope='global' OR
        (scope='engine' AND scope_value=backend_name) OR
        (scope='repo' AND scope_value=repo_key)
    )

    DB 不可用或表不存在时降级到 SANDBOX_WHITELIST_FALLBACK 环境变量。
    """
    # 1. 检查缓存（5 分钟 TTL）
    # 2. 缓存未命中 → 查 DB
    # 3. DB 无数据/不可用 → 降级到环境变量
    # 4. 更新缓存
    # 5. 加入动态白名单（127.0.0.1 + GRAPE_SERVER_URL）

def invalidate_whitelist_cache():
    """手动失效缓存（API 增删白名单后调用）。"""
```

### 8.4 降级链

```
DB 表 sandbox_whitelist（有数据）
  ↓ DB 不可用 / 表不存在 / 无数据
环境变量 SANDBOX_WHITELIST_FALLBACK
  ↓ 环境变量未设
硬编码默认值（config.py 中的 12 个域名）
```

### 8.5 API 端点（后续实现）

通过 grape API 增删白名单（进 OpenAPI 契约，CLI 可操作）：
- `GET /api/sandbox/whitelist` — 列表
- `POST /api/sandbox/whitelist` — 新增（调用后 invalidate_whitelist_cache）
- `DELETE /api/sandbox/whitelist/{id}` — 删除（调用后 invalidate_whitelist_cache）
- `PATCH /api/sandbox/whitelist/{id}` — 启用/禁用（调用后 invalidate_whitelist_cache）

---

## 九、后续增强

### v8 新增（借鉴 isolate + m9m）

| # | 改进 | 来源 | 说明 |
|---|------|------|------|
| 1 | /etc 白名单式挂载 | isolate safeEtcMounts | --ro-bind-try 安全子集（ssl/ca-certificates/pki/resolv.conf/hosts/nsswitch.conf/localtime/ld.so.*），不含 ssh 密钥/shadow/nginx/数据库密码 |
| 2 | memory.high 90% 软上限 | m9m CgroupManager | v2: memory.high = 90% of max；v1: memory.soft_limit_in_bytes = 90% |
| 3 | 审计异步写入 | m9m buffered channel | _AsyncWriter: Queue(10000) + daemon 线程，哈希链同步，磁盘 I/O 异步 |
| 4 | cgroup 资源统计 | m9m GetStats | get_stats() 读 cpu.stat/memory.peak（v2）或 cpuacct.usage/memory.max_usage_in_bytes（v1） |
| 5 | K8s 资源量格式 | isolate ParseMemory/ParseCPU | parse_memory("256Mi")→256MB, parse_cpu("500m")→500 毫核 |
| 6 | 标识符可读性 | 外部审查 | {safe_task_id[:16]}-{sha256[:8]}，socket 和 cgroup 用同一格式 |
| 7 | cgroup v2-first + v1 降级 | m9m + 用户要求 | _detect_cgroup_version() 检测 v2/v1，v2 用 memory.max/cpu.max/pids.max |
| 8 | grape 端口动态解析 | 用户反馈 | 从 GRAPE_SERVER_URL 解析端口，不硬编码 3000 |
| 9 | per-instance handler 子类 | 并发审查 | type() 创建独立 handler 类，避免类变量并发覆盖 |
| 10 | 白名单 DB 表 + 5 分钟缓存 | 用户要求 | sandbox_whitelist 表 scope(global/repo/engine) + 按 (backend_name, repo_key) 缓存 |
| 11 | detect.py cgroup v2 检测 | 第三轮审查 | check_cgroup() 从 v1 硬编码改为 v2 优先（cgroup.controllers）+ v1 降级 |
| 12 | loopback 端口范围校验 | 第三轮审查 | GRAPE_SERVER_URL 解析端口限制 3000-9999，误配时降级到 3000 |
| 13 | config.py 重复定义清理 | 第三轮审查 | 删除 SANDBOX_WHITELIST 旧定义（被 FALLBACK 覆盖，冗余） |
| 14 | 白名单 DB seed 初始化 | 用户反馈 | init_database 调用 _seed_sandbox_whitelist()，把 FALLBACK 默认域名写入 DB 表（scope=global, enabled=1），幂等 |
| 15 | codex API 端点加入白名单 | codex 兼容性审查 | SANDBOX_WHITELIST_FALLBACK 加入 api.openai.com（codex 默认 API 端点） |
| 16 | codex 凭据 key 映射 | codex 兼容性审查 | _assemble_env else 分支对 provider=openai 做 api_key → OPENAI_API_KEY 映射 |
| 17 | K8s env 解析内联实现 | 第四轮审查 | config.py 内联 _parse_mem_env/_parse_cpu_env，不 import cgroup（避免循环导入） |
| 18 | 审计 _AsyncWriter sentinel 修复 | 第四轮审查 | _FLUSH_SENTINEL 改为类变量，避免 _run 线程 NameError 崩溃 |
| 19 | 审计双锁统一 | 第四轮审查 | 删除 RotatingAuditLog._lock（类级），统一用模块级 _lock |
| 20 | cgroup get_stats 接入审计 | 自检遗漏 | executor.py cleanup 前调 get_stats()，写 resource_stats 事件到审计日志（CPU/内存峰值 + OOM 标记） |
| 21 | /opt/node 挂载 | codex worker 实测 | BWRAP_RO_SYSTEM_OPT = ('/opt/node',)，--ro-bind-try，解决 worker 上 node 符号链接断裂。不存在时跳过，不影响 opencode/codearts/hermes |
| 22 | codex worker 实测验证 | 新加坡 worker | codex v0.153.4 + Node v26.8.1 + bwrap v0.9.0 + cgroup v2。API 调用走代理，白名单放行 opengw，拒绝 github.com/chatgpt.com |

### 专家审查注释（v9 新增）

以下局限性已在代码注释中标注，完整方案留给后续 Issue：

| # | 局限性 | 当前状态 | 完整方案 |
|---|--------|---------|---------|
| A | memory.high 单独设置只延迟 OOM | memory.max 硬上限仍有效 | 监控 memory.events high 计数器 + 接入 systemd-oomd |
| B | 审计异步崩溃丢数据 | Queue 满降级同步写 | Transactional Outbox Pattern（DB outbox 表 + worker） |
| C | cgroup 时序竞态（bwrap --info-fd） | 未使用 --info-fd，无竞态 | 如引入需加原子锁串行化 namespace 初始化 |
| D | /etc/resolv.conf WSL 符号链接 | grape 部署 Linux 非 WSL | cross-mount repair（--tmpfs 占位 + --ro-bind 真实目标） |

### 后续 Issue（不在 #139 范围内）

| 增强 | 来源 | 说明 |
|------|------|------|
| 凭据加密存储 + 代理层注入 | m9m CredentialStore | AES-256 + scrypt，phantom token 模式 |
| 隔离等级枚举 | m9m IsolationLevel | 5 级，Issue #145 SandboxProvider |
| 审计事件类型分类 | m9m AuditEvent | 30+ 事件类型 + 严重级别 + 保留期 |
| 白名单管理 API | — | GET/POST/DELETE/PATCH /api/sandbox/whitelist |
| seccomp BPF | Anthropic | 阻止 socket(AF_UNIX)，需 C 二进制 |

---

## 十、验收标准

| # | 场景 | 预期 |
|---|------|------|
| 1 | curl https://gitcode.com | ✅ 200 |
| 2 | curl https://evil.com | ❌ 403 + JSON body |
| 3 | curl https://api.gitcode.com | ✅ 200 |
| 4 | curl https://opengw.clouddeveloper.club | ✅ 200 |
| 5 | Agent 内存超限（GRAPE_SANDBOX_MEM_LIMIT_MB=512 测试） | exit_code=137 |
| 6 | 审计日志 | policy + reason + prev_hash + record_hash + latency_ms + status_code |
| 7 | 哈希链验证 | verify_chain() 通过 |
| 8 | 审计轮转 | 100MB 后轮转，保留 5 个 |
| 9 | GRAPE_SANDBOX_NET=off | 回退 --share-net（默认 on，手动关闭回滚） |
| 10 | 无 sudo | cgroup 降级，Agent 可运行 |
| 11 | git clone/push | 通过代理正常 |
| 12 | 任务结束 | cgroup + socket + 代理 + 桥接全部清理 |
| 13 | 通配符 *.gitcode.com | 匹配 api.gitcode.com |
| 14 | DNS 重绑定到 127.0.0.1 | 403 |
| 15 | DNS 重绑定到 169.254.169.254 | 403 |
| 16 | 凭据遮蔽 | Agent env 无 BOT_GITCODE_TOKEN |
| 17 | 代理崩溃 | kill Agent（不等超时） |
| 18 | SIGTERM | terminate_active_agents 清理所有沙箱 |
| 19 | 环境预检 | bwrap 不可用→拒绝；cgroup 不可用→降级 |
| 20 | 敏感 env 扫描 | 发现未遮蔽的含 TOKEN/KEY/SECRET 的变量→warn |
| 21 | 请求走私 | CL + TE 共存→400 |
| 22 | hop-by-hop header | connection/proxy-authorization 不转发到上游 |
| 23 | CONNECT 超时 | 30s 内未建立→502 |
| 24 | 隧道空闲超时 | 300s 无数据→关闭 |
| 25 | SSH 外联 | GIT_SSH_COMMAND=/bin/false→失败 |
| 26 | /etc/shadow | 不可读（symlink → /dev/null） |
| 27 | HTTP 转发 | GET http://gitcode.com/... 正常代理 |
| 28 | 审计 path 脱敏 | token=xxx → token=[REDACTED] |

---

## 十一、实施步骤

| 步骤 | 内容 | 参考 |
|------|------|------|
| 0 | **实施前验证（已完成）** | — |
| 0a | ✅ 验证通过：四个引擎全部走 HTTP_PROXY。opencode（bun）3 个 CONNECT → 模型回复 "hello"。codearts（bun）4 个 CONNECT（含 snap-access.cn-north-4.myhuaweicloud.com）。hermes（Python httpx）5 个 CONNECT → 模型回复 "hello"。codex（Node.js v26.8.1）在新加坡 worker（124.243.190.182）实测：API 调走代理，白名单放行 opengw.clouddeveloper.club，拒绝 github.com/chatgpt.com。codex 返回 "Hello! How can I help you today?" | 不需要 global-agent |
| 0b | ✅ 验证通过：--cap-drop ALL 不影响 pip install / npm / node / touch / chmod。whoami=id=yangaiwu（非 root，正常用户） | 保留 --cap-drop ALL |
| 0c | ✅ 验证通过：bwrap --unshare-net 后 lo（127.0.0.1）可用。沙箱内 Python ThreadingTCPServer bind 127.0.0.1:3128 成功，三个引擎都连上了代理 | 确定沙箱内桥接可行 |

**验证中发现的兼容性问题**：
- bwrap 0.4.1 **不支持 --disable-userns**（`Unknown option --disable-userns`）。从方案中去掉此项。
- Unix socket 文件必须用 `--bind`（不是 `--ro-bind`），且路径不能在被 `--tmpfs` 覆盖的 /tmp 下。建议放在 `{WORKSPACE_DIR}` 或 home 目录下。
- codearts 尝试连 `snap-access.cn-north-4.myhuaweicloud.com:443`（华为云 xCenter），不在白名单中。需确认是否必须，如是则加入白名单或确保 OPENCODE_DISABLE_MODELS_FETCH=1 生效。
- codearts 需要可写的 `.local/state/codeartsdoer` 和 `.cache/codeartsdoer` 目录（bwrap --bind 而非 --ro-bind）。
- hermes 需要 grape 的 venv 路径（`/home/yangaiwu/grape/.venv`）在沙箱内可见。
| 1 | detect.py — 环境预检 + 敏感 env 扫描 | deepagents detect.py |
| 2 | audit.py — RotatingAuditLog + 哈希链 + verify_chain | agent-bom proxy_audit.py |
| 3 | cgroup.py — CgroupSlice + 降级 + context manager | deepagents cgroup.py |
| 4 | proxy.py — HTTP 代理（ThreadingTCPServer）+ 白名单 + 动态白名单（GRAPE_SERVER_URL）+ DNS 校验 + hop-by-hop + 走私检查 + CONNECT 超时/半关闭 + HTTP 转发 + path 脱敏 + 桥接 + env 生成 + 崩溃监控 | Anthropic |
| 5 | sandbox.py — build_bwrap_cmd + --cap-drop ALL + --disable-userns + --symlink shadow + NO_PROXY 移除 localhost + prepare/cleanup + 沙箱内脚本 | Anthropic + deepagents |
| 6 | executor.py — prepare/cleanup + OOM + terminate + register | deepagents executor.py |
| 7 | runtime/agent.py — Worker 端同步 | — |
| 8 | config.py — 配置项（cgroup 默认 0/凭据清单空/NO_PROXY 只含 ::1） | — |
| 9 | tests/test_sandbox_net.py — 验收 1-28 | — |
| 10 | 本机验证 GRAPE_SANDBOX_NET=on | — |

---

## 十二、功能影响分析

### 阻断性问题（实施前必须解决）

| # | 改动 | 影响 | 解决方案 | 状态 |
|---|------|------|----------|------|
| 1 | --unshare-net | Agent 调 grape API（localhost:3000）不可达 | NO_PROXY 移除 localhost/127.0.0.1 + 白名单动态加入 127.0.0.1 + 代理转发到宿主 3000 | ✅ 已更新方案 |
| 2 | --unsetenv BOT_GITCODE_TOKEN | QA/SRE skill 全部 curl 调 GitCode API 401 | P0 不遮蔽，靠网络白名单限制 | ✅ 已更新方案 |
| 3 | --unsetenv HUAWEICLOUD_SDK_AK/SK | QA/SRE terraform plan/apply 失败 | P0 不遮蔽，靠网络白名单限制 | ✅ 已更新方案 |
| 4 | Node.js 不认 HTTP_PROXY | opencode/codearts 调模型 API 失败 | 已验证：opencode/codearts 是 bun 编译，原生走 HTTP_PROXY | ✅ 已验证通过 |

### 中影响项

| # | 改动 | 影响 | 解决方案 | 状态 |
|---|------|------|----------|------|
| 5 | cgroup 限额 | opencode + pip/npm install 可能不够 | 默认 0=自动取宿主机 50% | ✅ 已更新方案 |
| 6 | --cap-drop ALL | terraform mount / npm 全局安装可能失败 | 已验证：pip/npm/node/touch/chmod 全部正常 | ✅ 已验证通过 |
| 7 | 代理单线程 | 并发 HTTP 请求排队 | ThreadingTCPServer | ✅ 已更新方案 |

### 低影响项（无需额外处理）

| 改动 | 影响 | 原因 |
|------|------|------|
| --unshare-pid | 无 | _kill_stale_engine 在主进程执行，不在沙箱内 |
| --disable-userns | N/A | bwrap 0.4.1 不支持此选项，已去掉 |
| GIT_SSH_COMMAND=/bin/false | 无 | grape git 走 HTTPS（repogit.py clone_url_for） |
| --symlink /dev/null /etc/shadow | 无 | Agent 不读 shadow |
| 沙箱内桥接进程 | 低 | 需要 python3 在沙箱内（已有 /usr ro-bind），额外 ~10MB 内存 |
| --new-session | 无 | bwrap 进程在新 session 中运行，不影响 Agent 通信 |
