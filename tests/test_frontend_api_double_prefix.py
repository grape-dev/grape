"""前端回归锁：API.get/post 禁止带 /api 前缀（双前缀 404）。

实弹事故（2026-11-08）：D102-L 凭据页 `API.get('/api/settings/vcs/platforms')`
——API helper 的 BASE 已是 /api，双前缀请求 /api/api/... 404，
"第三方仓库凭据"卡永远停在"加载中"。本守卫全量扫描 static js。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "api" / "static" / "js"

# API.get('/api/... / API.post('/api/... 等调用形态
_BANNED = re.compile(r"API\.(get|post|put|del|delete|patch)\(\s*['\"]/api/")


def test_no_double_prefixed_api_calls():
    hits = []
    for f in STATIC.rglob("*.js"):
        if "vendor" in f.parts:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _BANNED.search(line):
                hits.append(f"{f.relative_to(STATIC.parent.parent)}:{i}: {line.strip()[:100]}")
    assert not hits, "API.xxx('/api/...') 双前缀调用（BASE 已含 /api）:\n" + "\n".join(hits)
