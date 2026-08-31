#!/usr/bin/env python3
"""Clean single worker 0006 - /tmp 100% full."""
import paramiko, os, shutil, time, glob

CLEAN_SCRIPT = r'''
import os, shutil, time, glob
now = time.time()
ttl = 3600
repo_ttl = 3 * 86400
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
    "/tmp/node-compile-cache",
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
'''

def clean_worker(ip, tag):
    print(f"\n=== {tag} ({ip}) ===")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, 22, "root", "YOUR_SSH_PASSWORD", timeout=15, look_for_keys=False, allow_agent=False)

    i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
    print(f"  Before: {o.read().decode().strip()}")

    sftp = c.open_sftp()
    with sftp.file("/tmp/_clean_worker.py", "w") as f:
        f.write(CLEAN_SCRIPT)
    sftp.close()

    i, o, e = c.exec_command("python3 /tmp/_clean_worker.py 2>&1", timeout=120)
    print(f"  Result: {o.read().decode().strip()}")

    i, o, e = c.exec_command("df -h /tmp | tail -1; du -sh /opt/grape-workspaces/ 2>/dev/null", timeout=10)
    print(f"  After:  {o.read().decode().strip()}")
    c.close()

# Clean 0006
clean_worker("192.168.0.213", "0006 sre-worker")

# Clean 0002 (retry with look_for_keys=False)
try:
    clean_worker("127.0.0.1", "0002 vod-worker")
except Exception as ex:
    print(f"  0002 ERROR: {ex}")
    print("  0002 needs manual check - password may differ")
