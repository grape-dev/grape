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
import time
import urllib.request

from src.kernel.config import log

#: 单次取卡超时的**默认值**（秒）。来源优先级：
#:   显式构造参数 > 环境变量 `GRAPE_PLATFORM_CARD_TIMEOUT` > 本常量。
#: 依据：取卡位于 worktree 准备的关键路径上（拿不到卡就没有 clone 地址）。
#: 2026-11-21 从 2.0s 提到 10.0s：2s 在主控慢查询 / GC / 冷启动时会把"慢"判成
#: "不可达"，而这次失败会被装配层当成「平台未注册」暴露给任务（见
#: PlatformCardUnavailableError 的引入）。取卡只在缓存未命中时发生（300s 一次），
#: 失败路径另有 1 次重试（_RETRY_DELAY），最坏等待 ≈ 10.0 × 2 + 0.5 ≈ 20.5s；
#: 相对"把可用主控判成不可用、整批 clone 失败"，这个等待是可接受的代价。
_DEFAULT_CARD_TIMEOUT = 10.0
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


class PlatformCardUnavailableError(PlatformCardContractError):
    """取卡通道**本身**不可用：网络/超时、HTTP 5xx、或服务端显式回 `error`
    （`platform_cards_unavailable` —— 主控读 mgmt 表失败）。

    与「平台未注册」严格区分，这是两类完全不同的故障：
      · 本异常  → 修链路（主控是否可达 / RUNTIME_TOKEN 是否有效 / 表是否可读）；
      · 平台未注册（`PlatformCardMissingError.code=platform_not_registered`）
        → 去配卡。
    为什么必须区分（2026-11-21）：服务端一直以 `{"cards": {}, "error": ...}` 形式
    报告故障（绝不 5xx），但客户端只取 `cards` 并把它当"没有卡"，装配层随即回落
    到代码默认卡（不含 gitee），最终任务侧看到的是 `platform_not_registered` ——
    把系统性故障伪装成"平台没注册"，排查方向被带偏。"""


#: 取卡失败后的重试次数与间隔（仅对 `PlatformCardUnavailableError` 生效；
#: 401/403/404 这类配置错不重试——重试不会让它变对）。
_RETRY_ATTEMPTS = 2
_RETRY_DELAY = 0.5


class PlatformCardContractClient:
    """主控平台卡只读契约的客户端（worker 侧唯一取卡通道）。

    Args:
        base_url: 主控 runtime 基址；缺省读 `RUNTIME_SERVER_URL`（拆地址部署时
                  worker 的回连基址，与 A2A/tasks 同一口径）。
        token:    runtime 凭证；缺省读 `RUNTIME_TOKEN`。
        timeout: 单次取卡超时上限（秒）；缺省读 `GRAPE_PLATFORM_CARD_TIMEOUT`，
                 再缺省用 `_DEFAULT_CARD_TIMEOUT`（10.0s）。取卡在任务准备关键
                 路径上；失败路径另有 1 次重试（`_RETRY_DELAY`），最坏等待约 20.5s。
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
        """取回 `{provider: 卡字段 raw}`；失败抛 `PlatformCardContractError` 系。

        返回值里不含凭据列——由服务端契约端点保证（mgmt 原语的列白名单），
        客户端不做二次裁剪，避免出现第二份投影。

        可重试的失败（网络 / 超时 / 5xx / 服务端 `error`）最多尝试
        `_RETRY_ATTEMPTS` 次；不可重试的失败（401/403/404、响应格式非法）立即抛。
        """
        if not self.configured():
            raise PlatformCardContractError("未配置 RUNTIME_SERVER_URL / RUNTIME_TOKEN")
        last: PlatformCardUnavailableError | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                return self._fetch_once()
            except PlatformCardUnavailableError as e:
                last = e
                if attempt >= _RETRY_ATTEMPTS:
                    break
                log.warning(f"platform-cards contract: 第 {attempt} 次取卡失败（{e}），"
                            f"{_RETRY_DELAY}s 后重试")
                time.sleep(_RETRY_DELAY)
        assert last is not None
        raise last

    def _fetch_once(self) -> dict[str, dict]:
        """单次契约调用（不含重试）。见 `fetch_cards` 的失败分类。"""
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
        except urllib.error.HTTPError as e:
            # urllib 对 4xx/5xx 是抛 HTTPError，不会返回 response.status ——
            # 必须在这里分流，否则 401/403 会被归成“通道不可用”并触发无意义重试
            # （2026-11-22 评审③：下面按 status 分类的分支曾是真传输永不到的死代码）。
            code = int(getattr(e, "code", 0) or 0)
            if code >= 500:
                raise PlatformCardUnavailableError(f"{url}: HTTP {code}") from e
            raise PlatformCardContractError(f"{url}: HTTP {code}") from e
        except Exception as e:  # noqa: BLE001 - 网络/超时统一归类为"通道不可用"
            raise PlatformCardUnavailableError(f"{url}: {e}") from e
        if not (200 <= int(status) < 300):
            # 5xx = 主控自身故障（可重试）；4xx = 凭据/路径配置错（重试无意义）
            if int(status) >= 500:
                raise PlatformCardUnavailableError(f"{url}: HTTP {status}")
            raise PlatformCardContractError(f"{url}: HTTP {status}")
        try:
            payload = json.loads(body)
        except Exception as e:  # noqa: BLE001
            raise PlatformCardContractError(f"{url}: 响应不是合法 JSON: {e}") from e
        cards = payload.get("cards") if isinstance(payload, dict) else None
        if not isinstance(cards, dict):
            raise PlatformCardContractError(f"{url}: 响应缺 cards 字典")
        # 服务端读表失败时回 `{"cards": {}, "error": "platform_cards_unavailable"}`。
        # 必须当作"通道不可用"抛出，不能当"没有卡"——否则退化到代码默认卡后，
        # 任务侧只会看到「平台未注册」。服务端的 error 优先于空 cards 判断。
        if isinstance(payload, dict) and str(payload.get("error") or "").strip():
            raise PlatformCardUnavailableError(
                f"{url}: {str(payload.get('error')).strip()}")
        # 空字符串列必须按"未配置"处理：表路径（card_projection）会省略空值列，
        # 契约路径若原样透传，webhook_verify="" / branch_create_form="" 会被 Builder
        # 判成非法枚举值而整卡拒绝（自建实例只写 dialect+host 时整卡丢失）。
        # 例外：`SEMANTIC_EMPTY_KEYS`（凭据列）的空串是**声明**（该平台无物理列，
        # S128 终态），与表投影共用同一常量，避免两侧各自实现漂移。
        from src.domains.runtime.vcs.card_projection import SEMANTIC_EMPTY_KEYS
        out: dict[str, dict] = {}
        for prov, raw in cards.items():
            if not isinstance(raw, dict):
                continue
            out[str(prov)] = {
                str(k): v for k, v in raw.items()
                if str(k) in SEMANTIC_EMPTY_KEYS
                or not (isinstance(v, str) and v.strip() == "")
            }
        return out


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
