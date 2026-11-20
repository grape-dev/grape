"""RedactEngine——敏感数据检测 + 令牌化引擎。

P1-8 输出侧防护：Agent 输出中不含敏感数据。

两层互补：
1. 精确值 redact — secret_values 列表中的值精确替换为 [REDACTED]
2. 正则模式令牌化 — 10 类敏感数据格式匹配替换为 [REDACTED]

设计原则：
- 精确值先跑，正则在精确值替换后的文本上兜底。精确值是高置信度、已知的 secret，
  优先消耗掉，避免正则片段替换导致精确值漏匹配。
- 占位符统一 [REDACTED]（不带类型，P2 再加 [REDACTED:type]）
- 正则编译在模块加载时 fail-fast（re.compile 放类定义层）。一个 typo 会让整个模块
  import 失败，所有依赖方全崩——这是有意的，配 smoke test 保障。
- 运行时处理异常 → fail-open + log.error + error_count counter，保留原文。
  模块加载期编译失败 → fail-fast（服务起不来）。
- redact 幂等：[REDACTED] 不匹配任何正则，再跑一遍不会变。

输入边界：
- str：正常处理
- bytes：decode(errors="replace") 后处理。不做 str(bytes)（会把明文包在 b'...' 里泄漏）
- None：返回空 RedactResult(text="") + log.warning
- 其他类型（dict/list/对象）：不做隐式 str()，log.warning + 返回 str(x) 但标记可能不完整
  调用方应自行 json.dumps 后再 redact

secret_values 生命周期：
- 构造后完全不可变（tuple 快照）。无 add_secret()。
- 任务内发现新 secret → 重建 RedactEngine 实例。
- 任务结束后调用方应调 clear() 清除引用，不依赖 GC。
- clear() 后 _secret_values 置空，后续 redact 走空列表（只做正则兜底）。

返回值契约：
- redact(text) → RedactResult(text=str, redacted_count=int, matches=list[dict])
- text：处理后的字符串（永远 str，不会 None/bytes）
- redacted_count：替换次数（精确值 + 正则）
- matches：匹配详情 list[{type, confidence, count}]
  - type：模式名（openai_key/aws_ak/private_key/exact_value 等）
  - confidence：high/medium/low（pattern 置信度，非 AlertSeverity）
  - count：该模式匹配次数
  - 不含 preview/原文片段——避免审计落盘泄漏

集成点（详见设计文档 issue-156-p1-8-redact-design.md）：
- executor.py _redact() 内部委托 RedactEngine（向后兼容签名不变）
- executor.py _stream_hermes_acp() 的 _redact_text() 改用 RedactEngine
- 覆盖 text/reasoning/tool_use/tool_result/done/stderr/error 全部事件类型
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

log = logging.getLogger("devops-bot")

_PLACEHOLDER = "[REDACTED]"
_MAX_INPUT_LEN = 1_000_000  # 1MB 截断防 ReDoS


# ===== 检测模式（模块加载时编译，fail-fast） =====

class _SensitivePatterns:
    """10 类敏感数据正则模式。编译失败 = 开发错误，模块加载时 fail-fast。

    正则锚定边界（前缀/\\b），避免长变量名/base64/hash 误匹配。
    SSH 路径降为 low——路径不是密钥本身。
    """

    # 1. OpenAI API Key（sk- 前缀 + 20+ 字母数字）
    OPENAI_KEY: re.Pattern = re.compile(r'sk-[a-zA-Z0-9]{20,}')
    # 2. GitCode PAT（已知前缀格式兜底，精确值 redact 为主）
    GITCODE_PAT: re.Pattern = re.compile(r'\b[A-Z]{4}_\d_[A-Za-z0-9_]{18,}_[A-Za-z0-9_]{16,}\b')
    # 3. AWS Access Key（AKIA 前缀 + 16 字符）
    AWS_AK: re.Pattern = re.compile(r'\bAKIA[0-9A-Z]{16}\b')
    # 4. AWS Secret Key（40 字符 base64，需上下文锚定）
    AWS_SK: re.Pattern = re.compile(
        r'(?i)aws_secret(?:_access)?_key["\s:=]+([A-Za-z0-9/+=]{40})')
    # 5. 华为云 AK（HPUA 前缀 + 16-20 字符）
    HUAWEI_AK: re.Pattern = re.compile(r'\bHPUA[A-Z0-9]{16,20}\b')
    # 6. 私钥 PEM 头
    PRIVATE_KEY: re.Pattern = re.compile(
        r'-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY-----')
    # 7. JWT（三段式 base64url）
    JWT: re.Pattern = re.compile(
        r'\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b')
    # 8. 数据库连接串（协议://user:pass@host）
    DB_CONN: re.Pattern = re.compile(
        r'\b(?:postgres|postgresql|mysql|mongodb|redis)://[^\s:]+:[^\s@]+@[^\s]+')
    # 9. 通用密码模式（password=value）
    PASSWORD: re.Pattern = re.compile(
        r'(?i)(?:password|passwd|pwd)["\s:=]+(\S{8,})')
    # 10. SSH 路径（low——路径不是密钥本身）
    SSH_PATH: re.Pattern = re.compile(r'(?:~/\.ssh|/home/[^\s]+/\.ssh)/[^\s]+')


# 模式 → 类型名映射（有序，先匹配的先替换）
_PATTERN_MAP: list[tuple[re.Pattern, str, str]] = [
    (_SensitivePatterns.PRIVATE_KEY, "private_key", "high"),
    (_SensitivePatterns.OPENAI_KEY, "openai_key", "high"),
    (_SensitivePatterns.AWS_AK, "aws_ak", "high"),
    (_SensitivePatterns.AWS_SK, "aws_sk", "high"),
    (_SensitivePatterns.HUAWEI_AK, "huawei_ak", "high"),
    (_SensitivePatterns.GITCODE_PAT, "gitcode_pat", "high"),
    (_SensitivePatterns.JWT, "jwt", "medium"),
    (_SensitivePatterns.DB_CONN, "db_connection", "high"),
    (_SensitivePatterns.PASSWORD, "password", "medium"),
    (_SensitivePatterns.SSH_PATH, "ssh_path", "low"),
]


# ===== RedactResult =====

@dataclass
class RedactResult:
    """令牌化结果。

    Attributes:
        text: 处理后的字符串（永远 str，不会 None/bytes）
        redacted_count: 替换次数（精确值 + 正则）
        matches: 匹配详情 list[{type, confidence, count}]
            不含 preview/原文片段——避免审计落盘泄漏
    """
    text: str
    redacted_count: int = 0
    matches: list[dict] = field(default_factory=list)


# ===== RedactEngine =====

class RedactEngine:
    """敏感数据检测 + 令牌化引擎。

    两层互补：
    1. 精确值 redact — secret_values 列表中的值精确替换为 [REDACTED]
    2. 正则模式令牌化 — 10 类敏感数据格式匹配替换为 [REDACTED]

    顺序：精确值先跑，正则在精确值替换后的文本上兜底。
    精确值是高置信度、已知的 secret，优先消耗掉，避免正则片段替换导致精确值漏匹配。

    fail 策略：
    - 模块加载期编译失败 → fail-fast（服务起不来）
    - 运行时处理异常 → fail-open + log.error + error_count counter，保留原文

    secret_values：构造后完全不可变（tuple）。无 add_secret()。
    clear() 后置空，后续 redact 走空列表（只做正则兜底）。
    不依赖 GC——调用方负责在任务结束后调 clear()。

    幂等性：[REDACTED] 不匹配任何正则，再跑一遍不会变。
    输入边界：str 正常处理；bytes 走 decode(errors="replace")；
    None 返回空结果 + log.warning；其他类型 log.warning + str() 兜底。
    """

    def __init__(self, secret_values: Sequence[str] | None = None):
        # 构造后完全不可变——tuple 快照
        self._secret_values: tuple[str, ...] = tuple(
            v for v in (secret_values or []) if v and len(v) >= 4)
        self._error_count = 0
        self._closed = False

    def redact(self, text: str) -> RedactResult:
        """两层令牌化：精确值 + 正则模式。fail-open。

        Args:
            text: 待脱敏的字符串。
                str → 正常处理
                bytes → decode(errors="replace") 后处理
                None → 返回空 RedactResult + log.warning
                其他 → log.warning + str() 兜底（可能不完整，调用方应自行 json.dumps）

        Returns:
            RedactResult: text=处理后的字符串, redacted_count=替换次数, matches=匹配详情
        """
        # 输入边界处理
        if text is None:
            log.warning("redact received None input, returning empty")
            return RedactResult(text="")
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8", errors="replace")
            except Exception as e:
                self._error_count += 1
                log.error(f"redact bytes decode failed (fail-open): {e}")
                return RedactResult(text="")
        elif not isinstance(text, str):
            log.warning(f"redact received non-str input type={type(text).__name__}, "
                        f"caller should json.dumps first")
            text = str(text)
        if not text:
            return RedactResult(text="")

        # 输入长度截断防 ReDoS
        if len(text) > _MAX_INPUT_LEN:
            log.warning(f"redact input truncated: {len(text)} > {_MAX_INPUT_LEN}")
            text = text[:_MAX_INPUT_LEN]

        result_text = text
        matches: list[dict] = []

        # 第一层：精确值替换（先跑——高置信度已知 secret 优先消耗）
        # clear() 后 _secret_values 为空 tuple，跳过
        secrets = self._secret_values if not self._closed else ()
        for secret in secrets:
            if secret and secret in result_text:
                count = result_text.count(secret)
                result_text = result_text.replace(secret, _PLACEHOLDER)
                matches.append({
                    "type": "exact_value",
                    "confidence": "high",
                    "count": count,
                })

        # 第二层：正则模式令牌化（后跑——在精确值替换后的文本上兜底）
        for pattern, type_name, confidence in _PATTERN_MAP:
            try:
                found = pattern.findall(result_text)
                if found:
                    count = len(found) if isinstance(found, list) else 1
                    result_text = pattern.sub(_PLACEHOLDER, result_text)
                    matches.append({
                        "type": type_name,
                        "confidence": confidence,
                        "count": count,
                    })
            except TypeError as e:
                self._error_count += 1
                log.error(
                    f"redact TypeError (fail-closed): pattern={type_name} "
                    f"input_len={len(text)} error={e}")
                # fail-closed：异常时脱敏整个文本段，防止未匹配的敏感数据泄漏
                result_text = _PLACEHOLDER
            except Exception as e:
                self._error_count += 1
                log.error(
                    f"redact error (fail-closed): pattern={type_name} "
                    f"input_len={len(text)} error={e}")
                result_text = _PLACEHOLDER

        return RedactResult(
            text=result_text,
            redacted_count=len(matches),
            matches=matches,
        )

    def clear(self) -> None:
        """清除 secret_values 引用。调用方在任务结束后调用。

        clear() 后 _secret_values 置空，后续 redact 走空列表（只做正则兜底）。
        不依赖 GC——这是确定性的清除。
        """
        self._closed = True
        self._secret_values = ()

    @property
    def error_count(self) -> int:
        """运行时错误计数（供监控/审计聚合读取）。"""
        return self._error_count


# ===== NullRedactEngine =====

class NullRedactEngine:
    """空令牌化引擎——redact 返回原文，不做任何替换。

    默认值，生产环境显式注入 RedactEngine。
    """

    def redact(self, text: str) -> RedactResult:
        if text is None:
            return RedactResult(text="")
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8", errors="replace")
            except Exception:
                return RedactResult(text="")
        elif not isinstance(text, str):
            text = str(text)
        return RedactResult(text=text)

    def clear(self) -> None:
        pass

    @property
    def error_count(self) -> int:
        return 0
