#!/usr/bin/env python3
"""Sync code + clean 0002 worker."""
import paramiko, time

ip = "127.0.0.1"
print(f"=== 0002 ({ip}) ===")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(ip, 22, "root", "YOUR_SSH_PASSWORD", timeout=10, look_for_keys=False, allow_agent=False)

# Before
i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
print(f"  Before: {o.read().decode().strip()}")

# Sync 3 files
sftp = c.open_sftp()
files = [
    ("/opt/grape/src/runtime/agent.py", "/opt/grape/src/runtime/agent.py"),
    ("/opt/grape/src/runtime/cleanup.py", "/opt/grape/src/runtime/cleanup.py"),
    ("/opt/grape/src/config.py", "/opt/grape/src/config.py"),
]
for local, remote in files:
    sftp.put(local, remote)
    print(f"  synced {remote}")
sftp.close()

# Clear pyc
c.exec_command("find /opt/grape -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null", timeout=10)

# Quick clean /tmp
clean_cmds = [
    "rm -rf /tmp/tirith-install-* /tmp/hermes-acp-* /tmp/hermes-iso-* 2>/dev/null",
    "rm -rf /tmp/pip-unpack-* /tmp/pip-build-env-* /tmp/pytest-of-* 2>/dev/null",
    "rm -rf /tmp/blog-* /tmp/tf-deploy-* /tmp/esbuild-* /tmp/superpowers 2>/dev/null",
    "rm -rf /tmp/grape-*.log /tmp/tmp.* /tmp/tmp_* 2>/dev/null",
    "rm -f /tmp/hermes-results/* 2>/dev/null",
]
for cmd in clean_cmds:
    c.exec_command(cmd, timeout=15)
time.sleep(2)

# Clean stale workspace dirs via remote python script
remote_script = """
import os, shutil, time
now = time.time()
ttl = 3*86400
cleaned = 0
freed = 0
ws = "/opt/grape-workspaces"
if os.path.isdir(ws):
    for owner in os.listdir(ws):
        od = os.path.join(ws, owner)
        if not os.path.isdir(od): continue
        for repo in os.listdir(od):
            rd = os.path.join(od, repo)
            if not os.path.isdir(rd): continue
            for sub in ["project-info", "codebase"]:
                t = os.path.join(rd, sub)
                if not os.path.isdir(t): continue
                try:
                    if now - os.path.getmtime(t) > ttl:
                        sz = 0
                        for root, dirs, files in os.walk(t):
                            for f in files:
                                try: sz += os.path.getsize(os.path.join(root, f))
                                except: pass
                        shutil.rmtree(t, ignore_errors=True)
                        cleaned += 1
                        freed += sz
                except: pass
            ir = os.path.join(rd, "issues")
            if os.path.isdir(ir):
                for num in os.listdir(ir):
                    idir = os.path.join(ir, num)
                    if not os.path.isdir(idir): continue
                    for ag in ["vod","issue","coding","qa","sre"]:
                        ad = os.path.join(idir, ag)
                        if not os.path.isdir(ad): continue
                        try:
                            if now - os.path.getmtime(ad) > ttl:
                                shutil.rmtree(ad, ignore_errors=True)
                                cleaned += 1
                        except: pass
print(f"ws_cleaned={cleaned} ws_freed_mb={freed//1024//1024}")
"""

# Write script remotely and run
sftp = c.open_sftp()
with sftp.file("/tmp/_clean_ws.py", "w") as f:
    f.write(remote_script)
sftp.close()

i, o, e = c.exec_command("python3 /tmp/_clean_ws.py 2>&1", timeout=60)
print(f"  Workspace: {o.read().decode().strip()}")

# Restart runtime-agent
c.exec_command("systemctl restart runtime-agent", timeout=15)
time.sleep(8)
i, o, e = c.exec_command("systemctl is-active runtime-agent", timeout=10)
print(f"  svc: {o.read().decode().strip()}")

# After
i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
print(f"  After:  {o.read().decode().strip()}")

c.close()
