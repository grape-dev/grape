#!/usr/bin/env python3
"""Batch clean /tmp + stale workspace dirs on all 5 workers via paramiko."""
import paramiko, os, shutil, time, glob, json

WORKERS = [
    ("0002", "127.0.0.1", "vod-worker"),
    ("0003", "127.0.0.1",  "issue-worker"),
    ("0004", "192.168.0.39",  "coding-worker"),
    ("0005", "192.168.0.194", "qa-worker"),
    ("0006", "192.168.0.213", "sre-worker"),
]

PASSWORD = "YOUR_SSH_PASSWORD"

# The cleanup script that runs ON each worker
CLEAN_SCRIPT = r"""
import os, shutil, time, glob
now = time.time()
ttl = 3600  # 1h for /tmp
repo_ttl = 3 * 86400  # 3 days for workspace dirs
cleaned = 0
freed_bytes = 0

def get_size(path):
    total = 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for root, dirs, files in os.walk(path):
        for f in files:
            try: total += os.path.getsize(os.path.join(root, f))
            except: pass
    return total

# 1. /tmp directory patterns
dir_patterns = [
    "/tmp/hermes-acp-*", "/tmp/hermes-iso-*", "/tmp/hermes-sandbox-*",
    "/tmp/hermes-snap-*", "/tmp/tf-deploy-*", "/tmp/tf-sys-xhy*",
    "/tmp/blog-build-*", "/tmp/blog-deploy*", "/tmp/blog-gen*",
    "/tmp/blog-publish-*", "/tmp/blog-codearts",
    "/tmp/pip-unpack-*", "/tmp/pip-build-env-*", "/tmp/pip-metadata-*",
    "/tmp/pytest-of-root", "/tmp/pytest-of-yangaiwu", "/tmp/pytest-of-liufei",
    "/tmp/tirith-install-*",
    "/tmp/dsh-deploy.tar.gz", "/tmp/dsh-src.tar.gz",
    "/tmp/tfschema.json", "/tmp/shelldev-check",
    "/tmp/tmp.UhC9rzjKv3", "/tmp/tmp__gfw5ym", "/tmp/tmpfq6yi765",
    "/tmp/superpowers", "/tmp/esbuild-*",
    "/tmp/grape-final-a.log", "/tmp/grape-i18n-test.log", "/tmp/grape-a-i18n.log",
]
for pattern in dir_patterns:
    for path in glob.glob(pattern):
        try:
            mtime = os.path.getmtime(path)
            if now - mtime < ttl: continue
            sz = get_size(path)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
            cleaned += 1
            freed_bytes += sz
        except: pass

# 2. hermes-results directory (clean old files inside)
hr = "/tmp/hermes-results"
if os.path.isdir(hr):
    for f in os.listdir(hr):
        fp = os.path.join(hr, f)
        try:
            if now - os.path.getmtime(fp) < ttl: continue
            if os.path.isfile(fp):
                sz = os.path.getsize(fp)
                os.remove(fp)
                cleaned += 1
                freed_bytes += sz
        except: pass

# 3. grape-workspaces: clean stale project-info/codebase
workspace = "/opt/grape-workspaces"
if os.path.isdir(workspace):
    for owner in os.listdir(workspace):
        owner_dir = os.path.join(workspace, owner)
        if not os.path.isdir(owner_dir): continue
        for repo in os.listdir(owner_dir):
            repo_dir = os.path.join(owner_dir, repo)
            if not os.path.isdir(repo_dir): continue
            for subdir in ["project-info", "codebase"]:
                target = os.path.join(repo_dir, subdir)
                if not os.path.isdir(target): continue
                try:
                    mtime = os.path.getmtime(target)
                    if now - mtime < repo_ttl: continue
                    sz = get_size(target)
                    shutil.rmtree(target, ignore_errors=True)
                    cleaned += 1
                    freed_bytes += sz
                except: pass
            # Also clean stale issue workdirs (older than 3 days)
            issues_root = os.path.join(repo_dir, "issues")
            if os.path.isdir(issues_root):
                for num in os.listdir(issues_root):
                    issue_dir = os.path.join(issues_root, num)
                    if not os.path.isdir(issue_dir): continue
                    for agent in ["vod","issue","coding","qa","sre"]:
                        ad = os.path.join(issue_dir, agent)
                        if not os.path.isdir(ad): continue
                        try:
                            mtime = os.path.getmtime(ad)
                            if now - mtime < repo_ttl: continue
                            shutil.rmtree(ad, ignore_errors=True)
                            cleaned += 1
                        except: pass

print(f"cleaned={cleaned} freed_mb={freed_bytes//1024//1024}")
"""

def run_on_worker(tag, ip, role):
    print(f"\n=== {tag} ({ip}) {role} ===")
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(ip, 22, "root", PASSWORD, timeout=10)

        # Get before stats
        i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
        before = o.read().decode().strip()
        print(f"  Before: {before}")

        # Write and run cleanup script
        sftp = c.open_sftp()
        with sftp.file("/tmp/_clean_worker.py", "w") as f:
            f.write(CLEAN_SCRIPT)
        sftp.close()

        i, o, e = c.exec_command("python3 /tmp/_clean_worker.py 2>&1", timeout=60)
        result = o.read().decode().strip()
        err = e.read().decode().strip()
        print(f"  Result: {result}")
        if err:
            print(f"  Stderr: {err[:200]}")

        # Get after stats
        i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
        after = o.read().decode().strip()
        print(f"  After:  {after}")

        c.close()
    except Exception as ex:
        print(f"  ERROR: {ex}")

for tag, ip, role in WORKERS:
    run_on_worker(tag, ip, role)
