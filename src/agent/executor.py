"""Agent 执行器：流式子进程调度 (NDJSON)，不绑定具体后端。"""
import os, subprocess, json, shutil
from src.config import log
from .base import AgentBackend
from .opencode import OpencodeBackend
from .codearts import CodeArtsBackend


_BACKENDS = {
    "opencode": OpencodeBackend(),
    "codearts": CodeArtsBackend(),
}


def get_backend(name: str) -> AgentBackend:
    return _BACKENDS.get(name, _BACKENDS["opencode"])


def stream_agent(prompt: str, cwd: str, session_id: str = "",
                 event_type: str = "", backend_name: str = "opencode",
                 account: str = "default"):
    """流式执行 Agent，yield 事件。"""
    backend = get_backend(backend_name)
    env = backend.get_env(account=account)
    env["DEVOPS_EVENT_TYPE"] = event_type

    args = backend.build_args(prompt, session_id)
    agent_cmd = shutil.which(backend.binary) or shutil.which(backend.binary + ".cmd")
    if not agent_cmd:
        log.error(f"{backend.name} CLI not found in PATH")
        yield ("done", {
            "exit_code": -1, "text": "", "oc_session_id": session_id,
            "errors": [f"{backend.name} not found"], "stderr": ""
        })
        return

    log.info(f"{backend.name} args: {' '.join(args)[:200]}...")
    log.info(f"{backend.name} cwd: {cwd}")

    stdin_pipe = subprocess.PIPE if backend.prompt_via_stdin else None
    proc = subprocess.Popen(
        [agent_cmd] + args,
        stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env, bufsize=0
    )
    if backend.prompt_via_stdin:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()

    collected_text = []
    new_session_id = session_id
    error_events = []
    step_count = 0

    for raw_line in iter(proc.stdout.readline, b""):
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        parsed = backend.parse_output(line)
        if parsed is None:
            yield ("raw", line[:200])
            continue
        et = parsed["type"]
        if et == "step_start":
            step_count += 1
            if parsed.get("sessionID"):
                new_session_id = parsed["sessionID"]
            yield ("step_start", f"Step {step_count} started (session: {new_session_id})")
        elif et == "text":
            collected_text.append(parsed["text"])
            yield ("text", parsed["text"][:150].replace("\n", " "))
        elif et == "reasoning":
            yield ("reasoning", parsed["text"][:200])
        elif et == "tool_use":
            yield ("tool_use", f"Tool: {parsed['tool']} {json.dumps(parsed.get('input', {}))[:200]}")
        elif et == "step_finish":
            yield ("step_finish", f"Step {step_count} done, tokens={parsed['tokens']}")
        elif et == "error":
            error_events.append(parsed["message"])
            yield ("error", parsed["message"])
        else:
            yield ("other", et)

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log.warning(f"{backend.name} timed out after 120s, killed")
        yield ("done", {
            "exit_code": -1, "text": "", "oc_session_id": new_session_id,
            "errors": [f"{backend.name} timed out"], "stderr": ""
        })
        return

    stdout_remainder = ""
    try:
        stdout_remainder = proc.stdout.read().decode("utf-8", errors="replace")
    except:
        pass

    if stdout_remainder:
        for line in stdout_remainder.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = backend.parse_output(line)
            if parsed and parsed["type"] == "text":
                collected_text.append(parsed["text"])

    full_text = "\n".join(collected_text)
    stderr_output = ""
    try:
        stderr_output = proc.stderr.read().decode("utf-8", errors="replace")[:500]
    except:
        pass

    yield ("done", {
        "exit_code": proc.returncode, "text": full_text,
        "oc_session_id": new_session_id,
        "errors": error_events, "stderr": stderr_output,
        "backend": backend.name,
    })


def stream_opencode(prompt, cwd, oc_session_id, event_type=""):
    """向后兼容包装器，默认使用 opencode 后端。"""
    yield from stream_agent(prompt, cwd, oc_session_id, event_type, backend_name="opencode")
