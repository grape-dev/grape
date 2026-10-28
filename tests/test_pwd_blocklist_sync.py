# -*- coding: utf-8 -*-
"""弱密码黑名单前后端同步对账（随 CI 跑的机制性校验）。

背景：前端 src/api/static/js/pwd-blocklist.js 由 scripts/gen-pwd-blocklist.py
从后端 src/auth/password.py _WEAK_PASSWORDS 生成。两份真源靠纪律维持会漂移
（后端改了黑名单忘记重新生成 → 前端三条件全绿、后端悄悄拒绝，错位回归）。
本测试把"纪律"变成"机制"：现场按生成器同款逻辑生成期望内容，与磁盘产物
逐字节比对，漂移即红。
"""
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PRODUCT = ROOT / "src" / "api" / "static" / "js" / "pwd-blocklist.js"
REGISTER_HTML = ROOT / "src" / "api" / "static" / "register.html"

HEADER = (
    "// 自动生成：与后端 src/auth/password.py _WEAK_PASSWORDS 同源（后端更新后须重新生成，scripts/gen-pwd-blocklist.py）\n"
    "// 勿手改\n"
)


def _expected_content() -> str:
    from src.auth.password import _WEAK_PASSWORDS
    words = sorted(_WEAK_PASSWORDS)
    return (
        HEADER
        + "window.PWD_BLOCKLIST = new Set(" + json.dumps(words) + ");\n"
        # 谓词尾部与生成器模板逐字节一致（复核 #3 收敛，2026-10-28）——
        # 生成器 scripts/gen-pwd-blocklist.py 变更模板时须同步本函数
        + "// 唯一谓词（复核 #3 收敛，2026-10-28）：所有前端密码入口（注册/改密/设密/忘记密码）\n"
        + "// 统一消费本函数，不得在各调用点散落 PWD_BLOCKLIST.has 拷贝——判断逻辑单点定义，\n"
        + "// 数据单源（本文件），消费面变更只需改调用点、名单变更只需重跑生成器。\n"
        + "window.isWeakPassword = function (p) {\n"
        + "  return !!(p && window.PWD_BLOCKLIST && window.PWD_BLOCKLIST.has(String(p).trim().toLowerCase()));\n"
        + "};\n"
        # C 家族收敛尾部（2026-10-28）：最短长度常量 + 长度谓词，与生成器模板逐字节一致
        + "// 最短长度常量（C 家族收敛，2026-10-28）：与后端 check_password_strength 的下限同源，\n"
        + "// 前端散落的 length<8 / length>=8 硬编码统一消费本常量——后端调下限时重跑生成器即全端同步。\n"
        + "window.PWD_MIN_LEN = 8;\n"
        + "window.isPwdTooShort = function (p) {\n"
        + "  return !p || String(p).length < window.PWD_MIN_LEN;\n"
        + "};\n"
    )


def test_pwd_blocklist_js_in_sync_with_backend():
    """产物内容必须与 _WEAK_PASSWORDS 现场生成结果逐字节一致。

    失败时的处置：运行 scripts/gen-pwd-blocklist.py 重新生成，并同步
    bump register.html 中该文件的 ?v= 缓存版本号。
    """
    assert PRODUCT.exists(), "pwd-blocklist.js 缺失：运行 scripts/gen-pwd-blocklist.py 生成"
    actual = PRODUCT.read_text(encoding="utf-8")
    expected = _expected_content()
    assert actual == expected, (
        "pwd-blocklist.js 与后端 _WEAK_PASSWORDS 不同步：请运行 "
        "scripts/gen-pwd-blocklist.py 重新生成，并 bump register.html 的 ?v="
    )


def test_register_html_references_blocklist_without_inline_copy():
    """register.html 必须引用产物文件（defer 外部加载），且不得内联第二份黑名单。"""
    html = REGISTER_HTML.read_text(encoding="utf-8")
    assert "pwd-blocklist.js" in html, "register.html 未引用 pwd-blocklist.js"
    inline = re.search(r"PWD_BLOCKLIST\s*=\s*new Set\s*\[", html)
    assert not inline, "register.html 内联定义了 PWD_BLOCKLIST（第二真源），必须移除"
