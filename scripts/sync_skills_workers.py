#!/usr/bin/env python3
"""Sync skills directory to all 5 workers."""
import paramiko, os

WORKERS = [
    ("0002", "127.0.0.1"),
    ("0003", "127.0.0.1"),
    ("0004", "192.168.0.39"),
    ("0005", "192.168.0.194"),
    ("0006", "192.168.0.213"),
]
PASSWORD = "YOUR_SSH_PASSWORD"
LOCAL_SKILLS = "/opt/grape/skills"

def sync_dir(sftp, local, remote):
    """Recursively sync a directory via SFTP."""
    for item in os.listdir(local):
        local_path = os.path.join(local, item)
        remote_path = remote + "/" + item
        if os.path.isdir(local_path):
            try:
                sftp.mkdir(remote_path)
            except:
                pass
            sync_dir(sftp, local_path, remote_path)
        else:
            sftp.put(local_path, remote_path)

for tag, ip in WORKERS:
    print(f"=== {tag} ({ip}) ===")
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(ip, 22, "root", PASSWORD, timeout=10, look_for_keys=False, allow_agent=False)
        sftp = c.open_sftp()

        # Create skills dir
        try:
            sftp.mkdir("/opt/grape/skills")
        except:
            pass

        # Sync all subdirectories
        for subdir in os.listdir(LOCAL_SKILLS):
            local_sub = os.path.join(LOCAL_SKILLS, subdir)
            if not os.path.isdir(local_sub):
                sftp.put(local_sub, "/opt/grape/skills/" + subdir)
                continue
            remote_sub = "/opt/grape/skills/" + subdir
            try:
                sftp.mkdir(remote_sub)
            except:
                pass
            sync_dir(sftp, local_sub, remote_sub)

        sftp.close()

        # Verify
        i, o, e = c.exec_command("find /opt/grape/skills -name SKILL.md 2>/dev/null | wc -l", timeout=10)
        count = o.read().decode().strip()
        print(f"  SKILL.md count: {count}")

        # Also sync skill-description.json
        sftp2 = c.open_sftp()
        sftp2.put("/opt/grape/configs/skill-description.json", "/opt/grape/configs/skill-description.json")
        sftp2.close()

        c.close()
    except Exception as ex:
        print(f"  ERROR: {ex}")
