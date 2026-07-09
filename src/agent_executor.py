"""Agent 执行器：opencode CLI 调度、NDJSON 流式解析"""
import os, subprocess, json, shutil
from src.config import log, SKILLS_DIR, GITCODE_API_TOKEN


def stream_opencode(prompt, cwd, oc_session_id, event_type=""):
    env = os.environ.copy()
    env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
    env["DEVOPS_EVENT_TYPE"] = event_type
    env["GITCODE_API_TOKEN"] = GITCODE_API_TOKEN

    args = ["run", "--format", "json", "--dangerously-skip-permissions"]
    if oc_session_id:
        args.extend(["--session", oc_session_id])
    args.append(prompt)

    log.info(f"opencode args: {' '.join(args)[:200]}...")
    log.info(f"opencode cwd: {cwd}")

    agent_cmd = shutil.which("opencode") or shutil.which("opencode.cmd")
    if not agent_cmd:
        log.error("opencode CLI not found in PATH")
        yield ("done", {"exit_code": -1, "text": "", "oc_session_id": oc_session_id, "errors": ["opencode not found"], "stderr": "opencode CLI not found"})
        return

    proc = subprocess.Popen(
        [agent_cmd] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        bufsize=0
    )

    collected_text = []
    new_oc_session_id = oc_session_id
    error_events = []
    step_count = 0

    for raw_line in iter(proc.stdout.readline, b""):
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            et = event.get("type", "unknown")
            if et == "step_start":
                step_count += 1
                sid = event.get("sessionID", "")
                if sid:
                    new_oc_session_id = sid
                yield ("step_start", f"Step {step_count} started (session: {new_oc_session_id})")
            elif et == "text":
                part = event.get("part", {})
                if isinstance(part, dict):
                    text = part.get("text", "")
                    ptype = part.get("type", "")
                    if text:
                        if ptype == "reasoning":
                            yield ("reasoning", text[:200])
                        elif ptype == "text":
                            collected_text.append(text)
                            yield ("text", text[:150].replace("\n", " "))
            elif et == "reasoning":
                part = event.get("part", {})
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        yield ("reasoning", text[:200])
            elif et == "tool_use":
                part = event.get("part", {})
                tool = ""
                if isinstance(part, dict):
                    tool = part.get("tool", "")
                    inp = part.get("state", {}).get("input", {})
                yield ("tool_use", f"Tool: {tool} {json.dumps(inp)[:200]}")
            elif et == "step_finish":
                tokens = event.get("part", {}).get("tokens", {})
                total = tokens.get("total", 0)
                yield ("step_finish", f"Step {step_count} done, tokens={total}")
            elif et == "error":
                err = event.get("error", {}).get("data", {}).get("message", str(event))
                error_events.append(err)
                yield ("error", err)
            else:
                yield ("other", et)
        except json.JSONDecodeError:
            yield ("raw", line[:200])

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log.warning("opencode timed out after 120s, killed")
        yield ("done", {"exit_code": -1, "text": "", "oc_session_id": new_oc_session_id, "errors": ["opencode timed out"], "stderr": ""})
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
            try:
                event = json.loads(line)
                if event.get("type") == "text":
                    part = event.get("part", {})
                    if isinstance(part, dict):
                        text = part.get("text", "")
                        if text:
                            collected_text.append(text)
            except json.JSONDecodeError:
                pass

    full_text = "\n".join(collected_text)
    stderr_output = ""
    try:
        stderr_output = proc.stderr.read().decode("utf-8", errors="replace")[:500]
    except:
        pass

    yield ("done", {
        "exit_code": proc.returncode,
        "text": full_text,
        "oc_session_id": new_oc_session_id,
        "errors": error_events,
        "stderr": stderr_output
    })
