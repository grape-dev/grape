"""S77 用例（D52，先测后改）：IM 域最后残留——绑定 provider 写死 "gitcode"。

现场（D51 扫描"other"族）：handler_async 3 处 upsert_repo_im_binding
第二参硬编码 "gitcode"——gitee 仓 IM 绑定行 provider 错标（FR-21.2 家族
尾巴：读侧按 repo_key 派生，写侧折叠）。修法：provider 按 repo_key
前缀派生（裸键=gitcode 存量语义）。守卫沿用 S5 结构守卫先例。
"""
import re

import pytest


def test_s77_im_binding_provider_not_hardcoded():
    """IM 绑定调用点不得硬编码 provider 字面量（按 repo_key 前缀派生）。"""
    src = open("src/domains/runtime/im/handler_async.py", encoding="utf-8").read()
    # 精确匹配旧形态：provider 参数位置的字面量（repo_key 后紧跟 "gitcode"）；
    # `or "gitcode"` 裸键兜底默认值合法
    hits = [l.strip() for l in src.splitlines()
            if re.search(r'upsert_repo_im_binding\([^)]*repo_key,\s*"gitcode"', l)]
    assert not hits, f"IM 绑定仍硬编码 provider: {hits}"
    assert src.count("upsert_repo_im_binding(") >= 3  # 调用点存在（防守卫失效）


def test_s77_im_provider_derivation():
    """派生语义：带前缀取前缀、裸键=gitcode（存量语义）。"""
    from src.kernel.repo_key import parse_repo_key
    for rk, expect in (("gitee:acme/web", "gitee"),
                       ("gitcode:acme/web", "gitcode"),
                       ("github:acme/web", "github"),
                       ("acme/web", "gitcode")):
        got = parse_repo_key(rk)[0] or "gitcode"
        assert got == expect, f"{rk}: {got!r} != {expect!r}"
