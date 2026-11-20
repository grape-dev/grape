"""worker/engine → 主控 的平台卡契约客户端（Issue #3632）。

3 库 4 进程下 worker 引擎进程的启动环境只有 `RUNTIME_TOKEN` +
`RUNTIME_SERVER_URL`（没有任何 `GRAPE_DSN_*`），因此它读不到
`grape_t_repo_platforms`（mgmt 域真源）。平台卡只能经主控的只读契约取回：

    GET {RUNTIME_SERVER_URL}/api/runtime/platform-cards
    Authorization: Bearer <RUNTIME_TOKEN>
    → {"cards": {provider: {卡字段...}}}

本模块只做"一次契约调用"这一件事（地址/令牌/超时/响应整形），
不缓存、不回落、不打业务日志——回落策略属于 `card_sources` 的来源对象，
缓存/TTL 属于门面（`platform_card`）。这样契约通道可以被独立替换
（例如将来改走 GSB 服务发现）而不动取卡链。
"""
from __future__ import annotations

import json
import os
import urllib.request

from src.kernel.config import log

#: 单次取卡超时的**默认值**（秒）。来源优先级：
#:   显式构造参数 > 环境变量 `GRAPE_PLATFORM_CARD_TIMEOUT` > 本常量。
#: 依据：取卡位于 worktree 准备的关键路径上（拿不到卡就没有 clone 地址），
#: 主控与 worker 同可用区内网往返实测 <100ms，2.0s ≈ 20× 余量；再放大只会拖住
#: 任务准备（失败也要等满超时），再缩小会在主控 GC / 冷启动时误判"不可达"而
#: 白白回落文件卡。可用环境变量按部署调整，不必改代码发版。
_DEFAULT_CARD_TIMEOUT = 2.0
_TIMEOUT_ENV = "GRAPE_PLATFORM_CARD_TIMEOUT"
_TIMEOUT_MAX = 30.0


def resolve_card_timeout(explicit: float | None = None) -> float:
    """解析单次取卡超时（秒）：显式值 > `GRAPE_PLATFORM_CARD_TIMEOUT` > 默认。

    解析一律**带守卫**：非数字 / ≤0 / >30s 都回落默认并告警——裸 float() 抛异常
    会被调用点的 try/except 吞成"取卡失败"，等于把配置错误伪装成网络故障。
    """
    if explicit is not None:
        return float(explicit)
    raw = (os.environ.get(_TIMEOUT_ENV) or "").strip()
    if not raw:
        return _DEFAULT_CARD_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        log.warning(f"platform_card: {_TIMEOUT_ENV}={raw!r} 非数字，回落默认 "
                    f"{_DEFAULT_CARD_TIMEOUT}s")
        return _DEFAULT_CARD_TIMEOUT
    if not (0 < value <= _TIMEOUT_MAX):
        log.warning(f"platform_card: {_TIMEOUT_ENV}={raw!r} 超出 (0, {_TIMEOUT_MAX}] 秒，"
                    f"回落默认 {_DEFAULT_CARD_TIMEOUT}s")
        return _DEFAULT_CARD_TIMEOUT
    return value


class PlatformCardContractError(RuntimeError):
    """契约取卡失败（不可达 / 非 2xx / 响应格式非法）。"""


class PlatformCardContractClient:
    """主控平台卡只读契约的客户端（worker 侧唯一取卡通道）。

    Args:
        base_url: 主控 runtime 基址；缺省读 `RUNTIME_SERVER_URL`（拆地址部署时
                  worker 的回连基址，与 A2A/tasks 同一口径）。
        token:    runtime 凭证；缺省读 `RUNTIME_TOKEN`。
        timeout: 单次取卡超时上限（秒）；缺省读 `GRAPE_PLATFORM_CARD_TIMEOUT`，
                 再缺省用 `_DEFAULT_CARD_TIMEOUT`（2.0s）。取卡在任务准备关键
                 路径上，宁可回落文件卡也不能拖住 worktree 准备。
        opener:  注入点（测试用），默认 `urllib.request.urlopen`。
    """

    PATH = "/api/runtime/platform-cards"

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float | None = None, opener=None):
        self._base_url = base_url
        self._token = token
        self._timeout = timeout
        # opener 也走**调用期**解析：地址/令牌已经是 property，opener 若在构造期
        # 绑定死 `urllib.request.urlopen`，测试替换与将来的连接池替换都打不进来
        # （集成用例实测踩到：patch urlopen 后仍走原函数）。
        self._opener = opener

    @property
    def timeout(self) -> float:
        """单次超时（秒），**调用期**解析（显式参数 > 环境变量 > `_DEFAULT_CARD_TIMEOUT`）。"""
        return resolve_card_timeout(self._timeout)

    @property
    def opener(self):
        return self._opener or urllib.request.urlopen

    # ---- 地址与凭据 ----------------------------------------------------------

    @property
    def base_url(self) -> str:
        if self._base_url is not None:
            return self._base_url.strip().rstrip("/")
        return (os.environ.get("RUNTIME_SERVER_URL") or "").strip().rstrip("/")

    @property
    def token(self) -> str:
        if self._token is not None:
            return self._token.strip()
        return (os.environ.get("RUNTIME_TOKEN") or "").strip()

    def configured(self) -> bool:
        """是否具备发起契约的条件（非 worker 进程为 False，属正常路径）。"""
        return bool(self.base_url and self.token)

    # ---- 契约调用 ------------------------------------------------------------

    def fetch_cards(self) -> dict[str, dict]:
        """取回 {provider: 卡字段 raw}；失败抛 `PlatformCardContractError`。

        返回值里不含凭据列——由服务端契约端点保证（mgmt 原语的列白名单），
        客户端不做二次裁剪，避免出现第二份投影。
        """
        if not self.configured():
            raise PlatformCardContractError("未配置 RUNTIME_SERVER_URL / RUNTIME_TOKEN")
        url = f"{self.base_url}{self.PATH}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json"},
        )
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None) or 200
                body = resp.read().decode("utf-8", "replace") or "{}"
        except Exception as e:  # noqa: BLE001 - 网络/超时/非 2xx 统一归类
            raise PlatformCardContractError(f"{url}: {e}") from e
        if not (200 <= int(status) < 300):
            raise PlatformCardContractError(f"{url}: HTTP {status}")
        try:
            payload = json.loads(body)
        except Exception as e:  # noqa: BLE001
            raise PlatformCardContractError(f"{url}: 响应不是合法 JSON: {e}") from e
        cards = payload.get("cards") if isinstance(payload, dict) else None
        if not isinstance(cards, dict):
            raise PlatformCardContractError(f"{url}: 响应缺 cards 字典")
        return {str(p): dict(raw) for p, raw in cards.items() if isinstance(raw, dict)}

class PlatformCardContractServer:
    """平台卡契约的**服务端**（runtime 进程内）：把 mgmt 契约行整形成对外响应。

    为什么服务端也建模成对象：契约两侧（worker 客户端 / 主控服务端）共用
    `PATH` 与响应结构，任何一侧单独改都会静默破坏对端；放在同一模块里，
    契约的"形状"只有一个定义点。

    数据来源必须是**跨域契约**（`get_service("mgmt").get_repo_platform_cards()`），
    不是直读 `grape_t_repo_platforms`：3 库 4 进程下该表归 mgmt 域，runtime 进程
    只持有 mgmt 的 DSN/契约，不持有它的表；经契约还能同时覆盖单库与拆库两种
    拓扑（矩阵恒 lib 时走 Local，将来解禁远程调用时自动走 Remote）。

    Args:
        mgmt_service: 注入点（测试用）；缺省 `get_service("mgmt")`。
    """

    def __init__(self, mgmt_service=None):
        self._mgmt = mgmt_service

    def _service(self):
        if self._mgmt is not None:
            return self._mgmt
        from src.kernel.gsb.provider import get_service  # noqa: PLC0415

        return get_service("mgmt")

    def snapshot(self, enabled_only: bool = True) -> dict:
        """返回 `{"cards": {provider: 卡字段...}}`；失败带 `error`，绝不打 5xx。

        失败降级为"空卡 + error 标记"而不是抛异常：worker 侧据此打 WARNING 并
        回落文件卡/代码默认（fail-open 且响亮）；若这里 500，worker 只会看到
        "取卡失败"而拿不到"表读不了"这一层信息。
        """
        from src.kernel.config import log  # noqa: PLC0415

        try:
            rows = self._service().get_repo_platform_cards(enabled_only=bool(enabled_only)) or []
        except Exception as e:  # noqa: BLE001
            log.warning(f"platform-cards contract: 读 mgmt 平台卡失败（返回空卡）: {e}")
            return {"cards": {}, "error": "platform_cards_unavailable"}
        cards: dict[str, dict] = {}
        for row in rows:
            provider = str((row or {}).get("provider") or "")
            if provider:
                cards[provider] = dict(row)
        log.info(f"platform-cards contract: served {sorted(cards)}")
        return {"cards": cards}
