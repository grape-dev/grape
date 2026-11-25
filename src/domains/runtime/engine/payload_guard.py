"""Autopilot 指令载荷审查与日志脱敏（Issue #3818）。

生产实测：同一份凭据外传脚本被 autopilot 定时任务每分钟派发，1 小时 66 次，
连续 88+ 轮无阻断；`prompt built` 打点又把整段 prompt（含 OBS 预签名 query、
内嵌 root 口令）以 INFO 写进 Loki。本模块只做纯文本处理、不碰 DB：

1. ``normalize`` —— 归一化（两轮 URL 解码、去零宽、空白折叠、大小写折叠），
   让「URL 编码 / 换行拆分 / 大小写混写」的变体与原串命中同一套规则；
2. ``scan`` —— 归一化文本上的规则集，返回 ``Finding`` 列表（每条只有规则名与
   说明，**不携带命中原文**，避免审查日志变成新的泄漏面）；
3. ``redact`` —— 兜底脱敏（预签名 query、口令/token、AK/SK、Bearer、私钥），
   给仍需输出片段的路径使用；打点主路径只输出元信息（``metadata_for_log``）。

规则姿态（Spec「关键设计决策」）：高危组合硬拒（``hard``），单点信号只告警
（``soft``）——合法运维脚本里出现一次 OBS 上传或一次预签名下载不该直接误杀。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote

SEVERITY_HARD = "hard"
SEVERITY_SOFT = "soft"

KIND_PRESIGNED_URL = "presigned_url"
KIND_REMOTE_FETCH_EXEC = "remote_fetch_exec"
KIND_INLINE_CREDENTIAL = "inline_credential"
KIND_PUBLIC_SSH_NO_HOSTKEY = "public_ssh_no_hostkey"
KIND_SENSITIVE_PATH_EXFIL = "sensitive_path_exfil"
KIND_PACK_AND_UPLOAD = "pack_and_upload"
KIND_PRIVATE_KEY = "private_key"

_KIND_LABELS = {
    KIND_PRESIGNED_URL: "预签名 URL 外传链接",
    KIND_REMOTE_FETCH_EXEC: "远程取脚本后直接执行",
    KIND_INLINE_CREDENTIAL: "内联口令/token",
    KIND_PUBLIC_SSH_NO_HOSTKEY: "公网主机 SSH 且跳过 host key 校验",
    KIND_SENSITIVE_PATH_EXFIL: "读取平台敏感目录后上传",
    KIND_PACK_AND_UPLOAD: "打包敏感文件后上传",
    KIND_PRIVATE_KEY: "内联私钥",
}


@dataclass(frozen=True)
class Finding:
    """单条审查命中：只有规则名与可公开说明，不含命中原文。"""

    kind: str
    severity: str
    detail: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "severity": self.severity, "detail": self.detail}


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028\u2029\u2060\ufeff]")
_WS_RE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """载荷归一化：两轮 URL 解码 + 去零宽 + 空白折叠 + 小写。

    两轮解码覆盖 ``%2528`` 这类双重编码；空白折叠让「按行拆分拼接」的变体
    （``curl ...`` 换行后 ``| sh``）落回同一行判定。
    """
    s = str(text or "")
    s = _ZERO_WIDTH_RE.sub("", s)
    for _ in range(2):
        decoded = unquote(s)
        if decoded == s:
            break
        s = decoded
    s = s.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
    s = _WS_RE.sub(" ", s)
    return s.strip().lower()


# ── 规则 ────────────────────────────────────────────────────────────────────

_PRESIGNED_RE = re.compile(
    r"(x-amz-signature|x-amz-credential|x-amz-security-token"
    r"|ossaccesskeyid|accesskeyid)"
    r"|(?:[?&]signature=)[^&\s]+[^ ]*(?:[?&]expires=|\bexpires\b)"
)
_REMOTE_FETCH_EXEC_RE = re.compile(
    r"(?:^|[\s;&|(])(?:curl|wget|fetch|aria2c)\b[^|]{0,400}\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b"
    r"|(?:^|[\s;&|(])(?:curl|wget)\b[^|]{0,400}\|\s*(?:sudo\s+)?(?:env\s+)?(?:python3?|perl|node)\b"
    r"|\b(?:ba|z|da)?sh\s+<\s*\(\s*(?:curl|wget)\b"
    r"|\beval\s+[\"']?\$\(\s*(?:curl|wget)\b"
    r"|\b(?:gzip\s+-d?c?|tar\s+-x[a-z]*|base64\s+(?:-d|--decode|-[Dd]))[^|]{0,200}\|\s*(?:sudo\s+)?(?:python3?|sh|bash|node)\b"
)
_INLINE_CREDENTIAL_RE = re.compile(
    r"(?:password|passwd|passphrase|pwd|secret|token|access[_-]?key|secret[_-]?key"
    r"|access[_-]?token|api[_-]?key|ak|sk)\s*[:=]\s*"
    r"[\"']?(?P<val>[^\s\"'`;|,)]{4,})"
)
_INLINE_SECRET_EXTRA_RE = re.compile(
    r"sshpass\s+-p\s*(?P<p1>\S{3,})"
    r"|(?:\b[a-z0-9_]{2,}):(?P<p2>[^\s:@/]{6,})@[a-z0-9.-]+"
    r"|\bmysql\b[^\n]{0,80}\s-p(?P<p3>[^\s-]\S{4,})"
)
_SSH_CMD_RE = re.compile(r"(?:^|[\s;&|(])(?:ssh|scp|sshpass|sftp)\b")
_SSH_NO_HOSTKEY_RE = re.compile(
    r"stricthostkeychecking\s*[= ]\s*(?:no|off|false|accept-new)"
    r"|userknownhostsfile\s*[= ]\s*(\/dev\/null|none)"
    r"|-o\s*stricthostkeychecking\s*=\s*no"
)
_PUBLIC_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SENSITIVE_PATH_RE = re.compile(
    r"(/etc/grape|/root/\.dsh|/root/\.ssh/id_[a-z0-9]+"
    r"|(?<![\w.-])\.env(?![\w-])|id_rsa|id_ed25519"
    r"|credentials?(?![\w-])|\.kube/config)"
)
_EGRESS_RE = re.compile(
    r"\bcurl\b[^|]{0,200}(?:-t\b|--upload-file|-f\s+@|--data-binary|-d\s+@|\s-x\s*post\b)"
    r"|\b(?:scp|rsync)\b"
    r"|\bobsutil\b|\bobs\s+cp\b|\baws\s+s3\b|\bs3cmd\b"
    r"|\b(?:requests|urllib3?)\.(?:post|put)\b|\bfiles\s*=\s*\{"
)
_PACK_RE = re.compile(
    r"\btar\s+-?[a-z]*c[a-z]*\b|\bzip\s+-r\b|\b7z\s+a\b|\bgzip\s+"
)
_PRIVATE_KEY_RE = re.compile(r"-----begin (?:rsa |openssh |ec |dsa |pgp )?private key-----")

_PLACEHOLDER_HINTS = ("${", "{{", "$(", "$_", "os.environ", "process.env", "getenv",
                      "xxx", "***", "******", "<", ">", "your_", "example", "changeme",
                      "redacted")


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    return any(h in low for h in _PLACEHOLDER_HINTS)


def _public_ipv4_present(text: str) -> bool:
    """文本中是否存在**公网** IPv4（私有段/回环/链路本地不算）。"""
    for ip in _PUBLIC_IPV4_RE.findall(text):
        _parts = ip.split(".")
        if not all(p.isdigit() and len(p) <= 3 for p in _parts):
            continue
        nums = [int(p) for p in _parts]
        if any(n > 255 for n in nums):
            continue
        a, b = nums[0], nums[1]
        if a in (0, 10, 127):
            continue
        if a == 172 and 16 <= b <= 31:
            continue
        if a == 192 and b == 168:
            continue
        if a == 169 and b == 254:
            continue
        if a == 100 and 64 <= b <= 127:
            continue
        return True
    return False


def scan(text: str | None) -> list[Finding]:
    """对指令/prompt 做静态载荷审查，返回命中列表（无原文，可安全写日志）。"""
    norm = normalize(text)
    if not norm:
        return []
    findings: list[Finding] = []

    def add(kind: str, severity: str) -> None:
        if any(f.kind == kind for f in findings):
            return
        findings.append(Finding(kind=kind, severity=severity,
                                detail=_KIND_LABELS.get(kind, kind)))

    if _PRIVATE_KEY_RE.search(norm):
        add(KIND_PRIVATE_KEY, SEVERITY_HARD)

    has_presigned = bool(_PRESIGNED_RE.search(norm))
    has_remote_exec = bool(_REMOTE_FETCH_EXEC_RE.search(norm))
    has_egress = bool(_EGRESS_RE.search(norm))
    has_pack = bool(_PACK_RE.search(norm))

    if has_presigned:
        # 预签名链接本身可能出现在合法运维任务里；「预签名 + 取回即执行/上传」才是外传链。
        add(KIND_PRESIGNED_URL,
            SEVERITY_HARD if (has_remote_exec or has_egress) else SEVERITY_SOFT)
    if has_remote_exec:
        add(KIND_REMOTE_FETCH_EXEC, SEVERITY_HARD)

    cred_hit = False
    for m in _INLINE_CREDENTIAL_RE.finditer(norm):
        if not _is_placeholder(m.group("val")):
            cred_hit = True
            break
    if not cred_hit:
        for m in _INLINE_SECRET_EXTRA_RE.finditer(norm):
            _v = m.group("p1") or m.group("p2") or m.group("p3") or ""
            if _v and not _is_placeholder(_v):
                cred_hit = True
                break
    if cred_hit:
        add(KIND_INLINE_CREDENTIAL, SEVERITY_HARD)

    if _SSH_CMD_RE.search(norm) and _SSH_NO_HOSTKEY_RE.search(norm) and _public_ipv4_present(norm):
        add(KIND_PUBLIC_SSH_NO_HOSTKEY, SEVERITY_HARD)

    if _SENSITIVE_PATH_RE.search(norm) and (has_egress or has_pack):
        add(KIND_SENSITIVE_PATH_EXFIL, SEVERITY_HARD)
    if has_pack and has_egress:
        add(KIND_PACK_AND_UPLOAD, SEVERITY_HARD)

    return findings


def is_blocked(text: str | None) -> bool:
    """命中任一 hard 规则 = 阻断（入库拒绝 / 派发拒绝）。"""
    return any(f.severity == SEVERITY_HARD for f in scan(text))


def blocked_kinds(text: str | None) -> list[str]:
    """阻断规则名列表（稳定 code，用于拒绝出参与审计，不含原文）。"""
    return [f.kind for f in scan(text) if f.severity == SEVERITY_HARD]


def blocked_reason(text: str | None) -> str:
    """阻断原因 code（逗号分隔的规则名）；未阻断返回空串。"""
    return ",".join(blocked_kinds(text))


_AUTOPILOT_INSTRUCTION_RE = re.compile(
    r"任务指令:\s*\n(.*?)\n{2,}(?=#|请执行以上任务指令)", re.S)


def instruction_from_autopilot_prompt(prompt: str | None) -> str:
    """从 autopilot prompt 里反抽「任务指令」原文（worker 侧熔断键与调度侧对齐）。

    调度侧以 instruction 的归一化 hash 记账；worker 只拿到拼好的 prompt。两侧必须
    落在同一行，否则「拒绝 → 重试」的熔断永远命不中。抽不到（非 autopilot prompt /
    格式漂移）返回空串，调用方回落到 prompt 自身 hash。
    """
    m = _AUTOPILOT_INSTRUCTION_RE.search(str(prompt or ""))
    return (m.group(1).strip() if m else "")


def payload_hash(text: str | None) -> str:
    """归一化载荷的 SHA-256（重复投递熔断键；抗空白/编码/大小写改写）。"""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def payload_hash_prefix(text: str | None, n: int = 12) -> str:
    """载荷 hash 前缀（日志元信息用，无法反推原文）。"""
    return payload_hash(text)[: max(1, int(n))]


# ── 脱敏（兜底）────────────────────────────────────────────────────────────

_REDACT_KV_RE = re.compile(
    r"(?P<key>password|passwd|passphrase|pwd|secret|token|access[_-]?key"
    r"|secret[_-]?key|access[_-]?token|api[_-]?key|client[_-]?secret)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s\"'`;|,&]{3,})",
    re.IGNORECASE,
)
_REDACT_QUERY_RE = re.compile(
    r"(?P<key>[?&](?:x-amz-signature|x-amz-credential|x-amz-security-token"
    r"|accesskeyid|ossaccesskeyid|signature|security-token|expires)=)[^&\s\"']+",
    re.IGNORECASE,
)
_REDACT_BEARER_RE = re.compile(r"(?P<pre>\bbearer\s+)[a-z0-9._\-]{8,}", re.IGNORECASE)
_REDACT_AKSK_RE = re.compile(r"\b(?P<pre>ak|sk)[a-z0-9]{16,}\b", re.IGNORECASE)
_REDACT_PRIVATE_KEY_RE = re.compile(
    r"-----begin [^-]*private key-----.*?-----end [^-]*private key-----",
    re.IGNORECASE | re.DOTALL,
)
_REDACT_SSHPASS_RE = re.compile(r"(?P<pre>sshpass\s+-p\s*)[^\s]+", re.IGNORECASE)
_REDACTED = "***REDACTED***"


def redact(text: str | None) -> str:
    """掩码凭据后返回文本：预签名 query、口令/token、AK/SK、Bearer、私钥。"""
    s = str(text or "")
    if not s:
        return ""
    s = _REDACT_PRIVATE_KEY_RE.sub(f"-----BEGIN PRIVATE KEY-----{_REDACTED}", s)
    s = _REDACT_QUERY_RE.sub(lambda m: m.group("key") + _REDACTED, s)
    s = _REDACT_KV_RE.sub(lambda m: m.group("key") + m.group("sep") + _REDACTED, s)
    s = _REDACT_BEARER_RE.sub(lambda m: m.group("pre") + _REDACTED, s)
    s = _REDACT_AKSK_RE.sub(lambda m: m.group("pre") + _REDACTED, s)
    s = _REDACT_SSHPASS_RE.sub(lambda m: m.group("pre") + _REDACTED, s)
    return s


def metadata_for_log(text: str | None, **extra) -> str:
    """打点元信息：长度 + 载荷 hash 前缀 + 附加键值，**不含任何原文**。"""
    body = f"chars={len(str(text or ''))} payload_hash={payload_hash_prefix(text)}"
    for key, value in extra.items():
        body += f" {key}={value}"
    return body
