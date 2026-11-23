# -*- coding: utf-8 -*-
"""Mock ACP 后端 — C05/A1 终极验收被测引擎（P0b）。

设计（executor.py _stream_acp docstring 认定的接入形态）:
新接入 ACP 引擎 = 薄 backend 子类（含 quirk）+ _BACKENDS 一行注册，
零 executor 分派核心改动。本文件即该「薄子类」的最小示范——不含任何
引擎名判定散点，启动命令/展示元数据全部在 engine/descriptors/mock-acp.json。

被测体: scripts/mock_acp_engine.py（stdio NDJSON JSON-RPC，协议口径与
hermes_acp 实测同构）。无凭据、CI 可复跑；失败注入 --chaos 钩子供 C12-I2。

与本文件配套的单测: tests/runtime/test_c05_mock_acp_engine.py（L1 段）。
运行期验收步 1-6 见 docs e2e 用例 C05。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Generator

from src.domains.runtime.agents.acp_client import _ACPSimpleClient

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_SCRIPT = _REPO_ROOT / "scripts" / "mock_acp_engine.py"


class MockACPBackend:
    """mock-acp ACP 后端 — 不走 build_args/parse_output，stream_execute 直 yield 事件。"""

    name = "mock-acp"
    binary = "mock-acp"  # 不落 PATH，启动走 _DEFAULT_SCRIPT（可用 MOCK_ACP_SCRIPT 覆盖）
    prompt_via_stdin = False  # ACP 模式不用 stdin 传 prompt，用 JSON-RPC

    # [P0a A3] ACP 能力钩子：executor.stream_agent 按 callable(acp_backend) 分派
    # 统一 _stream_acp。本 backend 自身就是 ACP 后端 → 返回 self。
    def acp_backend(self):
        return self

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        # ACP 模式不用 CLI 参数传 prompt；保留接口以兼容 AgentRuntime 契约
        return ["--version"]

    def prepare_env(self, env: dict, cwd: str = "") -> str | None:
        # mock 引擎无凭据穿透需求（CI 验收基线即无凭据可跑）
        return None

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        # mock 引擎不消费任何凭据/模型 key
        return {}

    def stream_execute(
        self, prompt: str, cwd: str, session_id: str = "", env: dict | None = None,
        max_exec_seconds: int = 300, agent_label: str = "",
    ) -> Generator:
        """起 mock_acp_engine.py 子进程走完整 ACP 链，yield text chunks + done。"""
        label = f"{agent_label} " if agent_label else ""
        script = os.environ.get("MOCK_ACP_SCRIPT") or str(_DEFAULT_SCRIPT)
        proc_env = {k: v for k, v in (env or {}).items() if k not in ("stdin",)}

        proc = subprocess.Popen(
            [sys.executable, script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd or None,
            env=proc_env or None, bufsize=0,
        )
        client = _ACPSimpleClient(stdin=proc.stdin, stdout=proc.stdout, label="mock-acp")
        texts: list[str] = []
        done: dict = {}
        prompt_done = threading.Event()

        client.on_message = lambda t, c: texts.append(c) if t == "text" else None
        client.on_prompt_done = lambda r: (done.update(r), prompt_done.set())

        def _reader():
            out = proc.stdout
            assert out is not None
            for raw in out:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    client.handle_line(line)
                except Exception:
                    pass  # --chaos 非法行等: 客户端不崩（I2）

        reader = threading.Thread(target=_reader, daemon=True, name="mock-acp-reader")
        reader.start()

        try:
            init, err = client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-agent", "version": "0.1.0"},
                "clientCapabilities": {},
            }, timeout=30)
            if err:
                yield ("done", {"rc": -1, "session_id": session_id,
                                "text": "", "stderr": f"initialize failed: {err}",
                                "errors": [err]})
                return

            sess, err = client.request("session/new",
                                       {"cwd": cwd or os.getcwd(), "mcpServers": []},
                                       timeout=30)
            if err or not (sess or {}).get("sessionId"):
                yield ("done", {"rc": -1, "session_id": session_id,
                                "text": "", "stderr": f"session/new failed: {err}",
                                "errors": [err or "no sessionId"]})
                return
            new_session = (sess or {}).get("sessionId") or ""

            # 与 hermes_acp 生产同构: prompt 前手工注册 pending（_handle_response
            # 只对 _pending 中的 id 触发 on_prompt_done）
            client.set_prompt_req_id(client.next_id())
            prompt_evt = threading.Event()
            with client._lock:
                client._pending[client._prompt_req_id] = prompt_evt
            client.write_line((json.dumps({
                "jsonrpc": "2.0", "id": client._prompt_req_id,
                "method": "session/prompt",
                "params": {"sessionId": new_session,
                           "prompt": [{"type": "text", "text": prompt}]},
            }) + "\n").encode("utf-8"))

            if not prompt_done.wait(timeout=max_exec_seconds):
                yield ("done", {"rc": -1, "session_id": new_session,
                                "text": "\n".join(texts), "stderr": "prompt timeout",
                                "errors": ["prompt timeout"]})
                return

            for chunk in texts:
                yield ("text", chunk)
            stop_reason = done.get("stopReason", "")
            rc = 0 if stop_reason in ("end_turn", "") else 1
            yield ("done", {"rc": rc, "session_id": new_session,
                            "text": "\n".join(texts), "stderr": "",
                            "stop_reason": stop_reason, "errors": []})
        finally:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
