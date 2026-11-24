"""Issue #4082：worker 探活/心跳的 `runtime_id` 合法性守卫 + 结构化错误 + 失败信号外发。

背景（生产实测 2026-11-24）：15 台 worker 每小时 203 次 `GET /api/runtime/runtimes/0/tags`
100% 401。根因不是鉴权，而是注册态缺失时把 `runtime_id` 静默降级成 `0`（URL 模板拼成
`/runtimes//tags` 归一为 `0`）——配置问题被伪装成鉴权失败，引擎 `role_tags/scene_tags`
长期为空且无任何 ERROR 级可观测。

本模块只做三件事，全部无 IO 或 warn-only：

1. `parse_runtime_id` / `is_valid_runtime_id`：**只有正整数**是合法 id（`0`/负数/`abc`/
   `1.5`/`bool`/`None` 一律非法）；
2. `RuntimeIdMissingError`（code=`RUNTIME_ID_MISSING`）：发请求**之前**抛出的结构化错误，
   带 host/context/原始值，供日志与上报甄别「未注册 vs 配置缺失」；
3. `ProbeFailureReporter` + `tag_probe_signal_sink`：标签探活失败按 host/reason 上报主控
   （服务端计 `grape_runtime_tags_probe_failure_total{host,reason}`，`deploy/alerts.yml`
   的 `GrapeRuntimeTagsProbeFailing` 规则消费）。**连续失败达阈值才发一次**，防每轮刷爆；
   **一个实例只代表一个引擎（一条 rid）**——多引擎主机上共享实例会被同轮其它引擎的成功清零，
   连败永远到不了阈值；契约外 reason（`http_403` 等）归 `other` 桶计数并留日志，不再静默。

口径与服务端 `src/domains/insight/observability` 为自包含双实现（worker 包闭包内没有
`src.domains.insight`），由 `tests/runtime/test_issue_4082_runtime_id_guard.py` 锁定。
"""

import json
import logging
import os
import socket
import time
import urllib.request

log = logging.getLogger("devops-bot")

# 结构化错误码（日志/信号/告警共用同一口径，禁止散落字符串）
RUNTIME_ID_MISSING = "RUNTIME_ID_MISSING"

# 契约外失败（`http_403`/`http_422` 等）的归并桶：不再静默丢弃——计入连败、照样上报，
# 真实 reason 随信号（`raw_reason`）与日志带出，服务端按 other 计数与告警。
OTHER_REASON = "other"

# 探活失败归因口径：未注册(id_missing) / 无权限(unauthorized) / 行已删(not_found) /
# 已注册但无标签(no_tags) / 契约外失败(other)。与服务端 RUNTIME_TAGS_PROBE_REASONS
# 同口径（漂移看护）。
PROBE_REASONS = ("id_missing", "unauthorized", "not_found", "no_tags", OTHER_REASON)

# 网络类失败不进标签探活指标（连通性由心跳指标负责），仅日志可诊断
NETWORK_REASON = "network"

# worker→主控失败信号端点（runtime.py 承接 → observability 计数）
SIGNAL_PATH = "/api/runtime/runtime-tags-probe/signal"

# 上报失败（死地址/无 token）的本地兜底落点：值守可据此发现「上报链路自身不可用」
FALLBACK_FILE = "runtime-tags-probe-alerts.jsonl"

# 连续失败达该次数才首次告警（env 可调；非法值回退默认）
ALERT_AFTER_ENV = "RUNTIME_TAGS_PROBE_ALERT_AFTER"
DEFAULT_ALERT_AFTER = 3


def parse_runtime_id(raw) -> int | None:
    """把各种形态的 runtime id 收敛成**正整数**，非法一律返回 None。

    - `bool` 显式排除（`True` 是 `1` 的子类，但语义不是 id）；
    - 浮点只接受整值（`12.0` → 12，`1.5` → None）；
    - 字符串去空白后必须全数字且首个数字非 `0`（`"012"`/`"0"` → None）。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() and raw > 0 else None
    if isinstance(raw, str):
        s = raw.strip()
        if not s or not s.isdigit():
            return None
        if len(s) > 1 and s[0] == "0":   # "012" 不是规范正整数形态
            return None
        val = int(s)
        return val if val > 0 else None
    return None


def is_valid_runtime_id(raw) -> bool:
    """是否为正整数形态的 runtime id（守卫入口，供客户端/路由共用）。"""
    return parse_runtime_id(raw) is not None


def _alert_after() -> int:
    """首次告警阈值（`RUNTIME_TAGS_PROBE_ALERT_AFTER`，默认 3）。"""
    raw = (os.environ.get(ALERT_AFTER_ENV) or "").strip()
    try:
        val = int(raw) if raw else DEFAULT_ALERT_AFTER
    except ValueError:
        return DEFAULT_ALERT_AFTER
    return max(1, val)


class RuntimeIdMissingError(Exception):
    """`runtime_id` 缺失/非法时**发请求前**抛出的结构化错误。

    调用方（claim 循环）据此：① 落 `RUNTIME_ID_MISSING` 结构化日志；② 计一次
    `id_missing` 探活失败；③ 触发退避重试注册（不自愈就永远拿不到真实 id）。
    """

    code = RUNTIME_ID_MISSING

    def __init__(self, runtime_id_raw, *, host: str = "", context: str = ""):
        self.runtime_id_raw = runtime_id_raw
        self.host = host or socket.gethostname()
        self.context = context
        self.message = (f"runtime_id {runtime_id_raw!r} is not a positive integer; "
                        f"request skipped (context={context or '-'}, host={self.host})")
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "host": self.host,
            "runtime_id": "" if self.runtime_id_raw is None else str(self.runtime_id_raw),
            "context": self.context,
            "message": self.message,
        }


def missing_runtime_id_error(runtime_id_raw, *, host: str = "", context: str = "") -> RuntimeIdMissingError:
    """构造结构化 `RUNTIME_ID_MISSING` 错误（统一入口，避免调用方各拼一份）。"""
    return RuntimeIdMissingError(runtime_id_raw, host=host, context=context)


def tag_probe_signal_sink(base_url: str | None = None, token: str | None = None,
                          config_dir: str | os.PathLike | None = None,
                          timeout: float = 5.0):
    """构造标签探活失败信号 sink（worker→主控，Issue #4082）。

    - 端点 `POST {base_url}{SIGNAL_PATH}`，Bearer = RUNTIME_TOKEN（rta_ 凭据）；
    - 上报失败（无地址/无 token/网络不可达/非 2xx）→ 追加写
      `{config_dir}/runtime-tags-probe-alerts.jsonl` 本地兜底并返回 False；
    - **永不抛异常**（warn-only：告警链路故障不得影响探活主链路）。

    返回 `sink(payload) -> bool`；sink 就地补 host/pid/ts，便于值守甄别来源主机。
    """
    from src.domains.runtime.engine.contract_guard import cache_dir, worker_server_url

    def _sink(signal: dict) -> bool:
        payload = dict(signal)
        payload.setdefault("host", socket.gethostname())
        payload.setdefault("pid", os.getpid())
        payload.setdefault("ts", time.time())
        url = (base_url if base_url is not None else worker_server_url(config_dir)).rstrip("/")
        if token is None:
            tok = (os.environ.get("RUNTIME_TOKEN") or "").strip()
        else:
            tok = token
        ok = False
        if url and tok:
            try:
                req = urllib.request.Request(
                    f"{url}{SIGNAL_PATH}",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": f"Bearer {tok}",
                             "Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = getattr(resp, "status", None)
                    if status is None:
                        status = resp.getcode()
                    ok = status is not None and 200 <= status < 300
            except Exception:
                ok = False
        if not ok:
            try:
                d = cache_dir(config_dir)
                d.mkdir(parents=True, exist_ok=True)
                line = dict(payload)
                line["fallback"] = "alert-sink-unreachable"
                with open(d / FALLBACK_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return ok

    return _sink


class ProbeFailureReporter:
    """连续失败上报器：达阈值才发一次信号，之后每 `emit_every` 次再发；成功即清零。

    为什么不在每次失败都发：claim 循环每轮每引擎都探一次标签，无标签是**稳态**
    （已注册但未配置标签），逐次上报会把指标打成噪声。阈值（默认 3）+ 每 N 次重复
    既能证明「连续失败」，又能把噪声压到可告警的量级。

    **一个实例只代表一个引擎（一条 `rid`）**：多引擎 worker 上多个引擎同轮探活，共享
    一个实例时任一引擎的成功（`observe("")`）会把其它引擎的连败清零，「1 失败 + 1 成功」
    交替就永远到不了阈值（告警对多引擎主机失效）。实例的创建/回收由调用方按 rid 维护
    （`agent.tags_probe_reporter_for` / `agent.prune_tags_probe_reporters`）。
    """

    def __init__(self, *, threshold: int | None = None, emit_every: int | None = None,
                 sink=None, host: str = "", scene: str = "", rid: int | None = None):
        self.threshold = max(1, int(threshold if threshold is not None else _alert_after()))
        self.emit_every = max(1, int(emit_every if emit_every is not None else self.threshold))
        self.sink = sink if sink is not None else tag_probe_signal_sink()
        self.host = host or socket.gethostname()
        self.scene = scene
        self.rid = rid
        self.consecutive = 0
        self.last_reason = ""
        self.last_raw_reason = ""
        self.emitted = 0
        self._logged_reasons: set[str] = set()

    def _log_reason_once(self, reason: str, why: str):
        """同一 reason 每个实例只出声一次（既不静默，也不把日志刷成噪声，最多记 64 种）。"""
        if reason in self._logged_reasons or len(self._logged_reasons) >= 64:
            return
        self._logged_reasons.add(reason)
        log.warning(f"runtime tags probe: {why} (rid={self.rid if self.rid is not None else '-'} "
                    f"reason={reason} host={self.host})")

    def observe(self, reason: str) -> bool:
        """记录一次探活结果；返回 True 表示本次应发信号。

        - 空 reason（成功）→ 计数清零，返回 False；
        - 网络类失败（`network`）→ 不计连续失败（连通性另有心跳指标），但每个实例留一条日志；
        - 契约内 reason → 连续计数 +1，命中阈值/重复窗口才发信号；
        - 契约外 reason（`http_403`/`http_422`…）→ 归 `other` 桶计入连败并照样上报，
          原始 reason 落一次日志 + 随信号带出（`raw_reason`），不再静默丢弃。
        """
        reason = (reason or "").strip().lower()
        if not reason:
            if self.consecutive:
                log.info(f"runtime tags probe recovered after {self.consecutive} consecutive "
                         f"failure(s) host={self.host} rid={self.rid if self.rid is not None else '-'}"
                         f" last_reason={self.last_reason or '-'}")
            self.consecutive = 0
            self.last_reason = ""
            self.last_raw_reason = ""
            return False
        if reason == NETWORK_REASON:
            # 连通性由心跳指标负责，这里不计连败——但「完全无痕」同样是问题，留一条线索
            self._log_reason_once(reason, "network failure not counted (connectivity is "
                                          "tracked by the heartbeat metric)")
            return False
        bucket = reason if reason in PROBE_REASONS else OTHER_REASON
        if bucket == OTHER_REASON and reason != OTHER_REASON:
            self._log_reason_once(reason, f"non-contract failure counted as '{OTHER_REASON}'")
        self.consecutive += 1
        self.last_reason = bucket
        self.last_raw_reason = reason
        due = self.consecutive == self.threshold or \
            (self.consecutive > self.threshold and
             (self.consecutive - self.threshold) % self.emit_every == 0)
        if not due:
            return False
        self.emitted += 1
        payload = {
            "host": self.host,
            "scene": self.scene,
            "reason": bucket,
            "raw_reason": "" if bucket == reason else reason,
            "runtime_id": "" if self.rid is None else str(self.rid),
            "consecutive": self.consecutive,
            "code": RUNTIME_ID_MISSING if bucket == "id_missing" else "",
            "detail": f"runtime tags probe failed {self.consecutive} times in a row "
                      f"(reason={bucket}"
                      f"{'' if bucket == reason else f', raw_reason={reason}'}, host={self.host})",
        }
        try:
            self.sink(payload)
        except Exception as e:  # sink 自身故障绝不打断探活主链路
            log.warning(f"runtime tags probe signal failed: {e}")
        return True

    def snapshot(self) -> dict:
        return {"host": self.host, "rid": self.rid, "consecutive": self.consecutive,
                "last_reason": self.last_reason, "last_raw_reason": self.last_raw_reason,
                "emitted": self.emitted, "threshold": self.threshold}