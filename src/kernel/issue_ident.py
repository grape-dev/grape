"""Issue 编号（ident）内核唯一入口。

Issue #3055：历史上"issue 号"存在三套判据（`isdigit` 门禁、`_is_native_issue_ident`、
零散的 `int()` 强转），字符串 ident 平台（gitee `IK*` 等）在任一处都会掉队：
入口 400 / cancelled 检查跳过 / 评论指令 no-op / 等态不恢复 / 关单不关任务。
本模块提供**唯一**判据与强转口径，业务代码一律走这里，禁止再自行 `isdigit` /
`int(issue_id)`。

编号形态（与历史 `_norm_issue_id` / `_is_native_issue_ident` 完全同源，向后兼容）：
- 纯数字：`30`、`#30`、` 30 ` → 平台原生 int（数字平台契约 #2072：数字编号恒 int）
- 平台 ident：`IKW28R`、`#IKW28R`、`I1J8U4` → 原样 str（大写字母开头，`[A-Z][A-Z0-9]*`）
- 其余（空、`0` 占位、`pr_123` 订阅伪 id、全小写、超长等）→ 非合法 ident
"""

import re

# 平台 ident 形态：大写字母开头，后续大写字母/数字（gitee `I1J8U4` 第二位常是数字；
# 旧文法 `[A-Z]{2,}[A-Z0-9]*` 会把真实单号判非法，比基线的 isalnum() 更窄 —— Issue #3055 QA）
_IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*")

# 长度上限：平台 ident 实际远小于此；超长视为垃圾输入（A2A 入口曾据此拒绝）
MAX_IDENT_LEN = 32

# Issue #4065：可回帖口径里，允许「平台原生字符串 ident」的 provider。
# gitee 的评论路由是 `/issues/{ident}/notes`，字符串 ident（IK*）本来就是合法路径；
# gitcode/github/gitlab 的 `/issues/{n}/comments` 只认数字，非数字恒 400「参数类型错误」。
STRING_IDENT_PROVIDERS = ("gitee",)

# Issue #4065：卡片主键（issue 表 issue_number 列是 TEXT 亲和）允许的合成键前缀。
# autopilot 定时任务的统计回链键就是 `autopilot-<字母>`（scheduler._autopilot_issue_ref），
# 它是 issue 表的真实主键 —— 判成非法等于把卡片刷新链一起打死。
SYNTHETIC_CARD_PREFIX = "autopilot-"

# Issue #4065：订阅伪 id（`pr_123`、`#pr_123_scene_id`）永不对应真实 Issue，也永不对应卡片。
PSEUDO_IDENT_PREFIXES = ("pr_", "pr-")

# Issue #3908：autopilot 任务不使用 events 表，agent_tasks.event_seq 用高位命名空间
# （900_000_000 + autopilot_executions.id）隔离。判据下沉 kernel 单点，scheduler 与
# event_pipeline 共用，避免字面量漂移；event_pipeline 不能反向 import scheduler（成环）。
AUTOPILOT_EVENT_SEQ_BASE = 900_000_000

# autopilot 任务统计回链合成键形如 `autopilot-<纯字母>`（scheduler._autopilot_issue_ref），
# 永不对应真实 Issue。与 SYNTHETIC_CARD_PREFIX 同源，禁止在业务层另写正则。
_AUTOPILOT_ISSUE_REF_RE = re.compile(re.escape(SYNTHETIC_CARD_PREFIX) + r"[a-z]+")


def is_autopilot_event_seq(value) -> bool:
    """事件序号是否落在 autopilot 高位合成命名空间（Issue #3908）。

    安全 int 化：None/空串/非法形态一律 False，避免异常影响真实任务路径。
    """
    try:
        return int(value or 0) >= AUTOPILOT_EVENT_SEQ_BASE
    except (TypeError, ValueError):
        return False


def is_autopilot_issue_ref(value) -> bool:
    """是否为 autopilot 合成 issue 回链键 `autopilot-<纯字母>`（Issue #3908）。"""
    if value is None:
        return False
    t = str(value).strip().lstrip("#").strip()
    return bool(_AUTOPILOT_ISSUE_REF_RE.fullmatch(t))

# 卡片主键字符域：数字 / 字母开头的紧凑标识（拒空白、分隔文本、URL 片段）
_CARD_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")


def normalize_issue_ident(value) -> str:
    """去首尾空白与全部前导 `#`，返回归一文本；非法形态返回 ""。

    不做大小写转换（ident 大小写敏感），不剥内部字符。
    """
    if value is None:
        return ""
    t = str(value).strip().lstrip("#").strip()
    if not t or len(t) > MAX_IDENT_LEN:
        return ""
    if t.isdigit():
        return t
    if _IDENT_RE.fullmatch(t):
        return t
    return ""


def is_native_issue_ident(value) -> bool:
    """是否为平台原生 issue 编号（纯数字或字符串 ident）。

    `#` 前缀容忍；空值、`pr_*` 订阅伪 id、含文本/空格/超长形态一律为假。
    """
    return normalize_issue_ident(value) != ""


def native_issue_number(value):
    """issue number 保持平台原生形态：数字 → int；ident → str。

    与历史 `vcs/api_client._native_issue_number` 同语义（D41/S61）。
    非法形态原样转 str 返回，交由调用方按 `is_native_issue_ident` 先行把关。
    """
    norm = normalize_issue_ident(value)
    if not norm:
        return "" if value is None else str(value)
    return int(norm) if norm.isdigit() else norm


def is_actionable_issue_number(value) -> bool:
    """合法且可执行的 issue 编号：ident 合法且不是 `0` 占位号。

    替代历史的 `isinstance(x, int) and x > 0` / `.isdigit()` 组合判据。
    """
    norm = normalize_issue_ident(value)
    if not norm:
        return False
    return norm != "0"


def deliverable_comment_ident(value, provider: str = "") -> str:
    """可回帖的 issue ident 归一（Issue #4065 唯一口径）。

    结果评论投递链路只认「平台能接的编号」：

    - 数字平台（gitcode/github/gitlab）：`/issues/{n}/comments` 只认数字，
      非数字 ident（`autopilot-as`、`IKX1DL`、`pr_123`）恒 400「参数类型错误」；
    - gitee：评论路由是 `/issues/{ident}/notes`，平台原生字符串 ident（`IK*`）合法。

    返回可投递的归一文本；不可投递（伪 id / 垃圾 / 空 / `0` 占位）返回 ""。
    调用方拿到 "" 必须短路，不得发起任何远程请求（#4065 契约）。
    """
    norm = normalize_issue_ident(value)
    if not norm or norm == "0":
        return ""
    if norm.isdigit():
        return norm
    if (provider or "").strip().lower() in STRING_IDENT_PROVIDERS:
        return norm
    return ""


def normalize_card_ident(value) -> str:
    """卡片主键（issue 表 issue_number）归一（Issue #4065）。

    可归一：纯数字、平台原生大写 ident（gitee `IK*`）、`autopilot-*` 合成键
    （issue 表 TEXT 列的真实主键，定时任务统计回链键）。
    不可归一（返回 ""）：`pr_*`/`pr-*` 订阅伪 id、空值、含空白/分隔文本、超长。

    与评论口径分离的原因：`autopilot-*` 能查卡片但不能回帖；`IK*` 在 gitee 两者皆可。
    """
    if value is None:
        return ""
    t = str(value).strip().lstrip("#").strip()
    if not t or len(t) > MAX_IDENT_LEN:
        return ""
    low = t.lower()
    if low.startswith(PSEUDO_IDENT_PREFIXES):
        return ""
    if t.isdigit():
        return t
    if not _CARD_IDENT_RE.fullmatch(t):
        return ""
    if low.startswith(SYNTHETIC_CARD_PREFIX) or _IDENT_RE.fullmatch(t):
        return t
    return ""


def card_ident_number(value):
    """卡片主键的平台原生形态（Issue #4065）：数字 → int，ident/合成键 → str，不可归一 → None。

    `_issue_num_or_ident` 等既有路径依赖「数字恒 int」（#995 契约），不得改成一律 str。
    """
    norm = normalize_card_ident(value)
    if not norm:
        return None
    return int(norm) if norm.isdigit() else norm


def is_valid_issue_number_input(value) -> bool:
    """线上 JSON 字段形态（A2A issue_number）：ident 判据 + 输入卫生。

    - ident 语义（纯数字 / 平台字符串 ident、长度与字符域）**只**由
      `is_native_issue_ident` 判定，本函数不复制第二判据；
    - 额外做线上形态卫生（2026-10-14 事故防线）：A2A 字段应裸传编号，
      `#` 前缀 / 首尾或内部空白 / 分隔符文本 / 超长一律按垃圾拒绝；
    - 与 `is_native_issue_ident` 的差异仅在「容忍 `#`」这一层，用于事件
      payload 内部形态（那里 `#` 前缀是历史合法回声），不得用于入口字段。
    """
    if value is None:
        return False
    s = str(value)
    if not s or s != s.strip() or "#" in s:
        return False
    if any(ch.isspace() for ch in s):
        return False
    return is_native_issue_ident(s)