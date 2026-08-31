#!/usr/bin/env python3
"""Verify worker code content (not just version string)."""
import paramiko

WORKERS = [
    ("0002", "127.0.0.1"),
    ("0003", "127.0.0.1"),
    ("0004", "192.168.0.39"),
    ("0005", "192.168.0.194"),
    ("0006", "192.168.0.213"),
]
PASSWORD = "YOUR_SSH_PASSWORD"

# Check for our fix markers in the actual code
CHECKS = [
    ("_auto_advance_pipeline", "/opt/grape/src/runtime/service.py"),
    ("_sweep_tmp_residuals", "/opt/grape/src/runtime/cleanup.py"),
    ("/tmp/.*.so", "/opt/grape/src/runtime/agent.py"),
    ("pipeline order fix", "/opt/grape/src/api/routes/webhook.py"),
    ("CHAT_CWD_BASE", "/opt/grape/src/config.py"),
]

for tag, ip in WORKERS:
    print(f"\n=== {tag} ({ip}) ===")
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(ip, 22, "root", PASSWORD, timeout=10, look_for_keys=False, allow_agent=False)

        # Check VERSION file
        i, o, e = c.exec_command("cat /opt/grape/data/cache/worker-package/VERSION 2>/dev/null", timeout=10)
        ver = o.read().decode().strip()
        print(f"  VERSION: {ver}")

        # Check code markers
        for marker, path in CHECKS:
            i, o, e = c.exec_command(f"grep -c '{marker}' {path} 2>/dev/null", timeout=10)
            count = o.read().decode().strip()
            status = "OK" if count and int(count) > 0 else "MISSING"
            print(f"  {marker} in {path.split('/')[-1]}: {status}")

        # Check skills
        i, o, e = c.exec_command("find /opt/grape/skills -name SKILL.md 2>/dev/null | wc -l", timeout=10)
        skill_count = o.read().decode().strip()
        print(f"  SKILL.md count: {skill_count}")

        c.close()
    except Exception as ex:
        print(f"  ERROR: {ex}")
