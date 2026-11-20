# 平台适配器（PlatformAdapter）设计方案 v3

> 分支：feat/devbox-sandbox-integration
> 日期：2026-10-10
> 状态：待确认（v3——采纳评审 + 业界参考后修订）

## 1. 问题分析

当前存在 6 处平台硬编码，新增平台需要修改框架代码。根因：框架直接操作平台特有字段，而不是让平台自己负责解析和转换。

## 2. 设计原则

- **面向契约编程**：统一 PlatformAdapter 契约，每个平台实现自己的适配器实例
- **适配器模式**：平台特有逻辑封装在适配器中，框架通过契约调用
- **配置文件驱动**：新增平台只需配置文件 + backend 文件，不改框架代码
- **自动发现**：pkgutil 目录扫描 + 约定导出（PROVIDER/ADAPTER），零改核心
- **显式优先级**：priority 类属性替代隐式注册顺序
- **Capability 声明**：插件声明支持的能力，框架按能力做特性降级
- **UI 自描述**：adapter.ui_schema() 返回声明式表单描述，前端通用渲染器消费

## 3. 可插拔四条硬标准

| 标准 | 含义 | 本方案达成方式 |
|------|------|---------------|
| C1 零改核心 | 新增插件不改框架任何一行 | pkgutil 自动发现 + 约定导出 PROVIDER/ADAPTER |
| C2 零重启装配 | 重启进程即生效 | 进程启动时扫描 backends/ 目录，import 模块触发注册 |
| C3 契约封闭 | 插件只依赖契约接口 | import 边界：允许 backends.api + 第三方 SDK + 标准库；禁止 manager/cloud_worker/settings/client |
| C4 边界自洽 | 插件声明 UI/校验/生命周期 | ui_schema() + validate_extra_config() + cleanup() |

## 4. PlatformAdapter 契约

```python
@dataclass
class ParsedConfig:
    """配置文件解析结果——通用数据结构，所有平台统一。"""
    endpoint: str
    api_key: str
    max_sandboxes: int = 10
    extra_config: dict = field(default_factory=dict)
    templates: list[dict] = field(default_factory=list)


class RuntimeConfig(TypedDict):
    """DB → Provider runtime config——normalize_db_config 的返回类型。"""
    platform: str
    name: str
    api_url: str
    api_key: str
    max_sandboxes: int
    # 平台特有字段由各 adapter 显式映射，未知字段丢弃并 warn


@dataclass
class ArtifactPath:
    """prepare_artifacts 返回的落盘文件——cloud_worker 统一管理生命周期。"""
    path: str
    cleanup_on_delete: bool = True


@dataclass
class FormField:
    """前端表单字段定义——声明式 UI schema。"""
    name: str
    type: str  # text | textarea | bool | select | file
    label: str = ""
    help: str = ""
    default: Any = ""
    required: bool = False
    placeholder: str = ""
    options: list[dict] = field(default_factory=list)  # [{value, label}]


@dataclass
class FormGroup:
    """表单分组——支持条件显示和折叠。"""
    title: str = ""
    fields: list[FormField] = field(default_factory=list)
    show_when: str = ""  # 条件：platform == "xxx" 时显示（空=始终显示）


@dataclass
class FormSchema:
    """完整表单 schema——adapter.ui_schema() 返回。"""
    groups: list[FormGroup] = field(default_factory=list)


@dataclass
class Capability:
    """平台能力声明——框架按能力做特性降级，不假设所有平台能力对等。"""
    ssl_cert: bool = False        # 支持 SSL 证书配置
    gateway_proxy: bool = False   # 支持网关代理
    snapshot: bool = False        # 支持快照
    file_upload: bool = False     # 支持文件上传
    port_expose: bool = False     # 支持端口暴露


class PlatformAdapter(Protocol):
    """平台适配器契约——每个平台实现自己的适配器实例。

    注册表存实例（非 class），方法为实例方法。
    框架通过此契约操作平台，不直接感知任何平台特有字段。
    """

    # --- 元数据 ---
    platform: str        # 平台名
    display_name: str     # 展示名
    priority: int        # 排序优先级（小的优先，默认 100）
    is_cloud: bool       # 是否云沙箱平台（bwrap/null = False）

    # --- 数据转换 ---
    def parse_config_file(self, raw: dict) -> ParsedConfig: ...
    def normalize_db_config(self, db_row: dict) -> RuntimeConfig: ...
    def prepare_artifacts(self, config: RuntimeConfig, workdir: str, provider_id: int) -> list[ArtifactPath]: ...

    # --- 校验 ---
    def validate_extra_config(self, raw: dict) -> dict: ...

    # --- UI 自描述 ---
    def ui_schema(self) -> FormSchema: ...

    # --- 能力声明 ---
    def capabilities(self) -> Capability: ...

    # --- 生命周期 ---
    def required_env(self) -> list[str]: ...
    def cleanup(self, provider_id: int) -> None: ...
```

## 5. 自动发现机制

### 5.1 pkgutil 目录扫描（C1 零改核心）

```python
# src/agent/sandbox/backends/__init__.py —— 永远不改

import pkgutil, importlib, functools, logging, os

log = logging.getLogger("sandbox-backends")

@functools.lru_cache(maxsize=1)
def _build_registries() -> tuple[dict, dict, dict]:
    """扫描 backends/ 目录，自动发现所有 Provider + Adapter。

    约定：每个 backend 模块导出 PROVIDER（class）和 ADAPTER（实例）。
    模块名以 _ 或 api 开头的跳过。
    import 失败捕获 (ModuleNotFoundError, ImportError) 并 warn。
    """
    cloud_registry: dict[str, type] = {}
    local_registry: dict[str, type] = {}
    adapter_registry: dict[str, object] = {}

    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name.startswith("_") or mod_info.name == "api":
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{mod_info.name}")
        except (ModuleNotFoundError, ImportError) as e:
            log.warning(f"backend module '{mod_info.name}' import failed: {e}")
            continue

        adapter = getattr(mod, "ADAPTER", None)
        provider = getattr(mod, "PROVIDER", None)
        if not adapter or not provider:
            continue

        assert adapter.platform == mod_info.name, \
            f"platform mismatch: module={mod_info.name}, adapter.platform={adapter.platform}"

        if getattr(adapter, "is_cloud", True):
            cloud_registry[adapter.platform] = provider
            adapter_registry[adapter.platform] = adapter
        else:
            local_registry[adapter.platform] = provider

    return cloud_registry, local_registry, adapter_registry


def get_cloud_registry() -> dict[str, type]: ...
def get_local_registry() -> dict[str, type]: ...
def get_adapter(platform: str) -> PlatformAdapter | None: ...
def get_all_adapters() -> list[PlatformAdapter]: ...
def get_supported_platforms() -> list[str]: ...
def get_default_cloud_platform() -> str | None: ...
```

### 5.2 backend 模块约定导出

每个 backend 模块文件底部导出两个顶层符号：

```python
# src/agent/sandbox/backends/cubesandbox.py（文件底部）

PROVIDER = CubeSandboxProvider
ADAPTER = CubeSandboxAdapter()
```

```python
# src/agent/sandbox/backends/bwrap.py（文件底部）

PROVIDER = BwrapProvider
ADAPTER = BwrapAdapter()  # is_cloud=False
```

### 5.3 新增平台步骤（C1 零改核心）

1. 创建 `configs/e2b-config.json`（含 templates 数组）
2. 创建 `src/agent/sandbox/backends/e2b.py`：
   - `E2BProvider`（继承 CloudProviderBase）
   - `E2BAdapter`（实现 PlatformAdapter 契约）
   - 文件底部：`PROVIDER = E2BProvider` / `ADAPTER = E2BAdapter()`
3. 重启服务

**不需要改的文件**：__init__.py、init_tasks、cloud_worker、client、settings、sandbox.js、manager.py、db_setup.py

### 5.4 未来扩展：Entry Points（P1 发布时）

```toml
# 第三方插件包的 pyproject.toml
[project.entry-points."grape.sandbox_platforms"]
e2b = "e2b_plugin:E2BAdapter"
```

框架同时支持目录扫描（开发时）和 entry points（发布时），双源发现。

## 6. 各层改造

### 6.1 配置文件导入（init_tasks/sandbox_provider_init.py）

adapter.parse_config_file() 替代 extract_extra_config()

### 6.2 cloud_worker 配置下发（client.py）

adapter.normalize_db_config() 替代 endpoint→api_url 硬编码

### 6.3 cloud_worker 运行时准备（cloud_worker.py）

adapter.prepare_artifacts() 替代 _materialize_ssl_cert，返回 ArtifactPath 列表，cloud_worker 管理生命周期

### 6.4 API 层校验（settings.py）

adapter.validate_extra_config() 替代 _PLATFORM_EXTRA_MODELS + if/elif

### 6.5 API 层 health check（settings.py）

cloud_registry factory + adapter.normalize_db_config 替代 if/elif 平台分支

### 6.6 前端动态渲染（sandbox.js）

/sandbox-platforms API 返回 adapter.ui_schema() → FormSchema，前端通用渲染器 v-for 消费

### 6.7 manager.py fallback

get_default_cloud_platform() 替代 "cubesandbox" 硬编码

### 6.8 cloud_worker _expand_templates 排序

adapter.priority 替代 "cubesandbox" 硬编码优先

## 7. 平台适配器实现示例

### 7.1 CubeSandboxAdapter

```python
class CubeSandboxAdapter:
    platform = "cubesandbox"
    display_name = "CubeSandbox"
    priority = 50
    is_cloud = True

    def parse_config_file(self, raw: dict) -> ParsedConfig:
        # api_url → endpoint, ssl_cert_file → 读文件内容 → ssl_cert_content
        ...

    def normalize_db_config(self, db_row: dict) -> RuntimeConfig:
        # endpoint → api_url, 未知字段丢弃并 warn
        ...

    def prepare_artifacts(self, config, workdir, provider_id) -> list[ArtifactPath]:
        # ssl_cert_content → 落盘 cube-rootCA-{pid}.pem (0o600)
        ...

    def validate_extra_config(self, raw: dict) -> dict:
        # 宽松模式：未知字段丢弃并 warn
        ...

    def ui_schema(self) -> FormSchema:
        return FormSchema(groups=[
            FormGroup(title="SSL", fields=[
                FormField(name="ssl_cert_content", type="textarea",
                          label="SSL 证书 (PEM)", help="粘贴 CA 证书内容",
                          placeholder="-----BEGIN CERTIFICATE-----"),
            ]),
        ])

    def capabilities(self) -> Capability:
        return Capability(ssl_cert=True)

    def required_env(self) -> list[str]:
        return []

    def cleanup(self, provider_id: int) -> None:
        # 清理 cube-rootCA-{pid}.pem
        ...

PROVIDER = CubeSandboxProvider
ADAPTER = CubeSandboxAdapter()
```

### 7.2 DevBoxAdapter

```python
class DevBoxAdapter:
    platform = "devbox"
    display_name = "DevBox"
    priority = 100
    is_cloud = True

    def ui_schema(self) -> FormSchema:
        return FormSchema(groups=[
            FormGroup(title="网关", fields=[
                FormField(name="gateway_url", type="text", label="Gateway URL",
                          required=True, placeholder="https://{tunnel_id}-{port}..."),
                FormField(name="gateway_ip", type="text", label="Gateway IP"),
            ]),
            FormGroup(title="TLS", fields=[
                FormField(name="gateway_skip_tls_verify", type="bool",
                          label="跳过 TLS 验证", default=False),
            ]),
        ])

    def capabilities(self) -> Capability:
        return Capability(gateway_proxy=True)

PROVIDER = DevBoxProvider
ADAPTER = DevBoxAdapter()
```

### 7.3 BwrapAdapter（本地沙箱）

```python
class BwrapAdapter:
    platform = "bwrap"
    display_name = "Bwrap (本地)"
    priority = 999
    is_cloud = False

    def ui_schema(self) -> FormSchema:
        return FormSchema()  # 无配置

    def capabilities(self) -> Capability:
        return Capability()

PROVIDER = BwrapProvider
ADAPTER = BwrapAdapter()
```

## 8. 删除的文件/代码

| 文件 | 删除内容 |
|------|---------|
| platform_registry.py | 整个文件 |
| settings.py _PLATFORM_EXTRA_MODELS | dict 注册表 |
| settings.py _validate_extra_config if/elif | 平台分支 |
| settings.py L2022 旧 _validate_platform | 残留硬编码 |
| settings.py health check if/elif | 平台分支 |
| cloud_worker.py _materialize_ssl_cert | 整个函数 |
| client.py endpoint→api_url 映射 | 硬编码 |
| cloud_worker.py _expand_templates L99 | cubesandbox 优先 |
| manager.py L250 | "cubesandbox" fallback |
| db_setup.py _migrate_provider_configs_to_db | 旧函数（无调用方） |
| db_setup.py _seed_sandbox_templates | 旧函数（无调用方） |

## 9. 全链路改造点清单（17 项）

| # | 文件 | 改造点 |
|---|------|--------|
| 1 | backends/api.py | 新增 PlatformAdapter 契约 + ParsedConfig + RuntimeConfig + ArtifactPath + FormSchema + Capability |
| 2 | cubesandbox.py | 新增 CubeSandboxAdapter + 导出 PROVIDER/ADAPTER |
| 3 | devbox.py | 新增 DevBoxAdapter + 导出 PROVIDER/ADAPTER |
| 4 | bwrap.py | 新增 BwrapAdapter + 导出 PROVIDER/ADAPTER |
| 5 | backends/__init__.py | pkgutil 自动发现 + lru_cache + 三组 registry |
| 6 | sandbox_provider_init.py | adapter.parse_config_file() |
| 7 | cloud_worker.py | adapter.prepare_artifacts() |
| 8 | client.py | adapter.normalize_db_config() |
| 9 | settings.py | adapter.validate_extra_config() |
| 10 | settings.py | cloud_registry factory + adapter health check |
| 11 | settings.py | 删除 L2022 旧 _validate_platform |
| 12 | manager.py | get_default_cloud_platform() |
| 13 | cloud_worker.py | adapter.priority 排序 |
| 14 | sandbox.js | ui_schema 动态渲染表单 + 详情页 |
| 15 | sandbox.js | 表单提交 extra_config 动态组装 |
| 16 | settings.py | /sandbox-platforms API 返回 ui_schema |
| 17 | platform_registry.py | 删除整个文件 |

## 10. 安全考虑

| 项 | 策略 |
|----|------|
| ssl_cert_content 落盘权限 | 0o600 |
| ssl_cert_content DB 存储 | 明文存 extra_config（CA 证书非私钥，风险低） |
| 前端 ssl_cert_content 展示 | autocomplete=off |
| API 不回显 ssl_cert_content | GET 时脱敏 |
| provider 删除时清理 | adapter.cleanup(provider_id) |

## 11. 迁移策略

- 过渡期 1 个 release：新旧路径并行 + 对比测试
- 旧函数加 DeprecationWarning
- normalize_db_config 未知字段丢弃并 warn
- 存量 DB 数据通过 normalize 兼容

## 12. 测试计划

- 契约一致性测试：所有 adapter 实现同一组契约测试
- 行为测试：cubesandbox 证书落盘/清理、devbox 直通
- 跨层集成测试：配置文件 → DB → cloud_worker → Provider 创建
- 前端渲染测试：ui_schema → 表单控件映射

## 13. 影响隔离

- 平台升级：只改 backends/{platform}.py
- 框架升级：只改 init_framework.py + init_tasks/
- 配置格式变更：只改 configs/{platform}-config.json + 对应 Adapter

## 14. 未来扩展

| 阶段 | 内容 | 参考 |
|------|------|------|
| P0 | pkgutil 自动发现（当前实施） | Koi 目录扫描 |
| P1 | Entry Points 双源发现（发布时） | pytest entry_points |
| P2 | manifest 自描述文件（注册不加载实现） | VSCode Contribution Points |
| P3 | gRPC 子进程隔离（第三方插件） | Terraform Provider |
