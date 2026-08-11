"""Worker 一键安装脚本端点测试（worker-install-optimization-plan.md 方案 B）。

GET /api/runtime/install 返回 install-worker.sh 内容（只读、无敏感信息，token 由调用方注入）。
"""
import os
import pytest


def test_install_endpoint_returns_script():
    from fastapi.testclient import TestClient
    from src.api.main import app
    client = TestClient(app)
    r = client.get("/api/runtime/install")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    body = r.text
    # 应含脚本关键内容，且不含真实 token（脚本用占位参数）
    assert "install-worker" in body
    assert "--token" in body
    assert "runtime-agent.service" in body
    assert "RUNTIME_SERVER_URL" in body
    # 脚本不应硬编码任何真实 rta_ token（RUNTIME_TOKEN 用 $TOKEN 变量引用，token 由调用方传入）
    assert "RUNTIME_TOKEN=$TOKEN" in body


def test_install_script_exists_on_disk():
    """脚本文件应存在于仓库 scripts/ 目录。"""
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "install-worker.sh")
    assert os.path.isfile(p), "scripts/install-worker.sh 应存在"
    content = open(p, encoding="utf-8").read()
    # 核心要素
    assert "git sparse-checkout" in content
    assert "python3.12" in content or "python3.11" in content
    assert "EnvironmentFile" in content
