#!/usr/bin/env python3
"""Upgrade hermes_acp.py to all 5 workers via SFTP + restart."""
import paramiko, time, sys

workers = [
    {"name": "vod-worker-0002", "ip": "127.0.0.1"},
    {"name": "issue-worker-0003", "ip": "127.0.0.1"},
    {"name": "coding-worker-0004", "ip": "192.168.0.39"},
    {"name": "qa-worker-0005", "ip": "192.168.0.194"},
    {"name": "sre-worker-0006", "ip": "192.168.0.213"},
]

local_file = "/opt/grape/src/agent/hermes_acp.py"
remote_file = "/opt/grape/src/agent/hermes_acp.py"

for w in workers:
    name = w["name"]
    ip = w["ip"]
    print(f"\n=== {name} ({ip}) ===", flush=True)
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, port=22, username="root", password="YOUR_SSH_PASSWORD", timeout=10)

        # backup
        stdin, stdout, stderr = ssh.exec_command(
            f"cp {remote_file} {remote_file}.bak.$(date +%Y%m%d%H%M%S)", timeout=5
        )
        stdout.read()
        print("  backup done", flush=True)

        # SFTP upload
        sftp = ssh.open_sftp()
        sftp.put(local_file, remote_file)
        sftp.close()
        print("  upload done", flush=True)

        # verify
        stdin, stdout, stderr = ssh.exec_command(f"ls -la {remote_file}", timeout=5)
        print(f"  verify: {stdout.read().decode().strip()}", flush=True)

        # delete pyc
        stdin, stdout, stderr = ssh.exec_command(
            "find /opt/grape -name '*.pyc' -delete 2>/dev/null; echo done", timeout=5
        )
        stdout.read()
        print("  pyc cleared", flush=True)

        # restart
        stdin, stdout, stderr = ssh.exec_command("systemctl restart runtime-agent", timeout=15)
        stdout.read()
        time.sleep(3)

        # check status
        stdin, stdout, stderr = ssh.exec_command(
            "systemctl is-active runtime-agent", timeout=5
        )
        status = stdout.read().decode().strip()
        print(f"  service: {status}", flush=True)

        # check patch marker
        stdin, stdout, stderr = ssh.exec_command(
            f"grep '2026-08-25 seq#2632' {remote_file}", timeout=5
        )
        ver = stdout.read().decode().strip()
        print(f"  patch verified: {'YES' if ver else 'NO'}", flush=True)

        ssh.close()
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)
