"""Bounded, no-tool AtomCode model health probe shared by server and worker."""

import json
import os
import subprocess
import tempfile
import time
import threading
from threading import Event as _ProbeEvent


_TIMEOUT = 60


_cache = {}
_cache_lock = threading.RLock()
_inflight = set()
_probe_flights = {}


def _cache_key(cli_path):
    return (cli_path or "atomcode", os.environ.get("GRAPE_ATOMCODE_PROVIDER", ""),
            os.environ.get("GRAPE_ATOMCODE_MODEL", ""), os.environ.get("ATOMCODE_HOME", ""))


def cached_atomcode_probe(cli_path: str = "") -> dict:
    """Return cached health immediately; refresh stale discovery in one thread."""
    key = _cache_key(cli_path)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return dict(cached[1])
        if key not in _inflight:
            _inflight.add(key)
            def refresh():
                try:
                    probe_atomcode(cli_path)
                finally:
                    with _cache_lock:
                        _inflight.discard(key)
            threading.Thread(target=refresh, daemon=True, name="atomcode-probe").start()
    return {"status": "pending", "latency": 0, "error": "AtomCode model probe pending"}


_REQUIRED_FLAGS = ("--ephemeral", "--no-tools", "--prompt-file", "--output-format", "--dev", "--no-telemetry")


def cli_support_error(cli_path: str) -> str:
    result = subprocess.run([cli_path or "atomcode", "--help"], capture_output=True,
                            text=True, timeout=5, stdin=subprocess.DEVNULL)
    if result.returncode != 0 or not all(flag in (result.stdout or "") for flag in _REQUIRED_FLAGS):
        return "AtomCode CLI lacks required headless flags; install a compatible version"
    return ""


def probe_atomcode(cli_path: str = "", *, timeout: int = _TIMEOUT) -> dict:
    key = _cache_key(cli_path)
    with _cache_lock:
        flight = _probe_flights.get(key)
        leader = flight is None
        if leader:
            flight = _ProbeEvent()
            _probe_flights[key] = flight
    if not leader:
        if flight.wait(_TIMEOUT + 10):
            with _cache_lock:
                return dict(_cache[key][1])
        return {"status": "fail", "latency": 0, "error": "AtomCode probe pending timeout"}
    try:
        result = _run_probe(cli_path, timeout=timeout)
        with _cache_lock:
            _cache[key] = (time.monotonic(), dict(result))
        return result
    finally:
        with _cache_lock:
            _probe_flights.pop(key, None)
            flight.set()


def _run_probe(cli_path: str, *, timeout: int) -> dict:
    """Check the configured provider by completing one ephemeral text turn.

    A CLI version only proves that a binary exists.  The JSONL terminal record
    and text response prove that this account and model can answer; process
    exit code alone would incorrectly accept a rate-limited turn.
    """
    if os.name == "nt":
        return {"status": "fail", "latency": 0, "error": "AtomCode requires macOS or Linux"}
    provider = os.environ.get("GRAPE_ATOMCODE_PROVIDER", "").strip()
    model = os.environ.get("GRAPE_ATOMCODE_MODEL", "").strip()
    if bool(provider) != bool(model):
        return {"status": "fail", "latency": 0, "error": "set both AtomCode provider and model"}

    cmd = [cli_path or "atomcode", "--ephemeral", "--no-tools", "--dev", "--no-telemetry", "--output-format", "jsonl"]
    if provider:
        cmd.extend(["--provider", provider, "--model", model])
    start = time.monotonic()
    try:
        support_error = cli_support_error(cli_path)
        if support_error:
            return {"status": "fail", "latency": 0, "error": support_error}
        with tempfile.TemporaryDirectory(prefix="grape-atomcode-probe-") as cwd:
            result = subprocess.run(cmd + ["-C", cwd, "-p", "Reply READY."], capture_output=True,
                                    text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        events = []
        malformed = False
        for line in (result.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
                else:
                    malformed = True
            except (ValueError, TypeError):
                malformed = True
        starts = [e for e in events if e.get("type") == "run.started"]
        terminals = [e for e in events if e.get("type") in ("turn.completed", "run.failed")]
        started = next((e for e in events if e.get("type") == "run.started"), {})
        terminal = next((e for e in reversed(events) if e.get("type") in ("turn.completed", "run.failed")), {})
        text_seen = any(e.get("type") == "message.delta" and str(e.get("text") or "").strip() for e in events)
        matches_model = not provider or (started.get("provider") == provider and started.get("model") == model)
        ok = (not malformed and len(starts) == 1 and len(terminals) == 1
              and events[0] is started and events[-1] is terminal
              and type(started.get("schema_version")) is int
              and not any(e.get("type") == "error" and e.get("retryable") is not True for e in events)
              and "[denied]" not in (result.stderr or "").lower()
              and result.returncode == 0 and started.get("schema_version") == 1 and matches_model and text_seen
              and terminal.get("type") == "turn.completed" and terminal.get("stop_reason") == "Stopped"
              and type(terminal.get("exit_code")) is int and terminal.get("exit_code") == 0)
        latency = round(time.monotonic() - start, 1)
        if ok:
            return {"status": "ok", "latency": latency, "error": ""}
        # Do not expose arbitrary CLI output, which may include model/user data.
        return {"status": "fail", "latency": latency, "error": "AtomCode model probe did not complete successfully"}
    except subprocess.TimeoutExpired:
        return {"status": "fail", "latency": round(time.monotonic() - start, 1), "error": f"timeout ({timeout}s)"}
    except FileNotFoundError:
        return {"status": "fail", "latency": 0, "error": "CLI not found"}
    except Exception as exc:
        return {"status": "fail", "latency": round(time.monotonic() - start, 1),
                "error": f"AtomCode probe failed: {type(exc).__name__}"}
