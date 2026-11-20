"""AlertEngine——审计行为序列分析 + 高风险操作告警。

P1-5：5 条组合攻击检测规则（数据外泄/高频扫描/非标端口/命令预检/配置漂移）
P1-9：3 条风险分级规则（CRITICAL/HIGH/MEDIUM）

任务结束后由 BwrapSandbox.cleanup() 调用，扫描该 task_id 的完整审计序列。
不改 P0 审计格式——只读审计记录，生成 Alert 写回审计 JSONL。

设计原则：
- 规则是纯函数（输入 events 输出 alerts）
- 阈值抽 _AlertConstants 常量，不散落魔法数字
- fingerprint 只用稳定标量 tuple，不含 timestamp/event_id
- 命令归一化后词元级匹配，不靠原始字符串正则
- cleanup() 中异常隔离——告警扫描失败不影响资源清理
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from .types import AlertSeverity

log = logging.getLogger("devops-bot")


# ===== 常量 =====

class _AlertConstants:
    """规则阈值和关键词常量。P2 可改为 DB 可配置。"""

    # ===== R1 数据外泄（双事件关联） =====
    SENSITIVE_PATH_PATTERNS = [
        r'\.ssh', r'\.env', r'\.aws', r'\.pem', r'\.key',
        r'id_rsa', r'credentials', r'\.git/config',
    ]
    EXTERNAL_METHODS = {"POST", "PUT"}
    INTERNAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
    R1_WINDOW_SECONDS = 60
    # R1 只认"先读后发"——反序不告警

    # ===== R2 高频请求 =====
    HIGH_FREQ_THRESHOLD = 20
    HIGH_FREQ_WINDOW_SECONDS = 60
    HIGH_FREQ_EXCLUDE_HOSTS = {"127.0.0.1", "localhost", "::1"}

    # ===== R3 非标端口 =====
    PORT_WHITELIST = {80, 443, 8080, 8443, 3000, 5000}
    PORT_CHECK_EXCLUDE_HOSTS = {"127.0.0.1", "localhost", "::1"}

    # ===== R5 配置漂移——P1 默认关闭 =====
    CONFIG_DRIFT_ENABLED = False

    # ===== R6 命令归一化 =====
    CMD_PREFIX_STRIP = {"sudo", "env", "timeout", "nohup", "exec"}
    RM_DANGEROUS_FLAGS = {"-rf", "-fr", "-r", "-f"}
    # rm -rf 目标排除目录（路径归一化后匹配 basename 或前缀）
    RM_SAFE_PATH_PREFIXES = [
        "/tmp/", "build/", "dist/", "node_modules/",
        "__pycache__/", ".cache/", "vendor/",
    ]
    # basename 匹配集——从 RM_SAFE_PATH_PREFIXES 提取目录名
    # 用于绝对路径 target 的安全判断（如 /home/user/build → basename=build → 安全）
    CRITICAL_PATTERNS = [
        r'git\s+push.*--force',
        r'git\s+push.*\s-f\b',
        r'DROP\s+TABLE',
        r'DROP\s+DATABASE',
    ]

    # ===== R7 HIGH =====
    HIGH_PATTERNS = [
        r'git\s+branch.*-D\s',
        r'git\s+reset\s+--hard',
        r'TRUNCATE\s+TABLE',
        r'rm\s+\.env',
        r'rm\s+.*\.pem',
    ]

    # ===== R8 MEDIUM/LOW =====
    MEDIUM_PATTERNS = [r'rollback', r'git\s+revert']
    LOW_PATTERNS = [r'close.*issue', r'git\s+merge.*--no-ff']

    # ===== get_events_by_task 上限 =====
    MAX_EVENTS_SCAN = 100_000  # 防止超大文件拖慢 cleanup


# ===== Alert 值对象 =====

@dataclass
class Alert:
    """安全告警——规则引擎输出。"""
    severity: AlertSeverity
    rule: str
    description: str
    task_id: str
    events: list[dict] = field(default_factory=list)
    timestamp: str = ""
    fingerprint: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.fingerprint:
            self.fingerprint = _make_fingerprint(self.rule, self.task_id)


def _make_fingerprint(*parts) -> str:
    """从稳定标量生成指纹。只接收稳定值（rule/task_id/host/path/pattern）。"""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ===== 命令归一化 =====

def _normalize_command(cmd_str: str) -> str:
    """命令归一化——剥前缀 + 压缩空白。

    1. 去首尾空格
    2. 压缩连续空白为单空格
    3. 递归剥除 CMD_PREFIX_STRIP 中的前缀
       - timeout N → 剥 timeout + 下一个词元（数字）
       - env KEY=VAL → 剥 env + 所有 KEY=VAL 词元
       - sudo/nohup/exec → 剥一个词元
    4. 返回归一化后的命令字符串
    """
    if not cmd_str:
        return ""
    tokens = re.split(r'\s+', cmd_str.strip())
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _AlertConstants.CMD_PREFIX_STRIP:
            del tokens[i]
            if tok == "timeout" and i < len(tokens):
                # timeout N → 剥数字参数（仅当下一个词元是数字时）
                if tokens[i].isdigit():
                    del tokens[i]
            elif tok == "env":
                # env KEY=VAL KEY2=VAL2 → 剥所有 KEY=VAL
                while i < len(tokens) and '=' in tokens[i]:
                    del tokens[i]
            # sudo/nohup/exec → 只剥自身
            continue
        break
    return ' '.join(tokens)


def _check_rm_rf_root(normalized_cmd: str) -> bool:
    """检测 rm -rf / 变体——词元级判断。

    归一化后的命令中：
    1. 找到 rm 词元
    2. 后续词元含 RM_DANGEROUS_FLAGS 中的标志组合
    3. 目标路径词元归一化后 == / 或以 / 开头
    4. 目标路径不在 RM_SAFE_PATH_PREFIXES 中
    """
    if not normalized_cmd:
        return False
    tokens = normalized_cmd.split()
    try:
        rm_idx = tokens.index("rm")
    except ValueError:
        return False

    # 收集 rm 后面的标志和目标
    has_r = False
    has_f = False
    targets: list[str] = []

    for tok in tokens[rm_idx + 1:]:
        if tok.startswith('-') and not tok.startswith('--'):
            # 短标志：-rf / -fr / -r / -f / -r -f
            flags = set(tok[1:])
            if 'r' in flags:
                has_r = True
            if 'f' in flags:
                has_f = True
        elif tok.startswith('--'):
            # --no-preserve-root 等长选项，跳过
            continue
        else:
            # 目标路径
            targets.append(tok)

    if not (has_r and has_f):
        return False

    if not targets:
        # rm -rf 无目标 → 不匹配（可能漏了路径，但不是 rm -rf /）
        return False

    for target in targets:
        # 归一化路径
        norm = os.path.normpath(target) if target else ""
        if not norm or not norm.startswith('/'):
            continue  # 相对路径不算根目录攻击

        # 检查是否在安全目录：前缀匹配 + basename 匹配
        is_safe = False
        basename = os.path.basename(norm)
        for safe_prefix in _AlertConstants.RM_SAFE_PATH_PREFIXES:
            # 前缀匹配（如 /tmp/foo → /tmp/）
            if norm.startswith(safe_prefix):
                is_safe = True
                break
            # basename 匹配（如 /home/user/build → build/ → basename=build）
            safe_name = safe_prefix.rstrip('/')
            if basename == safe_name:
                is_safe = True
                break
        if not is_safe:
            return True  # / 开头且不在安全目录 → CRITICAL

    return False





def _is_command_guard_event(evt: dict) -> bool:
    """判断是否是 command_guard 的 deny 事件（排除 prompt_injection）。

    有 check 字段时严格判断；无 check 字段时宽松接受（向后兼容 P0 审计记录）。
    """
    detail = evt.get("detail", {})
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            detail = {}
    check = detail.get("check", "")
    if check:
        return check == "command_guard"
    # 无 check 字段：如果是 command_guard kind 或无明确来源，宽松接受
    return evt.get("kind") == "command_guard" or not detail.get("score")


def _get_deny_reason(evt: dict) -> str:
    """从 deny 事件中提取命令文本。"""
    detail = evt.get("detail", {})
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            detail = {}
    return detail.get("reason", "") or detail.get("detail", "")


# ===== 规则实现 =====

def _rule_r1_data_exfiltration(task_id: str, events: list[dict]) -> list[Alert]:
    """R1 数据外泄嫌疑——双事件关联。

    同一 task 内 60s 窗口，先读敏感文件，后发外部 POST/PUT。
    """
    alerts: list[Alert] = []

    # 收集敏感文件读事件
    sensitive_reads: list[dict] = []
    for evt in events:
        path = evt.get("path", "")
        kind = evt.get("kind", "")
        for pattern in _AlertConstants.SENSITIVE_PATH_PATTERNS:
            if re.search(pattern, path, re.IGNORECASE):
                sensitive_reads.append(evt)
                break

    if not sensitive_reads:
        return alerts

    # 收集外部 POST/PUT 事件
    external_requests: list[dict] = []
    for evt in events:
        method = evt.get("method", "").upper()
        host = evt.get("host", "")
        kind = evt.get("kind", "")
        outcome = evt.get("outcome", "")
        if (kind == "network_decision" and outcome == "allow"
                and method in _AlertConstants.EXTERNAL_METHODS
                and host not in _AlertConstants.INTERNAL_HOSTS):
            external_requests.append(evt)

    if not external_requests:
        return alerts

    # 关联：先读后发，60s 窗口
    seen_fingerprints: set[str] = set()
    for read_evt in sensitive_reads:
        read_ts = _parse_ts(read_evt.get("ts", ""))
        read_path = read_evt.get("path", "")
        for req_evt in external_requests:
            req_ts = _parse_ts(req_evt.get("ts", ""))
            if not read_ts or not req_ts:
                continue
            delta = (req_ts - read_ts).total_seconds()
            if delta < 0 or delta > _AlertConstants.R1_WINDOW_SECONDS:
                continue  # 反序或超窗口
            host = req_evt.get("host", "")
            fp = _make_fingerprint("R1", task_id, host)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)
            alerts.append(Alert(
                severity=AlertSeverity.CRITICAL,
                rule="R1_data_exfiltration",
                description=f"敏感文件 {read_path} 读取后 {delta:.0f}s 内向 {host} 发送 {req_evt.get('method', '')} 请求",
                task_id=task_id,
                events=[read_evt, req_evt],
                fingerprint=fp,
            ))
    return alerts


def _rule_r2_high_frequency(task_id: str, events: list[dict]) -> list[Alert]:
    """R2 高频请求——同一外部 host 60s 内 > 20 次 allow。"""
    alerts: list[Alert] = []

    # 按 host 分组外部 allow 请求
    by_host: dict[str, list[dict]] = {}
    for evt in events:
        if evt.get("kind") != "network_decision" or evt.get("outcome") != "allow":
            continue
        host = evt.get("host", "")
        if host in _AlertConstants.HIGH_FREQ_EXCLUDE_HOSTS:
            continue
        by_host.setdefault(host, []).append(evt)

    for host, evts in by_host.items():
        if len(evts) < _AlertConstants.HIGH_FREQ_THRESHOLD:
            continue
        # 按时间排序
        evts.sort(key=lambda e: e.get("ts", ""))
        # 滑动窗口
        window = _AlertConstants.HIGH_FREQ_WINDOW_SECONDS
        threshold = _AlertConstants.HIGH_FREQ_THRESHOLD
        max_count = 0
        for i, evt in enumerate(evts):
            base_ts = _parse_ts(evt.get("ts", ""))
            if not base_ts:
                continue
            count = 1
            for j in range(i + 1, len(evts)):
                next_ts = _parse_ts(evts[j].get("ts", ""))
                if not next_ts:
                    continue
                if (next_ts - base_ts).total_seconds() <= window:
                    count += 1
                else:
                    break
            max_count = max(max_count, count)
        if max_count > threshold:
            fp = _make_fingerprint("R2", task_id, host)
            alerts.append(Alert(
                severity=AlertSeverity.HIGH,
                rule="R2_high_frequency_scan",
                description=f"host {host} 60s 内 {max_count} 次请求（阈值 {threshold}）",
                task_id=task_id,
                events=evts[:max_count],
                fingerprint=fp,
            ))
    return alerts


def _rule_r3_non_standard_port(task_id: str, events: list[dict]) -> list[Alert]:
    """R3 非标端口——外部连接非白名单端口。"""
    alerts: list[Alert] = []
    seen: set[str] = set()

    for evt in events:
        if evt.get("kind") != "network_decision" or evt.get("outcome") != "allow":
            continue
        host = evt.get("host", "")
        if host in _AlertConstants.PORT_CHECK_EXCLUDE_HOSTS:
            continue
        # 从 detail 提取端口
        detail = evt.get("detail", {})
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except Exception:
                detail = {}
        port = detail.get("destination_port") or detail.get("port") or _extract_port_from_path(evt.get("path", ""))
        if not port:
            continue
        try:
            port_num = int(port)
        except (ValueError, TypeError):
            continue
        if port_num in _AlertConstants.PORT_WHITELIST:
            continue
        fp = _make_fingerprint("R3", task_id, host, str(port_num))
        if fp in seen:
            continue
        seen.add(fp)
        alerts.append(Alert(
            severity=AlertSeverity.LOW,
            rule="R3_non_standard_port",
            description=f"外部连接 {host}:{port_num}（非标准端口）",
            task_id=task_id,
            events=[evt],
            fingerprint=fp,
        ))
    return alerts


def _rule_r4_command_guard_denied(task_id: str, events: list[dict]) -> list[Alert]:
    """R4 命令预检拦截——任何 COMMAND_GUARD/POLICY_DECISION deny → HIGH。

    R4 和 R6/R7/R8 互斥：如果 R6/R7/R8 匹配则不重复报 R4。
    """
    alerts: list[Alert] = []
    deny_evts = [e for e in events
                 if e.get("kind") in ("command_guard", "policy_decision")
                 and e.get("outcome") == "deny"
                 and _is_command_guard_event(e)]
    if not deny_evts:
        return alerts
    fp = _make_fingerprint("R4", task_id)
    alerts.append(Alert(
        severity=AlertSeverity.HIGH,
        rule="R4_command_guard_denied",
        description=f"命令预检拦截 {len(deny_evts)} 次危险命令",
        task_id=task_id,
        events=deny_evts,
        fingerprint=fp,
    ))
    return alerts


def _rule_r5_config_drift(task_id: str, events: list[dict]) -> list[Alert]:
    """R5 配置漂移——P1 默认关闭。"""
    if not _AlertConstants.CONFIG_DRIFT_ENABLED:
        return []
    alerts: list[Alert] = []
    # P2 实现：检查白名单允许 + 预检未拦截 curl 的组合
    # P1 空实现
    return alerts


def _rule_r6_critical_operation(task_id: str, events: list[dict]) -> list[Alert]:
    """R6 CRITICAL 操作——命令归一化后匹配 force push / DROP TABLE / rm -rf /。"""
    alerts: list[Alert] = []
    seen: set[str] = set()

    # deny 事件筛选：只看 command_guard 的 deny，不看 prompt_injection
    deny_evts = [e for e in events
                 if e.get("kind") in ("command_guard", "policy_decision")
                 and e.get("outcome") == "deny"
                 and _is_command_guard_event(e)]

    for evt in deny_evts:
        reason = _get_deny_reason(evt)
        if not reason:
            continue
        normalized = _normalize_command(reason)

        matched_pattern = None
        # 先检查 rm -rf / 变体（词元级判断）
        if _check_rm_rf_root(normalized):
            matched_pattern = "rm_rf_root"
        # 再检查正则模式
        if not matched_pattern:
            for pattern in _AlertConstants.CRITICAL_PATTERNS:
                if re.search(pattern, normalized, re.IGNORECASE):
                    matched_pattern = pattern
                    break

        if matched_pattern:
            fp = _make_fingerprint("R6", task_id, matched_pattern)
            if fp in seen:
                continue
            seen.add(fp)
            alerts.append(Alert(
                severity=AlertSeverity.CRITICAL,
                rule="R6_critical_operation",
                description=f"CRITICAL 操作拦截：{matched_pattern}（归一化命令：{normalized[:80]}）",
                task_id=task_id,
                events=[evt],
                fingerprint=fp,
            ))
    return alerts


def _rule_r7_high_risk_operation(task_id: str, events: list[dict]) -> list[Alert]:
    """R7 HIGH 风险操作——force delete branch / hard reset / TRUNCATE / rm .env。"""
    alerts: list[Alert] = []
    seen: set[str] = set()

    deny_evts = [e for e in events
                 if e.get("kind") in ("command_guard", "policy_decision")
                 and e.get("outcome") == "deny"
                 and _is_command_guard_event(e)]

    for evt in deny_evts:
        reason = _get_deny_reason(evt)
        if not reason:
            continue
        normalized = _normalize_command(reason)

        for pattern in _AlertConstants.HIGH_PATTERNS:
            if re.search(pattern, normalized, re.IGNORECASE):
                fp = _make_fingerprint("R7", task_id, pattern)
                if fp in seen:
                    continue
                seen.add(fp)
                alerts.append(Alert(
                    severity=AlertSeverity.HIGH,
                    rule="R7_high_risk_operation",
                    description=f"HIGH 风险操作拦截：{pattern}（归一化命令：{normalized[:80]}）",
                    task_id=task_id,
                    events=[evt],
                    fingerprint=fp,
                ))
                break
    return alerts


def _rule_r8_medium_risk_operation(task_id: str, events: list[dict]) -> list[Alert]:
    """R8 MEDIUM 风险操作——rollback / revert。"""
    alerts: list[Alert] = []
    seen: set[str] = set()

    deny_evts = [e for e in events
                 if e.get("kind") in ("command_guard", "policy_decision")
                 and e.get("outcome") == "deny"
                 and _is_command_guard_event(e)]

    for evt in deny_evts:
        reason = _get_deny_reason(evt)
        if not reason:
            continue
        normalized = _normalize_command(reason)

        for pattern in _AlertConstants.MEDIUM_PATTERNS:
            if re.search(pattern, normalized, re.IGNORECASE):
                fp = _make_fingerprint("R8", task_id, pattern)
                if fp in seen:
                    continue
                seen.add(fp)
                alerts.append(Alert(
                    severity=AlertSeverity.MEDIUM,
                    rule="R8_medium_risk_operation",
                    description=f"MEDIUM 风险操作：{pattern}（归一化命令：{normalized[:80]}）",
                    task_id=task_id,
                    events=[evt],
                    fingerprint=fp,
                ))
                break
    return alerts


# ===== 辅助函数 =====

def _parse_ts(ts_str: str):
    """解析 ISO 时间戳为 datetime 对象。"""
    if not ts_str:
        return None
    try:
        # 兼容带/不带时区后缀的 ISO 格式
        if ts_str.endswith('Z'):
            ts_str = ts_str[:-1] + '+00:00'
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def _extract_port_from_path(path: str) -> int | None:
    """从 CONNECT 请求的 path 中提取端口号。

    只匹配 CONNECT host:port 格式（如 "evil.com:4444"），
    不匹配 URL path 中的冒号数字（如 "/proxy?target=host:8080"）。
    """
    if not path:
        return None
    # CONNECT 请求的 path 格式是 host:port（无前导 /）
    if path.startswith('/'):
        return None  # URL path，不是 CONNECT
    m = re.match(r'^[\w.\-]+:(\d{2,5})$', path)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


# ===== AlertEngine =====

class AlertEngine:
    """审计行为序列分析 + 高风险操作告警。

    任务结束后扫描该 task_id 的完整审计序列，匹配规则生成 Alert。
    不改 P0 审计格式——只读审计记录，生成告警写回审计 JSONL。
    """

    def __init__(self):
        self._rules: list = [
            _rule_r1_data_exfiltration,
            _rule_r2_high_frequency,
            _rule_r3_non_standard_port,
            _rule_r5_config_drift,
            _rule_r6_critical_operation,
            _rule_r7_high_risk_operation,
            _rule_r8_medium_risk_operation,
        ]

    def analyze(self, task_id: str, events: list[dict]) -> list[Alert]:
        """扫描审计序列，返回去重后的告警列表。

        规则按优先级执行，R4 依赖 R6/R7/R8 的结果做互斥判断。
        """
        if not task_id or not events:
            return []

        all_alerts: list[Alert] = []

        # 先执行 R6/R7/R8（高风险分级）
        r6_alerts = _rule_r6_critical_operation(task_id, events)
        r7_alerts = _rule_r7_high_risk_operation(task_id, events)
        r8_alerts = _rule_r8_medium_risk_operation(task_id, events)
        all_alerts.extend(r6_alerts)
        all_alerts.extend(r7_alerts)
        all_alerts.extend(r8_alerts)

        # R4：如果有 R6/R7/R8 匹配，不重复报 R4
        has_r678 = bool(r6_alerts or r7_alerts or r8_alerts)
        if not has_r678:
            all_alerts.extend(_rule_r4_command_guard_denied(task_id, events))

        # 执行其他规则
        for rule_fn in self._rules:
            if rule_fn in (_rule_r6_critical_operation, _rule_r7_high_risk_operation,
                           _rule_r8_medium_risk_operation):
                continue  # 已执行
            all_alerts.extend(rule_fn(task_id, events))

        # 按 fingerprint 去重，保留最高 severity
        return _deduplicate_alerts(all_alerts)


def _deduplicate_alerts(alerts: list[Alert]) -> list[Alert]:
    """按 fingerprint 去重，同指纹保留最高 severity（按 weight 比较）。"""
    if not alerts:
        return []
    by_fp: dict[str, Alert] = {}
    for alert in alerts:
        existing = by_fp.get(alert.fingerprint)
        if existing is None:
            by_fp[alert.fingerprint] = alert
        else:
            if alert.severity.weight > existing.severity.weight:
                by_fp[alert.fingerprint] = alert
    return list(by_fp.values())


# ===== NullAlertEngine =====

class NullAlertEngine:
    """空告警引擎——完全实现，analyze 返回空列表。"""

    def analyze(self, task_id: str, events: list[dict]) -> list[Alert]:
        return []
