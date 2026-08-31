#!/usr/bin/env python3
"""Clean 0006 + 0002 via SSH exec_command only (no SFTP, /tmp is full)."""
import paramiko

def clean_via_exec(ip, tag):
    print(f"\n=== {tag} ({ip}) ===")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, 22, "root", "YOUR_SSH_PASSWORD", timeout=15, look_for_keys=False, allow_agent=False)

    # Before
    i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
    print(f"  Before: {o.read().decode().strip()}")

    # Step 1: Quick clean /tmp obvious junk to free space (no script needed)
    quick_cmds = [
        "rm -rf /tmp/tirith-install-* 2>/dev/null",
        "rm -rf /tmp/hermes-acp-* /tmp/hermes-iso-* /tmp/hermes-sandbox-* /tmp/hermes-snap-* 2>/dev/null",
        "rm -rf /tmp/pip-unpack-* /tmp/pip-build-env-* /tmp/pip-metadata-* 2>/dev/null",
        "rm -rf /tmp/pytest-of-* 2>/dev/null",
        "rm -rf /tmp/blog-* /tmp/tf-deploy-* /tmp/tf-sys-xhy* /tmp/tfschema.json 2>/dev/null",
        "rm -rf /tmp/dsh-deploy.tar.gz /tmp/dsh-src.tar.gz 2>/dev/null",
        "rm -rf /tmp/esbuild-* /tmp/superpowers /tmp/shelldev-check 2>/dev/null",
        "rm -rf /tmp/tmp.* /tmp/tmp_* 2>/dev/null",
        "rm -rf /tmp/grape-*.log /tmp/node-compile-cache 2>/dev/null",
        "rm -f /tmp/hermes-results/* 2>/dev/null",
    ]
    for cmd in quick_cmds:
        c.exec_command(cmd, timeout=15)

    import time
    time.sleep(2)

    # Check /tmp after quick clean
    i, o, e = c.exec_command("df -h /tmp | tail -1", timeout=10)
    print(f"  /tmp after quick: {o.read().decode().strip()}")

    # Step 2: Clean stale workspace dirs (project-info, codebase, old issue workdirs)
    ws_cmd = """python3 -c '
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
' 2>&1"""
    i, o, e = c.exec_command(ws_cmd, timeout=60)
    print(f"  Workspace: {o.read().decode().strip()}")

    # After
    i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
    print(f"  After:  {o.read().decode().strip()}")
    c.close()

# Clean 0006
try:
    clean_via_exec("192.168.0.213", "0006 sre-worker")
except Exception as ex:
    print(f"  0006 ERROR: {ex}")

# Clean 0002
try:
    clean_via_exec("127.0.0.1", "0002 vod-worker")
except Exception as ex:
    print(f"  0002 ERROR: {ex}")
