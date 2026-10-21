"""Codex Runtime 实现（OpenAI Codex CLI 执行引擎）。

调用形态（codex exec 非交互模式）：
    codex exec --json --sandbox danger-full-access <prompt>

--json: 输出 JSONL 事件流（每行一个 JSON 对象，含 type 字段）
--sandbox danger-full-access: 允许 agent 读写工作区文件并访问网络

    workspace-write 模式默认禁止网络，导致 agent 调 GitCode API / git fetch
    时被拦截（CODEX_SANDBOX_NETWORK_DISABLED=1），任务失败。改用
    danger-full-access 放开网络限制。后续通过独立 issue 以网络白名单
    策略收紧（方案 B/C）。

输出协议（JSONL 事件流，见 codex-rs/exec/src/exec_events.rs）：
  顶层事件 type：
    - thread.started    — 线程启动（含 thread_id，映射 step_start）
    - turn.started      — 对话轮次启动（映射 step_start）
    - turn.completed    — 轮次完成（含 usage token 统计，映射 step_finish）
    - turn.failed       — 轮次失败（含 error.message，映射 error）
    - item.started      — item 开始（item.type 决定具体类型）
    - item.updated      — item 更新
    - item.completed    — item 完成
    - error             — 致命错误（含 message，映射 error）

  item.type（snake_case）：
    - agent_message      — agent 文本回复（text）→ text 事件
    - reasoning          — agent 推理摘要（text）→ reasoning 事件
    - command_execution  — 命令执行（command/aggregated_output/exit_code/status）
                           started → tool_use；completed → tool_result
    - file_change        — 文件变更（changes/status）
                           completed → tool_use (apply_patch)
    - mcp_tool_call      — MCP 工具调用（server/tool/arguments/result/error/status）
                           started → tool_use；completed → tool_result
    - web_search         — 网页搜索
    - todo_list          — 待办列表
    - error              — item 级错误

session 续接：codex 用 `exec resume <thread_id>` 而非 `--session`，首期不做 resume。
"""
import json
import os

from .base import AgentRuntime


class CodexRuntime(AgentRuntime):
    name = "codex"
    binary = "codex"
    # issue #351: prompt 经 stdin 传入（codex exec 以 "-" 为位置参数时从 stdin 读取），
    # 规避 Windows .cmd 壳把 argv 中的换行符当作命令分隔符截断多行提示词。
    prompt_via_stdin = True

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        """构建 codex exec 参数。

        codex exec --json --sandbox danger-full-access -
        --sandbox danger-full-access: 允许读写工作区 + 网络访问（对齐 opencode 自动执行模式）
        "-": prompt 从 stdin 读取（issue #351，规避 argv 多行截断）
        session_id 暂不启用——codex resume 与 opencode --session 形态不同，首期不做。
        """
        args = ["exec", "--json", "--sandbox", "danger-full-access", "--skip-git-repo-check"]
        if prompt:
            args.append("-")  # stdin 占位标记
        return args

    def parse_output(self, line: str) -> dict | None:
        """解析 Codex JSONL 事件流，映射到 grape 统一事件格式。

        Codex --json 每行输出一个 JSON 事件对象（含 type 字段），
        按 type 分发到 grape 统一事件：text/tool_use/tool_result/step_finish/error 等。
        未知事件类型返回 {"type": "other", "raw": ...}，不中断流。
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None

        et = event.get("type", "")

        # ── 顶层事件 ──
        if et == "thread.started":
            thread_id = event.get("thread_id", "")
            return {"type": "step_start", "sessionID": thread_id}

        if et == "turn.started":
            return {"type": "step_start", "sessionID": ""}

        if et == "turn.completed":
            usage = event.get("usage", {}) or {}
            total = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                     + usage.get("reasoning_output_tokens", 0))
            # 可观察性：除 total（前端沿用）外，把 token 拆分一并带出，供 LLM 用量台账采集
            # （codex 没有落盘账本，只能从事件流拿）。见 observability-llm-usage-design.md §3.1
            split = {
                "input": int(usage.get("input_tokens", 0) or 0),
                "output": int(usage.get("output_tokens", 0) or 0),
                "reasoning": int(usage.get("reasoning_output_tokens", 0) or 0),
                "cache_read": int(usage.get("cached_input_tokens", 0) or 0),
            }
            return {"type": "step_finish", "tokens": total, "usage": split}

        if et == "turn.failed":
            err = event.get("error", {}).get("message", str(event))
            return {"type": "error", "message": err}

        if et == "error":
            msg = event.get("message", str(event))
            return {"type": "error", "message": msg}

        # ── item 事件（started/updated/completed 共用 item 解析）──
        if et in ("item.started", "item.updated", "item.completed"):
            item = event.get("item", {}) or {}
            return self._parse_item(item, et)

        # 未知顶层事件
        return {"type": "other", "raw": et}

    def _parse_item(self, item: dict, event_type: str) -> dict | None:
        """解析 item 对象，按 item.type 映射到 grape 统一事件。

        event_type 为 item.started/item.updated/item.completed，用于区分
        tool_use（started）与 tool_result（completed）。
        """
        item_type = item.get("type", "")
        is_completed = event_type == "item.completed"

        if item_type == "agent_message":
            text = item.get("text", "")
            if text:
                return {"type": "text", "text": text, "sessionID": ""}
            return None

        if item_type == "reasoning":
            text = item.get("text", "")
            if text:
                return {"type": "reasoning", "text": text, "sessionID": ""}
            return None

        if item_type == "command_execution":
            command = item.get("command", "")
            if is_completed:
                output = item.get("aggregated_output", "")
                exit_code = item.get("exit_code")
                text = output
                if exit_code is not None:
                    text = f"{output}\n[exit_code={exit_code}]" if output else f"[exit_code={exit_code}]"
                return {"type": "tool_result", "text": text, "sessionID": ""}
            if command:
                return {"type": "tool_use", "tool": "shell",
                        "input": {"command": command}, "sessionID": ""}
            return None

        if item_type == "mcp_tool_call":
            tool_name = item.get("tool", "")
            server = item.get("server", "")
            arguments = item.get("arguments", {}) or {}
            if is_completed:
                result = item.get("result", {}) or {}
                error = item.get("error", {}) or {}
                if error and error.get("message"):
                    return {"type": "tool_result",
                            "text": f"Error: {error['message']}", "sessionID": ""}
                content = result.get("content", [])
                if isinstance(content, list) and content:
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(c["text"])
                    if parts:
                        return {"type": "tool_result", "text": "\n".join(parts),
                                "sessionID": ""}
                return {"type": "tool_result", "text": str(result), "sessionID": ""}
            if tool_name:
                full_tool = f"{server}.{tool_name}" if server else tool_name
                return {"type": "tool_use", "tool": full_tool,
                        "input": arguments, "sessionID": ""}
            return None

        if item_type == "file_change":
            changes = item.get("changes", []) or []
            if is_completed and changes:
                return {"type": "tool_use", "tool": "apply_patch",
                        "input": {"changes": changes},
                        "sessionID": ""}
            return None

        if item_type == "web_search":
            query = item.get("query", "")
            if query and is_completed:
                return {"type": "text", "text": f"[web_search] {query}",
                        "sessionID": ""}
            return None

        if item_type == "todo_list":
            return None

        if item_type == "error":
            msg = item.get("message", str(item))
            return {"type": "error", "message": msg}

        # 未知 item 类型
        return {"type": "other", "raw": f"{event_type}:{item_type}"}

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        """注入 codex 凭据环境变量。

        codex CLI 优先读 CODEX_API_KEY，回退 OPENAI_API_KEY。
        凭据由部署方通过用户云凭据（provider=openai 或自定义）按 user_id 注入，
        对齐 codearts AK/SK 注入模式（_assemble_env 中统一注入）。
        """
        env = os.environ.copy()
        from src.config import SKILLS_DIR
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        return env
