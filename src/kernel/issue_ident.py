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