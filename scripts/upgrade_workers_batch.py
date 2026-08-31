#!/usr/bin/env python3
"""Batch upgrade 5 workers via SCP + install-worker.sh."""
import paramiko, time, os, subprocess

WORKERS = [
    ("0002", "127.0.0.1"),
    ("0003", "127.0.0.1"),
    ("0004", "192.168.0.39"),
    ("0005", "192.168.0.194"),
    ("0006", "192.168.0.213"),
]
PASSWORD = "YOUR_SSH_PASSWORD"
PKG_PATH = "/opt/grape/data/cache/worker-package/grape-worker.tar.gz"

# Read token from first worker's runtime.env
def get_worker_token(ip):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, 22, "root", PASSWORD, timeout=10, look_for_keys=False, allow_agent=False)
    i, o, e = c.exec_command("grep RUNTIME_TOKEN /etc/grape/runtime.env | head -1", timeout=10)
    line = o.read().decode().strip()
    c.close()
    if "=" in line:
        return line.split("=", 1)[1].strip()
    return ""

def upgrade_worker(tag, ip, token):
    print(f"\n=== {tag} ({ip}) ===")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, 22, "root", PASSWORD, timeout=15, look_for_keys=False, allow_agent=False)

    # Before
    i, o, e = c.exec_command("cat /opt/grape/data/cache/worker-package/VERSION 2>/dev/null || echo unknown; systemctl is-active runtime-agent", timeout=10)
    before = o.read().decode().strip().replace("\n", " | ")
    print(f"  Before: {before}")

    # Stop runtime-agent
    c.exec_command("systemctl stop runtime-agent", timeout=15)
    time.sleep(3)

    # Kill lingering processes
    c.exec_command("pkill -f 'opencode run' 2>/dev/null; pkill -f 'hermes acp' 2>/dev/null; pkill -f 'codearts run' 2>/dev/null", timeout=10)
    time.sleep(2)

    # Clean /tmp .so cache
    c.exec_command("rm -f /tmp/.*.so 2>/dev/null", timeout=10)

    # SCP the package
    sftp = c.open_sftp()
    print(f"  uploading package...")
    sftp.put(PKG_PATH, "/tmp/grape-worker.tar.gz")
    sftp.close()

    # Extract and install
    cmds = [
        "mkdir -p /opt/grape-new",
        "tar -xzf /tmp/grape-worker.tar.gz -C /opt/grape-new --strip-components=1",
        # Preserve runtime.env
        "cp /etc/grape/runtime.env /opt/grape-new/runtime.env.bak 2>/dev/null",
        # Update code files
        "cp /opt/grape-new/src/runtime/agent.py /opt/grape/src/runtime/agent.py",
        "cp /opt/grape-new/src/runtime/cleanup.py /opt/grape/src/runtime/cleanup.py",
        "cp /opt/grape-new/src/runtime/service.py /opt/grape/src/runtime/service.py",
        "cp /opt/grape-new/src/runtime/resource_stack.py /opt/grape/src/runtime/resource_stack.py 2>/dev/null",
        "cp /opt/grape-new/src/config.py /opt/grape/src/config.py",
        "cp /opt/grape-new/src/user/db.py /opt/grape/src/user/db.py 2>/dev/null",
        "cp /opt/grape-new/src/storage/__init__.py /opt/grape/src/storage/__init__.py 2>/dev/null",
        "cp -r /opt/grape-new/src/api/routes/ /opt/grape/src/api/routes/ 2>/dev/null",
        "cp -r /opt/grape-new/src/api/event_pipeline.py /opt/grape/src/api/event_pipeline.py 2>/dev/null",
        "cp -r /opt/grape-new/src/api/event_archive.py /opt/grape/src/api/event_archive.py 2>/dev/null",
        "cp -r /opt/grape-new/src/api/event_store.py /opt/grape/src/api/event_store.py 2>/dev/null",
        "cp -r /opt/grape-new/src/session_manager.py /opt/grape/src/session_manager.py 2>/dev/null",
        # Copy all source to be safe
        "cp -r /opt/grape-new/src/ /opt/grape/src/",
        # Copy skills
        "cp -r /opt/grape-new/skills/ /opt/grape/skills/ 2>/dev/null",
        # Copy configs
        "cp /opt/grape-new/configs/skill-description.json /opt/grape/configs/ 2>/dev/null",
        "cp /opt/grape-new/configs/pipelines.json /opt/grape/configs/ 2>/dev/null",
        # Update VERSION
        "cp /opt/grape-new/data/cache/worker-package/VERSION /opt/grape/data/cache/worker-package/VERSION 2>/dev/null",
        "mkdir -p /opt/grape/data/cache/worker-package && cp /opt/grape-new/data/cache/worker-package/VERSION /opt/grape/data/cache/worker-package/VERSION",
        # Clean pyc
        "find /opt/grape -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null",
        # Clean up
        "rm -rf /opt/grape-new /tmp/grape-worker.tar.gz",
    ]
    for cmd in cmds:
        i, o, e = c.exec_command(cmd, timeout=30)
        o.read()

    # Start runtime-agent
    c.exec_command("systemctl start runtime-agent", timeout=15)
    time.sleep(5)

    # Verify
    i, o, e = c.exec_command("cat /opt/grape/data/cache/worker-package/VERSION 2>/dev/null; systemctl is-active runtime-agent; df -h /tmp | tail -1", timeout=10)
    after = o.read().decode().strip().replace("\n", " | ")
    print(f"  After: {after}")

    c.close()

# Get token from first worker
print("Getting worker token...")
token = get_worker_token(WORKERS[0][1])
print(f"Token: {token[:20]}...")

for tag, ip in WORKERS:
    try:
        upgrade_worker(tag, ip, token)
    except Exception as ex:
        print(f"  ERROR: {ex}")
