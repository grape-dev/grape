# SPEC: USP 统一沙箱协议——A1 实施方案（实施级，可开工）

- 日期：2026-11-02
- 分支：feat/devbox-sandbox-integration5 @ 8d360a835（i4→i5 迁移后行号已漂移：executor.py:1771-1782 / hermes_acp.py:2045-2051——**实施时以锚点符号定位，不逐行信本 spec 行号**）
- 状态：**待开工**（A1 排期首位；决策点已收敛至 2 个，见 §8）
- 定位：本 spec 合并并取代两份前置文档的实施部分——"沙箱核心代码与平台解耦实施方案"（战略框架 P0-P5 + E2B 参照）与"Worker 接入插件化批 0 收口"（四件套实施细节）。战略背景与 E2B 实证论据见前者，不再重复；**入代码仓时须自包含**——前置文档的关键结论已复制进本文（E2B 参照表精华见 §2.1 契约注释与 §11 成本模型）
- 对外口径：USP（Unified Sandbox Protocol）——平台与各沙箱厂商之间的统一适配层，**语义锚点 = E2B 形态**（cube 族镜像天然内置 envd，E2B SDK 是沙箱界事实标准方言）

---

## 0. 目标与总验收（先立靶）

**目标**：核心执行代码（executor / hermes_acp / cloud_worker / 主控路由）零平台 import；新增一个云沙箱平台 = 只新增 `backends/{platform}.py`（+MANIFEST 声明），不改任何核心文件。

**总验收（C1 终极标准，grep 范围=全 src/ 减 backends/，不用手工文件清单——防"半迁=全缺"漏检）**：
```bash
grep -rn "from cubesandbox import\|from devbox import" src/ --include="*.py" | grep -v "sandbox/backends/"
# → 零命中（SDK import 圈死在 backends/*.py）

grep -rn "_sandbox\.pty" src/ --include="*.py" | grep -v "sandbox/backends/"
# → 零命中（私有对象穿透清除，sandbox.exec_session()/pty_create() 是唯一调用形态）
```

**不改的部分（划线）**：
- Provider CRUD / 凭据治理链 / engine_template_binding——已实施且稳定，零碰
- engine_strategies.py 引擎安装策略——已平台无关（SandboxContext），零碰
- bwrap / null 本地后端——is_cloud=False 路径保留，不参与本 spec
- DB 表结构——**零 DDL**

---

## 1. 现状精确坐标（integration4 实测 @ 61b2ba0fa，实施前重跑核对）

### 1.1 PTY 直穿两处（G1-致命）

**executor.py:1771-1782（i4 时为 1717-1730）**（PTY wrapper 路径）：
```python
from cubesandbox import PtySize                       ← SDK 类型进核心
_pty_handle = sandbox._sandbox.pty.create(            ← 私有对象穿透
    PtySize(rows=50, cols=200), cwd=..., envs=_safe_env, timeout=None)
_pty_handle.send_stdin("stty raw -echo\n"); time.sleep(0.3)
_pty_handle.send_stdin(f"python3 /tmp/{_wrapper_name}; exit\n")
handle = _PtyExecHandleAdapter(_pty_handle)           ← 适配器留在 executor（合理）
```

**hermes_acp.py:2045-2051（i4 时为 1972-1984）**（CubeSandboxTransport.start）：同款 `PtySize` import + `_sandbox.pty.create` + `send_stdin("sh /tmp/start_hermes.sh\n")`。**⚠ 注意：这处穿透在 Transport 类内部——§3 搬迁时必须与 §2.4 联动替换，否则穿透跟着搬进 backends（验收 grep 范围已扩到全 src，会命中）**。

PTY handle **全部消费面**（收编必须一一覆盖）：`send_stdin`（executor 3 处 1779/1782/1806 + Transport 内 2 处）、迭代 yield bytes（_PtyExecHandleAdapter.stream_stdout/iter_events）、`wait()`（异常内含退出码——正则提取 `exit (?:status|code) (\d+)`）、`kill()`（2 处）、`pid` 属性。

### 1.2 Transport 分叉（G1-c）

executor.py:1625 附近（PTY 分派段）：
```python
if sandbox.supports_pty():
    _transport = CubeSandboxTransport(sandbox, ...)   ← hermes_acp import 进 executor
else:
    _transport = DevBoxTransport(sandbox, ...)
```
两个 Transport 类平铺在 hermes_acp.py（1758/1956，i5 实测），不在 backends/ 体系内。**新平台=改执行核心分支**——这是分叉的第二处平台耦合。

### 1.3 已就位的地基（不重造）

| 资产 | 位置 | 状态 |
|---|---|---|
| supports_pty() 能力声明 | src/agent/sandbox/backends/cloud_base.py:199（cube override cubesandbox.py:124） | ✅ |
| run_command/write_file/read_file 契约 | cloud_base.py:466/259/460（**完整路径均为 src/agent/sandbox/backends/cloud_base.py**） | ✅ |
| file_exists / read_file_bytes | cubesandbox.py:358/:351、devbox.py:427/:421（**Transport 消费面成员，非 run_command/write_file 独大**） | ✅ |
| adapter 注册表 + priority | backends/__init__.py（get_adapter）+ api.py:100 | ✅ |
| ui_schema/validate_extra_config/cleanup | api.py:104-158 | ✅ |
| cloud_worker `_platform_priority` 已查 adapter.priority | cloud_worker.py:98-100 | ✅（G2 部分已消化） |

### 1.4 残余散布点（G2）

- `src/utils/devbox_dns.py` 全局模块（唯一调用方 devbox.py:733）
- runtime.py:390-393 三/四段引擎名解析裸写
- cubesandbox.py:130 `_build_egress_network`（"沙箱访问 grape"是共性）

---

## 2. 设计核心：ExecSession 契约（D1 已定案）

### 2.1 命名裁定（本 spec 收敛既有冲突）

批0 spec 用 PtySession，decoupling spec D1 定 ExecSession（E2B CommandHandle 印证：commands 与 pty 统一 handle）。**本 spec 定名 ExecSession**——PTY 只是它的一种来源；协议面采纳批0 的消费面映射（send_stdin/iter/wait/kill/pid 一一对应现 PtyHandle 消费面，mock 零改动）。

### 2.2 契约定义（cloud_base.py 新增）

```python
class ExecSession(Protocol):
    """执行会话契约——平台各自实现，executor/hermes_acp 只依赖本协议。
    语义锚点：E2B CommandHandle（commands.run 与 pty.create 返回同一 handle 形态）。

    wait() 契约：永不抛平台 SDK 异常——退出码提取逻辑（executor.py:1388-1405
    的 exit (?:status|code) (\d+) 正则）下沉进平台实现，契约只承诺返回 int。"""
    def send_stdin(self, data: str | bytes) -> None: ...
    def close_stdin(self) -> None: ...                 # 语义差异实测注意: devbox CloseStdin 有 500 容错(executor.py:1805 附近注释); CubeSandbox SDK handle 的 close_stdin 行为未实测——TDD 第 1 步 fake 断言须含两平台 close_stdin 语义用例
    def __iter__(self) -> Iterator[bytes]: ...         # yield 输出块
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...
    @property
    def pid(self) -> int: ...
    # 可选能力（非协议必选）:
    # keepalive() —— E2B inputHandle 启发, 长空闲会话防中间层静默断流（hwtunnel 坑）；
    #                批1 按需加, 契约预留方法位不强制实现

CloudSandboxBase 新增（默认路径）:
def pty_create(self, rows: int = 50, cols: int = 200, cwd: str | None = None,
               envs: dict | None = None, timeout: float | None = None) -> ExecSession:
    """创建 PTY 型 ExecSession。supports_pty()=False 的平台调用即 RuntimeError。"""
    raise RuntimeError(f"{type(self).__name__} does not support PTY")
```

**run_command 归一（第二步，不阻塞本批）**：run_command 的 ExecHandle（cloud_base.py:466）逐步归一到 ExecSession——两 handle 并存过渡，签名兼容（ExecHandle 已有 send_stdin/close_stdin/wait 面）。本批只收 PTY 面。

### 2.3 平台实现

**CubeSandbox**（cubesandbox.py，唯一允许 import PtySize 的地方）：
```python
def pty_create(self, rows=50, cols=200, cwd=None, envs=None, timeout=None):
    from cubesandbox import PtySize
    _h = self._sandbox.pty.create(PtySize(rows=rows, cols=cols), cwd=cwd,
                                  envs=envs or {}, timeout=timeout)
    return _CubePtySession(_h)   # 薄包装: wait() 异常→退出码提取逻辑迁入此处
```
SDK 升级时只改此一处（对齐既有 `_post_create_hook` 钩子哲学）。

**DevBox**：`supports_pty()=False`（现状）+ 不实现 pty_create——调用即 RuntimeError，语义清晰。

### 2.4 两处调用点切换

- executor.py PTY 分支（1771-1782）→ `sandbox.pty_create(rows=50, cols=200, cwd=cwd, envs=_safe_env, timeout=None)`；`_PtyExecHandleAdapter` 改包 ExecSession（签名不变则零逻辑改动）
- hermes_acp.py CubeSandboxTransport.start（2045-2051）→ 同款替换
- `from cubesandbox import PtySize` 从两个核心文件**物理删除**

---

## 3. B：Transport 注册化（G1-c 收编）

**搬迁**：hermes_acp.py 的 `CubeSandboxTransport`（1956-）与 `DevBoxTransport`（1758-）**原样搬**到各自 backends 文件尾部。hermes_acp.py 保留 `PipeTransport`（bwrap 用，不碰）。

**⚠ 评审修正 1——Transport 真实依赖面（实测 i5）**：远不止 run_command/write_file——
| Transport | 依赖面（全部） |
|---|---|
| CubeSandboxTransport | run_command / write_file / file_exists / read_file_bytes / **_sandbox 私有对象（start 内 pty.create，2050）** |
| DevBoxTransport | run_command / file_exists / read_file_bytes |

**搬迁必须与 §2.4 联动**：CubeSandboxTransport.start 的 PTY 直穿同步替换为 `sandbox.pty_create()`——否则"搬了家但穿透还在"，把 G1 从 hermes_acp.py 挪进 backends/cubesandbox.py（验收 grep 已扩全 src 会命中，翻车变门禁红灯）。

**⚠ 评审修正 4——ACPTransport 基类随迁（门禁互斥解）**：Transport 继承 `ACPTransport`（hermes_acp.py:1697 Protocol；同区还有 TransportError/TransportTimeout/TransportClosed 异常族 1685-1693，Transport 一并消费）——若基类留在 hermes_acp.py，backends import 它即触发 §5 门禁 FORBIDDEN（第一天就红）。**方案①（采纳）**：ACPTransport 协议 + Transport 异常族（TransportError/Timeout/Closed）随迁至 backends/api.py（或新 backends/acp_transport.py），hermes_acp.py 留 re-export 别名过渡（与 Transport 搬迁同款纪律）。PipeTransport（bwrap 消费）留原地不动。

**注册**（api.py 契约新增一条）：
```python
def build_transport(self, sandbox, hermes_bin: str, envs: dict):
    """返回该平台的 ACP Transport 实例。无 ACP 能力的平台 NotImplementedError。"""
```
- CubeSandboxAdapter.build_transport → CubeSandboxTransport(sandbox, ...)
- DevBoxAdapter.build_transport → DevBoxTransport(sandbox, ...)

**executor.py:1626 附近（PTY 分派段）分叉改写**：
```python
_transport = _adapter_for(sandbox).build_transport(sandbox, hermes_bin=_hermes_bin, envs=_acp_envs)
```
`_adapter_for(sandbox)`：从 sandbox.platform 查 backends 注册表（get_adapter 已有）。**supports_pty() 分叉在 executor 消失**——平台内部自决（CubeTransport 用 PTY、DevTransport 用文件轮询，选择逻辑回归 adapter 知识）。

**搬迁纪律（防半迁）**：整块搬 + hermes_acp.py 留 re-export 别名**一个过渡 commit**，全部引用点切换后下一 commit 删别名。引用对账清单：executor.py 1626 附近分叉两行 import、hermes_acp.py 内部互引、测试文件 grep 两个类名。**搬迁依赖面以 §3 勘误表为准（五成员含私有穿透）——勿沿用旧版"仅 run_command/write_file"前提**。

**D2 裁定（本 spec 定案）**：CubeSandboxTransport **不改名不挪模块**（只挪到 backends/cubesandbox.py 尾部）——P0 最小化，日志关键词保持排查习惯连续；改名（PtyFifoTransport）与 hermes_acp.py 瘦身（2414 行）挪到 P4 契约测试阶段一并做。

---

## 4. C：MANIFEST 能力声明（G4）

### 4.1 形态（代码即 manifest，各 adapter 类属性）

```python
class CubeSandboxAdapter:
    MANIFEST = {
        "platform": "cubesandbox",
        "display_name": "CubeSandbox",
        "capabilities": ["create", "kill", "exec", "pty", "file_io",
                         "list_templates", "health_check", "egress_control",
                         "probe_engines"],        # probe_engines: 探测 spec 能力位（detected 参与调度链）
        "config_schema": None,                    # 复用 ui_schema()（不重复定义, manifest 引用它）
        "sdk": {"package": "cubesandbox>=0.7", "lazy": True},   # 声明; 消费（懒安装）批1
        "priority": 10,
        "min_worker_version": "",
    }
```

### 4.2 消费方

| 消费方 | 用法 |
|---|---|
| 前端 | `/sandbox-platforms` 返回体加 manifest 字段；按 capabilities 渲染按钮（无 list_templates 不显"扫描"）——**只加显隐判断，不做 UI 改版** |
| 主控端点 | 操作合法性前置校验（capabilities 含才放行，替代点击后 400） |
| worker | 批1 懒装 SDK 依据（sdk.package）；本批只声明不消费 |
| 契约测试 | P4 按 capabilities 逐项验证 |

### 4.3 一致性断言（冒烟契约，启动/测试时 fail-fast）

- `MANIFEST.capabilities 含 "pty" ⇔ supports_pty()==True`——**双向**：声明了没实现要报错；实现了没声明同样报（漏写=前端隐藏按钮/调度链静默降级，比多写更险）
- `含 "list_templates" ⇔ hasattr(adapter, "list_templates")`
- `含 "probe_engines" ⇔ hasattr(adapter, "probe_engines")`（探测 spec 的能力位同规则）
- 防声明与实现漂移（G4 根因复防）

---

## 5. D：CI import 边界门禁（G3 机器化半步）

`scripts/check_backend_boundaries.py`（pre-commit + CI 挂钩）：
```python
# 正向: backends/{cubesandbox,devbox}.py 的 import 白名单
ALLOWED = 自身 / backends.api / backends.types(如有) / 平台 SDK / 标准库
FORBIDDEN = src.runtime.* / src.api.* / src.agent.executor / src.agent.hermes_acp / src.agent.manager
# 反向: executor.py / hermes_acp.py / cloud_worker.py / api/routes/runtime.py
#        不得 from cubesandbox import / from devbox import
# 违规 → 非零退出 + 违规行打印（7fc03ce5d 教训: 半迁=全缺, 机器看护）
```
白名单显式列模块路径；新增白名单项需 spec 级裁定（防门禁松脱）。

---

## 6. P1 残余项（本批可选，含 P1.5 异常翻译）

优先级低于 A-D 四件套，随批推进：

| 项 | 内容 | 量级 |
|---|---|---|
| P1.1 devbox DNS 内聚 | src/utils/devbox_dns.py → backends/devbox_dns.py（唯一调用方 devbox.py:733） | ~搬文件+改 import |
| P1.2 引擎名解析公共化 | runtime.py:390-393 三/四段解析抽 backends/naming.py（parse/build_engine_name），主控+worker 统一引用 | ~60 行 |
| P1.3 egress 共性上移 | cubesandbox.py:130 网段推断 → CloudProviderBase 默认 build_egress_network，cube override 差异部分（D3 待拍：放基类还是配置层） | ~80 行 |
| P1.5 异常翻译层 | 本仓 SandboxError 基类 + TimeoutError/NotRunningError/UnreachableError/NotEnoughSpaceError（backends/errors.py）；adapter 内捕 SDK 异常翻译再抛；核心只捕本仓异常——**P4 契约测试前置** | ~100 行 |

---

## 7. 实施顺序（TDD）与验收

### 7.1 TDD 顺序

1. RED：ExecSession 契约测试（fake 实现：send_stdin/iter/wait/kill/pid/close_stdin 语义断言，含 **wait 异常→退出码提取**迁移验证）
2. GREEN：cloud_base.py ExecSession 协议 + pty_create + Cube 实现 + wait 异常下沉（§2.3）
3. RED：两处直穿点的行为等价测试（现有 PTY 测试改造——fake pty 改 fake ExecSession）
4. GREEN：executor.py:1771 / hermes_acp.py:2045 替换 + SDK import 物理删除（§0 验收 1 过半）
5. RED：build_transport 契约测试（两平台各返回正确类型）
6. GREEN：Transport 整块搬迁 + executor 分叉改写 + re-export 别名 + 引用对账 + 删别名（**搬迁单独 commit 利回溯**）
7. GREEN：MANIFEST 三 adapter + 一致性断言 + /sandbox-platforms 扩展 + 前端按钮显隐（静态 JS 改后 bump ?v=）
8. GREEN：门禁脚本 + 两个方向违规样例自测
9. E2E 验收（§7.2）+ stash 基线对照

### 7.2 E2E 验收（真实证据链）

1. **cube PTY 路径**：hermes 引擎云任务自然事件触发（不走 redispatch——云 E2E 要自然事件），completed + 实时日志流回传正常（PTY fifo bridge 路径真实跑通）
2. **cube 非 PTY 路径**：opencode 引擎（run_command 路径）任务 completed
3. **devbox 路径**（无 PTY）：DevBoxTransport 搬迁后冒烟
4. `node --check` + 前端模拟渲染冒烟（按钮显隐不抛错）
5. pytest 沙箱套件 stash 前后基线一致（既有 test_cubesandbox TestCloneRepo 2 例失败为已知基线）
6. commit 规范：中文三段式（根因+修复+验证证据）；B 搬迁单独一条，A/C/D 可合一条原子收口

---

## 8. 决策点（已收敛至 2 个）

| # | 决策点 | 状态 |
|---|---|---|
| ~~D1~~ | ExecSession vs PtySession 命名 | **已定案**：ExecSession（E2B CommandHandle 印证），协议面沿批0 消费面映射 |
| ~~D2~~ | CubeSandboxTransport 改名/挪模块 | **已定案**（本 spec §3）：不改名，搬 backends/cubesandbox.py 尾部；改名挪 P4 |
| D3 | egress 共性上移位置（P1.3） | 待拍：CloudProviderBase 默认 + cube override（建议） vs provider 配置层——不阻塞 A-D |
| ~~D5~~ | worker 懒装分发形态 | 不在本批（批1），sdk.package 只声明 |

---

## 9. 风险表

| 风险 | 对策 |
|---|---|
| Transport 搬迁漏引用（半迁=全缺, 7fc03ce5d） | 引用对账清单（grep 两个类名全仓）+ re-export 别名过渡一个 commit |
| PTY wait() 异常语义迁移改变错误路径 | 契约文档写死"wait 永不抛 SDK 异常, 退出码内嵌" + 现有错误提取逻辑单测迁移（TDD 第 1 步） |
| 搬迁后 fifo bridge 隐式依赖断裂 | 前置核查以 §3 依赖面勘误表为准（run_command/write_file/file_exists/read_file_bytes/_sandbox 私有穿透五成员），穿透项须随 §2.4 联动替换后再搬迁 |
| PTY 收编引入时序回归（stty raw/sleep 0.3 魔法数字） | 改动逐字搬运不重构语义；E2E hermes 真任务验证；回退=两个调用点还原 |
| MANIFEST 与实现漂移 | 启动一致性断言 fail-fast（§4.3） |
| 门禁脚本误伤合法 import | 白名单显式列路径；新增需 spec 级裁定 |
| mock 面变化破坏既有测试 | ExecSession 协议签名与现 PtyHandle 消费面一一对应, fake 只换类型不改行为 |
| 改 executor 后端 py 未重启假象 | 改后必重启 uvicorn（红线） |

---

## 10. 后继

本 spec 全部验收过后的后继路线：
- P4 契约测试套（G5）+ Transport 改名 + run_command 归一 ExecSession
- 批1：worker 懒装 SDK（MANIFEST.sdk 消费）+ keepalive + P1.5 异常翻译进契约断言
- 引擎探测（worker 上报 detected binding 方案）的 probe_engines 能力位已被本 spec §4.1 预留

---

## 11. 附录：新平台接入成本模型（对接无 E2B 协议沙箱的改动量推演）

### 11.1 前提：E2B 协议族给了现成平台的四样东西

envd 守护进程（IO 语义统一）、SDK 契约形态（PtySize/CommandHandle）、模板元数据（envd_version）、平台 API 面（REST 模板管理）。无 E2B 协议的裸容器/Docker API 平台，四样全部自建。

### 11.2 情况一：本 spec 完成后接新平台（USP 的承诺态）

```
新增文件:  backends/<newplatform>.py     ~400-600 行
改动核心:  0 行（总验收 C1 的语义——机器可查）
```

adapter 文件内组件：

| 组件 | 量级 | 说明 |
|---|---|---|
| ADAPTER 注册 + MANIFEST | ~70 行 | capabilities 如实声明（无 pty 不写 pty） |
| build_provider | ~40 行 | 构造 Provider 实例 |
| pty_create / run_command / write_file / read_file | ~200-300 行 | **最大头**——E2B 系是"调 SDK 翻译"，裸容器平台要自己封装 exec API/attach 流 |
| build_transport | ~10-150 行 | 无 PTY 平台学 DevBoxTransport 文件轮询兜底（现成范式） |
| health_check / list_templates / kill_sandbox / ui_schema | ~100 行 | 平台 API 各自对接 |
| 异常翻译（P1.5） | ~30 行 | 平台错误→SandboxError 子类 |

配置侧：DB 一条 Provider 行 + requirements-worker 加 SDK 依赖。engine_strategies 引擎安装**零改动**（SandboxContext 平台原语签名不变，实现 run_cmd/write_file 即可装引擎）。能力残缺平台的降级路径已被 DevBox 范式验证（非 envd 系、无 PTY、文件轮询 transport，全链路在跑）。

**接入成本 ≈ 一个 adapter 文件，半天到两天（P4 契约测试套就绪后的稳态值；P4 前的前 1-2 个新平台另含人肉 E2E 成本——§7.2 三路径验证无法契约化复用）。**

### 11.3 情况二：本 spec 未完成就接（对照，说明 spec 的价值）

| 位置 | 要改什么 |
|---|---|
| executor.py:1626 附近 | Transport if-else **加第三个分支**（核心代码） |
| executor.py:1771 / hermes_acp.py:2045 | 新平台若有类 PTY 能力 → 再加一对直穿点 |
| cloud_worker.py | 展开注册的兜底逻辑 |
| 测试 | 无契约套，每平台各自 mock |

每个新平台重复在核心代码开刀——G1 穿透的结构性代价。

### 11.4 隐藏成本（诚实清单）

无 E2B 协议平台的真正成本不在接入面，在 **IO 语义的工程可靠性**——cube 的 PTY/exec 稳定靠 envd 在沙箱内的长期工程化。自建 IO（docker exec + attach 流）要重踩一遍坑族：流断检测、退出码提取、时序魔法数字。ExecSession 契约统一**语义**（wait 永不抛异常/close_stdin 容错/pid 属性），语义之下的**实现质量**是各平台自己的工程投入。

因此新平台评估顺序：**先问能否走 E2B 兼容路线**（CubeSandbox 示范了第三方自建平台直接采纳协议的路径——镜像内置 envd + SDK 兼容，IO 层零适配）；不能走再按本 spec 契约自建，DevBox 范式兜底。
