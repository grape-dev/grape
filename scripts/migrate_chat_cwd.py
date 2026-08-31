#!/usr/bin/env python3
"""一次性迁移 opencode.db 中【被 chat_conversations 引用】的 chat 会话 directory 到新结构。

web-chat-no-response-cwd-fix.md §4 存量修正：
旧形态（上次执行确认 119 个 chat-pattern session，仅 25 个被 conversation.runtime_session_id 引用）：
  /home/grape/chat/{uid}/{conv}
  /opt/grape_workspace/chat/{uid}/{conv}
  {WORKSPACE_DIR}/chat/{uid}/{conv}
新形态：
  {WORKSPACE_DIR}/{owner}/{repo}/chat/{uid}/{conv_id}

- 仅迁移「conversation.runtime_session_id == session.id」的会话（活跃对话可续聊），
  孤儿 session 不迁移（历史残留，不影响使用）。
- owner/repo/uid/conv 从 chat_conversations 权威取（repo_key / user_id / id）。
- 幂等：已在新结构的跳过。
"""
import os, sqlite3, sys

GRAPE_DB = "/opt/grape/data/sessions.db"
OPCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
NEW_BASE = os.path.expanduser("~/grape-workspaces")
DRY_RUN = "--apply" not in sys.argv


def main():
    from src.repo_key import parse_repo_key  # noqa: E402

    g = sqlite3.connect(GRAPE_DB)
    # id -> (repo_key, user_id)
    convs = {}
    try:
        for rid, rk, uid in g.execute("SELECT id, repo_key, user_id FROM chat_conversations").fetchall():
            convs[rid] = (rk or "", uid or 0)
    except Exception as e:
        print("read chat_conversations failed:", e); return 1

    # session.id -> conv_id（被引用的）
    sess_to_conv = {}
    try:
        for sid, cid in g.execute("SELECT runtime_session_id, id FROM chat_conversations WHERE runtime_session_id LIKE 'ses_%'").fetchall():
            if sid:
                sess_to_conv[sid] = cid
    except Exception as e:
        print("read runtime_session_id failed:", e); return 1

    db = sqlite3.connect(OPCODE_DB)
    rows = db.execute("SELECT id, directory FROM session").fetchall()
    planned = 0
    for sid, directory in rows:
        if not directory or "/chat/" not in directory:
            continue
        conv_id = sess_to_conv.get(sid)
        if not conv_id:
            continue  # 只迁被引用的
        rk, uid = convs.get(conv_id, ("", 0))
        if not rk:
            print("SKIP (conv 无 repo_key):", sid); continue
        _, owner, repo = parse_repo_key(rk)
        if not owner or not repo:
            print("SKIP (repo_key 空件):", sid); continue
        new_dir = os.path.join(NEW_BASE, owner, repo, "chat", str(uid), conv_id)
        if new_dir == directory:
            continue
        print(("DRY  " if DRY_RUN else "MOVE ") + f"{sid}: {directory}\n      -> {new_dir}")
        if not DRY_RUN:
            db.execute("UPDATE session SET directory=?, path=? WHERE id=?", (new_dir, new_dir, sid))
        planned += 1
    if not DRY_RUN:
        db.commit()
        print(f"updated {planned} sessions")
    else:
        print(f"dry-run：{planned} 个被引用会话将迁移（加 --apply 生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())