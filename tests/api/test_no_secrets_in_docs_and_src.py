"""设计/需求文档与源码里不得出现真实凭据（2026-11-15 用户要求）。

判据是"值像不像真东西"，不是"字段名像不像"：
- 已知前缀（ghp_ / glpat- / github_pat_ / ghp）直接命中；
- `*_token` / `*_secret` / `password` 等键的**值**若是 20+ 位随机串且不落在占位形态里，命中；
- 占位形态（`<...>`、CHANGE_ME、your_、xxx、${...}`、os.environ/getenv、example/dummy/test/mock）一律放行。

历史实锤：`docs/weekly-06/oauth-provider-plugin-design.md` 曾把线上 gitcode bot token 与 webhook
secret 原文写进设计稿（2026-11-15 已替换为 `<REDACTED_...>`），本守卫防回潮。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCAN_GLOBS = [
    ("docs/**/*.md",),
    ("src/**/*.py",),
    ("src/**/*.js",),
    ("src/**/*.vue",),
]
SKIP_PARTS = ("node_modules", "static-dist", "vendor", ".git")

# 键名里带这些词的值需要检查；值命中 REALISH 且不含 PLACEHOLDER 即判违规
# 注意不要用前缀 \b：`gitcode_api_token` 里 `_` 是词字符，\bapi 永远匹配不上
# （首版就是这么漏的，2026-11-15 自测打回）。
SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?token|bot[_-]?token|webhook[_-]?(?:token|secret)|client[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|password|passwd|secret[_-]?key)\b"
)
REALISH = re.compile(r"^[A-Za-z0-9_\-+/@]{16,}$")
PLACEHOLDER = re.compile(
    r"(?i)(<[^>]*>|CHANGE_ME|your[_-]|xxx|\.\.\.|\$\{|\{\{|os\.environ|getenv|"
    r"example|placeholder|dummy|test[_-]?token|mock|redacted|env\[|sample)"
)
KNOWN_PREFIX = re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_\-]{15,}|github_pat_[A-Za-z0-9_]{20,})")

# 形如 "key": "value" 或 key = "value"（含 f-string 前的普通字面量）
KV = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)[\"']?\s*[:=]\s*[\"'](?P<val>[^\"'\n]{8,})[\"']")


def _iter_files():
    for (glob,) in SCAN_GLOBS:
        for path in ROOT.glob(glob):
            if any(part in path.parts for part in SKIP_PARTS):
                continue
            yield path


def _violations(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("#", "//", "*")) and "password" not in line.lower():
            # 注释行仍可能藏 token，但绝大多数是说明文字；下面继续按前缀/键值判，不整行跳过
            pass
        for m in KNOWN_PREFIX.finditer(line):
            hits.append((lineno, "已知凭据前缀", m.group(0)[:12] + "…"))
        for m in KV.finditer(line):
            key, val = m.group("key"), m.group("val")
            if not SENSITIVE_KEY.search(key):
                continue
            if PLACEHOLDER.search(val) or PLACEHOLDER.search(key):
                continue
            if re.fullmatch(r"[a-z0-9_]+", val):
                # 纯小写 + 下划线 = meta 键名/配置键名（如 global_bot_gitcode_token），不是凭据值
                continue
            if REALISH.match(val):
                hits.append((lineno, f"疑似真实凭据（键 {key}）", val[:6] + "…"))
    return hits


def test_no_real_credentials_in_docs_or_src():
    """设计/需求文档与源码不得出现真实 token / secret / 口令原文。"""
    offenders = []
    for path in _iter_files():
        for lineno, why, sample in _violations(path):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno} {why} {sample}")
    assert not offenders, "发现疑似真实凭据：\n" + "\n".join(offenders[:20])