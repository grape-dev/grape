# 全分支深度审查修复方案

> 分支：feat/devbox-sandbox-integration
> 审查日期：2026-10-07
> 审查报告：17 BUG + 18 遗漏 + 10 体验级
> 原始报告：见会话 @session:default/20261007_122504_d27082

## 1. 修复优先级

### 合并阻塞（必须修复，6 个）
| BUG | 文件 | 一句话描述 |
|-----|------|-----------|
| BUG-1 | issues.py:250-285 | _group_events_by_issue 定义了 _sel 但未使用，实际 SQL 缺列 |
| BUG-2 | conftest.py | 未设置 GRAPE_DB_ENCRYPTION_KEY，crypto 测试全部失败 |
| BUG-4 | proxy.py:204,238-255 | _check_domain 校验 IP 后构造 URL 用 domain，DNS rebinding 风险 |
| BUG-8 | executor.py:1158+ | _run_cloud_sandbox 入口缺少 policy_manager.check_command/check_input |
| BUG-12 | settings.py:51-52 | huawei_cloud 回显 AK/SK 明文，codearts 只返回 exists |
| BUG-13 | event_pipeline.py:1954 / runtime.py:1175 | issue关闭和手动kill用DELETE，worker上报用UPDATE killed，不一致 |

### 建议修复（11 个）
| BUG | 文件 | 一句话描述 |
|-----|------|-----------|
| BUG-3 | cubesandbox.py:150-155 | _do_kill 无条件 return True，kill 失败不可检测 |
| BUG-5 | devbox.py:308-337 | _clone_repo 缺少幂等检查，重复 clone 失败 |
| BUG-6 | cubesandbox.py:392-425 | _configure_grape_cli 直接用 cmds.run，无重连保护 |
| BUG-7 | cubesandbox.py:337-356 | upload_skill 不检查 exit_code，失败静默通过 |
| BUG-9 | cubesandbox.py CubeSandboxInstance | 缺少 send_stdin/close_stdin 实例方法 |
| BUG-10 | devbox_dns.py + devbox.py | cleanup_dns_override 从未被调用，DNS monkey-patch 无清理 |
| BUG-11 | seccomp.py:206 | BPF sock_filter k 字段用 signed i 替代 unsigned I |
| BUG-14 | network.py:1083-1088 | _WhitelistCache 加载失败时 _loaded=True 空白名单，fail-open |
| BUG-15 | bwrap.py:594-608 | _SAFE_TOKENS 硬编码允许 HUAWEICLOUD_SDK_AK/SK 进入沙箱 |
| BUG-16 | network.py:560-564 | admin 白名单 HTTP 代理无 body 大小限制 |
| BUG-17 | cubesandbox.py:77-96 | CubeSandboxInstance.__init__ 不接收 work_branch 参数 |

### 后续处理（遗漏+体验级，记录不修复）
遗漏-2/3/4/5/6/7/8/9/10/11/12/13/14/15/16/17/18 + 体验-1~10

## 2. 逐个修复方案

### BUG-1: _group_events_by_issue SQL 列不匹配

**根因**: L252-253 定义了 `_sel` 变量（正确的 SQL 列含 runtime/skill/structured_output/agent_logs），但 L254-256 实际执行的 SQL 没用 `_sel`，只 SELECT 了 `entity_type`（第10列）。代码 L275 用 `r[9]` 取 runtime（实际是 entity_type），L276 用 `r[10]` 取 skill（越界）。

**修复**: 实际执行的 SQL 使用 `_sel` 变量。

```python
# 修复前（L254-256）
c.execute(
    f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, entity_type "
    f"FROM events WHERE repo_key IN ({placeholders}) AND issue_id!='' ORDER BY seq ASC",
    repo_keys)

# 修复后
c.execute(
    f"{_sel} FROM events WHERE repo_key IN ({placeholders}) AND issue_id!='' ORDER BY seq ASC",
    repo_keys)
```

同时修正列索引映射：`_sel` 含 `entity_type` 在第 11 位（有 structured_output 时第 12-13 位），需要调整 r[9]/r[10]/r[11]/r[12] 的索引。

**正确列顺序**（_sel）:
```
seq(0), issue_id(1), event_type(2), agent_type(3), title(4), actor(5), summary(6), status(7), created_at(8), runtime(9), skill(10), [structured_output(11), agent_logs(12)]
```
entity_type 不在 _sel 中——需要从 _sel 中加上 entity_type，或在解析时不依赖它。

实际上需要确认：原代码用 `r[9]` 作为 entity_type 过滤 PR 事件，同时用 `r[9]` 作为 runtime。这是同一个索引被两个语义复用的 bug。修复后 entity_type 需要单独查询或加入 _sel。

**最终修复方案**: _sel 加入 entity_type 列：
```python
_sel = (f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, runtime, skill, entity_type"
        + (", structured_output, agent_logs" if _has_so else ""))
```
列索引：seq(0) issue_id(1) event_type(2) agent_type(3) title(4) actor(5) summary(6) status(7) created_at(8) runtime(9) skill(10) entity_type(11) [structured_output(12) agent_logs(13)]

解析代码调整：
- `r[9]` → runtime（正确）
- `r[10]` → skill（正确）
- `r[11]` → entity_type（PR 过滤）
- `r[12]` → structured_output（如有）
- `r[13]` → agent_logs（如有）

**影响**: 修复后 `/api/issues/{owner}/{repo}/{number}/events` 正确返回 runtime/skill/output/logs 字段。

---

### BUG-2: conftest.py 未设置 GRAPE_DB_ENCRYPTION_KEY

**根因**: crypto.py 的 `_get_aesgcm()` 要求 `GRAPE_DB_ENCRYPTION_KEY` 环境变量或 `configs/crypto.json` 文件。conftest.py 没有设置，导致所有 encrypt()/decrypt() 调用抛 RuntimeError。

**修复**: 在 conftest.py 的 `_ensure_test_db` fixture 中设置环境变量：
```python
import base64
os.environ.setdefault("GRAPE_DB_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
```

**影响**: test_provider_config_dispatch.py 4个测试恢复，test_fencing_and_double_signal.py 批量运行不再受 DB 状态污染。

---

### BUG-4: proxy.py SSRF TOCTOU 竞态

**根因**: _check_domain() 解析域名校验 IP 后返回 resolved_ip，但 L238 构造 URL 用 domain 而非 resolved_ip，L246 httpx.Client 发请求时再做一次 DNS 解析。攻击者可 DNS rebinding：第一次解析返回公网 IP 通过校验，第二次解析返回内网 IP 实现 SSRF。此外 _check_domain 只校验第一个 IP 就 return，多 A 记录场景下其他 IP 可能是内网。

**修复**:
1. _check_domain 返回**所有**解析出的合法 IP（不只是第一个），任一在内网段就拒绝
2. URL 保持 domain（不用 resolved_ip）——HTTPS 证书校验和 SNI 需要 domain
3. DNS rebinding 残余风险通过短连接超时 + 实际环境 DNS 服务器可信缓解
4. 后续可升级为 httpx 自定义 transport 在连接层绑定已校验的 IP

**修正记录**（2026-10-07 深度分析）：
- 初版用 resolved_ip 构造 URL → HTTPS 证书校验失败（SNI 用 IP 而非域名）
- 改回 URL 保持 domain，只保留 _check_domain 校验所有 IP 的修复

```python
def _check_domain(domain: str) -> list[str]:
    """校验 domain：禁止 IP 字面量、内网段。返回所有解析后的合法 IP。"""
    # ... 禁止 IP 字面量、localhost ...
    infos = socket.getaddrinfo(domain, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    valid_ips = []
    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        for network in _DENIED_RANGES:
            if ip in network:
                raise HTTPException(403, f"domain resolves to denied address {ip_str}")
        valid_ips.append(ip_str)
    if not valid_ips:
        raise HTTPException(400, "No valid IP resolved")
    return valid_ips
```

构造 URL 时用第一个 IP，Host header 保持 domain：
```python
resolved_ips = _check_domain(domain)
resolved_ip = resolved_ips[0]
url = f"{scheme}://{resolved_ip}:{port}/{path.lstrip('/')}"
safe_headers["Host"] = domain  # 保持原 domain
```

**影响**: 消除 DNS rebinding SSRF 风险。

---

### BUG-8: 云沙箱路径缺少命令预检

**根因**: bwrap 路径的 `_run_acp_session`（executor.py:265）调用了 `policy_manager.check_command` + `check_input`。但 `_run_cloud_sandbox`（executor.py:1158）入口没有调用这些安全检查。设计文档§4.2一致性矩阵声称云沙箱路径也有命令预检（✅），但实现中缺失。

**修复**: 在 `_run_cloud_sandbox` 入口处添加命令预检 + 提示注入检测：
```python
def _run_cloud_sandbox(sandbox, backend, prompt, cwd, proc_env, session_id,
                       label, max_exec_seconds, use_session_id):
    # 安全预检——与 _run_acp_session 入口一致
    try:
        from src.agent.policy import policy_manager
        from src.agent.policy.errors import CommandBlocked
        from src.agent.policy.types import Decision, InputDecision
        _cmd = [getattr(backend, "binary", "agent"), "acp"]  # backend.binary 是 CLI 路径
        _guard_result = policy_manager.check_command(_cmd, task_id=session_id or "")
        if _guard_result.decision is Decision.DENY:
            raise CommandBlocked(f"dangerous command: {_guard_result.reason}")
        _input_result = policy_manager.check_input(prompt, task_id=session_id or "")
        if _input_result.decision is InputDecision.DENY:
            raise CommandBlocked(f"prompt injection detected: {_input_result.detail}")
    except CommandBlocked as _e:
        log.error(f"{label}cloud sandbox blocked: {_e}")
        yield ("done", {"exit_code": -1, "text": "", "errors": [str(_e)], "stderr": ""})
        return
    except ImportError:
        pass  # policy 模块不可用时跳过

    # ... 原有逻辑 ...
```

**影响**: 云沙箱路径的 prompt 经安全预检，恶意 prompt 注入被拦截。

---

### BUG-12: settings.py 回显华为云 AK/SK 明文

**根因**: L51-52 `huawei_cloud.ak` 和 `huawei_cloud.sk` 返回明文值，而 codearts 只返回 `ak_exists`/`sk_exists`。安全策略不一致。

**修复**: 删除明文返回，改为 masked 展示（前4字符+***）：
```python
"huawei_cloud": {
    "ak_exists": bool(acc.get("huawei_cloud_ak")),
    "sk_exists": bool(acc.get("huawei_cloud_sk")),
    "ak_masked": (acc.get("huawei_cloud_ak", "")[:4] + "***") if acc.get("huawei_cloud_ak") else "",
},
```

**影响**: 消除高危安全漏洞，任何登录用户无法获取华为云永久凭据明文。

---

### BUG-13: cloud_sandboxes 状态机不一致

**根因**: 三处 kill 操作用了不同的 DB 操作：
- event_pipeline.py:1954 → `DELETE FROM cloud_sandboxes`（issue 关闭时）
- runtime.py:1175 → `DELETE FROM cloud_sandboxes`（手动 kill API）
- runtime.py:1042 → `UPDATE state='killed'`（worker 上报 killed）

DELETE 后 worker 的后续 killed 状态上报会触发 INSERT OR IGNORE 重新插入 running 记录。审计历史丢失。

**修复**: 统一为 `UPDATE state='killed'`，不 DELETE。

event_pipeline.py:1954 修复：
```python
# 修复前
c.execute("DELETE FROM cloud_sandboxes WHERE sandbox_id=?", (sandbox_id,))

# 修复后
c.execute("UPDATE cloud_sandboxes SET state='killed', last_refresh_at=? WHERE sandbox_id=?",
          (_now, sandbox_id))
```

runtime.py:1175 修复：
```python
# 修复前
c.execute("DELETE FROM cloud_sandboxes WHERE sandbox_id=?", (sandbox_id,))

# 修复后
c.execute("UPDATE cloud_sandboxes SET state='killed', last_refresh_at=? WHERE sandbox_id=?",
          (_now, sandbox_id))
```

idle reaper 清理已 killed 超过 N 天的记录时再 DELETE（已有逻辑或需新增）。

**影响**: 状态机一致，审计历史保留，不会重新插入 running 记录。

---

### BUG-3: CubeSandbox _do_kill 永远返回 True

**修复**: 用 try/except 包裹：
```python
def _do_kill(self) -> bool:
    if not self._sandbox:
        return False
    try:
        self._sandbox.kill()
        return True
    except Exception as e:
        log.error(f"[cubesandbox] kill failed: {e}")
        return False
```

---

### BUG-5: DevBox _clone_repo 缺少幂等检查

**修复**: 在 _clone_repo 开头添加幂等检查：
```python
def _clone_repo(self) -> None:
    if not self._sandbox or not self._repo_url or not self._bot_token:
        return
    import shlex as _shlex
    # 幂等检查——已 clone 则跳过
    _ec, _out = self.run_cmd(f"test -d {self._REPO_DIR}/.git && echo exists", timeout=5)
    if "exists" in _out:
        log.info(f"[devbox] repo already cloned at {self._REPO_DIR}, skip")
        return
    # ... 原有 clone 逻辑 ...
```

---

### BUG-6: CubeSandbox _configure_grape_cli 直接用 cmds.run

**修复**: 所有 `cmds.run(...)` 调用改为 `self.run_cmd(...)`，走 background=True + 重连机制：
```python
def _configure_grape_cli(self) -> None:
    import shlex as _shlex
    # 先追加 PATH（与 DevBox 对齐顺序）
    _path_export = "export PATH=$PATH:/tmp/pylibs/bin"
    self.run_cmd(f"grep -q '/tmp/pylibs/bin' ~/.bashrc 2>/dev/null || echo '{_path_export}' >> ~/.bashrc", timeout=5)
    self.run_cmd(f"grep -q '/tmp/pylibs/bin' ~/.bash_profile 2>/dev/null || echo '{_path_export}' >> ~/.bash_profile", timeout=5)
    self.run_cmd(f"grep -q '/tmp/pylibs/bin' ~/.profile 2>/dev/null || echo '{_path_export}' >> ~/.profile", timeout=5)
    # 再配置 grape-cli
    self.run_cmd(f"grape config set-server {_shlex.quote(self._grape_server_url)}", timeout=10)
    if self._grape_cli_token:
        self.run_cmd("grape config set-token $GRAPE_TOKEN",
                     envs={"GRAPE_TOKEN": self._grape_cli_token}, timeout=10)
```

注意：这也修复了体验-6（PATH 追加顺序与 DevBox 不一致）。

---

### BUG-7: CubeSandbox upload_skill 无错误检查

**修复**: 检查 exit_code 并抛异常：
```python
def upload_skill(self, obs_url: str) -> None:
    if not self._sandbox or not obs_url:
        return
    import shlex as _shlex
    _skill_dir = f"{self.repo_dir}/.skills"
    _tmp_zip = "/tmp/skill.zip"
    r = self.run_cmd(
        f'curl -sSL {_shlex.quote(obs_url)} -o {_tmp_zip} && '
        f'mkdir -p {_skill_dir} && '
        f'unzip -q {_tmp_zip} -d {_skill_dir} && rm {_tmp_zip}',
        timeout=60,
    )
    _ec = r[0] if isinstance(r, tuple) else getattr(r, "exit_code", -1)
    if _ec != 0:
        _out = r[1] if isinstance(r, tuple) else getattr(r, "stdout", "")
        raise RuntimeError(f"upload_skill failed (exit={_ec}): {(_out or '')[:200]}")
    self._SKILL_DIR = _skill_dir
    log.info(f"[cubesandbox] skill zip downloaded for {self._sandbox_id}")
```

---

### BUG-9: CubeSandboxInstance 缺少 send_stdin/close_stdin

**修复**: 在 CubeSandboxInstance 中添加实例级方法：
```python
def send_stdin(self, data: str | bytes) -> None:
    """CubeSandbox 不支持交互式 stdin。"""
    raise NotImplementedError("cubesandbox does not support interactive stdin")

def close_stdin(self) -> None:
    """无 stdin 可关闭，no-op。"""
    pass
```

注意：不抛 UnsupportedCapability 而抛 NotImplementedError，与基类 cloud_base.py:439-443 的默认实现一致。

---

### BUG-10: DNS override 全局 monkey-patch 无清理

**修复**:
1. init_dns_override 支持 gateway_ip 变更更新（已实施）
2. DevBoxProvider 添加 cleanup() 方法调用 cleanup_dns_override（已实施）
3. cloud_worker main() 退出时调用 cleanup_dns_override（深度分析修正补充）

```python
# cloud_worker.py main() 退出路径
_exit_code = asyncio.run(_amain())
# BUG-10 修复：退出时清理 DNS override（进程级 monkey-patch 恢复）
try:
    from src.utils.devbox_dns import cleanup_dns_override
    cleanup_dns_override()
except Exception as _e:
    log.warning(f"DNS override cleanup failed: {_e}")
```

**修正记录**（2026-10-07 深度分析第二轮）：
- 初版只在 DevBoxProvider 添加了 cleanup() 方法，但从未接入调用链
- 深度分析发现 cloud_worker main() 退出时没有调用 cleanup_dns_override
- 补充：直接在 main() 的 _amain() 返回后调用 cleanup_dns_override（不依赖 provider 实例）

---

### BUG-11: seccomp.py BPF sock_filter 用有符号 i

**修复**: `struct.pack('<HBBi', ...)` 改为 `struct.pack('<HBBI', ...)`：
```python
filters = [
    struct.pack('<HBBI', 0x20, 0, 0, 0),  # BPF_LD|BPF_W|BPF_ABS
    struct.pack('<HBBI', 0x15, 1, 0, self._NR_SOCKET),
    struct.pack('<HBBI', 0x06, 0, 0, self._SECCOMP_RET_ALLOW),
    struct.pack('<HBBI', 0x20, 0, 0, 16),
    struct.pack('<HBBI', 0x15, 2, 0, self._AF_INET),
    struct.pack('<HBBI', 0x15, 1, 0, self._AF_INET6),
    struct.pack('<HBBI', 0x06, 0, 0, self._SECCOMP_RET_ALLOW),
    struct.pack('<HBBI', 0x06, 0, 0, self._SECCOMP_RET_ERRNO),
]
```

---

### BUG-14: _WhitelistCache 加载失败时 fail-open

**修复**: 加载失败时不标记 _loaded=True，标记 _load_error=True：
```python
def _do_load(self) -> None:
    try:
        # ... 尝试从 API / DB 加载 ...
        self._loaded = True
    except Exception as e:
        log.warning(f"WhitelistCache load failed: {e}")
        if not self._loaded:
            self._domains = set()
            self._load_error = True  # 标记加载失败（不设 _loaded=True）
```

调用方检查 _load_error 并 fail-closed 报错。

---

### BUG-15: bwrap _SAFE_TOKENS 允许云 AK/SK 进入沙箱

**状态**: 暂不修复——当前业务需要 HUAWEICLOUD_SDK_AK/SK（OBS 操作）和 CODEARTS_CLI_AK/SK（dummy 值绕过 CLI 检查）进入沙箱。后续改为通过 grape 后端 /api/proxy 中转后再移除。

**记录**: 安全风险已知，接受当前状态。

---

### BUG-16: admin 白名单 HTTP 代理无 body 大小限制

**修复**: 对 admin 白名单来源也加 max_body 上限（可设更大值如 10MB）：
```python
else:
    # admin 白名单来源——加 max_body 校验（与 skill egress 来源一致）
    _ADMIN_MAX_BODY = 10 * 1024 * 1024  # 10MB
    cl = int(self.headers.get("Content-Length", 0))
    if cl > _ADMIN_MAX_BODY:
        self._audit(method, host, path, {"allowed": False, "reason": f"admin max_body exceeded {_ADMIN_MAX_BODY}", "source": "admin"}, 0, 413)
        self._send_denial(413, host, f"admin max_body exceeded {_ADMIN_MAX_BODY}")
        return
    if cl > 0:
        upstream.sendall(self.rfile.read(cl))
```

---

### BUG-17: CubeSandboxInstance.__init__ 不接收 work_branch

**修复**: 在 __init__ 中接收 work_branch 参数：
```python
def __init__(self, *, issue_id: str, repo_key: str, user_id: int,
             config: dict, template_id: str = "", state_reporter=None,
             engine_type: str = "", work_branch: str = ""):
    super().__init__(
        issue_id=issue_id,
        repo_key=repo_key,
        provider_name="cubesandbox",
        user_id=user_id,
        engine_type=engine_type,
        state_reporter=state_reporter,
    )
    self._config = config
    self._sandbox: Any = None
    self._sandbox_id: str = ""
    self._template_id = template_id
    self._init_envs: dict = {}
    self._grape_server_url: str = self._config.get("grape_server_url", "")
    self._grape_cli_token: str = self._config.get("grape_cli_token", "")
    self._repo_url: str = ""
    self._bot_token: str = ""
    self._work_branch: str = work_branch  # 显式初始化
```

---

## 3. 遗漏级发现处理方案

### 建议同步修复（低风险，与 BUG 修复一起做）
| 遗漏 | 修复方案 |
|------|---------|
| 遗漏-2 | ENGINE_REGISTRY.get(engine_type.lower().strip()) |
| 遗漏-3 | CubeSandbox run_cmd 添加 envs 参数 |

### 记录不修复（后续处理）
遗漏-4~18 + 体验-1~10：记录在 §13 已知问题表中，后续迭代处理。

---

## 4. 修复顺序

1. 合并阻塞 BUG（BUG-1/2/4/8/12/13）
2. 建议修复 BUG（BUG-3/5/6/7/9/10/11/14/15/16/17）
3. 遗漏-2/3（低风险同步修复）
4. 运行测试验证
5. 更新 §13 已知问题表
6. 提交推送

## 5. 测试验证

- 修复后运行全部测试：目标 511 passed, 0 failed
- 重点关注：
  - test_issue_events_api.py（BUG-1）
  - test_provider_config_dispatch.py（BUG-2）
  - test_proxy.py（BUG-4）
  - test_settings.py（BUG-12）
  - test_cloud_sandbox_lifecycle.py（BUG-13）
  - test_cubesandbox.py（BUG-3/6/7/9/17）
  - test_devbox.py（BUG-5/10）
  - test_seccomp.py（BUG-11）
  - test_network.py（BUG-14/16）
  - test_bwrap.py（BUG-15）

## 6. 交叉引用

- 归一文档：docs/weekly-05/sandbox-architecture.md §13 已知问题
- 审查报告原文：会话 @session:default/20261007_122504_d27082
- 设计文档§4.2 一致性矩阵：docs/weekly-05/sandbox-architecture.md

## 7. bwrap 沙箱 UDS 挂载修复（2026-10-07 追加）

> commit: 8770faca
> 验证：194 远程 worker bwrap opencode exit=0 text_len=47 task=completed

### 7.1 根因（三个叠加问题）

| # | 根因 | 代码位置 | 症状 |
|---|------|---------|------|
| 1 | bwrap 0.4.1 不支持 --bind 直接挂载 Unix socket 文件 | bwrap.py L207 `--bind uds_path uds_path` | `Can't create file at ...: No such device or address` |
| 2 | --tmpfs /tmp 覆盖了 HOME 前缀的 RO/RW bind | bwrap.py L329 `--tmpfs /tmp` 在 RO/RW bind 之后 | 沙箱内 `$HOME/.config/opencode/opencode.json` 不存在；stage2 日志 `UDS connect failed: FileNotFoundError` |
| 3 | /api/runtime/whitelist 要求 system scope | runtime.py L1973 `if cred.scope != "system"` | worker user scope 凭据被 403，_WhitelistCache 加载失败，opencode 辅助域名（models.opencode.ai 等）被 egress 策略拒绝。**已修复（bola-simplify-design）**：scope=system 废弃，统一用 global |

### 7.2 修复方案

| 文件 | 修复 |
|------|------|
| network.py L1144 | UDS socket 放在专用目录 `~/.grape-sandbox-uds/` |
| bwrap.py L205-213 | UDS bind 从 --tmpfs /tmp 之前移除，改为只读 uds_path 变量 |
| bwrap.py L325-331 | UDS 目录 bind 放在 --tmpfs /tmp 之后 |
| bwrap.py L284-315 | HOME 前缀的 RO/RW 路径收集到 `_post_tmpfs_binds`，在 --tmpfs /tmp 之后 bind |
| bwrap.py L335-340 | _post_tmpfs_binds 循环执行 --ro-bind / --bind |
| runtime.py L1966-1974 | 移除 /api/runtime/whitelist 的 system scope 限制 |
| base.py L37-44 | 新增 `runtime_egress_domains` 属性（引擎运行时辅助域名） |
| DB engine_sandbox_whitelist | 加 models.opencode.ai / registry.npmjs.org / opencode.ai |

### 7.3 bwrap opts 顺序（修复后）

```
--unshare-user --unshare-uts --unshare-net
--dev /dev --proc /proc
--ro-bind /usr /usr ... (系统 RO，非 HOME 前缀)
--ro-bind ~/.config/opencode /tmp/grape-home/.config/opencode → 延迟到 --tmpfs 后
--bind ~/.cache/opencode /tmp/grape-home/.cache/opencode → 延迟到 --tmpfs 后
--tmpfs /tmp                          ← 覆盖 /tmp
--ro-bind ~/.config/opencode /tmp/grape-home/.config/opencode  ← 在 tmpfs 后，可见
--bind ~/.cache/opencode /tmp/grape-home/.cache/opencode       ← 在 tmpfs 后，可见
--bind ~/.grape-sandbox-uds ~/.grape-sandbox-uds               ← UDS 目录，在 tmpfs 后
--dir /tmp/grape-home                 ← 在 tmpfs 上创建 HOME 目录
```

### 7.4 取证链路

1. strace 确认 opencode 连 127.0.0.1:8888（HTTP_PROXY 生效）
2. stage2 日志 `connected to UDS`（UDS 目录 bind 修复生效）
3. 沙箱内 `ls $HOME/.config/opencode/opencode.json` 存在（RO bind 顺序修复生效）
4. 审计日志 `outcome: allow, reason: whitelist match`（白名单 API + DB 修复生效）
5. opencode `exit=0 text_len=47`（端到端成功）

### 7.5 交叉引用

- skill：bwrap-sandbox-env-leak（已更新 UDS socket 挂载失败案例）
- sandbox-architecture.md §13 已知问题

## 8. RW 路径并发隔离（2026-10-07 追加）

> commit: d3ad9fb0
> 验证：194 远程 worker bwrap opencode task=completed

### 8.1 问题

多个 bwrap 沙箱 bind 同一个宿主机 RW 目录，并发写同一个文件导致冲突。

影响范围（HOME 前缀 RW 路径）：

| 引擎 | RW 路径 | 冲突文件 |
|------|---------|---------|
| opencode | `~/.cache/opencode` | models.json（5.3MB，每次启动重写） |
| opencode | `~/.local/share/opencode` | opencode.db（490MB SQLite，WAL 模式但多进程写仍锁竞争） |
| codearts | `~/.codeartsdoer/cli-data` | checkpoint/cron 数据 |
| codex | `~/.codex` | config.toml（通常只读，低风险） |
| hermes | 无（bwrap_overlay_fn 动态处理） | 无 |

### 8.2 修复

HOME 前缀的 RW 路径不 bind 宿主机源目录，改为在 `--tmpfs /tmp` 后用 `--dir` 创建空目录。

每个任务的 RW 数据完全隔离：
- opencode 启动时自动创建新的空 opencode.db
- 缓存（models.json 等）丢失无影响——opencode 通过 API 重新拉取
- apiKey 在 RO 路径 `~/.config/opencode/opencode.json` 中，不受 RW 隔离影响

非 HOME 前缀的 RW 路径（`/var/cache/bun-compile`、`/var/cache/codearts-tasks`）仍 bind 宿主机目录：
- bun-compile：按内容 hash 命名 + 原子写，并发安全
- codearts-tasks：按 task_id 命名目录，已隔离

### 8.3 并发安全矩阵

| 路径类型 | 隔离方式 | 并发安全 |
|---------|---------|---------|
| UDS socket | 按 task_id 区分文件名 | ✅ |
| HOME 前缀 RO 路径 | 按用户 HOME 区分（只读） | ✅ |
| HOME 前缀 RW 路径 | tmpfs 空目录（per-task 独立） | ✅ |
| 非 HOME RW 路径 | bind 宿主机（hash/task_id 隔离） | ✅ |
| 沙箱内 HOME | per-task tmpfs | ✅ |
| stage2 端口 8888 | per-task 独立网络命名空间 | ✅ |
