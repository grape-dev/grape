"""Issue #2094（上游 #13）回归：引擎/设备身份收敛到 engine_host()，支持 GRAPE_HOSTNAME 覆盖。

容器化部署每次重建 hostname 都变 → 每部署一次多一条失联运行时记录（旧记录永不清除），
重部署后「本机直连」路由还可能命中旧记录。
"""
import re
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.kernel.host_identity import engine_host  # noqa: E402

# 直接调用 gethostname()/platform.node() 属正常语义的模块（真实 host 有业务意义）
ALLOW_DIRECT = {
    "src/kernel/host_identity.py",       # 兜底实现本身
    "src/kernel/config.py",              # 展示用途
    "src/kernel/lock.py",                # 分布式锁：需唯一
    "src/kernel/gsb/router.py",          # gsb 路由命名空间
    "src/domains/runtime/im/bot_lease.py",       # 飞书事件租约：需真实 host
    "src/domains/runtime/im/feishu_async.py",    # 租约心跳
    "src/api/patrol.py",                 # 巡检 key = hostname#pid
    "src/runtime/os_adapter.py",         # 平台探测
}


def test_engine_host_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("GRAPE_HOSTNAME", "grape-primary")
    assert engine_host() == "grape-primary"


def test_engine_host_strips_whitespace(monkeypatch):
    monkeypatch.setenv("GRAPE_HOSTNAME", "  grape-primary  ")
    assert engine_host() == "grape-primary"


def test_engine_host_falls_back_to_system_hostname(monkeypatch):
    monkeypatch.delenv("GRAPE_HOSTNAME", raising=False)
    assert engine_host() == (socket.gethostname() or "unknown")


def test_engine_host_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("GRAPE_HOSTNAME", "   ")
    assert engine_host() == (socket.gethostname() or "unknown")


def test_identity_callsites_use_engine_host():
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel in ALLOW_DIRECT:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\b(?:socket|_sock)\.gethostname\(\)", text):
            offenders.append(rel)
    assert not offenders, (
        "这些模块直接用 socket.gethostname() 取身份，容器重建会漂移；"
        "请改用 src.kernel.host_identity.engine_host()：\n  " + "\n  ".join(sorted(offenders))
    )


def test_engine_identity_site_list_converged():
    must = [
        "src/api/main.py",
        "src/api/server_tasks.py",
        "src/domains/insight/routes/dashboard.py",
        "src/domains/mgmt/routes/settings.py",
        "src/domains/runtime/engine/engine_select.py",
    ]
    missing = [rel for rel in must if "engine_host()" not in (ROOT / rel).read_text(encoding="utf-8", errors="replace")]
    assert not missing, f"这些身份/路由点未收敛到 engine_host()：{missing}"
