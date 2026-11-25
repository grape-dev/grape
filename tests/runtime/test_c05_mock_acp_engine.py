# -*- coding: utf-8 -*-
"""C05/A1 终极验收 · mock ACP 引擎单测（L1 段，P0b）。

验收命题: 「descriptor + 1 JSON 零改动接入第四个 ACP 引擎」——
mock_acp_engine.py 是被测引擎本体（stdio NDJSON JSON-RPC），本套件锁其协议行为。
管道口径与生产同构（hermes_acp.py Popen: 二进制 PIPE、write_line 收 bytes）:

- T1 --version: 探活口径（probe.mode=cli_run 命令）退出 0 且有版本输出
- T2 initialize: 返回 protocolVersion=1
- T3 session/new: 返回 sessionId 非空
- T4 session/prompt: 恰好 3 条 agent_message_chunk 文本 + PromptResponse{stopReason:"end_turn"}
- T5 session/cancel: 有响应（非崩溃）
- T6 descriptor mock-acp.json: L0 schema 合法（registry 载入）且 probe.command 指向本脚本
- T7 --chaos: 注入一行非法 NDJSON 后仍收 PromptResponse（I2 失败注入钩子）

运行期步 1-6（建设备/探活/派任务/页面）见 e2e 用例 C05，依赖验证副本，不在本套件。
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_MOCK = _REPO / "scripts" / "mock_acp_engine.py"

sys.path.insert(0, str(_REPO))
from src.domains.runtime.agents.acp_client import _ACPSimpleClient  # noqa: E402


def _spawn(extra_args=()):
    # 与生产同构: 二进制 PIPE（hermes_acp.py bufsize=0）
    return subprocess.Popen(
        [sys.executable, str(_MOCK), *extra_args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0,
    )


def _is_valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False


def _start_reader(client, proc, lines=None):
    def _reader():
        for raw in proc.stdout:
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            if lines is not None:
                lines.append(s)
            try:
                client.handle_line(s)
            except Exception:
                pass  # 非法行/客户端守卫自有日志, 不杀 reader
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


@pytest.mark.skipif(not _MOCK.exists(), reason="scripts/mock_acp_engine.py 未交付（C05 前置）")
class TestMockAcpeEngine:

    def test_t1_version_probe(self):
        r = subprocess.run([sys.executable, str(_MOCK), "--version"],
                           capture_output=True, text=True, timeout=15)
        assert r.returncode == 0
        assert "mock-acp" in r.stdout

    def test_t2_t3_t4_full_prompt_chain(self):
        proc = _spawn()
        client = _ACPSimpleClient(stdin=proc.stdin, stdout=proc.stdout, label="mock-test")
        chunks: list[str] = []
        done: dict = {}
        ev = threading.Event()
        client.on_message = lambda t, c: chunks.append(c) if t == "text" else None
        client.on_prompt_done = lambda r: (done.update(r), ev.set())
        try:
            _start_reader(client, proc)

            init, err = client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-agent", "version": "0.1.0"},
                "clientCapabilities": {},
            }, timeout=15)
            assert err is None, f"initialize err: {err}"
            assert init and init.get("protocolVersion") == 1

            sess, err = client.request("session/new", {"cwd": str(_REPO), "mcpServers": []},
                                       timeout=15)
            assert err is None, f"session/new err: {err}"
            assert sess and sess.get("sessionId")

            client.set_prompt_req_id(client.next_id())
            # 与生产同构（hermes_acp.py 发 prompt 前手工注册 pending——
            # _handle_response 只对 _pending 中的 id 触发 prompt_done）
            prompt_evt = threading.Event()
            with client._lock:
                client._pending[client._prompt_req_id] = prompt_evt
            client.write_line((json.dumps({
                "jsonrpc": "2.0", "id": client._prompt_req_id, "method": "session/prompt",
                "params": {"sessionId": sess["sessionId"],
                           "prompt": [{"type": "text", "text": "hello from C05"}]},
            }) + "\n").encode("utf-8"))
            assert ev.wait(timeout=15), "PromptResponse 未到"
            texts = [c for c in chunks if c]
            assert len(texts) == 3, f"agent_message_chunk 应为 3 条, 实得: {texts}"
            assert any("chunk 1/3" in t for t in texts), f"应为 mock 固定文本: {texts}"
            assert done.get("stopReason") == "end_turn"
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_t5_session_cancel(self):
        proc = _spawn()
        client = _ACPSimpleClient(stdin=proc.stdin, stdout=proc.stdout, label="mock-test")
        try:
            _start_reader(client, proc)
            _, err = client.request("session/cancel", {"sessionId": "any"}, timeout=15)
            assert err is None, f"session/cancel err: {err}"
            assert proc.poll() is None, "cancel 后 mock 进程应存活"
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_t6_descriptor_schema(self):
        from src.domains.runtime.engine.descriptor import get_registry
        registry = get_registry()  # 默认 _DESCRIPTOR_DIR，含 mock-acp.json
        d = registry.get("mock-acp")
        assert d, "registry 未载入 mock-acp"
        assert d["protocol"] == "acp"
        assert d["probe"]["mode"] == "cli_run"
        cmd = d["probe"]["command"]
        assert "mock_acp_engine.py" in " ".join(cmd), f"probe.command 应指向 mock 脚本: {cmd}"

    def test_t8_backend_dispatch_stream(self):
        """薄子类经 executor.get_backend 分派（S1 契约面）+ stream_execute 全链事件流。"""
        from src.domains.runtime.agents.executor import get_backend
        backend = get_backend("mock-acp")
        assert callable(backend.acp_backend)
        assert backend.acp_backend() is backend
        events = list(backend.stream_execute(
            "hello from C05", cwd=str(_REPO), session_id="", env={},
            max_exec_seconds=60, agent_label="c05",
        ))
        kinds = [e[0] for e in events]
        assert kinds.count("text") == 3, f"应有 3 条 text, 实得事件: {kinds}"
        assert kinds[-1] == "done"
        done = events[-1][1]
        assert done["rc"] == 0 and done["stop_reason"] == "end_turn"
        assert "chunk 1/3" in done["text"], f"结果应为 mock 固定文本: {done['text']}"

    def test_t9_engines_meta_mounted_on_arch4p_mgmt(self):
        """arch-4p 缺口守卫（E2E C05 步 3 实测发现）: /api/engines/meta 只挂单体
        main.py，四进程 grape_server.py 未 mount → 前端 _ENGINE_META 恒空态。
        本条锁 mgmt app 必须暴露该路由。"""
        from src.api.grape_server import app
        from tests.runtime.test_issue_596_p1p2p3_three_process_split import _app_paths
        paths = _app_paths(app)
        assert "/api/engines/meta" in paths, \
            "arch-4p mgmt 未挂 /api/engines/meta（engines.router 缺席 grape_server）"

    def test_t7_chaos_emits_invalid_ndjson_then_recovers(self):
        """I2 失败注入钩子: --chaos 在 chunk 前注入一行非法 NDJSON；服务不崩且仍回 PromptResponse。"""
        proc = _spawn(["--chaos"])
        client = _ACPSimpleClient(stdin=proc.stdin, stdout=proc.stdout, label="mock-chaos")
        lines: list[str] = []
        ev = threading.Event()
        done: dict = {}
        client.on_prompt_done = lambda r: (done.update(r), ev.set())
        try:
            _start_reader(client, proc, lines)

            _, err = client.request("initialize", {"protocolVersion": 1}, timeout=15)
            assert err is None
            _, err = client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=15)
            assert err is None
            client.set_prompt_req_id(client.next_id())
            prompt_evt = threading.Event()
            with client._lock:
                client._pending[client._prompt_req_id] = prompt_evt
            client.write_line((json.dumps({
                "jsonrpc": "2.0", "id": client._prompt_req_id, "method": "session/prompt",
                "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "x"}]},
            }) + "\n").encode("utf-8"))
            assert ev.wait(timeout=15), "chaos 模式 PromptResponse 未到"
            assert done.get("stopReason") == "end_turn"
            bad = [l for l in lines if not _is_valid_json(l)]
            assert bad, "chaos 模式应注入至少一行非法 NDJSON"
            assert proc.poll() is None, "mock 进程应存活（chaos 行不致崩溃）"
        finally:
            proc.kill()
            proc.wait(timeout=5)
