"""Hermes Runtime 实现（Nous Research Hermes Agent 执行引擎）。

调用形态（实测 hermes CLI）：
    hermes -z <prompt> --yolo --cli [--continue] [--skills SKILLS]
- `-z` 一次性提问（One-shot mode），退出码 0 表示完成；
- `--yolo` 自动批准工具调用（等价 opencode --dangerously-skip-permissions）；
- `--cli` 非交互纯文本输出（只打印最终回复，无 NDJSON 事件流）；
- `--continue` 继续最近会话（hermes 无精确 session id 恢复，用最近会话语义，
  因此 hermes 引擎建议限并发 1，见 grape-hermes-engine-analysis.md §5）。

输出协议：hermes --cli 输出纯文本（每行一段最终回复），与 opencode 的 NDJSON
事件流不同。parse_output 将每行 stdout 转换为 text 事件（Grape 事件记录与
对话前端主要消费 text，逐步过程事件不产出，属预期取舍）。
"""
import os
import subprocess
import threading
from .base import AgentRuntime


class HermesRuntime(AgentRuntime):
    name = "hermes"
    binary = "hermes"

    def __init__(self):
        super().__init__()
        self._cli_ok: bool | None = None
        self._cli_probe_lock = threading.Lock()

    def _probe_cli_flag(self) -> bool:
        """探测当前 hermes 是否支持 --cli（一次性，带锁缓存）。

        老版本（如 lvjuntao 机器 v0.11.0，2026.4）无 --cli 参数，硬带会
        'unrecognized arguments: --cli' 直接 exit=2（远程 hermes 跑失败根因）。
        以 `hermes --help` 输出是否含 --cli 判断，比版本号阈值更直接。
        """
        with self._cli_probe_lock:
            if self._cli_ok is not None:
                return self._cli_ok
            ok = False
            try:
                res = subprocess.run([self.binary, "--help"],
                                     capture_output=True, text=True, timeout=10)
                ok = "--cli" in (res.stdout or "")
            except Exception:
                ok = False
            self._cli_ok = ok
            return ok

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        args = ["-z", prompt, "--yolo"]
        if self._probe_cli_flag():
            args.append("--cli")  # 新版本：非交互纯文本输出
        # 老版本（无 --cli）：-z 单次执行同样输出纯文本（实测 v0.11 OK）
        if session_id:
            args.append("--continue")  # 继续最近会话（hermes 无指定 id 恢复）
        return args

    def parse_output(self, line: str) -> dict | None:
        """覆盖基类：hermes --cli 输出纯文本，逐行转 text 事件。

        基类默认按 opencode NDJSON 解析，对非 JSON 行返回 None（丢弃）；
        hermes 需保留每行文本作为回复内容。
        """
        line = line.rstrip("\n")
        if not line.strip():
            return None
        return {"type": "text", "text": line, "sessionID": ""}

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        env = os.environ.copy()
        from src.config import SKILLS_DIR
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        return env
