# 平台适配器实施改造方案 v4（逐文件 + 行号）

> 基于设计文档 platform-adapter-design.md v3 的 17 个改造点
> + 审查发现的 6 个额外硬编码点
> + 三轮评审修订
> 日期：2026-10-10

---

## 评审修订历史

### 第一轮（15 条，全部采纳）

build_provider 去 if/elif / 删 ArtifactPath / 删 Capability / assert→log+continue / 白名单统一 / client 删 fallback / delete 补全 / lru_cache 导出 reload / 前端 watch 初始化 / 前端加 select/number / repository 排序一致 / 新增平台验证 / 测试影响评估 / bwrap/null 说明

### 第二轮（5 条 P0/P1 + 8 条 P2，全部采纳）

build_provider 调用点显式改造 / health_check 契约下沉 / 删 required_env / _PROVIDER_FIELDS 独立 / validate 真校验 / row 列名映射 / _cloud_list 空检查 / mockplatform 文件名 / prepare_artifacts docstring / init_tasks 说明 / UI 入口过滤 is_cloud

### 第三轮（2 条 P0 + 5 条 P1 + 10 条 P2）

| 评审项 | 级别 | 决策 | 验证 |
|--------|------|------|------|
| health() 返回类型 | P0 | health() 返回 HealthStatus dataclass（有 .healthy），.healthy 正确；加 except log.warning + 兼容写法 | 已确认 types.py L73-78 |
| repository 排序覆盖 SQL 语义 | P0 | 保留 SQL fetchone()+LIMIT 1，SQL 排序改为 engines_match + updated_at DESC，去掉 cubesandbox 硬编码；不引入 Python 重排 | 已确认原码用 fetchone |
| validate 白名单与 normalize 不一致 | P1 | validate 白名单改为 _PROVIDER_FIELDS ∪ ui_schema 字段名 | — |
| _ 前缀豁免放过 _comment | P1 | 去掉 _ 前缀豁免，所有不在 known 里的字段都丢弃 | — |
| api.py docstring 与实际矛盾 | P1 | docstring 区分"新增平台不改"和"本次契约变更要改" | — |
| kill 绕过契约 | P1 | 契约加 kill_sandbox(config, sandbox_id)->bool | — |
| Vue $set 陷阱 | P1 | 不成立——项目用 Vue 3，普通赋值即可 | 已确认 |
| health_check 丢失 reason | P2 | 契约改为 health_check(config)->tuple[bool,str] | — |
| mockplatform validate 缺类型检查 | P2 | 补齐 | — |
| 前端 required 只画红星不拦截 | P2 | saveProvider 前端先校验必填 | — |
| openEditProvider api_key 空值语义 | P2 | 文档说明"空=不更新" | — |
| get_default_cloud_platform 返回 None | P2 | manager.py 已有 or "" 兜底 | — |
| datetime/timezone import | P2 | init_tasks 已有 | — |
| bwrap/null is_cloud 依赖 | P2 | 文档强调"所有 UI 入口必须过滤" | — |
| CloudProviderBase 属性 | P2 | 已确认 | — |
| delete active 结构 | P2 | 文档展示完整 | — |
| seccomp 噪音参数 | P2 | v3 保留，加 TODO 注释 | — |

### 第四轮（1 条 P0 + 3 条 P1 + 9 条 P2）

| 评审项 | 级别 | 决策 | 验证 |
|--------|------|------|------|
| repository 删 cubesandbox CASE 后无替代 | P0 | 动态拼 adapter.priority 进 SQL CASE provider WHEN...，保留 fetchone+LIMIT 1 | — |
| cleanup 硬编码两处 _ARTIFACT_DIR | P1 | cleanup 加 workdir 参数，和 prepare_artifacts 一致，调用方传入 | — |
| 配置文件删除藏在 init_tasks 改造里 | P1 | 移出范围——init_tasks 只换 platform_registry→adapter，保留原删除逻辑不变 | — |
| 前端默认平台用数组顺序不是 priority | P1 | API 按 priority 排序返回 + 加 priority 字段 | — |
| _migrated 收集后未使用 | P2 | 保留原删除逻辑后 _migrated 恢复用途 | — |
| logging 重复 | P2 | adapter 模块顶部定义一次 | — |
| templates log 缩进 | P2 | 修正到循环外 | — |
| active 4 元组 | P2 | 文档展示 SQL 列顺序 | — |
| Vue 3 delete | P2 | providerForm 是 reactive 不是 ref | — |
| mockplatform validate 完整代码 | P2 | §七补齐 | — |
| _PROVIDER_FIELDS 重复 | P2 | 各 adapter 独立定义（字段不同） | — |
| bwrap health_check 原因 | P2 | 文档说明"本地沙箱不做健康探测" | — |
| health_check 通用消息 | P2 | 保留"see server log"，用户可查日志 | — |
| validate 严格拒绝字符串 bool | P2 | 文档说明"API 调用须传正确类型" | — |

### 第五轮（代码审查 BUG 修复 + E2E 验证）

| 评审项 | 级别 | 决策 | 验证 |
|--------|------|------|------|
| BUG-1 config 文件 commit 前删除 | BUG | run() 内先 db.commit() 再删文件 | — |
| BUG-2 cloud worker scope 不一致 | BUG | create_cloud_worker scope='system'→'global' | — |
| BUG-3 init_framework except 缺 rollback | BUG | except 块加 db.rollback() | — |
| BUG-4 example.json 信息泄露 | BUG | 真实 IP/路径改为 placeholder | — |
| BUG-5 build_provider config 必填 | BUG | bwrap/null build_provider config 加默认值 None | E2E 验证修复 |
| BUG-A sandbox-templates scope 检查 system | BUG | L1688 'system'→'global'（阻断 cloud_worker） | — |
| BUG-B create_cloud_worker 不校验 name 前缀 | BUG | 加 'cloud-' 前缀校验 | — |
| bwrap 模式三引擎 A2E | E2E | hermes/opencode/codearts 全部 completed exit=0 | 194 机器验证通过 |
| 非bwrap 模式三引擎 A2A | E2E | hermes/opencode/codearts 全部 completed exit=0 | 194 机器验证通过 |
| scope 维度区分 | 审查 | sandbox_provider_config.scope=system/user（Provider 维度）vs runtime_credentials.scope=global/repo/user（凭据维度），不混改 | — |

---

## 最终契约方法清单

```
PlatformAdapter:
  # 元数据
  platform: str          # 必须和模块文件名一致
  display_name: str
  priority: int          # 小的优先
  is_cloud: bool         # bwrap/null = False

  # Provider 构造
  def build_provider(self, *, audit, credential, seccomp, alert_engine, config) -> Provider
      # 全关键字。bwrap/null 忽略 config。

  # 数据转换
  def parse_config_file(self, raw: dict) -> ParsedConfig
  def normalize_db_config(self, db_row: dict) -> RuntimeConfig
      # 白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。不豁免 _ 前缀。
  def prepare_artifacts(self, config: dict, workdir: str, provider_id: int) -> None
      # 允许原地修改 config（如注入 _ssl_cert_path）。

  # 校验
  def validate_extra_config(self, raw: dict) -> dict
      # 必填+类型检查+白名单过滤。白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。
      # 不合法抛 ValueError。

  # 健康检查
  def health_check(self, config: dict) -> tuple[bool, str]
      # 返回 (healthy, reason)。adapter 自己决定探测方式。

  # 沙箱销毁
  def kill_sandbox(self, config: dict, sandbox_id: str) -> bool
      # adapter 决定如何构造 Provider 并 kill。

  # UI 自描述
  def ui_schema(self) -> FormSchema

  # 生命周期
  def cleanup(self, provider_id: int, workdir: str) -> None
      # workdir 和 prepare_artifacts 一致，调用方传入。
```

删掉的：ArtifactPath、Capability、required_env()
新增的：build_provider、health_check、kill_sandbox

---

## 全景硬编码清单（23 处）

| # | 文件 | 行号 | 硬编码内容 | 改造点 |
|---|------|------|-----------|--------|
| H1 | settings.py | 1949-1964 | CubeSandboxExtraConfig + DevBoxExtraConfig + _PLATFORM_EXTRA_MODELS | P7 删除 |
| H2 | settings.py | 1968-1977 | _validate_extra_config if/elif Pydantic model | P7 改 adapter |
| H3 | settings.py | 2022 | `if provider not in ("cubesandbox", "devbox")` | 补充1 |
| H4 | settings.py | 2075-2081 | _validate_platform 从 platform_registry 获取 | P9 改 adapter |
| H5 | settings.py | 2087-2099 | list_sandbox_platforms 返回 extra_fields | P9/P16 改 ui_schema |
| H6 | settings.py | 2288-2291 | delete_sandbox_provider 硬编码 CubeSandboxProvider | 补充2 |
| H7 | settings.py | 2348-2358 | health check if/elif 平台分支 | P8 改 adapter.health_check |
| H8 | cloud_worker.py | 98-99 | _expand_templates `0 if platform == "cubesandbox" else 1` | P13 改 priority |
| H9 | cloud_worker.py | 166-191 | _materialize_ssl_cert 硬编码 cubesandbox | P7 改 prepare_artifacts |
| H10 | client.py | 395-411 | get_provider_configs 手动 endpoint→api_url + extra_config 展开 | P8 改 normalize_db_config |
| H11 | manager.py | 250 | `or "cubesandbox"` fallback | P10 改 get_default_cloud_platform |
| H12 | manager.py | 267,289-290,307 | factory 位置参数调用 | P3 改全关键字 |
| H13 | repository.py | 146 | SQL `CASE WHEN provider = 'cubesandbox' THEN 0` | 补充3 改 Python 重排 |
| H14 | repository.py | 158,164 | SQL `sandbox_provider IN ('devbox', 'cubesandbox')` | 补充3 |
| H15 | platform_registry.py | 1-143 | 整个文件 | P17 删除 |
| H16 | init_tasks/sandbox_provider_init.py | 41 | import platform_registry | P6 改 adapter |
| H17 | sandbox.js | 141-148 | saveProvider 按 platform if 组装 extra_config | P15 改动态组装 |
| H18 | sandbox.js | 504-523 | v-if platform === 'cubesandbox'/'devbox' 硬编码表单 | P14 改 ui_schema 渲染 |
| H19 | sandbox.js | 349-353 | 详情页 v-if platform === 'cubesandbox'/'devbox' | P14 改动态展示 |
| H20 | sandbox.js | 79 | _defaultProviderForm 硬编码 ssl_cert_content/gateway_* | P14 改动态 form |
| H21 | db_setup.py | 2138 | _migrate_provider_configs_to_db（无调用方死代码） | 补充4 删除 |
| H22 | db_setup.py | 4343 | _seed_sandbox_templates（无调用方死代码） | 补充4 删除 |
| H23 | backends/__init__.py | 1-37 | 手动 import + lambda factory | P3 改 pkgutil + build_provider |

---

## 一、新建文件

### 1.1 backends/api.py — 契约 + 数据类（P1）

文件：src/agent/sandbox/backends/api.py

```python
"""PlatformAdapter 契约 + 数据类——平台适配器设计 v3。

新增平台只需（3 步）：
1. 创建 configs/{platform}-config.json
2. 创建 backends/{platform}.py：{Platform}Provider + {Platform}Adapter
3. 重启服务

新增平台不需要改：__init__.py、init_tasks、cloud_worker、client、settings、sandbox.js、manager.py

注意：本次改造（v3）会改 manager.py 三处 factory 调用点（位置参数→全关键字），
但这是"契约变更"造成的，与"新增平台"无关。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict, runtime_checkable

log = logging.getLogger("sandbox-backends")


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
class FormField:
    """前端表单字段定义——声明式 UI schema。"""
    name: str
    type: str  # text | textarea | bool | select | number | file
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
    show_when: str = ""


@dataclass
class FormSchema:
    """完整表单 schema——adapter.ui_schema() 返回。"""
    groups: list[FormGroup] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "groups": [
                {
                    "title": g.title,
                    "show_when": g.show_when,
                    "fields": [
                        {
                            "name": f.name, "type": f.type, "label": f.label,
                            "help": f.help, "default": f.default,
                            "required": f.required, "placeholder": f.placeholder,
                            "options": f.options,
                        }
                        for f in g.fields
                    ],
                }
                for g in self.groups
            ]
        }


@runtime_checkable
class PlatformAdapter(Protocol):
    """平台适配器契约——每个平台实现自己的适配器实例。

    注册表存实例（非 class），方法为实例方法。
    框架通过此契约操作平台，不直接感知任何平台特有字段。
    """

    # --- 元数据 ---
    platform: str
    display_name: str
    priority: int
    is_cloud: bool

    # --- Provider 构造 ---
    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        """由 adapter 负责构造自己的 Provider。全关键字参数。
        bwrap/null 忽略 config；云沙箱使用 config。
        TODO: 未来 Provider 需要新参数时改为 ProviderContext dataclass 打包。"""
        ...

    # --- 数据转换 ---
    def parse_config_file(self, raw: dict) -> ParsedConfig: ...
    def normalize_db_config(self, db_row: dict) -> RuntimeConfig: ...
    def prepare_artifacts(self, config: dict, workdir: str, provider_id: int) -> None:
        """允许原地修改 config（如注入 _ssl_cert_path）。"""
        ...

    # --- 校验 ---
    def validate_extra_config(self, raw: dict) -> dict:
        """必填+类型检查+白名单过滤。不合法抛 ValueError。"""
        ...

    # --- 健康检查 ---
    def health_check(self, config: dict) -> tuple[bool, str]:
        """返回 (healthy, reason)。adapter 自己决定探测方式。"""
        ...

    # --- 沙箱销毁 ---
    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool:
        """adapter 决定如何构造 Provider 并 kill。"""
        ...

    # --- UI 自描述 ---
    def ui_schema(self) -> FormSchema: ...

    # --- 生命周期 ---
    def cleanup(self, provider_id: int, workdir: str) -> None:
        """清理落盘文件。workdir 和 prepare_artifacts 一致。"""
        ...
```

---

## 二、Adapter 实现（P2）

### 2.1 cubesandbox.py 追加 CubeSandboxAdapter

```python
class CubeSandboxAdapter:
    """CubeSandbox 平台适配器。"""
    platform = "cubesandbox"
    display_name = "CubeSandbox"
    priority = 50
    is_cloud = True

    # parse_config_file 用——判断哪些字段不入 extra_config
    _COMMON_FIELDS = {
        "api_url", "api_key", "max_sandboxes", "templates",
        "grape_server_url", "grape_cli_token",
        "request_timeout", "timeout", "pool_size",
        "_comment", "_fields",
    }

    # normalize_db_config + validate_extra_config 用——判断哪些字段安全传给 Provider
    # 和 _COMMON_FIELDS 独立：不含 _comment/_fields/templates 等元数据
    _PROVIDER_FIELDS = {
        "platform", "name", "api_url", "api_key", "max_sandboxes",
        "ssl_cert_content", "sandbox_domain",
        "timeout", "request_timeout",
        "grape_server_url", "grape_cli_token",
    }

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        return CubeSandboxProvider(
            audit=audit, credential=credential,
            alert_engine=alert_engine, config=config)

    def parse_config_file(self, raw: dict) -> ParsedConfig:
        """从 configs/cubesandbox-config.json 解析。
        ssl_cert_file → 读文件内容 → extra_config["ssl_cert_content"]
        """
        from .api import ParsedConfig
        endpoint = raw.get("api_url", "")
        api_key = raw.get("api_key", "")
        max_sandboxes = int(raw.get("max_sandboxes", 10))
        templates = raw.get("templates", [])
        extra = {}
        for key, val in raw.items():
            if key in self._COMMON_FIELDS:
                continue
            if val is None:
                continue
            extra[key] = val
        if "ssl_cert_file" in extra:
            try:
                with open(extra["ssl_cert_file"], "r") as f:
                    extra["ssl_cert_content"] = f.read()
                del extra["ssl_cert_file"]
            except Exception:
                pass
        return ParsedConfig(endpoint=endpoint, api_key=api_key,
                            max_sandboxes=max_sandboxes,
                            extra_config=extra, templates=templates)

    def normalize_db_config(self, db_row: dict) -> RuntimeConfig:
        """白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。
        不豁免 _ 前缀——_comment 等元数据字段一律丢弃。"""
        import logging
        log = logging.getLogger("sandbox-backends")
        schema = self.ui_schema()
        ui_fields = {f.name for g in schema.groups for f in g.fields}
        known = self._PROVIDER_FIELDS | ui_fields

        _extra = db_row.get("extra_config", {})
        if isinstance(_extra, str):
            import json as _json
            try: _extra = _json.loads(_extra)
            except Exception: _extra = {}
        if not isinstance(_extra, dict):
            _extra = {}

        cfg = {
            "platform": db_row.get("platform", "cubesandbox"),
            "name": db_row.get("name", "cubesandbox"),
            "api_url": db_row.get("endpoint", ""),
            "api_key": db_row.get("api_key", ""),
            "max_sandboxes": db_row.get("max_sandboxes", 10),
        }
        cfg.update(_extra)

        # 不豁免 _ 前缀——所有不在 known 里的字段都丢弃
        for key in list(cfg.keys()):
            if key not in known:
                log.warning(f"[cubesandbox] unknown field '{key}' in db config, discarding")
                del cfg[key]
        return cfg

    def prepare_artifacts(self, config, workdir: str, provider_id: int) -> None:
        """ssl_cert_content → 落盘 cube-rootCA-{pid}.pem (0o600)。
        允许原地修改 config：注入 _ssl_cert_path。"""
        cert_content = config.get("ssl_cert_content", "")
        if not cert_content:
            return
        cert_path = os.path.join(workdir, f"cube-rootCA-{provider_id}.pem")
        try:
            os.makedirs(workdir, exist_ok=True)
            old_umask = os.umask(0o077)
            try:
                with open(cert_path, "w") as f:
                    f.write(cert_content)
                os.chmod(cert_path, 0o600)
            finally:
                os.umask(old_umask)
            config["_ssl_cert_path"] = cert_path
            log.info(f"[cubesandbox] ssl_cert materialized to {cert_path} for provider_id={provider_id}")
        except Exception as e:
            log.warning(f"[cubesandbox] ssl_cert materialize failed for provider_id={provider_id}: {e}")

    def validate_extra_config(self, raw: dict) -> dict:
        """校验 extra_config——必填+类型检查+白名单过滤。
        白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。"""
        import logging
        log = logging.getLogger("sandbox-backends")
        schema = self.ui_schema()
        ui_field_defs = {f.name: f for g in schema.groups for f in g.fields}
        known = self._PROVIDER_FIELDS | set(ui_field_defs.keys())

        result = {}
        # ui_schema 字段做必填+类型校验
        for f in ui_field_defs.values():
            val = (raw or {}).get(f.name)
            if f.required and (val is None or val == ""):
                raise ValueError(f"字段 '{f.label or f.name}' 不能为空")
            if val is not None and val != "":
                if f.type == "bool" and not isinstance(val, bool):
                    raise ValueError(f"字段 '{f.label}' 必须是布尔值")
                if f.type == "number" and not isinstance(val, (int, float)):
                    raise ValueError(f"字段 '{f.label}' 必须是数字")
                result[f.name] = val
        # _PROVIDER_FIELDS 中非 ui_schema 的字段（如 sandbox_domain/timeout）——透传
        for key, val in (raw or {}).items():
            if key in known and key not in result and val is not None and val != "":
                result[key] = val
        # 白名单外字段丢弃+warn
        for key in (raw or {}):
            if key not in known:
                log.warning(f"[cubesandbox] unknown extra_config field '{key}' discarded")
        return result

    def health_check(self, config: dict) -> tuple[bool, str]:
        """CubeSandbox 健康检查——构造 Provider + health()。
        health() 返回 HealthStatus dataclass（有 .healthy 属性）。"""
        try:
            _prov = CubeSandboxProvider(config=config)
            result = _prov.health()
            if hasattr(result, "healthy"):
                return (result.healthy is True, getattr(result, "reason", ""))
            return (bool(result), "")
        except Exception as e:
            log.warning(f"[cubesandbox] health_check failed: {e}")
            return (False, "health check failed, see server log")

    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool:
        """构造 Provider + kill_sandbox。"""
        try:
            _prov = CubeSandboxProvider(config=config)
            _prov.kill_sandbox(sandbox_id)
            return True
        except Exception as e:
            log.warning(f"[cubesandbox] kill {sandbox_id} failed: {e}")
            return False

    def ui_schema(self):
        from .api import FormSchema, FormGroup, FormField
        return FormSchema(groups=[
            FormGroup(title="SSL", fields=[
                FormField(name="ssl_cert_content", type="textarea",
                          label="SSL 证书 (PEM)", help="粘贴 CA 证书内容",
                          placeholder="-----BEGIN CERTIFICATE-----"),
            ]),
        ])

    def cleanup(self, provider_id: int, workdir: str) -> None:
        cert_path = os.path.join(workdir, f"cube-rootCA-{provider_id}.pem")
        try:
            os.remove(cert_path)
            log.info(f"[cubesandbox] cleaned up {cert_path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"[cubesandbox] cleanup {cert_path} failed: {e}")


PROVIDER = CubeSandboxProvider
ADAPTER = CubeSandboxAdapter()
```

### 2.2 devbox.py 追加 DevBoxAdapter

```python
class DevBoxAdapter:
    """DevBox 平台适配器。"""
    platform = "devbox"
    display_name = "DevBox"
    priority = 100
    is_cloud = True

    _COMMON_FIELDS = {
        "api_url", "api_key", "max_sandboxes", "templates",
        "grape_server_url", "grape_cli_token",
        "request_timeout", "timeout", "pool_size",
        "_comment", "_fields",
    }

    _PROVIDER_FIELDS = {
        "platform", "name", "api_url", "api_key", "max_sandboxes",
        "gateway_url", "gateway_ip", "gateway_skip_tls_verify",
        "timeout", "grape_server_url", "grape_cli_token",
    }

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        return DevBoxProvider(audit=audit, credential=credential,
                              alert_engine=alert_engine, config=config)

    def parse_config_file(self, raw: dict) -> ParsedConfig:
        from .api import ParsedConfig
        endpoint = raw.get("api_url", "")
        api_key = raw.get("api_key", "")
        max_sandboxes = int(raw.get("max_sandboxes", 10))
        templates = raw.get("templates", [])
        extra = {}
        for key, val in raw.items():
            if key in self._COMMON_FIELDS: continue
            if val is None: continue
            extra[key] = val
        return ParsedConfig(endpoint=endpoint, api_key=api_key,
                            max_sandboxes=max_sandboxes,
                            extra_config=extra, templates=templates)

    def normalize_db_config(self, db_row: dict) -> RuntimeConfig:
        """白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。不豁免 _ 前缀。"""
        import logging
        log = logging.getLogger("sandbox-backends")
        schema = self.ui_schema()
        ui_fields = {f.name for g in schema.groups for f in g.fields}
        known = self._PROVIDER_FIELDS | ui_fields
        _extra = db_row.get("extra_config", {})
        if isinstance(_extra, str):
            import json as _json
            try: _extra = _json.loads(_extra)
            except Exception: _extra = {}
        if not isinstance(_extra, dict): _extra = {}
        cfg = {
            "platform": db_row.get("platform", "devbox"),
            "name": db_row.get("name", "devbox"),
            "api_url": db_row.get("endpoint", ""),
            "api_key": db_row.get("api_key", ""),
            "max_sandboxes": db_row.get("max_sandboxes", 10),
        }
        cfg.update(_extra)
        for key in list(cfg.keys()):
            if key not in known:
                log.warning(f"[devbox] unknown field '{key}' in db config, discarding")
                del cfg[key]
        return cfg

    def prepare_artifacts(self, config, workdir, provider_id) -> None:
        pass

    def validate_extra_config(self, raw: dict) -> dict:
        """白名单 = _PROVIDER_FIELDS ∪ ui_schema 字段名。"""
        import logging
        log = logging.getLogger("sandbox-backends")
        schema = self.ui_schema()
        ui_field_defs = {f.name: f for g in schema.groups for f in g.fields}
        known = self._PROVIDER_FIELDS | set(ui_field_defs.keys())
        result = {}
        for f in ui_field_defs.values():
            val = (raw or {}).get(f.name)
            if f.required and (val is None or val == ""):
                raise ValueError(f"字段 '{f.label or f.name}' 不能为空")
            if val is not None and val != "":
                if f.type == "bool" and not isinstance(val, bool):
                    raise ValueError(f"字段 '{f.label}' 必须是布尔值")
                if f.type == "number" and not isinstance(val, (int, float)):
                    raise ValueError(f"字段 '{f.label}' 必须是数字")
                result[f.name] = val
        for key, val in (raw or {}).items():
            if key in known and key not in result and val is not None and val != "":
                result[key] = val
        for key in (raw or {}):
            if key not in known:
                log.warning(f"[devbox] unknown extra_config field '{key}' discarded")
        return result

    def health_check(self, config: dict) -> tuple[bool, str]:
        try:
            _prov = DevBoxProvider(config=config)
            result = _prov.health()
            if hasattr(result, "healthy"):
                return (result.healthy is True, getattr(result, "reason", ""))
            return (bool(result), "")
        except Exception as e:
            log.warning(f"[devbox] health_check failed: {e}")
            return (False, "health check failed, see server log")

    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool:
        try:
            _prov = DevBoxProvider(config=config)
            _prov.kill_sandbox(sandbox_id)
            return True
        except Exception as e:
            log.warning(f"[devbox] kill {sandbox_id} failed: {e}")
            return False

    def ui_schema(self):
        from .api import FormSchema, FormGroup, FormField
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

    def cleanup(self, provider_id: int, workdir: str) -> None:
        pass


PROVIDER = DevBoxProvider
ADAPTER = DevBoxAdapter()
```

### 2.3 bwrap.py 追加 BwrapAdapter

```python
class BwrapAdapter:
    """Bwrap 本地沙箱适配器——无配置文件、无 DB 行、无落盘。
    is_cloud=False → 不出现在 UI 云沙箱平台列表中。
    所有 UI 入口必须过滤 is_cloud。"""
    platform = "bwrap"
    display_name = "Bwrap (本地)"
    priority = 999
    is_cloud = False

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        return BwrapProvider(audit=audit, credential=credential,
                             seccomp=seccomp, alert_engine=alert_engine)

    def parse_config_file(self, raw: dict):
        from .api import ParsedConfig
        return ParsedConfig(endpoint="", api_key="")
    def normalize_db_config(self, db_row: dict): return {}
    def prepare_artifacts(self, config, workdir, provider_id) -> None: pass
    def validate_extra_config(self, raw: dict): return {}
    def health_check(self, config: dict) -> tuple[bool, str]: return (True, "")
    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool: return True
    def ui_schema(self):
        from .api import FormSchema
        return FormSchema()
    def cleanup(self, provider_id: int, workdir: str) -> None: pass


PROVIDER = BwrapProvider
ADAPTER = BwrapAdapter()
```

### 2.4 null.py 追加 NullAdapter

```python
class NullAdapter:
    """Null 沙箱适配器。is_cloud=False → 不进 UI 平台列表。"""
    platform = "null"
    display_name = "None"
    priority = 9999
    is_cloud = False

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        return NullProvider()

    def parse_config_file(self, raw: dict):
        from .api import ParsedConfig
        return ParsedConfig(endpoint="", api_key="")
    def normalize_db_config(self, db_row: dict): return {}
    def prepare_artifacts(self, config, workdir, provider_id) -> None: pass
    def validate_extra_config(self, raw: dict): return {}
    def health_check(self, config: dict) -> tuple[bool, str]: return (True, "")
    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool: return True
    def ui_schema(self):
        from .api import FormSchema
        return FormSchema()
    def cleanup(self, provider_id: int, workdir: str) -> None: pass


PROVIDER = NullProvider
ADAPTER = NullAdapter()
```

---

## 三、backends/__init__.py — pkgutil 自动发现（P3）

```python
"""backends 包——pkgutil 自动发现平台 + Provider。

新增平台只需：1) 创建 backends/{platform}.py 导出 PROVIDER/ADAPTER
              2) 重启服务
不需要改本文件。
"""
from __future__ import annotations

import functools
import importlib
import logging
import pkgutil
from typing import Callable, Mapping

log = logging.getLogger("sandbox-backends")


@functools.lru_cache(maxsize=1)
def _build_registries() -> tuple[dict, dict, dict]:
    """扫描 backends/ 目录，自动发现所有 Provider + Adapter。

    一个模块的 platform 名不匹配或 import 失败不影响其他模块。
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

        if adapter.platform != mod_info.name:
            log.error(f"platform mismatch: module={mod_info.name}, "
                      f"adapter.platform={adapter.platform}, skipping")
            continue

        if getattr(adapter, "is_cloud", True):
            cloud_registry[adapter.platform] = provider
        else:
            local_registry[adapter.platform] = provider
        adapter_registry[adapter.platform] = adapter

    return cloud_registry, local_registry, adapter_registry


def get_cloud_registry() -> dict[str, type]: return _build_registries()[0]
def get_local_registry() -> dict[str, type]: return _build_registries()[1]
def get_adapter(platform: str): return _build_registries()[2].get(platform)
def get_all_adapters() -> list: return list(_build_registries()[2].values())
def get_supported_platforms() -> list[str]: return sorted(_build_registries()[2].keys())
def get_cloud_platforms() -> list[str]: return sorted(_build_registries()[0].keys())

def get_default_cloud_platform() -> str | None:
    cloud = _build_registries()[0]
    adapters = _build_registries()[2]
    if not cloud: return None
    return min(cloud.keys(), key=lambda p: getattr(adapters[p], "priority", 100))

def default_registry() -> Mapping[str, Callable]:
    """兼容包装——用 adapter.build_provider 消除 if/elif。"""
    _, _, adapter_registry = _build_registries()
    return {platform: functools.partial(adapter.build_provider)
            for platform, adapter in adapter_registry.items()}

def reload_registries() -> None:
    """清除 lru_cache，测试/开发时手动调用。"""
    _build_registries.cache_clear()
```

---

## 四、各层改造（P4-P9）

### 4.1 init_tasks/sandbox_provider_init.py（P6）

改造点 H16：删除 `from src.api.platform_registry import discover_platform_configs, extract_extra_config`

只有云平台支持配置导入。init_tasks 已有 `from datetime import datetime, timezone`。

```python
def run(self, c, db_lock) -> None:
    from src.utils.crypto import encrypt
    from src.agent.sandbox.backends import get_adapter, get_cloud_platforms
    import os, json as _json

    _migrated = []
    configs_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "configs"))
    for platform in get_cloud_platforms():
        config_path = os.path.join(configs_dir, f"{platform}-config.json")
        if not os.path.isfile(config_path): continue
        try:
            with open(config_path, "r") as f: local_cfg = _json.load(f)
        except Exception as e:
            log.warning(f"config load failed: {config_path}: {e}"); continue

        c.execute("SELECT 1 FROM sandbox_provider_config WHERE platform=? AND scope='system'", (platform,))
        if c.fetchone(): continue

        adapter = get_adapter(platform)
        if not adapter: continue
        parsed = adapter.parse_config_file(local_cfg)

        endpoint = parsed.endpoint
        api_key_enc = encrypt(parsed.api_key) if parsed.api_key else ""
        extra_json = _json.dumps(parsed.extra_config, ensure_ascii=False)

        c.execute(
            "INSERT INTO sandbox_provider_config "
            "(provider, name, platform, endpoint, api_key_enc, extra_config, enabled, scope, user_id, max_sandboxes) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 'system', 0, ?)",
            (platform, platform, platform, endpoint, api_key_enc, extra_json, parsed.max_sandboxes))
        provider_id = c.lastrowid
        _migrated.append((platform, config_path, provider_id))

        templates = parsed.templates
        _now = datetime.now(timezone.utc).isoformat()
        for t in templates:
            # ... 原有模板导入逻辑不变
        if templates:
            log.info(f"seeded {len(templates)} templates for {platform} (provider_id={provider_id})")

    # 迁移成功后删除本地配置文件——保留原有逻辑不变
    # 本次改造只换 platform_registry→adapter，不改变配置文件删除行为
    for platform, config_path, provider_id in _migrated:
        try:
            c.execute(
                "SELECT 1 FROM sandbox_provider_config WHERE platform=? AND scope='system'",
                (platform,)
            )
            if not c.fetchone():
                log.warning(f"migration cleanup: DB row for {platform} not found, keeping {config_path}")
                continue
            os.remove(config_path)
            log.info(f"migration cleanup: removed {config_path} (migrated to DB)")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"migration cleanup: failed to remove {config_path}: {e}")
```

### 4.2 cloud_worker.py（P7 + P13）

#### _materialize_ssl_cert → _prepare_artifacts（H9，L166-191）

```python
_ARTIFACT_DIR = "/var/lib/grape/cloud-worker"

def _prepare_artifacts(provider_configs: dict) -> None:
    from src.agent.sandbox.backends import get_adapter
    for _pid, _cfg in provider_configs.items():
        _platform = _cfg.get("platform", "")
        if not _platform: continue
        adapter = get_adapter(_platform)
        if adapter:
            adapter.prepare_artifacts(_cfg, _ARTIFACT_DIR, _pid)
```

调用点：L262 `_materialize_ssl_cert(configs)` → `_prepare_artifacts(configs)`；L286 同理。

#### _expand_templates 排序（H8，L98-103）

```python
from src.agent.sandbox.backends import get_adapter
def _platform_priority(platform: str) -> int:
    adapter = get_adapter(platform)
    return getattr(adapter, "priority", 100) if adapter else 999

for t in sorted(templates, key=lambda t: (
    _platform_priority(t.get("platform", "")),
    0 if t.get("scope") == "system" else 1,
    t.get("provider_id", 0),
    t.get("template_id", ""),
)):
```

### 4.3 client.py（P8，H10）

评审 2.2：删 fallback。

```python
from src.agent.sandbox.backends import get_adapter
for p in providers:
    _pid = p.get("id", 0)
    if not _pid: continue
    _platform = p.get("platform", "")
    adapter = get_adapter(_platform)
    if not adapter:
        log.error(f"no adapter for platform '{_platform}', provider {_pid} skipped")
        continue
    configs[_pid] = adapter.normalize_db_config(p)
```

### 4.4 settings.py（P7 + P8 + P9 + 补充1-2）

#### 4.4.1 删除 Pydantic model + _validate_extra_config（H1, H2）

```python
def _validate_extra_config(platform: str, extra: dict) -> dict:
    """通过 adapter 校验——必填+类型+白名单。不合法抛 ValueError→400。"""
    from src.agent.sandbox.backends import get_adapter
    adapter = get_adapter(platform)
    if adapter:
        try:
            return adapter.validate_extra_config(extra or {})
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"code": 400, "message": str(e)})
    return extra or {}
```

#### 4.4.2 update_provider_config 白名单（H3，L2022）

```python
from src.agent.sandbox.backends import get_cloud_platforms
if provider not in get_cloud_platforms():
    raise HTTPException(status_code=400, detail={"code": 400, "message": f"platform 必须为 {get_cloud_platforms()}"})
```

#### 4.4.3 _validate_platform（H4）

```python
def _validate_platform(platform: str) -> str:
    from src.agent.sandbox.backends import get_cloud_platforms
    supported = get_cloud_platforms()
    if platform not in supported:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"platform 必须为 {supported}"})
    return platform
```

#### 4.4.4 list_sandbox_platforms（H5）→ 返回 ui_schema

```python
@router.get("/sandbox-platforms", ...)
def list_sandbox_platforms(user: dict = Depends(require_no_session_token)):
    """返回云沙箱平台列表 + 每个平台的 ui_schema。本地沙箱不在此列表中。
    按 adapter.priority 排序（小的优先），前端默认选第一个。"""
    from src.agent.sandbox.backends import get_all_adapters
    cloud_adapters = sorted(
        [a for a in get_all_adapters() if getattr(a, "is_cloud", True)],
        key=lambda a: getattr(a, "priority", 100),
    )
    platforms = []
    for adapter in cloud_adapters:
        platforms.append({
            "platform": adapter.platform,
            "display_name": getattr(adapter, "display_name", adapter.platform),
            "priority": getattr(adapter, "priority", 100),
            "ui_schema": adapter.ui_schema().to_dict(),
        })
    return {"platforms": platforms}
```

#### 4.4.5 health check（H7，L2338-2361）

扩展 SQL 查全列 + 用 adapter.health_check：

```python
c.execute("SELECT scope, user_id, name, platform, endpoint, api_key_enc, "
          "extra_config, max_sandboxes "
          "FROM sandbox_provider_config WHERE id=?", (provider_id,))
row = c.fetchone()
# ... 权限校验不变 ...

# 列名映射
prov = {"scope": row[0], "user_id": row[1], "name": row[2], "platform": row[3],
        "endpoint": row[4], "api_key_enc": row[5], "extra_config": row[6],
        "max_sandboxes": row[7]}

_platform = prov["platform"]
_api_key = decrypt(prov["api_key_enc"]) if prov["api_key_enc"] else ""

from src.agent.sandbox.backends import get_adapter
adapter = get_adapter(_platform)
if not adapter:
    return {"healthy": False, "reason": f"no adapter for platform '{_platform}'"}

_cfg = adapter.normalize_db_config({
    "platform": _platform, "name": prov["name"], "endpoint": prov["endpoint"],
    "api_key": _api_key, "extra_config": prov["extra_config"],
    "max_sandboxes": prov["max_sandboxes"],
})
_healthy, _reason = adapter.health_check(_cfg)
return {"healthy": _healthy, "reason": _reason}
```

#### 4.4.6 delete_sandbox_provider kill（H6，L2265-2307）

评审 2.4 + P2-1：补全完整代码 + 列名映射 + adapter.kill_sandbox

扩展 L2265 SQL 查全列：
```python
c.execute("SELECT scope, user_id, name, platform, endpoint, api_key_enc, extra_config "
          "FROM sandbox_provider_config WHERE id=?", (provider_id,))
row = c.fetchone()
if not row:
    raise HTTPException(status_code=404, detail={"code": 404, "message": "Provider 不存在"})

prov = {"scope": row[0], "user_id": row[1], "name": row[2], "platform": row[3],
        "endpoint": row[4], "api_key_enc": row[5], "extra_config": row[6]}
_scope, _uid, _name, _platform = prov["scope"], prov["user_id"], prov["name"], prov["platform"]
# ... 权限校验不变 ...

# 查活跃沙箱（原有 SQL 不变）
c.execute("SELECT sandbox_id, issue_id, repo_key, state FROM cloud_sandboxes "
          "WHERE provider_id=? AND state IN ('creating','running')", (provider_id,))
active = c.fetchall()
# active: list of (sandbox_id, issue_id, repo_key, state)
```

kill + cleanup（用 adapter.kill_sandbox，先 kill 再 cleanup）：
```python
if not confirm:
    return {"provider_id": provider_id, "provider_name": _name,
            "running_sandboxes": [{"sandbox_id": a[0], "issue_id": a[1],
                                   "repo_key": a[2], "state": a[3]} for a in active],
            "running_sandbox_count": len(active)}

from src.agent.sandbox.backends import get_adapter
from src.utils.crypto import decrypt

_adapter = get_adapter(_platform)
_api_key = decrypt(prov["api_key_enc"]) if prov["api_key_enc"] else ""
killed = 0

if _adapter:
    _cfg = _adapter.normalize_db_config({
        "platform": _platform, "name": _name,
        "endpoint": prov["endpoint"], "api_key": _api_key,
        "extra_config": prov["extra_config"],
    })
    for sb_id, issue_id, repo_key, state in active:
        try:
            if _adapter.kill_sandbox(_cfg, sb_id):
                killed += 1
        except Exception as e:
            log.warning(f"kill sandbox {sb_id} failed: {e}")
    # 先 kill 所有活跃沙箱，再 cleanup 落盘文件
    _adapter.cleanup(provider_id, "/var/lib/grape/cloud-worker")
else:
    log.warning(f"no adapter for platform '{_platform}', skipping kill + cleanup")

# 模板数量 + 级联删除（原有逻辑不变）
```

### 4.5 manager.py（P10 + P0-1，H11 + H12）

#### L250 cubesandbox fallback

```python
from .backends import get_default_cloud_platform
_platform = _cfg.get("platform", "") or ctx.platform or get_default_cloud_platform() or ""
```

#### 三处 factory 调用点改全关键字

**L267**（云沙箱路径）：
```python
provider = factory(audit=self._audit, credential=self._credential,
                   seccomp=self._seccomp, alert_engine=self._alert_engine, config=_cfg)
```

**L289-290**（bwrap 路径）：
```python
return (factory(audit=self._audit, credential=self._credential,
                seccomp=self._seccomp, alert_engine=self._alert_engine), "")
```

**L307**（bwrap fallback in _select_provider）：
```python
return (factory(audit=self._audit, credential=self._credential,
                seccomp=self._seccomp, alert_engine=self._alert_engine), "")
```

### 4.6 repository.py（补充3，H13-H14）

评审 P0：动态拼 adapter.priority 进 SQL CASE provider WHEN...，保留 fetchone+LIMIT 1。

原码 L140-152 用 fetchone()+LIMIT 1。原 SQL 的 `CASE WHEN provider = 'cubesandbox' THEN 0 ELSE 1 END`
表达的是"平台优先级"语义。删掉后需要用 adapter.priority 动态替代。

L140-152 改造：
```python
from src.agent.sandbox.backends import get_cloud_platforms, get_adapter

# 按 priority 排序生成 CASE WHEN provider = ? THEN N
_cloud_platforms = sorted(
    get_cloud_platforms(),
    key=lambda p: getattr(get_adapter(p), "priority", 100),
)
if _cloud_platforms:
    _case_parts = " ".join(
        f"WHEN ? THEN {i}" for i, _ in enumerate(_cloud_platforms)
    )
    c.execute(
        f"SELECT template_id, provider FROM sandbox_templates "
        f"WHERE engines LIKE ? AND enabled=1 AND last_check_status='ok' "
        f"ORDER BY "
        f"  CASE provider {_case_parts} ELSE 999 END, "
        f"  CASE WHEN engines = ? THEN 0 ELSE 1 END, "
        f"  updated_at DESC LIMIT 1",
        (*_cloud_platforms, f'%"{engine_name}"%', f'[\"{engine_name}\"]'),
    )
    row = c.fetchone()
    if row:
        return row[0] or "", row[1] or ""
# 无行 → fallback 到 engine_sandbox_config（原有逻辑不变）
# 最终都查不到 → return "", ""（原码 L172，保留）
```

设计决策：
- adapter.priority 作为第一排序键（平台优先级）
- engines 匹配度作为第二排序键
- updated_at 作为第三排序键
- 保留 fetchone+LIMIT 1——不改变原码查询模式
- 不引入 Python 重排——动态拼 SQL 是最干净的方案

L158, 164 改为动态 + 空检查：
```python
from src.agent.sandbox.backends import get_cloud_platforms
_cloud_list = get_cloud_platforms()
if not _cloud_list:
    return "", ""
_in_placeholders = ",".join("?" * len(_cloud_list))
if runtime_id is not None and engine_name:
    c.execute(
        f"SELECT sandbox_provider FROM engine_sandbox_config "
        f"WHERE runtime_id=? AND engine_name=? "
        f"AND sandbox_provider IN ({_in_placeholders})",
        (runtime_id, engine_name, *_cloud_list))
else:
    c.execute(
        f"SELECT sandbox_provider FROM engine_sandbox_config "
        f"WHERE sandbox_provider IN ({_in_placeholders}) LIMIT 1",
        tuple(_cloud_list))
```

空 rows 语义：原码 L170-172 `except: pass; return "", ""`。方案保留。

### 4.7 sandbox.js（P14-P15，H17-H20）

项目用 Vue 3（vue.global.prod.min.js + createApp + Proxy）——普通赋值天然响应式，不需要 $set。

#### 前端动态渲染表单（H18）

```javascript
computed: {
  currentPlatformSchema() {
    var p = this.platforms.find(p => p.platform === this.providerForm.platform);
    return (p && p.ui_schema) ? p.ui_schema : { groups: [] };
  },
},
```

弹窗内平台专属字段区域（含 text/textarea/bool/number/select）：
```html
<div v-for="group in currentPlatformSchema.groups" :key="group.title"
     style="border-top:1px solid var(--b);padding-top:10px;margin-top:10px">
  <label v-if="group.title" style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">{{ group.title }}</label>
  <div v-for="field in group.fields" :key="field.name" style="margin-bottom:8px">
    <label style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">
      {{ field.label }}<span v-if="field.required" style="color:#dc2626">*</span>
    </label>
    <input v-if="field.type === 'text'" v-model="providerForm[field.name]"
           :placeholder="field.placeholder" autocomplete="off"
           style="width:100%;padding:8px;border:1px solid var(--b);border-radius:8px;background:var(--c);font-size:12px">
    <textarea v-else-if="field.type === 'textarea'" v-model="providerForm[field.name]"
              :placeholder="field.placeholder" autocomplete="off"
              style="width:100%;padding:8px;border:1px solid var(--b);border-radius:8px;background:var(--c);font-size:11px;height:80px;font-family:monospace">
    </textarea>
    <input v-else-if="field.type === 'bool'" type="checkbox" v-model="providerForm[field.name]">
    <input v-else-if="field.type === 'number'" v-model.number="providerForm[field.name]" type="number"
           style="width:100%;padding:8px;border:1px solid var(--b);border-radius:8px;background:var(--c);font-size:12px">
    <select v-else-if="field.type === 'select'" v-model="providerForm[field.name]"
            style="width:100%;padding:8px;border:1px solid var(--b);border-radius:8px;background:var(--c);font-size:12px">
      <option v-for="opt in field.options" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
    </select>
  </div>
</div>
```

#### saveProvider 动态组装 + 前端必填校验（H17）

```javascript
async saveProvider() {
    // 前端必填校验
    var schema = this.currentPlatformSchema;
    for (var gi = 0; gi < (schema.groups || []).length; gi++) {
        for (var fi = 0; fi < (schema.groups[gi].fields || []).length; fi++) {
            var f = schema.groups[gi].fields[fi];
            if (f.required && (!this.providerForm[f.name] || this.providerForm[f.name] === '')) {
                alert((this.isZh ? '必填项不能为空：' : 'Required: ') + (f.label || f.name));
                return;
            }
        }
    }
    // 动态组装 extra_config
    var extra = {};
    (schema.groups || []).forEach(g => {
        (g.fields || []).forEach(f => {
            if (this.providerForm[f.name] !== undefined && this.providerForm[f.name] !== '') {
                extra[f.name] = this.providerForm[f.name];
            }
        });
    });
    // ... 构建 body + API 调用（原有逻辑不变）
```

#### _defaultProviderForm + 字段初始化（H20）

```javascript
_defaultProviderForm() {
    return { name: '', platform: '', endpoint: '', api_key: '', max_sandboxes: 10, enabled: true, scope_level: 'user' };
},
_initPlatformFields(platform) {
    // 清除旧平台的动态字段（保留通用字段）
    var commonKeys = new Set(['name', 'platform', 'endpoint', 'api_key', 'max_sandboxes', 'enabled', 'scope_level']);
    // 先收集所有平台的 ui_schema 字段名
    var allUiFields = new Set();
    (this.platforms || []).forEach(p => {
        if (p.ui_schema) {
            (p.ui_schema.groups || []).forEach(g => {
                (g.fields || []).forEach(f => allUiFields.add(f.name));
            });
        }
    });
    Object.keys(this.providerForm).forEach(k => {
        if (!commonKeys.has(k) && allUiFields.has(k)) {
            delete this.providerForm[k];
        }
    });
    // 初始化新平台的字段默认值
    var p = this.platforms.find(p => p.platform === platform);
    if (!p || !p.ui_schema) return;
    (p.ui_schema.groups || []).forEach(g => {
        (g.fields || []).forEach(f => {
            if (this.providerForm[f.name] === undefined) {
                this.providerForm[f.name] = f.default !== undefined ? f.default : '';
            }
        });
    });
},
openCreateProvider() {
    this.editingProvider = null;
    this.providerForm = this._defaultProviderForm();
    // 默认选第一个可用平台
    if (this.platforms.length > 0) {
        this.providerForm.platform = this.platforms[0].platform;
    }
    this._initPlatformFields(this.providerForm.platform);
    this.showProviderDialog = true;
},
openEditProvider(p) {
    this.editingProvider = p;
    var extra = p.extra_config || {};
    this.providerForm = {
        name: p.name, platform: p.platform, endpoint: p.endpoint, api_key: '',
        max_sandboxes: p.max_sandboxes || 10, enabled: p.enabled,
        scope_level: (p.scope === 'user' && p.repo_key) ? 'repo' : 'user',
    };
    // api_key 设空——后端"空=不更新，保留旧值"
    this._initPlatformFields(p.platform);
    if (extra) {
        for (var k in extra) {
            if (this.providerForm[k] !== undefined) {
                this.providerForm[k] = extra[k];
            }
        }
    }
    this.showProviderDialog = true;
},
watch: {
    'providerForm.platform'(newVal) { this._initPlatformFields(newVal); },
},
```

#### 详情页动态展示（H19）

```html
<template v-for="group in getProviderSchema(selectedProvider.platform).groups">
  <div v-for="field in group.fields" :key="field.name">
    <span style="color:var(--t3)">{{ field.label }}:</span>
    {{ formatExtra(selectedProvider, field) }}
  </div>
</template>
```

```javascript
getProviderSchema(platform) {
    var p = this.platforms.find(p => p.platform === platform);
    return (p && p.ui_schema) ? p.ui_schema : { groups: [] };
},
formatExtra(provider, field) {
    var extra = provider.extra_config || {};
    var val = extra[field.name];
    if (field.type === 'bool') return val ? '✅' : '❌';
    return val || '—';
},
```

---

## 五、删除旧代码（P10-P12 + 补充4）

### 5.1 platform_registry.py 整个文件删除（H15）
### 5.2 db_setup.py 死代码删除（H21, H22）
- L2138 _migrate_provider_configs_to_db()
- L4343 _seed_sandbox_templates()

---

## 六、测试影响与迁移

搜索旧测试：
- `from src.api.platform_registry import` → 改为 `from src.agent.sandbox.backends import`
- `mock extract_extra_config` → 改为 mock `adapter.validate_extra_config`
- `from src.api.routes.settings import _PLATFORM_EXTRA_MODELS` → 删除
- `from src.api.routes.settings import CubeSandboxExtraConfig` → 删除

---

## 七、新增平台端到端验证

文件名用 `mockplatform.py`（不以 _ 开头，确保被 pkgutil 扫描到；用 mock 前缀避免 pytest 命名冲突）。验证后删除。

1. 创建 `configs/mockplatform-config.json` + `src/agent/sandbox/backends/mockplatform.py`
2. 启动服务验证：
   - /sandbox-platforms 返回 mockplatform + ui_schema
   - 前端下拉框出现 Mock Platform
   - 选 Mock Platform → 显示 Region 输入框（required 红星）
   - 保存（不填 region）→ 前端拦截 + 后端 400
   - 保存（填 region）→ extra_config 含 region
   - DB 有 mockplatform 行
   - health_check → (True, "")
   - kill_sandbox → True
3. 验证通过后删除文件

mockplatform.py 的 validate_extra_config 必须和 cubesandbox 模板一致（含类型检查）。

---

## 八、验证计划

### 8.1 启动验证
1. 启动主控服务
2. /tmp/grape-server.log 无 import 错误
3. GET /api/settings/sandbox-platforms 返回 ui_schema 格式
4. GET /api/settings/sandbox-providers 返回正常

### 8.2 功能验证
1. 平台下拉框显示 cubesandbox + devbox
2. 选 cubesandbox → SSL textarea；选 devbox → 网关字段
3. 保存（缺必填）→ 前端拦截 + 后端 400
4. 保存（完整）→ extra_config 动态组装
5. 健康检查 → adapter.health_check 返回 (healthy, reason)
6. 删除 Provider → 先 kill 沙箱，再 adapter.cleanup

### 8.3 新增平台验证
按 §七 步骤

### 8.4 E2E 验证
1. 配置文件导入 → DB → cloud_worker → Provider 创建
2. cloud_worker → _prepare_artifacts 落盘 SSL 证书（0o600）
3. A2A 链路

---

## 九、实施顺序

1. P1: 更新 backends/api.py
2. P2: 四个 Adapter 追加
3. P3: backends/__init__.py 重写
4. P6: init_tasks 改 adapter
5. P7: cloud_worker _prepare_artifacts + _expand_templates
6. P8: client.py normalize_db_config
7. P9: settings.py _validate_extra_config + _validate_platform + list_sandbox_platforms
8. P8b: settings.py health check（adapter.health_check）+ delete kill（adapter.kill_sandbox）
9. P10: manager.py fallback + 三处 factory 全关键字
10. P13: repository.py 排序（动态拼 adapter.priority 进 SQL + fetchone+LIMIT 1 + _cloud_list 空检查）
11. P14-15: sandbox.js 前端动态渲染
12. P17: 删除 platform_registry.py
13. 补充4: 删除 db_setup.py 死代码
14. 新增平台验证（mockplatform）
15. E2E 验证
