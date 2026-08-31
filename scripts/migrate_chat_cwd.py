#!/usr/bin/env python3
"""一次性迁移 opencode.db 中 chat 会话的 directory 到新统一结构
（web-chat-no-response-cwd-fix.md §4 存量修正）。

旧形态：
  /home/grape/chat/{uid}/{conv}
  /opt/grape_workspace/chat/{uid}/{conv}
  {WORKSPACE_DIR}/chat/{uid}/{conv}
  {WORKSPACE_DIR}/feishu/{owner_repo}/{sess}

新形态：
  {WORKSPACE_DIR}/{owner}/{repo}/chat/{uid}/{conv_id}

依赖 chat_conversations 表（repo_key, user_id, id=conv_id）反查 owner/repo/uid。
"""
import os, sqlite3, sys

GRAPE_DB = "/opt/grape/data/sessions.db"
OPCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
WORKSPACE_DIR = os.path.expanduser("~")
DRY_RUN = "--apply" not in sys.argv


def parse_chat_old(path: str):
    """识别旧 chat 结构，返回 (uid, conv_id) 或 None；feishu 旧结构返回 ("feishu", sess)。"""
    if "/chat/" in path:
        # .../chat/{uid}/{conv}
        idx = path.index("/chat/")
        rest = path[idx + len("/chat/"):]
        parts = rest.strip("/").split("/")
        if len(parts) >= 2:
            return ("uid_conv", parts[0], parts[1])
    if "/feishu/" in path:
        # .../feishu/{owner_repo}/{sess}
        idx = path.index("/feishu/")
        rest = path[idx + len("/feishu/"):]
        parts = rest.strip("/").split("/")
        if len(parts) >= 2:
            return ("feishu_sess", parts[0], parts[1])
    return None


def main():
    g = sqlite3.connect(GRAPE_DB)
    convs = {}
    try:
        for r in g.execute("SELECT id, repo_key, user_id FROM chat_conversations").fetchall():
            convs[r[0]] = (r[1] or "", r[2] or 0)
    except Exception as e:
        print("chat_conversations 读取失败:", e); return 1

    from src.repo_key import parse_repo_key  # noqa: E402

    if not os.path.exists(OPCODE_DB):
        print("opencode.db 不存在:", OPCODE_DB); return 0
    db = sqlite3.connect(OPCODE_DB)
    rows = db.execute("SELECT id, directory FROM session").fetchall()
    changed = 0
    for sid, directory in rows:
        if not directory:
            continue
        old = parse_chat_old(directory)
        if old is None:
            continue
        kind = old[0]
        if kind == "uid_conv":
            uid, conv = old[1], old[2]
            repo_key, db_uid = convs.get(conv, ("", 0))
            owner_id = uid if db_uid == 0 else db_uid
        else:  # feishu_sess：通过 im_channel 反查 conv 不在此；跳过记录，需人工
            print("SKIP (feishu 需人工反查 conv_id):", sid, directory)
            continue
        _, owner, repo = parse_repo_key(repo_key or "")
        if not owner or not repo:
            print("SKIP (无 repo_key):", sid, directory); continue
        new_dir = os.path.join(WORKSPACE_DIR, "grape-workspaces", owner, repo,
                               "chat", str(owner_id), conv)
        if new_dir == directory:
            continue
        print(("DRY  " if DRY_RUN else "MOVE ") + f"{sid}: {directory} -> {new_dir}")
        if not DRY_RUN:
            db.execute("UPDATE session SET directory=?, path=? WHERE id=?", (new_dir, new_dir, sid))
            changed += 1
    if not DRY_RUN:
        db.commit()
        print(f"updated {changed} sessions")
    else:
        print("dry-run，未改动；加 --apply 生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())