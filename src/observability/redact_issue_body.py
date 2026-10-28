"""Issue 正文出站脱敏（feedback→Issue 转换器，M-1 整改）。

治理面：`src/api/feedback_issue_converter._build_issue_body` 生成的 Issue 正文可能发布到**公开仓库**
（`skill.scope=official` → `GRAPE_SKILLS_REPO`）。Agent result_text / events.result 衍生的
fail_summary 与 description 可能携带内网 IP、绝对路径、凭据串，未经脱敏会导致部署拓扑泄露。

本模块提供幂等的纯函数 `redact_issue_text`：
- 复用 `src.observability.logs.redact`（凭据/URL 打码：key=value、scheme://user:pass@host、
  authorization、敏感 token 前缀）
- 补充 IP（完整 IPv4 形态 + 可选端口）与绝对路径（Unix / Windows）打码为 `<redacted>`

设计：docs/weekly-08/feedback-issue-body-redact-m1-design.md
"""
import re

from src.observability.logs import redact as _logs_redact

# IP 打码：完整 IPv4 四段（避免把版本号 1.2.3 误伤），可选端口；负向边界防吞数字上下文
_IP_RE = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::\d+)?(?![0-9])")
# Unix 绝对路径：常见部署根前缀（白名单，避免误伤普通文案中的斜杠/URL 剩余段）
_UNIX_PATH_RE = re.compile(
    r"(?:/(?:root|home|etc|opt|var|srv|data|tmp|app|workspace|config|srv|deploy|grape))"
    r"[^\s\"',;)`]*"
)
# Windows 绝对路径：盘符 + 反斜杠
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"',;)`]*")


def _redact_once(text: str) -> str:
    s = _logs_redact(text)                      # 凭据 / URL / token 前缀
    s = _IP_RE.sub("<redacted>", s)             # 全量 IPv4（含公网 + 端口，整段一次吃掉）
    s = _UNIX_PATH_RE.sub("<redacted>", s)      # Unix 绝对路径
    s = _WIN_PATH_RE.sub("<redacted>", s)       # Windows 绝对路径
    return s


def redact_issue_text(text: str | None, max_len: int = 2000) -> str:
    """把 Issue 正文的敏感内容打码（幂等），并做长度截断。

    - 无 IO / 无 DB / 无 env 依赖（纯函数），可供 converter（worker 线程）与 submit（写侧）复用。
    - 幂等：对已脱敏文本再执行结果不变（R4 不变量）。
    - 长度语义：先脱敏后截断 `[:max_len]`，与现状 2000/500 一致；已脱敏文本可能更短。
    """
    if not text:
        return ""
    t = str(text)
    prev = None
    while prev != t:                            # 迭代至不动点 = 幂等（防御嵌套拼接后二次替换）
        prev = t
        t = _redact_once(t)
    return t[:max_len]