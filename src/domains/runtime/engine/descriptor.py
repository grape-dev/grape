"""引擎 descriptor 单源注册表（P0b B1/B2，2026-11-13 spec）。

根因：引擎元数据散布 6 处 + 两套注册表（executor._BACKENDS 8 vs
engine_strategies.ENGINE_REGISTRY 5）靠注释对齐——漏改任一处即漂移。
D-2 拍板：descriptor 一期随 worker 包内置（网络下发属 P2 远期）。

用法：
- get_registry()：进程内单例，加载 src/domains/runtime/engine/descriptors/*.json
- reg.get(name)：canonical 或 alias 精确查找（不做前缀匹配——前缀最长匹配
  语义复杂、保留在 instances.canonical_engine，其映射表由 descriptor 派生对账）
- reg.prefix_map()：派生 {前缀: canonical}（instances.ENGINE_CANONICAL_PREFIXES
  的单源数据；drift CI 比对）

字段契约（spec B1 首版六字段；新字段须有对应散布点证据才准入——spec §7 红线）：
- canonical: 规范引擎键（文件名同）
- aliases: 别名精确键（如 aishell → hwcloud）
- protocol: acp | ndjson | uhp | ghost（cursor 仅 known 探测不可执行）
- discover: {cli, pip}（executor.discover_backends 消费）
- display: {name, dot_color}（settings 展示名 + 前端徽标颜色）
- probe: {mode, command}（engine_probe 消费；acp_handshake=真实 ACP 握手探活）
- usage: {mode}（usage_collect 账本分派消费：none/event_stream/hermes_cumulative/
  hwcloud_offset/opencode_watermark——读账本的 quirk 实现留代码，分派值域在此）
- model_config_path: engine_model 配置探测路径
- usage_retry / device_known / capabilities.sandbox_installable: 各散布点布尔位
"""
from __future__ import annotations

import json
import os
import threading

_DESCRIPTOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "descriptors")

_PROTOCOLS = ("acp", "ndjson", "uhp", "ghost")
_PROBE_MODES = ("cli_run", "acp_handshake", "none")
_USAGE_MODES = ("none", "event_stream", "hermes_cumulative",
                "hwcloud_offset", "opencode_watermark",
                "aishell_dsh_watermark")  # arch 基线二期解析轨（M1 记录水位），融合保留

_singleton: "EngineDescriptorRegistry | None" = None


class DescriptorValidationError(ValueError):
    """descriptor JSON schema 校验失败（携带文件路径，可定位）。"""
_singleton_lock = threading.Lock()


class EngineDescriptorRegistry:
    """进程内 descriptor 注册表：目录加载 + canonical/alias 查找 + 值域派生。"""

    def __init__(self, directory: str = _DESCRIPTOR_DIR):
        self._dir = directory
        self._by_canonical: dict[str, dict] = {}
        self._by_alias: dict[str, str] = {}
        self._load()

    # ── 加载与校验 ──────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.isdir(self._dir):
            return
        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._dir, fname)
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            canonical = d.get("canonical") or fname[:-5]
            if canonical != fname[:-5]:
                raise DescriptorValidationError(
                    f"{path}: descriptor 文件名须等于 canonical（got {canonical!r}）")
            self._validate(d, path)
            self._by_canonical[canonical] = d
            for alias in d.get("aliases", []):
                self._by_alias[alias] = canonical

    @staticmethod
    def _validate(d: dict, path: str) -> None:
        """schema 校验（L0 段①：纯文件，CI 恒跑）。

        显式异常（非 assert）：`python -O` 下 assert 整体失效，且裸
        AssertionError 无文件上下文——校验必须带 descriptor 文件路径可定位。
        """
        def _err(msg: str) -> None:
            raise DescriptorValidationError(f"{path}: {msg}")

        if d.get("protocol") not in _PROTOCOLS:
            _err(f"protocol 值域: {d.get('protocol')}")
        if "cli" not in d.get("discover", {}):
            _err("discover.cli 必备")
        if "name" not in d.get("display", {}):
            _err("display.name 必备")
        probe = d.get("probe", {})
        if probe.get("mode") not in _PROBE_MODES:
            _err(f"probe.mode 值域: {probe.get('mode')}")
        if "command" not in probe:
            _err("probe.command 必备")
        # [B5 两档齐备] 深探实现与浅探参数齐备性：防「深探有配置浅探没有」
        # （或反之）的半接入。
        if probe.get("mode") == "acp_handshake" and not probe.get("impl"):
            _err("probe.mode=acp_handshake 须有 probe.impl（分派器实现 token）")
        if probe.get("light_args") is not None:
            if not (isinstance(probe["light_args"], list) and probe["light_args"]):
                _err("probe.light_args 须为非空列表")
        if not isinstance(d.get("capabilities", {}).get("sandbox_installable"), bool):
            _err("capabilities.sandbox_installable 必备布尔")
        if d.get("usage", {}).get("mode") not in _USAGE_MODES:
            _err(f"usage.mode 值域: {d.get('usage', {}).get('mode')}")

    # ── 查询 ────────────────────────────────────────────────────

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def get(self, name: str | None) -> dict | None:
        """canonical/alias 精确查找。未注册返回 None（不猜、不 fallback）。"""
        if not name:
            return None
        if name in self._by_canonical:
            return self._by_canonical[name]
        canonical = self._by_alias.get(name)
        return self._by_canonical.get(canonical) if canonical else None

    def all(self) -> dict[str, dict]:
        return dict(self._by_canonical)

    def count(self) -> int:
        return len(self._by_canonical)

    def prefix_map(self) -> dict[str, str]:
        """派生 instances.ENGINE_CANONICAL_PREFIXES 同构映射（drift 对账数据源）。

        键 = canonical + aliases 全集；值 = canonical（alias 映射回所属引擎）。
        排除：is_default 引擎（canonical_engine 的未知名 fallback，不在翻译表——
        opencode.json is_default=true，证据 instances.canonical_engine 兜底返回值）
        与 ghost 引擎（cursor 仅 known 探测，不进实例名翻译链）。
        最长前缀匹配语义由消费方 instances.canonical_engine 保留。
        """
        m = {}
        for canonical, d in self._by_canonical.items():
            if d.get("is_default") or d.get("protocol") == "ghost":
                continue
            m[canonical] = canonical
            for alias in d.get("aliases", []):
                m[alias] = canonical
        return m

    def discover_map(self) -> dict[str, dict]:
        """派生 executor.discover_backends 的 known 表（drift 对账数据源）。

        discover.capability 透传（aishell-dsh="acp"）：executor 借此做 CLI 同名
        防误识别（issue #865 假 aishell CLI 判 installed=False），漏传=守卫失效。
        """
        out: dict[str, dict] = {}
        for c, d in self._by_canonical.items():
            m = {"cli": d["discover"]["cli"], "pip": d["discover"].get("pip", "")}
            if d["discover"].get("capability"):
                m["capability"] = d["discover"]["capability"]
            out[c] = m
        return out

    def probe_commands(self) -> dict[str, list[str]]:
        """派生 cli_run 探活命令表（probe_dispatch 消费；仅 mode=cli_run 的引擎）。"""
        return {c: list(d["probe"]["command"])
                for c, d in self._by_canonical.items() if d["probe"]["mode"] == "cli_run"}

    def light_probe_args(self, name: str | None) -> list[str]:
        """浅探参数（B5 两档保留裁定）：descriptor probe.light_args，默认 ["--version"]。

        hermes 覆盖 ["acp", "--check"]（--version 探不出 ACP 依赖缺失，见
        hermes-acp-auto-bootstrap-design.md）——原 local_probe.py 的手抄覆盖
        收进此处。
        """
        d = self.get(name)
        if d is None:
            return ["--version"]
        return list(d.get("probe", {}).get("light_args") or ["--version"])

    def probe_local_acp_deep_check(self, name: str | None) -> bool:
        """本机探活深检位（F1 恢复，PR #2047 复审）：probe.local_acp_deep_check。

        true = 浅探（light_probe_args）通过后追加深检——真实 ACP 握手
        （_acp_check）+ 模型调用探针（Issue #1982 R1 / #2545）。
        与 sandbox.acp_deep_check（建箱层深检位）正交：一个管本机探活，
        一个管云沙箱建箱。hermes 是当前唯一 true 引擎。
        """
        d = self.get(name)
        if d is None:
            return False
        return bool(d.get("probe", {}).get("local_acp_deep_check"))

    def usage_mode(self, name: str | None) -> str:
        """账本分派模式（B5 收敛）：descriptor usage.mode，alias 归一后取值
        （aishell → hwcloud 同走 hwcloud_offset，原换名分支语义保留）。
        未注册引擎返回 "none"（不猜、不 fallback 到任何账本）。
        """
        d = self.get(name)
        if d is None:
            return "none"
        return d.get("usage", {}).get("mode") or "none"

    def sandbox_probe_engines(self) -> list[str]:
        """沙箱建箱层探测白名单（B5 收敛）：sandbox_installable 引擎的箱内 CLI 名。

        名字取 sandbox.probe_cli（缺省 = canonical）；hwcloud 箱内二进制名是
        其别名 aishell（散布点证据=cubesandbox 原手抄元组），经字段显式声明。
        按 canonical 排序保证确定性。探测顺序语义与原元组不同（列表展示顺序
        级差异），探测集合/名字严格一致。
        """
        out = []
        for canonical in sorted(self._by_canonical):
            d = self._by_canonical[canonical]
            if not d.get("capabilities", {}).get("sandbox_installable"):
                continue
            out.append(d.get("sandbox", {}).get("probe_cli") or canonical)
        return out

    def sandbox_engine_names(self) -> frozenset:
        """引擎名平台解析白名单（B5 收敛，settings 消费）：sandbox_installable
        引擎的箱内 CLI 名集合（hwcloud=别名 aishell，散布点证据=settings
        原手抄元组）。与 sandbox_probe_engines 同源。"""
        return frozenset(self.sandbox_probe_engines())

    def im_session_valid_names(self) -> frozenset:
        """IM 会话续接能力位派生（B5 收敛，handler_async 消费）：
        capabilities.im_session_valid 真值引擎（opencode/hermes/aishell-dsh
        ——aishell-dsh 二期起支持 session/resume；
        codearts 忽略 session_id 的既有语义）。"""
        return frozenset(
            c for c, d in self._by_canonical.items()
            if d.get("capabilities", {}).get("im_session_valid")
        )

    def page_type(self, name: str | None) -> str:
        """canonical → settings 页面类型标签（B3 点 7 收敛）。

        值域归 descriptor `display.page_type`：hwcloud 特例 = "aishell"
        （历史展示名，与前端 _availTypes 一致）；缺省 = canonical。
        消费方：settings `_engine_type_of`、`_device_engine_types`。
        未注册名返回 ""（不猜不 fallback）。
        """
        d = self.get(name)
        if d is None:
            return ""
        disp = d.get("display", {})
        return disp.get("page_type") or (name or "")

    def device_type_engines(self) -> list[str]:
        """设备视图引擎类型清单（原 `_device_engine_types` 手抄 5 家元组的
        descriptor 派生；canonical 排序保证确定性）。"""
        return sorted(
            c for c, d in self._by_canonical.items()
            if d.get("display", {}).get("device_types")
        )

    def sandbox_acp_deep_check(self, name: str | None) -> bool:
        """箱内 ACP 深检位（B5 收敛）：仅真值引擎做建箱层 ACP 握手深检
        （hermes：--version 探不出可选 extra 缺失，2026-11-06 修复口径）。
        hwcloud 同为 acp_handshake 但不做箱内深检——mode 不足以分派，独立位。
        alias 归一后判定。
        """
        d = self.get(name)
        return bool(d and d.get("sandbox", {}).get("acp_deep_check"))


def get_registry() -> EngineDescriptorRegistry:
    """进程内单例（D-2：随包内置目录，无网络下发）。"""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = EngineDescriptorRegistry()
        return _singleton
