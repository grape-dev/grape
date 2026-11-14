"""前缀匹配纪律守卫（P2-6 扩散）：禁止用 `LIKE '<prefix>:%'` 做前缀过滤。

provider 命名允许下划线（`^[a-z][a-z0-9_-]{1,31}$`），而 `_` 是 SQL LIKE 的单字符
通配符 → `LIKE 'a_b:%'` 会同时命中 `aXb:…`，在"按平台删缓存/查影响面"这类路径上
静默删掉别的平台的数据。2026-11-14 评审 P2-6 实锤后，全仓已统一为
`substr(col, 1, ?) = ?` 前缀相等。

本守卫禁止源码里再出现 `:%"` 形态的前缀参数（关键词检索的 LIKE 不受影响）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 允许出现的位置：mysql_compat 里对"前缀匹配约定"的文档说明
ALLOW = {"src/kernel/mysql_compat.py"}

# 基线自带的两处 meta 命名空间前缀（arch-weekly-11 原文）：ns 取自 _META_NAMESPACES
# 白名单常量，不是用户/平台卡输入，也没有下划线歧义；基线为准不回改，按条数登记。
EXEMPT_NS_LIKE = {"ns + \":%\"": 2}
PATTERN = re.compile(r':%[%"]')

# 盲区（2026-11-14 复核补）：上面 PATTERN 只拦"参数形态" `:%"`，拦不住**内嵌进 SQL 文本**
# 的 f-string 前缀 —— `LIKE '{provider}:%'` 里 `:%` 后面是引号而不是 % 或 "，正则命中不了。
# 因此另立一条：禁止出现"带占位符的前缀 LIKE"（r"LIKE '{...}:%'" 形态）。
# 唯一豁免 = mgmt/user/db.py::_path_b_prefix_clause（只读查询；provider 来自平台卡枚举、
# 非用户输入；且默认平台需匹配"裸键"（NOT LIKE '%:%'）的存量语义，无法机械换成 substr）。
PATTERN_EMBEDDED = re.compile(r"LIKE\s+'\{[^}]*\}[^']*:%'", re.I)
EXEMPT_EMBEDDED = {"src/domains/mgmt/user/db.py": 2}  # _path_b_prefix_clause 两条 return


def test_no_like_prefix_with_provider_wildcard():
    hits = []
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOW or "__pycache__" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if PATTERN.search(line):
                if any(sn in line for sn in EXEMPT_NS_LIKE):
                    continue
                hits.append(f"{rel}:{i} {line.strip()[:100]}")
    assert not hits, "又出现 LIKE 前缀通配（改用 substr(col,1,?)=?）：\n" + "\n".join(hits)


def test_substr_prefix_is_the_documented_way():
    """存量改写点必须按"文件 × 片段 × 次数"全量在册（防回退、也防漏登）。

    2026-11-14 复核：全仓同一形状共 8 处（db.py 2 / grape_auth.py 4 / settings.py 1 /
    onboarding.py 1）。早先版本只对 4 个文件各断言"某串存在一次"，grape_auth.py 里
    使用 `substr(repo_key, 1, ?)` 的两个点（964 / 1051）回退成 LIKE 时守卫仍绿。
    新增前缀点必须同步登记到本表（次数用 == 而非 >=，多一处也要登记）。
    """
    registry = {
        # 更新 2026-11-16（rebased onto arch-weekly-11）：update_managed_repo_bot_token 的
        # 前缀过滤随基线归位搬到 insight 域（同口径 substr），本文件只剩 save_user_repos。
        "src/domains/mgmt/user/db.py": {
            "substr(repo_key, 1, ?)": 1,          # save_user_repos
        },
        "src/domains/insight/public/__init__.py": {
            "substr(repo_key, 1, ?)": 1,          # repo_cred_repos_by_value 具名查询
        },
        "src/domains/mgmt/routes/grape_auth.py": {
            "substr(m.repo_key, 1, ?)": 2,        # 954 / 1022 解绑影响面
            "substr(repo_key, 1, ?)": 2,          # 964 路径 B 列表 / 1051 解绑清理
        },
        "src/domains/mgmt/routes/settings.py": {
            "substr(repo_key, 1, ?)": 1,          # 2634 _cascade_pat_clear
        },
        # onboarding 实现随基线迁入 runtime 域（src/api/onboarding.py 只剩 alias）
        "src/domains/runtime/onboarding.py": {
            "substr(`key`, 1, ?)": 1,             # cleanup_stale_repo_data
        },
    }
    problems = []
    for rel, needles in registry.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for needle, expected in needles.items():
            got = text.count(needle)
            if got != expected:
                problems.append(f"{rel}: {needle!r} 期望 {expected} 处，实际 {got} 处")
    assert not problems, (
        "前缀过滤点数量与登记表不符（回退成 LIKE 会少、新增点未登记会多）：\n" + "\n".join(problems)
    )


def test_no_embedded_like_prefix_with_interpolated_provider():
    """内嵌 f-string 的前缀 LIKE 也必须禁止（守卫正则的已知盲区）。

    豁免只有 _path_b_prefix_clause 的两条 return；数量用 == 登记——改动即红，
    逼人回到这里重新论证，避免"豁免"变成永久免检区。
    """
    hits = {}
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if "__pycache__" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if PATTERN_EMBEDDED.search(line):
                hits.setdefault(rel, []).append(f"{rel}:{i}")
    got = {k: len(v) for k, v in hits.items()}
    assert got == EXEMPT_EMBEDDED, (
        "内嵌 f-string 的前缀 LIKE 数量与豁免登记不符（新增点必须改用 substr(col,1,?)=?；"
        "若确需豁免，在此登记并写明只读/枚举来源的理由）：\n"
        + "\n".join(f"{k}: {v}" for k, v in hits.items())
    )
