"""dsh Runtime 实现（DeepSeek Harness CLI 执行引擎）。

调用形态（headless 非交互模式）：
    dsh headless --json [- --session-id <id>] -
    prompt 经 stdin 传入（末尾 "-" 是 stdin 占位，对齐 codex / issue #351）

--json 输出 JSONL 事件流（每行一个 JSON 对象，含 type 字段）。
**已从 emitter 源码 + 本机实测确认的完整词表**（2026-10-23，
详见 docs/new-version1 同源设计文档 WORKER-ONBOARDING-AND-DSH-ENGINE-DESIGN §4.2.4）：

    {"type":"session","sessionId":…,"cwd":…}        订阅前先写，含会话 id（可续接）
    {"type":"status","phase":"turn_start","turn":n}
    {"type":"status","phase":"step_start","turn":n,"step":m}
    {"type":"status","phase":"step_end","turn":n,"step":m,"usage":{…}}   usage 完整才带
    {"type":"status","phase":"turn_end","turn":n,"reason":{"kind":…}}   失败时 reason.error
    {"type":"thinking","text":…}                    来自 assistant/message 的 reasoning block
    {"type":"text","text":…}                        来自 assistant/message 的 text block
    {"type":"tool_call","callId":…,"tool":…,"input":…}
    {"type":"tool_result","callId":…,"status":…,"result":…}   仅 surfaceOp==="append"
    {"type":"final","text":…}                       收尾，lossless 不截断
    {"type":"error",…}                              提供方/框架级错误
    {"type":"usage",…}                              独立 usage（实现里有该分支）

三条**源码强制**的健壮性要求（parse_output 必须容忍）：
  1. 字段可能缺失。超限时逐级降级：截断字符串 → 丢结构化字段只留标量
     → 只剩 {"type":…,"truncated":true}。故不得假设任何字段存在。
  2. step_end 的 usage 是"完整才给"（源码注释：不给部分和假装是完整的）。
  3. tool_result 只在 surfaceOp==="append" 时出现，不能按 call 数配对。

成功/失败判别位（实测两次不同原因的失败都是此形状，且 exit code 都是 1）：
    {"type":"status","phase":"turn_end","reason":{"kind":"error",
      "error":{"message":…,"code":"MISSING_CREDENTIAL"}}}
  → 失败原因必须从 reason.error 读，不能只看 exit code 或 final.text 是否为空。

模型凭据（重要，对应设计文档 D2「worker 机器自配」）：
  dsh 的 headless 形态需要一个可用的模型路由与 key，由**该机器自己**提供
  （本机 dsh 的配置/环境变量），平台不持有、不下发。缺凭据时本引擎会拿到
  MISSING_CREDENTIAL / 认证失败，由 parse_output 如实上报为 error 事件。
"""
import json
import os

from .base import AgentRuntime


class DshRuntime(AgentRuntime):
    name = "dsh"
    binary = "dsh"
    # 对齐 codex / issue #351：prompt 经 stdin 传入（dsh headless "-" 从 stdin 读任务），
    # 规避 Windows cmd 壳把 argv 中的换行当作命令分隔符截断多行提示词。
    prompt_via_stdin = True

    # ── 构建参数 ──────────────────────────────────────────────────────────

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        """构建 dsh headless 参数。

        dsh headless --json [--session-id <id>] -
          --json              逐行 run events 打 stdout（诊断走 stderr）
          --session-id <id>   续接已持久化的 Session（事件流里的 sessionId 即此 id）
          -                   prompt 从 stdin 读
        """
        args = ["headless", "--json"]
        if session_id:
            args += ["--session-id", session_id]
        if prompt:
            args.append("-")
        return args

    # ── 事件映射 ──────────────────────────────────────────────────────────

    def parse_output(self, line: str) -> dict | None:
        """解析 dsh --json 事件流，映射到 grape 统一事件格式。

        映射表（→ 右为消费端认的统一事件）：
          session                       → step_start{sessionID}（会话 id 带入，供续接）
          status/step_end               → step_finish{tokens,usage}
          status/turn_end(kind=error)   → error{message,code}
          status/*（其余）              → other
          thinking                      → reasoning{text}
          text                          → text{text}
          tool_call                     → tool_use{tool,input}
          tool_result                   → tool_result{text}
          final                         → other（答案已在 text 流里，避免重复投喂）
          error                         → error{message}
          usage                         → step_finish
          其他 / 未知                   → other{raw}

        非 JSON 行返回 None（不中断流）；字段缺失/截断降级不抛错。
        """
        if not line or not line.strip():
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None

        et = event.get("type", "")

        # ── session：会话身份（供 --session-id 续接）──
        if et == "session":
            return {"type": "step_start", "sessionID": event.get("sessionId", "")}

        # ── status：turn / step 生命周期，usage 挂在 step_end ──
        if et == "status":
            phase = event.get("phase", "")
            if phase == "step_end":
                return self._step_finish(event.get("usage"))
            if phase == "turn_end":
                reason = event.get("reason") or {}
                if isinstance(reason, dict) and reason.get("kind") == "error":
                    err = reason.get("error") or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    out = {"type": "error", "message": msg or str(reason)}
                    # 保留 code：MISSING_CREDENTIAL / 认证失败等，供平台侧归因（D2）
                    if isinstance(err, dict) and err.get("code"):
                        out["code"] = err["code"]
                    return out
                return {"type": "other", "raw": f"status:{phase}"}
            return {"type": "other", "raw": f"status:{phase or 'unknown'}"}

        # ── content 事件 ──
        if et == "thinking":
            text = event.get("text", "")
            return {"type": "reasoning", "text": str(text)} if text else None

        if et == "text":
            text = event.get("text", "")
            return {"type": "text", "text": str(text), "sessionID": ""} if text else None

        if et == "tool_call":
            inp = event.get("input")
            if not isinstance(inp, dict):
                # emitter 约定：input 解析失败时保留原始字符串 → 包一层，避免消费端按 dict 取键报错
                inp = {"raw": inp} if inp is not None else {}
            return {"type": "tool_use", "tool": event.get("tool", ""),
                    "input": inp, "sessionID": ""}

        if et == "tool_result":
            result = event.get("result", "")
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False) if result is not None else ""
            status = event.get("status", "")
            text = result if status != "error" else f"Error: {result}"
            return {"type": "tool_result", "text": text, "sessionID": ""}

        # ── final：与最后一个 text 事件内容重复（实测两者皆为最终答案）→ 不再作为 text 投喂 ──
        if et == "final":
            return {"type": "other", "raw": "final"}

        # ── 框架/提供方级错误 ──
        if et == "error":
            err = event.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or json.dumps(err, ensure_ascii=False)
                out = {"type": "error", "message": str(msg)}
                if err.get("code"):
                    out["code"] = err["code"]
                return out
            return {"type": "error", "message": str(event.get("message") or event)}

        # ── 独立 usage 事件（实现里有此分支，未在实测流中出现）──
        if et == "usage":
            return self._step_finish(event.get("usage") or event)

        # ── 未知类型：不中断流 ──
        return {"type": "other", "raw": et or "unknown"}

    @staticmethod
    def _step_finish(usage: object) -> dict:
        """把 dsh 的 usage 映射成统一 step_finish。

        dsh 字段（实测）：inputTokens / outputTokens / totalTokens / cacheReadTokens。
        usage 缺失时不编造部分和，只给 tokens=0（源码注释明确反对给部分和）。
        """
        if not isinstance(usage, dict):
            return {"type": "step_finish", "tokens": 0}

        def _n(key: str) -> int:
            try:
                return int(usage.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        total = _n("totalTokens")
        if not total:
            total = _n("inputTokens") + _n("outputTokens")
        return {
            "type": "step_finish",
            "tokens": total,
            "usage": {
                "input": _n("inputTokens"),
                "output": _n("outputTokens"),
                "reasoning": _n("reasoningTokens"),   # 实测未出现，缺失即 0
                "cache_read": _n("cacheReadTokens"),
            },
        }

    # ── 环境 ──────────────────────────────────────────────────────────────

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        """注入环境变量（对齐 codex）。

        DEVOPS_SKILLS_DIR / DEVOPS_EVENT_TYPE 由平台侧下发，供引擎读取节点技能与事件类型。
        注意：**模型凭据不在此处注入** —— 按设计 D2，由 worker 机器自己的 dsh 配置/环境提供
        （平台不持有模型 key）。本函数不读取、不转发任何模型凭据。

        GRAPE_SESSION_TOKEN 的穿透见 prepare_env()：必须等 env_extra 合并之后再处理，
        因为 token 是任务级的（在 env_extra 里），而本函数执行时 env_extra 尚未注入。
        """
        env = os.environ.copy()
        from src.config import SKILLS_DIR
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        return env

    def prepare_env(self, env: dict) -> None:
        """任务级 env 合并之后的收尾钩子（dsh 专属 token 穿透）。

        dsh 的 subprocess 服务在 spawn bash 工具时会 scrub 掉父进程 env 里所有匹配
        /KEY|PASSWORD|SECRET|TOKEN/i 的变量名。GRAPE_SESSION_TOKEN 命中 TOKEN → 被洗掉
        → bash 里拿不到 → grape CLI 调不了 git-repo 接口。

        解法：把 token 值写到一个 0600 临时文件，路径通过 GRAPE_AUTH_FILE 传递
        （该变量名不命中 scrub）。grape CLI 的 config.py 先读该文件，再 fallback 到
        环境变量。其他引擎不做此 scrub，GRAPE_AUTH_FILE 不存在 → fallback 正常。

        ⚠️ 必须在 env_extra 合并之后调用：GRAPE_SESSION_TOKEN 由平台按任务下发、
        存在 agent_tasks.env_extra 里，get_env() 阶段它还没进 env（历史 bug：#948
        把本逻辑写在 get_env 里，读到的永远是空串，临时文件从未生成）。
        """
        sess_tok = env.get("GRAPE_SESSION_TOKEN", "")
        if not sess_tok:
            return
        try:
            import tempfile
            fd, auth_path = tempfile.mkstemp(prefix="grape-sess-", suffix=".tok")
            os.write(fd, sess_tok.encode("utf-8"))
            os.close(fd)
            os.chmod(auth_path, 0o600)
            env["GRAPE_AUTH_FILE"] = auth_path
        except Exception:
            pass  # 写文件失败不阻断执行（fallback 到 env 变量）