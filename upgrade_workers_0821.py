#!/usr/bin/env python3
"""Worker upgrade script: paramiko SSH to 5 workers, serial execution."""
import paramiko, time, sys

workers = [
    {"name": "vod-worker", "host": "127.0.0.1", "user": "root", "password": "YOUR_SSH_PASSWORD",
     "token": "rta_R1hsPCyoYgTG0cNLskisOZ4qGKnTh5eImEZXxr08E6M"},
    {"name": "issue-worker", "host": "127.0.0.1", "user": "root", "password": "YOUR_SSH_PASSWORD",
     "token": "rta_cGq-ALCn3aGqL4uN_9tDqH_89aY7Ak_yQdChKXKGYiw"},
    {"name": "coding-worker1", "host": "192.168.0.39", "user": "root", "password": "YOUR_SSH_PASSWORD",
     "token": "rta_Ap00FktnZfJKJQcO24lAkA5hA_3-xFdRap9O36KACqE"},
    {"name": "qa-worker1", "host": "192.168.0.194", "user": "root", "password": "YOUR_SSH_PASSWORD",
     "token": "rta_4N5qSWmJawGUOjuq2kWEZXO2dFJPvMMsc-ydEqWMtr4"},
    {"name": "sre-worker2", "host": "192.168.0.213", "user": "root", "password": "YOUR_SSH_PASSWORD",
     "token": "rta_e3p_ThKDldRYqcdVp5ul_1ncR-f8RHv0hGXU3UJDhwA"},
]

server = "https://example.com"
results = []

for w in workers:
    name = w["name"]
    host = w["host"]
    print(f"\n=== {name} ({host}) ===", flush=True)
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=22, username=w["user"], password=w["password"], timeout=15)

        # Pre-check: current version
        stdin, stdout, stderr = ssh.exec_command("cat /etc/grape/worker.version 2>/dev/null || echo none", timeout=10)
        old_ver = stdout.read().decode().strip()
        print(f"  old version: {old_ver}", flush=True)

        # Backup runtime.env
        stdin, stdout, stderr = ssh.exec_command("cp /etc/grape/runtime.env /etc/grape/runtime.env.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null; echo done", timeout=10)
        stdout.read()
        print(f"  runtime.env backed up", flush=True)

        # Execute upgrade
        cmd = f"curl -fsSL {server}/api/runtime/install | bash -s -- --token {w['token']} --server {server}"
        print(f"  running upgrade...", flush=True)
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        exit_code = stdout.channel.recv_exit_status()

        # Show last 5 lines of output
        out_lines = out.split("\n")[-5:] if out else []
        for l in out_lines:
            print(f"  {l}", flush=True)
        if err:
            err_lines = err.split("\n")[-3:]
            for l in err_lines:
                print(f"  [stderr] {l}", flush=True)
        print(f"  exit_code={exit_code}", flush=True)

        # Restart runtime-agent
        stdin, stdout, stderr = ssh.exec_command(
            "systemctl restart runtime-agent 2>/dev/null || systemctl restart grape-agent 2>/dev/null || echo no-service",
            timeout=15,
        )
        restart_out = stdout.read().decode().strip()
        print(f"  restart: {restart_out[:80]}", flush=True)

        time.sleep(3)

        # Post-check: version + service status + PID
        post_cmd = (
            "cat /etc/grape/worker.version 2>/dev/null; echo '---';"
            "systemctl is-active runtime-agent 2>/dev/null || systemctl is-active grape-agent 2>/dev/null || echo unknown;"
            "echo '---';"
            "pgrep -f 'runtime.agent|grape.agent' | head -3"
        )
        stdin, stdout, stderr = ssh.exec_command(post_cmd, timeout=10)
        post = stdout.read().decode().strip()
        print(f"  post-check:", flush=True)
        for line in post.split("\n"):
            print(f"    {line}", flush=True)

        ssh.close()
        results.append({"name": name, "host": host, "old_ver": old_ver, "exit_code": exit_code, "post": post})
        print(f"  DONE", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)
        results.append({"name": name, "host": host, "error": str(e)})
    time.sleep(2)

print("\n=== SUMMARY ===", flush=True)
for r in results:
    has_error = "error" in r
    exit_ok = r.get("exit_code") == 0
    post_ok = "active" in r.get("post", "")
    status = "OK" if (exit_ok and post_ok and not has_error) else "FAIL"
    print(f"  {r['name']:20s} {r['host']:16s} {status}", flush=True)
