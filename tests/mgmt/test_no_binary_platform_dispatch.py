"""二元平台枚举的棘轮守卫（2026-11-14 「深度检查」后加的机检）。

背景：这一批实锤了同一类缺陷反复出现——「非 github 即 gitcode」的二分把 gitee/gitlab
静默归错平台，症状各不相同（仓库分组错、托管打错平台 API、链接指向错的域名、迁移写错
前缀、webhook 预检恒 unsupported）。

本守卫只锁**已经证明会造成错误行为的写法**，不追求穷尽所有平台字面量：

前端（shell/js + app/src）
  1. `x ? 'github:' : 'gitcode:'` / `'gitcode:' + ...`（前缀二分）
  2. `? 'github.com' : 'gitcode.com'`（域名二分）
  3. `startsWith('github:')` / `indexOf('github:') === 0`（分组二分）
  4. `{ gitcode: ... github: ... }` 名表（显示名要平台目录驱动）

后端（src/，排除 provider 模块与测试）
  5. `not in ("gitcode", "github")`（能力/预检白名单）

白名单只放「目录真源」与历史注释所需的最小集合；新增命中必须先改写法再进来。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SHELL = ROOT / "src/api/frontend/shell"
APPSRC = ROOT / "src/api/frontend/app/src"
SRC = ROOT / "src"

# 前端：文件 → 允许的规则编号（目录真源/渲染层允许持有映射）
FRONTEND_ALLOW = {
    "src/api/frontend/shell/js/platform-icons.js": {"1", "2", "3", "4"},   # 平台目录真源（host/name/color/icon）
    "src/api/frontend/app/src/lib/repo-provider.js": {"1", "2"},           # 注释里记录历史写法
}

FRONTEND_RULES = [
    ("1", re.compile(r"\?\s*'github:'\s*:\s*'gitcode:'|\?\s*'gitcode:'\s*:\s*'github:'|'gitcode:'\s*\+")),
    ("2", re.compile(r"\?\s*'github\.com'\s*:\s*'gitcode\.com'")),
    ("3", re.compile(r"(startsWith|indexOf)\(\s*'github:'")),
    ("4", re.compile(r"\{\s*gitcode:\s*'")),
    # 平台名的二元三目（`=== 'github' ? 'GitHub' : 'GitCode'`）——显示名必须走 platformLabel
    ("6", re.compile(r"===\s*'github'\s*\?\s*'GitHub'|===\s*'gitcode'\s*\?\s*'GitCode'")),
    # 「未绑定」判定只看 OAuth 身份行（bindings），PAT 接入会被误判成未绑定
    ("7", re.compile(r"bindings\s*(\|\|\s*\[\s*\])?\s*\.length\s*===?\s*0|\(\s*_?b(indings|u)?\.bindings\s*\|\|\s*\[\s*\]\s*\)\.length\s*===?\s*0")),
]

# 后端：允许的「能力声明」用法（provider 模块本身、以及 webhook 分派里 gitcode/github 有独立路由）
BACKEND_ALLOW: dict[str, set[str]] = {}
BACKEND_RULES = [
    ("5", re.compile(r"not in \(\s*\"gitcode\",\s*\"github\"\s*\)")),
]


def _iter_files(base: Path, suffixes):
    for p in base.rglob("*"):
        if p.is_file() and p.suffix in suffixes and "__pycache__" not in p.parts:
            yield p


def _scan(files, allow, rules):
    hits = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        allowed = allow.get(rel, set())
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("//") or line.strip().startswith("#"):
                continue
            for rid, pat in rules:
                if rid in allowed:
                    continue
                if pat.search(line):
                    hits.append(f"{rel}:{lineno} [{rid}] {line.strip()[:110]}")
    return hits


def test_no_binary_platform_dispatch_in_frontend():
    files = list(_iter_files(SHELL / "js", {".js"})) + list(_iter_files(APPSRC, {".js", ".vue"}))
    hits = _scan(files, FRONTEND_ALLOW, FRONTEND_RULES)
    assert not hits, "前端又出现平台二分写法（改用 platformLabel/platformHost/repoKeyOf）：\n" + "\n".join(hits)


def test_no_provider_whitelist_in_backend():
    files = [p for p in _iter_files(SRC, {".py"})
             if "/providers/" not in p.as_posix() and "/tests/" not in p.as_posix()]
    hits = _scan(files, BACKEND_ALLOW, BACKEND_RULES)
    assert not hits, "后端又出现平台白名单（改按平台卡在册判定）：\n" + "\n".join(hits)