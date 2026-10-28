#!/usr/bin/env python3
"""重新生成前端弱密码黑名单（与后端 src/auth/password.py 同源）。

用法：.venv/bin/python scripts/gen-pwd-blocklist.py
后端 _WEAK_PASSWORDS 变更后运行一次，产物 src/api/static/js/pwd-blocklist.js。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.auth.password import _WEAK_PASSWORDS  # noqa: E402

words = sorted(_WEAK_PASSWORDS)
out = ROOT / "src/api/static/js/pwd-blocklist.js"
out.write_text(
    "// 自动生成：与后端 src/auth/password.py _WEAK_PASSWORDS 同源（后端更新后须重新生成，scripts/gen-pwd-blocklist.py）\n"
    "// 勿手改\n"
    "window.PWD_BLOCKLIST = new Set(" + json.dumps(words) + ");\n"
    "// 唯一谓词（复核 #3 收敛，2026-10-28）：所有前端密码入口（注册/改密/设密/忘记密码）\n"
    "// 统一消费本函数，不得在各调用点散落 PWD_BLOCKLIST.has 拷贝——判断逻辑单点定义，\n"
    "// 数据单源（本文件），消费面变更只需改调用点、名单变更只需重跑生成器。\n"
    "window.isWeakPassword = function (p) {\n"
    "  return !!(p && window.PWD_BLOCKLIST && window.PWD_BLOCKLIST.has(String(p).trim().toLowerCase()));\n"
    "};\n"
    "// 最短长度常量（C 家族收敛，2026-10-28）：与后端 check_password_strength 的下限同源，\n"
    "// 前端散落的 length<8 / length>=8 硬编码统一消费本常量——后端调下限时重跑生成器即全端同步。\n"
    "window.PWD_MIN_LEN = 8;\n"
    "window.isPwdTooShort = function (p) {\n"
    "  return !p || String(p).length < window.PWD_MIN_LEN;\n"
    "};\n",
    encoding="utf-8",
)
print(f"generated {out} ({len(words)} words)")
