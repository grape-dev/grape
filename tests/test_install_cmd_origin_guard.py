# -*- coding: utf-8 -*-
"""安装命令 --server 地址家族守卫（随 CI 跑的静态断言）。

背景（2026-10 实测发现）：Add Device 面板 doCreateCredentialInPanel 曾用裸
window.location.origin 拼接 worker 安装命令，host-heal 自愈的 server_base_url
没有传导到该面板——用户在 worker 机上执行时 curl 到 worker 自己的 localhost，
必然失败。devspace 凭据流 2026-10-15 已修过同一问题，但修复未做家族扫描。

本守卫禁止 runtimes.js 再出现「裸 origin 直接拼安装命令」的写法（必须先经
health.server_base_url / grape_server_base 缓存解析为 serverUrl 变量），
防止同族问题第三次复发。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNTIMES_JS = ROOT / "src" / "api" / "static" / "js" / "pages" / "runtimes.js"

# 禁止形态：origin 与安装端点路径的直接字符串拼接（模板串或 + 拼接）
_FORBIDDEN = re.compile(
    r"window\.location\.origin\s*\+\s*['\"`]/api/runtime/install"
)


def test_no_naked_origin_in_install_commands():
    """安装命令不得用裸 window.location.origin 拼接安装端点。"""
    src = RUNTIMES_JS.read_text(encoding="utf-8")
    m = _FORBIDDEN.search(src)
    assert not m, (
        "runtimes.js 第 %d 行用裸 window.location.origin 拼接安装命令——"
        "必须改用 health.server_base_url（localStorage 缓存 grape_server_base、"
        "回退 origin）解析出的 serverUrl，参考 doCreateCredentialInPanel。" % (
            src[: m.start()].count("\n") + 1,
        )
    )


def test_credential_panel_resolves_server_url():
    """doCreateCredentialInPanel 必须走 serverUrl 解析（防回退成裸 origin）。"""
    src = RUNTIMES_JS.read_text(encoding="utf-8")
    fn = src[src.index("function doCreateCredentialInPanel"):]
    fn = fn[: fn.index("\nfunction ")] if "\nfunction " in fn else fn
    assert "grape_server_base" in fn, "面板凭据流未读取 grape_server_base 缓存"
    assert "server_base_url" in fn, "面板凭据流未从 /health 取 server_base_url"
    assert "window.location.origin" in fn, "缺少 origin 回退路径（health 失败时兜底）"
