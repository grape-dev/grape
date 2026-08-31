#!/usr/bin/env python3
"""Immediate cleanup of /tmp residuals on local + sibling node."""
import subprocess, shutil, os, time, glob

def clean_tmp():
    """Clean /tmp residual files older than 1 hour."""
    now = time.time()
    ttl = 3600
    cleaned = 0

    # Directory patterns to clean
    dir_patterns = [
        "/tmp/hermes-acp-*",
        "/tmp/hermes-iso-*",
        "/tmp/hermes-sandbox-*",
        "/tmp/hermes-snap-*",
        "/tmp/tf-deploy-*",
        "/tmp/tf-sys-xhy*",
        "/tmp/blog-build-*",
        "/tmp/blog-deploy*",
        "/tmp/blog-gen*",
        "/tmp/blog-publish-*",
        "/tmp/pip-unpack-*",
        "/tmp/pip-metadata-*",
        "/tmp/pytest-of-root",
    ]

    for pattern in dir_patterns:
        for path in glob.glob(pattern):
            try:
                mtime = os.path.getmtime(path)
                if now - mtime < ttl:
                    continue
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    cleaned += 1
                elif os.path.isfile(path):
                    os.remove(path)
                    cleaned += 1
            except Exception:
                pass

    # File targets (clean files inside directories + standalone files)
    file_targets = [
        "/tmp/hermes-results",
        "/tmp/tf-state-backup.json",
        "/tmp/inventory_state.json",
        "/tmp/inventory*.json",
        "/tmp/blog-sys-*.tar.gz",
        "/tmp/.39ee*.so",
    ]
    for pattern in file_targets:
        for path in glob.glob(pattern):
            try:
                if os.path.isdir(path):
                    for f in os.listdir(path):
                        fp = os.path.join(path, f)
                        try:
                            mtime = os.path.getmtime(fp)
                            if now - mtime < ttl:
                                continue
                            if os.path.isfile(fp):
                                os.remove(fp)
                                cleaned += 1
                        except Exception:
                            pass
                elif os.path.isfile(path):
                    mtime = os.path.getmtime(path)
                    if now - mtime < ttl:
                        continue
                    os.remove(path)
                    cleaned += 1
            except Exception:
                pass

    return cleaned

if __name__ == "__main__":
    import sys

    # Clean local
    print(f"=== Cleaning {os.uname().nodename} ===")
    before = subprocess.run(["du", "-sh", "/tmp"], capture_output=True, text=True).stdout.strip()
    print(f"Before: {before}")
    n = clean_tmp()
    after = subprocess.run(["du", "-sh", "/tmp"], capture_output=True, text=True).stdout.strip()
    print(f"Cleaned {n} items")
    print(f"After: {after}")

    # Clean sibling 0001 via SSH
    print("\n=== Cleaning sibling 0001 (127.0.0.1) ===")
    script = __file__
    # Copy script to sibling and run it there
    subprocess.run(["scp", script, "root@127.0.0.1:/tmp/clean_tmp.py"], timeout=10)
    r = subprocess.run(
        ["ssh", "root@127.0.0.1",
         "python3 /tmp/clean_tmp.py 2>&1 || .venv/bin/python3 /tmp/clean_tmp.py 2>&1 || /opt/grape/.venv/bin/python3 /tmp/clean_tmp.py 2>&1"],
        capture_output=True, text=True, timeout=30)
    print(r.stdout)
    if r.stderr:
        print(f"stderr: {r.stderr[:200]}")
