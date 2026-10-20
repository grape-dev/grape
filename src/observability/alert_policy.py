"""告警策略层（P1-1 / D1）：通知时段、重复抑制、聚合、升级、事件级别。

分层的意义：`alerts.py` 负责**求值**（指标 → 事件）与**投递**（事件 → 渠道），
本模块负责**该不该发、什么时候发、发成几条**——纯函数优先，便于单测。

口径（与设计文档 §9 一致）：

* **级别 severity**：`critical > severe > important > warning > info`，默认 `warning`。
  非法值归一为 `warning` 并记日志（不因脏配置让求值器崩）。
* **重复抑制 `repeat_sec`**：`0` = 同一开放事件只通知一次（旧行为，默认）。
  `>0` = 距上次通知 ≥ `repeat_sec` 且仍未恢复 → 复推一次（"还在告警"）。
* **通知时段 `notify_window_json`**：空 = 全天。窗口外**只记录**（事件 `notify_deferred=1`），
  窗口内恢复后再推一次（推完清标记 → 不会每天反复提醒）。
  形如 `{"days":[1,2,3,4,5],"start":"09:00","end":"19:00"}`；`days` 取 1=周一…7=周日，
  省略 = 每天；`start > end` 表示跨零点。
* **聚合 `aggregate_mode`**：`none` 立即发（默认）；`rule` 同一规则自**首条**待发事件起算窗口，
  窗口结束时发**一条汇总**；`period` 与**时钟对齐**窗口（固定节拍），窗口结束时发汇总。
* **升级 `escalate_json`**：`{"after_sec":1800,"severity":"critical","channels":"feishu"}`
  —— 开放事件持续 ≥ `after_sec` 仍未恢复且未升级过 → 升级通知一次（级别提升、可追加渠道）。

时区：固定 **UTC+8**（`CST`），不依赖系统 tzdata（EulerOS 上未必有）。
聚合缓冲是**进程内**状态：进程重启丢失未发送的汇总（可接受，事件本身都在库里）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("grape.observability.alert_policy")

CST = timezone(timedelta(hours=8))

#: 由重到轻
SEVERITIES: Tuple[str, ...] = ("critical", "severe", "important", "warning", "info")
DEFAULT_SEVERITY = "warning"
#: 数字越小越严重，便于排序/比较
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

AGG_MODES: Tuple[str, ...] = ("none", "rule", "period")


# ---------------------------------------------------------------- 级别

def normalize_severity(raw: Any) -> str:
    """把任意输入归一成合法级别；非法值 → warning（并记日志，不抛）。"""
    s = str(raw or "").strip().lower()
    if s in SEVERITIES:
        return s
    if s:
        log.warning(f"obs alert policy: 未知级别 {raw!r} → {DEFAULT_SEVERITY}")
    return DEFAULT_SEVERITY


def severity_rank(sev: Any) -> int:
    return SEVERITY_RANK.get(normalize_severity(sev), SEVERITY_RANK[DEFAULT_SEVERITY])


def more_severe(a: Any, b: Any) -> str:
    """取两者中更严重的级别。"""
    return normalize_severity(a) if severity_rank(a) <= severity_rank(b) else normalize_severity(b)


# ---------------------------------------------------------------- 通知时段

def parse_notify_window(raw: Any) -> Optional[Dict[str, Any]]:
    """解析通知时段。空/非法 → None（= 全天可通知）。

    合法：`{"days":[1,2,3,4,5],"start":"09:00","end":"19:00"}`；`days` 省略 = 每天。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        obj = raw
    else:
        s = str(raw).strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
        except Exception:
            log.warning(f"obs alert policy: notify_window_json 解析失败，按全天处理: {raw!r}")
            return None
    if not isinstance(obj, dict):
        return None

    def _mins(v: Any, default: int) -> int:
        try:
            parts = str(v).strip().split(":")
            if len(parts) == 2:
                return max(0, min(23, int(parts[0]))) * 60 + max(0, min(59, int(parts[1])))
            return max(0, min(1439, int(v)))
        except Exception:
            return default

    days = obj.get("days")
    try:
        day_set = sorted({int(d) for d in days if 1 <= int(d) <= 7}) if days else [1, 2, 3, 4, 5, 6, 7]
    except Exception:
        day_set = [1, 2, 3, 4, 5, 6, 7]
    if not day_set:
        return None
    return {"days": day_set, "start": _mins(obj.get("start"), 0), "end": _mins(obj.get("end"), 1439)}


def in_notify_window(win: Optional[Dict[str, Any]], when: datetime) -> bool:
    """`when`（aware 或 naive 视为 UTC）是否落在通知时段内。None = 全天 True。"""
    if not win:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(CST)
    iso_dow = local.isoweekday()          # 1=周一 … 7=周日
    mins = local.hour * 60 + local.minute
    start, end = int(win.get("start", 0)), int(win.get("end", 1439))
    days = win.get("days") or [1, 2, 3, 4, 5, 6, 7]
    if start <= end:
        return iso_dow in days and start <= mins <= end
    # 跨零点：当日 start 之后 或 次日 end 之前
    if iso_dow in days and mins >= start:
        return True
    prev_dow = 7 if iso_dow == 1 else iso_dow - 1
    return prev_dow in days and mins <= end


# ---------------------------------------------------------------- 升级

def parse_escalation(raw: Any) -> Optional[Dict[str, Any]]:
    """解析升级规则。空/非法/after_sec<=0 → None（不升级）。

    `{"after_sec":1800,"severity":"critical","channels":"feishu,webhook"}`
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        obj = raw
    else:
        s = str(raw).strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
        except Exception:
            log.warning(f"obs alert policy: escalate_json 解析失败，按不升级处理: {raw!r}")
            return None
    if not isinstance(obj, dict):
        return None
    try:
        after = int(obj.get("after_sec") or 0)
    except Exception:
        after = 0
    if after <= 0:
        return None
    return {"after_sec": after,
            "severity": normalize_severity(obj.get("severity") or "critical"),
            "channels": str(obj.get("channels") or "").strip()}


# ---------------------------------------------------------------- 策略归一化

def sanitize_policy(payload: Dict[str, Any], base: Optional[Dict[str, Any]] = None
                    ) -> Tuple[Dict[str, Any], List[str]]:
    """校验并归一化策略字段（管理台写入用）。

    返回 (clean, errors)。`errors` 非空时调用方应拒绝写入（400）；
    `clean` 只含本次出现的字段（PATCH 语义）。
    """
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    base = base or {}

    if "threshold" in payload:
        raw_thr = payload.get("threshold")
        if raw_thr is None:
            errors.append("threshold 不能为 null")
        else:
            try:
                clean["threshold"] = float(raw_thr)
            except Exception:
                errors.append("threshold 必须是数字")

    if "severity" in payload:
        sev = str(payload.get("severity") or "").strip().lower()
        if sev not in SEVERITIES:
            errors.append(f"severity 必须是 {list(SEVERITIES)} 之一")
        else:
            clean["severity"] = sev

    if "repeat_sec" in payload:
        try:
            v = int(payload.get("repeat_sec") or 0)
            if v < 0:
                raise ValueError
            clean["repeat_sec"] = v
        except Exception:
            errors.append("repeat_sec 必须是非负整数（秒）")

    if "aggregate_mode" in payload:
        m = str(payload.get("aggregate_mode") or "none").strip().lower()
        if m not in AGG_MODES:
            errors.append(f"aggregate_mode 必须是 {list(AGG_MODES)} 之一")
        else:
            clean["aggregate_mode"] = m

    if "aggregate_window_sec" in payload:
        try:
            v = int(payload.get("aggregate_window_sec") or 0)
            if v < 0:
                raise ValueError
            clean["aggregate_window_sec"] = v
        except Exception:
            errors.append("aggregate_window_sec 必须是非负整数（秒）")

    if "notify_window_json" in payload:
        raw = payload.get("notify_window_json")
        if raw in (None, "", {}):
            clean["notify_window_json"] = ""
        else:
            parsed = parse_notify_window(raw)
            if parsed is None:
                errors.append("notify_window_json 格式不合法（示例 {\"days\":[1,2,3,4,5],"
                              "\"start\":\"09:00\",\"end\":\"19:00\"}）")
            else:
                clean["notify_window_json"] = json.dumps(parsed, ensure_ascii=False)

    if "escalate_json" in payload:
        raw = payload.get("escalate_json")
        if raw in (None, "", {}):
            clean["escalate_json"] = ""
        else:
            parsed = parse_escalation(raw)
            if parsed is None:
                errors.append("escalate_json 的 after_sec 必须是正整数"
                              "（示例 {\"after_sec\":1800,\"severity\":\"critical\"}）")
            else:
                clean["escalate_json"] = json.dumps(parsed, ensure_ascii=False)

    if "notify_channels" in payload:
        clean["notify_channels"] = str(payload.get("notify_channels") or "").strip()

    if "enabled" in payload:
        clean["enabled"] = 1 if payload.get("enabled") else 0

    # 交叉校验：聚合方式非 none 但窗口为 0 → 用默认 300s（不给错误，只补默认）
    mode = clean.get("aggregate_mode") or base.get("aggregate_mode") or "none"
    win = clean.get("aggregate_window_sec")
    if win is None:
        win = int(base.get("aggregate_window_sec") or 0)
    if mode != "none" and int(win or 0) <= 0:
        clean["aggregate_window_sec"] = 300
    return clean, errors


# ---------------------------------------------------------------- 发不发

def decide_notify(rule: Dict[str, Any], ev: Dict[str, Any], now: datetime) -> Tuple[str, str]:
    """决定这条事件本轮怎么处理。

    返回 `(action, reason)`：
    - `send`    立即投递（调用方投递成功后应更新 last_notified_at/notify_count）
    - `defer`   窗口外，只记录（调用方置 notify_deferred=1）
    - `skip`    静默中 / 重复抑制期内 / 无渠道
    - `aggregate` 交给聚合缓冲（调用方 add 进 Aggregator）
    """
    chans = (rule.get("notify_channels") or "").strip()
    if not chans:
        return "skip", "no_channel"

    if ev.get("silenced_until"):
        return "skip", "silenced"

    if not in_notify_window(parse_notify_window(rule.get("notify_window_json")), now):
        return "defer", "out_of_notify_window"

    repeat = int(rule.get("repeat_sec") or 0)
    last = (ev.get("last_notified_at") or "").strip()
    if last and repeat <= 0:
        return "skip", "already_notified"
    if last and repeat > 0:
        last_dt = ev.get("_last_notified_dt") or _parse_dt(last)
        if last_dt and (now - last_dt).total_seconds() < repeat:
            return "skip", "repeat_suppressed"

    if str(rule.get("aggregate_mode") or "none").lower() != "none":
        return "aggregate", "aggregate"
    return "send", "ok"


def _parse_dt(raw: Any) -> Optional[datetime]:
    try:
        s = str(raw or "").strip().replace("Z", "+00:00")
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def repeat_due(rule: Dict[str, Any], ev: Dict[str, Any], now: datetime) -> bool:
    """开放事件是否该按 `repeat_sec` **复推**（"还在告警"）。

    `repeat_sec<=0` → False（旧行为：只通知一次）。
    `repeat_sec>0`：从未通知过 → True（例如规则刚从"空渠道"改成有渠道，而事件还在）；
    已通知过则要等满 `repeat_sec`。
    求值器用它把开放事件重新提名进通知候选，随后仍由 `decide_notify` 把关
    （静默 / 通知时段 / 聚合）。
    """
    repeat = int(rule.get("repeat_sec") or 0)
    if repeat <= 0:
        return False
    last = (ev.get("last_notified_at") or "").strip()
    if not last:
        return True
    dt = _parse_dt(last)
    return bool(dt) and (now - dt).total_seconds() >= repeat


def escalation_due(rule: Dict[str, Any], ev: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """该开放事件是否到了升级时机。返回升级配置或 None（未配置/未到点/已升级过）。"""
    cfg = parse_escalation(rule.get("escalate_json"))
    if not cfg:
        return None
    if (ev.get("escalated_at") or "").strip():
        return None
    first = _parse_dt(ev.get("first_fired_at"))
    if not first:
        return None
    if (now - first).total_seconds() < int(cfg["after_sec"]):
        return None
    return cfg


# ---------------------------------------------------------------- 聚合缓冲

class Aggregator:
    """进程内聚合缓冲：窗口结束时产出一条汇总。

    - `mode='rule'`：同一规则一条缓冲，窗口自**首条**待发事件起算
    - `mode='period'`：同一规则 + 时钟对齐窗口一条缓冲（固定节拍）

    仅持有"待发汇总"，已发送即出队；进程重启丢失未发送汇总（事件本体不丢）。
    """

    def __init__(self) -> None:
        self._groups: Dict[Tuple[int, str], Dict[str, Any]] = {}

    @staticmethod
    def _bucket_key(rule_id: int, mode: str, window: int, now: datetime) -> Tuple[int, str]:
        if mode == "period":
            epoch = int(now.timestamp())
            return (int(rule_id), f"period:{epoch // max(1, window)}")
        return (int(rule_id), "rule")

    def add(self, rule: Dict[str, Any], ev: Dict[str, Any], now: datetime) -> None:
        mode = str(rule.get("aggregate_mode") or "none").lower()
        window = int(rule.get("aggregate_window_sec") or 0) or 300
        key = self._bucket_key(int(rule.get("id") or 0), mode, window, now)
        g = self._groups.get(key)
        if g is None:
            g = {"rule_id": int(rule.get("id") or 0), "rule_name": rule.get("name") or ev.get("rule_name"),
                 "level": rule.get("level") or ev.get("level") or "user",
                 "severity": normalize_severity(rule.get("severity") or ev.get("severity")),
                 "metric": ev.get("metric") or rule.get("metric"), "threshold": ev.get("threshold"),
                 "mode": mode, "started_at": now, "window_end": now + timedelta(seconds=window),
                 "event_ids": [], "dims": [], "repos": [], "max_value": None, "escalated": False}
            self._groups[key] = g
        if mode == "period":
            # 周期聚合固定节拍：窗口末 = 当前节拍结束
            epoch = int(now.timestamp())
            g["window_end"] = datetime.fromtimestamp((epoch // window + 1) * window, tz=now.tzinfo)
        eid = ev.get("id")
        if eid and int(eid) not in g["event_ids"]:
            g["event_ids"].append(int(eid))
        if ev.get("dim_key") and ev["dim_key"] not in g["dims"]:
            g["dims"].append(ev["dim_key"])
        if ev.get("repo_key") and ev["repo_key"] not in g["repos"]:
            g["repos"].append(ev["repo_key"])
        raw_v = ev.get("value")
        if raw_v is not None:
            try:
                v = float(raw_v)
                g["max_value"] = v if g["max_value"] is None else max(g["max_value"], v)
            except Exception:
                pass
        if ev.get("escalated"):
            g["escalated"] = True

    def due(self, now: datetime) -> List[Dict[str, Any]]:
        """取走已到期的汇总（窗口结束）。"""
        out: List[Dict[str, Any]] = []
        for key in list(self._groups.keys()):
            g = self._groups[key]
            if now >= g["window_end"]:
                out.append(g)
                self._groups.pop(key, None)
        out.sort(key=lambda x: x.get("started_at") or now)
        return out

    def pending(self) -> int:
        return len(self._groups)

    def clear(self) -> None:
        self._groups.clear()


#: 进程内单例（求值器与投递都在 API 进程里）
AGGREGATOR = Aggregator()