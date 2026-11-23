"""平台卡来源策略与装配器（Issue #3632）。

为什么要有这一层
---------------
3 库 4 进程拓扑下，"某个 provider 的卡从哪来"取决于**进程角色**，而不是
取决于代码想读哪：

======================  ==========================================
进程                   可用的卡来源
======================  ==========================================
mgmt                    meta / 文件卡 / 代码默认 / 表（自有 DSN）
event、worker（主进程）  meta（经 mgmt 契约或 DSN）/ 文件卡 / 代码默认 / 表
worker 引擎（任务进程）  代码默认 / 文件卡（若随包下发）/ **主控契约**
======================  ==========================================

原先把"读哪"写死在 `_build_cards()` 里，任一路径在某进程不可用就静默退成
"这个平台没有卡"——gitee 在 worker 引擎上三条来源全断，worktree 拿不到
clone 地址后降级只读，任务"完成"却没有仓库（#3632）。

这里把每个来源建模成 `PlatformCardSource`（策略对象），由
`PlatformCardAssembler` 按 D109/S83 的优先级链装配：

    meta（逐键覆盖） > configs/platforms/*.json > 代码内置默认 > 表 / 契约

表源与契约源互为替补：能读 mgmt 库就用表（真源），读不到（拆库进程/引擎
进程）就用契约；两者都不可得才返回空——且**每一种不可用都要留日志**。
来源对象只负责"取一组 raw"，缓存/TTL/失效留在门面 `platform_card`，因此
本模块可以独立测试，不需要 DB 也不需要 HTTP。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Callable, Iterable

from src.kernel.config import log
from src.domains.runtime.vcs.card_contract import (
    PlatformCardContractClient,
    PlatformCardContractError,
    PlatformCardUnavailableError,
)


class PlatformCardSource(ABC):
    """一个平台卡来源（策略对象）：返回 {provider: 卡字段 raw}。"""

    #: 日志与诊断用的来源名（"table" / "server contract" / "file" ...）
    name: str = "unknown"

    @abstractmethod
    def raws(self) -> "dict[str, dict]":
        """返回本来源可见的卡 raw；不可用/失败时必须返回 {} 并留日志。"""

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"<{type(self).__name__} name={self.name}>"


class MetaOverlaySource(PlatformCardSource):
    """admin 运行时覆盖（meta `vcs_providers_config`）。

    语义是**逐键覆盖**（D105-19），不是整卡替换——合并动作在装配器里做，
    因为"覆盖到哪一层基底"是装配顺序的知识，不属于来源本身。
    """

    name = "meta"

    def __init__(self, read_text: Callable[[], str]):
        self._read_text = read_text

    def raws(self) -> "dict[str, dict]":
        try:
            raw_cfg = self._read_text() or ""
        except Exception as e:  # noqa: BLE001 - DB 未就绪（测试/启动早期）
            log.debug(f"platform_card: read meta failed ({e}), using defaults")
            return {}
        if not raw_cfg:
            return {}
        try:
            data = json.loads(raw_cfg)
        except Exception as e:  # noqa: BLE001
            log.warning(f"platform_card: vcs_providers_config 解析失败: {e}")
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(p): dict(raw or {}) for p, raw in data.items()
                if isinstance(raw, dict)}


class FileDirCardSource(PlatformCardSource):
    """`configs/platforms/*.json`（新平台"纯配置接入"的内置载体，S83/D59）。

    目录缺失属**预期状态**（worker 包历史上不含 configs），因此降级为代码默认；
    但必须 WARNING：这正是"为什么这台机器上没有 gitee"的第一手证据。
    """

    name = "file"

    def __init__(self, dir_path: str, read_raws: Callable[[str], "dict[str, dict]"],
                 isdir: Callable[[str], bool] | None = None):
        import os as _os

        self.dir_path = dir_path
        self._read_raws = read_raws
        self._isdir = isdir or _os.path.isdir

    def available(self) -> bool:
        return bool(self._isdir(self.dir_path))

    def raws(self) -> "dict[str, dict]":
        if not self.available():
            log.warning(f"platform_card: 配置目录缺失 {self.dir_path}，回退代码内置默认")
            return {}
        try:
            return self._read_raws(self.dir_path)
        except Exception as e:  # noqa: BLE001
            log.error(f"platform_card: 读文件卡失败 {self.dir_path}: {e}")
            return {}


class BuiltinCardSource(PlatformCardSource):
    """代码内置默认卡（目录缺失时的最后兜底）。"""

    name = "builtin"

    def __init__(self, defaults: "dict[str, dict]"):
        self._defaults = defaults

    def raws(self) -> "dict[str, dict]":
        return {p: dict(raw) for p, raw in self._defaults.items()}


class OwnerTableCardSource(PlatformCardSource):
    """owner 域表 `grape_t_repo_platforms`（D109 运行时真源）。

    Issue #3632 第 1/2 层修复点：判据从"本进程库有没有表"收口到 owner 域——
      * `ready()` 为假 = 本进程读不到 owner 库（拆库进程无 mgmt DSN）或导入
        未完成；此时**不再静默**，留 debug 说明原因，由装配器换契约源；
      * `ready()` 为真 = 走 mgmt public 原语（lib 命名查询，不 ORM 直读）。

    注意 `ready()` 在拆库引擎进程里恒假是"设计如此"，不是偶发——所以
    这里只留 debug（每次缓存重建一条），真正的证据由契约源的成功/失败日志给。
    """

    name = "table"

    def __init__(self, read_rows: Callable[..., "list[dict]"] | None = None,
                 ready: Callable[[], bool] | None = None,
                 has_owner_dsn: Callable[[], bool] | None = None,
                 skip_columns: Iterable[str] = ()):
        self._read_rows = read_rows
        self._ready = ready
        self._has_owner_dsn = has_owner_dsn
        # 列白名单由契约/owner 原语保证，这里只剥"非卡字段"（id/时间戳/来源），
        # 不维护第二份列清单——两份清单必然漂移（硬约束：不给表再加投影）。
        self._skip = set(skip_columns)

    # 默认读写一律**调用期**解析：测试对 importer.is_table_backed /
    # mgmt_public.repo_platform_cards 的 monkeypatch 才能生效（D109 契约）。
    def _rows(self, enabled_only: bool) -> "list[dict]":
        if self._read_rows is not None:
            return self._read_rows(enabled_only=enabled_only)
        # 跨域一律走契约（3 库 4 进程）：矩阵恒 lib 时 `get_service("mgmt")` 返回
        # MgmtServiceLocal（在本进程执行 mgmt 域代码、用 mgmt 自有 DSN），
        # 将来解禁远程调用时自动切 Remote——调用方无需感知拓扑。
        from src.kernel.gsb.provider import get_service  # noqa: PLC0415

        return get_service("mgmt").get_repo_platform_cards(enabled_only=enabled_only)

    def _ready_state(self) -> bool:
        if self._ready is not None:
            return bool(self._ready())
        from src.domains.platform.public import is_table_backed  # noqa: PLC0415

        return bool(is_table_backed())

    def _owner_dsn_state(self) -> str:
        probe = self._has_owner_dsn
        if probe is None:
            try:
                from src.kernel import db_registry  # noqa: PLC0415

                probe = lambda: db_registry.uses_own_dsn("mgmt")  # noqa: E731
            except Exception:  # noqa: BLE001
                return "unknown"
        try:
            return "yes" if probe() else "no"
        except Exception:  # noqa: BLE001
            return "unknown"

    def raws(self) -> "dict[str, dict]":
        try:
            if not self._ready_state():
                log.debug("platform_card: 平台对象表未就绪（is_table_backed=False，"
                          f"owner_dsn={self._owner_dsn_state()}），跳过本地表读卡；"
                          "契约接口/文件卡/代码默认仍生效")
                return {}
            rows = self._rows(enabled_only=True) or []
        except Exception as e:  # noqa: BLE001
            log.warning(f"repo_platforms table read failed, fallback to meta+file: {e}")
            return {}
        out: dict[str, dict] = {}
        for row in rows:
            provider = str((row or {}).get("provider") or "")
            if not provider:
                continue
            # Issue #3580（空列投毒）：跨域读原语把 NULL 列答成 ""，照抄进 raw 会让
            # Builder 把可选枚举列（branch_create_form / issue_write_form /
            # pr_head_filter …）当成"显式给了非法值"整卡拒绝，最终回退文件卡——
            # 表行加了也不生效。语义对齐配置文件："键缺席 = 未声明"，
            # 因此**省略空列**，但保留 0 / False 这类有效假值。
            out[provider] = {
                k: v for k, v in row.items()
                if k not in self._skip and v is not None and v != ""
            }
        return out


class ServerContractCardSource(PlatformCardSource):
    """主控只读契约（`GET /api/runtime/platform-cards`，RUNTIME_TOKEN）。

    worker 引擎进程的唯一取卡通道。失败必须 WARNING 后回落——fail-open
    但不许静默：这条日志就是"平台未注册"与"取卡系统坏了"的分界线。
    """

    name = "server contract"

    def __init__(self, client: PlatformCardContractClient | None = None):
        self._client = client or PlatformCardContractClient()
        #: 本次 raws() 的降级原因（"" = 正常）。装配层据此把「取卡通道坏了」
        #: 从「平台未注册」里分出来（见 card_contract.PlatformCardUnavailableError）。
        self.degraded = ""

    def raws(self) -> "dict[str, dict]":
        self.degraded = ""
        if not self._client.configured():
            # 非 worker 进程（无 RUNTIME_TOKEN）：正常路径，不打日志，也不算降级
            return {}
        try:
            cards = self._client.fetch_cards()
        except PlatformCardUnavailableError as e:
            self.degraded = "contract_unavailable"
            log.warning(f"platform_card: 服务端平台卡契约**通道不可用**（{e}）"
                        "——回落文件卡/代码默认；任务侧会看到 code="
                        "platform_card_contract_unavailable（不是「平台未注册」）")
            return {}
        except PlatformCardContractError as e:
            self.degraded = "contract_error"
            log.warning(f"platform_card: 服务端平台卡契约取卡失败（{e}）"
                        "——回落文件卡/代码默认")
            return {}
        if not cards:
            # 服务端确实没返回任何卡（表里 0 行 enabled）：属配置问题，不算通道故障
            self.degraded = "contract_empty"
        return cards


class PlatformCardAssembler:
    """按优先级链把多个来源装配成 {provider: raw}，再交给 Builder 建卡。

    优先级（S83/D59 + D109，Issue #3632 未改）：
        表 / 契约（整卡覆盖） > meta（逐键覆盖） > 文件卡 > 代码默认

    注意**次序与直觉相反**：表/契约是 D109 的运行时真源，行存在时整卡覆盖
    meta/文件合并结果（文件独有平台仍保留）；meta 只是对"基底（文件或代码
    默认）"逐键覆盖，不是最终层。原地实现在 `assemble_raws()` 里依次：
    base(file|builtin) → meta 逐键 merge → 表/契约整卡覆盖。

    文件卡与代码默认二选一：目录在就用文件卡（每平台一个文件的真载体），
    目录缺失才用代码兜底——与改前行为逐字一致。
    """

    def __init__(self, meta: MetaOverlaySource,
                 file_source: FileDirCardSource,
                 builtin: BuiltinCardSource,
                 fallback_sources: "Iterable[PlatformCardSource]"):
        #: 本次装配的降级原因（"" = 所有来源都正常）。门面把它挂到卡缓存上，
        #: clone 端据此区分「取卡通道坏了」与「平台未注册」。
        self.last_degraded = ""
        self.meta = meta
        self.file_source = file_source
        self.builtin = builtin
        self.fallback_sources = tuple(fallback_sources)

    # ---- 1) raw 装配 --------------------------------------------------------

    def assemble_raws(self) -> "dict[str, dict]":
        if self.file_source.available():
            base = dict(self.file_source.raws())
        else:
            # 目录缺失的 WARNING 由 FileDirCardSource.raws() 统一打
            self.file_source.raws()
            base = self.builtin.raws()

        # meta：逐键覆盖（只覆盖它声明的键，缺席键落到基底卡值）
        for provider, raw in self.meta.raws().items():
            merged = dict(base.get(provider) or {})
            merged.update(raw)
            base[provider] = merged

        # 表 / 契约：整卡覆盖已合并结果，文件独有平台仍保留（防表缺行致平台消失）
        self.last_degraded = ""
        for source in self.fallback_sources:
            raws = source.raws()
            _deg = str(getattr(source, "degraded", "") or "")
            if _deg and _deg != "contract_empty" and not self.last_degraded:
                self.last_degraded = _deg
            if not raws:
                continue
            for provider, raw in raws.items():
                base[provider] = dict(raw)
            log.info(f"platform cards from {source.name}: {sorted(raws)}")
            break
        return base

    # ---- 2) 建卡（含非法回退） ----------------------------------------------

    def build(self, builder: Callable[[str, dict], object],
              error_type: "type[Exception]") -> dict:
        cards = {}
        for provider, raw in self.assemble_raws().items():
            try:
                cards[provider] = builder(provider, raw)
                continue
            except error_type as e:
                # meta 合并产物非法：退回纯文件卡（meta 覆盖被拒），不静默丢平台
                fallback_raw = (self.file_source.raws().get(provider)
                                if self.file_source.available() else None)
                if fallback_raw is not None:
                    log.warning(f"platform_card: {provider} meta 合并卡非法（{e}），回退文件卡")
                    try:
                        cards[provider] = builder(provider, fallback_raw)
                        continue
                    except error_type as e2:  # noqa: BLE001
                        e = e2
                log.error(f"platform_card: {provider} 卡构建失败: {e}（整卡拒绝）")
        return cards

def default_assembler(config_dir: str,
                      read_config_dir: Callable[[str], "dict[str, dict]"],
                      defaults: "dict[str, dict]",
                      *,
                      read_meta: Callable[[], str] | None = None,
                      table_source: PlatformCardSource | None = None,
                      server_source: PlatformCardSource | None = None,
                      ) -> PlatformCardAssembler:
    """按部署实况装配来源链（门面 `platform_card` 的唯一入口）。

    Args:
        config_dir:       `configs/platforms` 目录（随包下发 → worker 上也有）。
        read_config_dir:  目录读取器（platform_card._builtin_raws_from_dir，
                          负责键集守卫与整卡拒绝）。
        defaults:         代码内置默认卡。
        read_meta:        meta 读取器（缺省走 mgmt public 原语）。
        table_source:     owner 库表来源（缺省自有列清单由调用方给）。
        server_source:    主控契约来源。

    这里用**工厂函数**而不是在门面里写装配分支：加来源（例如将来走 GSB 服务
    发现）只改本函数，`_build_cards` 保持不变。
    """
    meta = MetaOverlaySource(read_meta or _read_meta_via_mgmt)
    file_source = FileDirCardSource(config_dir, read_config_dir)
    builtin = BuiltinCardSource(defaults)
    fallbacks = [src for src in (table_source, server_source) if src is not None]
    return PlatformCardAssembler(meta=meta, file_source=file_source,
                                 builtin=builtin, fallback_sources=fallbacks)


def _read_meta_via_mgmt() -> str:
    """meta `vcs_providers_config`（跨域走 owner 域 public 原语，不裸 SELECT）。"""
    from src.domains.mgmt import public as _mgmt_public  # noqa: PLC0415

    return _mgmt_public.get_meta_value("vcs_providers_config") or ""
