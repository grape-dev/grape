"""
DevOps Bot v2 - NDJSON streaming agent executor
Architecture: Bot = shell, Agent = brain.
Bot listens events → builds prompt → spawns opencode with NDJSON streaming
Agent autonomously reads skills, calls GitCode API, takes actions
Bot monitors & logs the Agent's execution process
"""
import socket, threading, json, urllib.request
from http.server import HTTPServer
from src.config import log, SKILLS_DIR, WORKSPACE_DIR, GITCODE_API_TOKEN, GITCODE_WEBHOOK_TOKEN
from src.feishu_handler import (
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BOT_NAME,
    start_ws, stop_ws, get_chat_members, ensure_configured
)
from src.session_manager import db, db_lock, set_feishu_open_id
from src.chat_handler import process_chat
from src.webhook_handler import Handler


def _sync_feishu_users():
    """Startup: sync Feishu group members to feishu_users table"""
    if not ensure_configured():
        return
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT chat_id, repo_key FROM feishu_bindings")
            bindings = c.fetchall()
        if not bindings:
            log.info("Feishu sync: no bindings found, skipping")
            return
        for chat_id, repo_key in bindings:
            members = get_chat_members(chat_id)
            if not members:
                continue
            # Get GitCode collaborators
            parts = repo_key.replace("gitcode:", "").split("/", 1)
            if len(parts) < 2:
                continue
            owner, repo = parts
            try:
                resp = urllib.request.urlopen(
                    urllib.request.Request(f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators"),
                    timeout=10
                )
                collabs = json.loads(resp.read())
            except Exception as e:
                log.warning(f"Feishu sync: failed to fetch collaborators for {repo_key}: {e}")
                continue
            # Match by nick_name
            matched = 0
            for collab in collabs:
                nick = collab.get("nick_name", "")
                login = collab.get("username", "")
                if not nick or not login:
                    continue
                for m in members:
                    if m.get("name") == nick:
                        set_feishu_open_id(repo_key, login, m["open_id"], nick)
                        matched += 1
                        break
            log.info(f"Feishu sync: {repo_key} -> {matched} users matched from {len(members)} group members")
    except Exception as e:
        log.warning(f"Feishu sync error: {e}")

if __name__ != "__main__":
    raise RuntimeError("src/main.py is the entry point, do not import it")

log.info(f"═══ DevOps Bot v2 (NDJSON streaming) starting on :8080 ═══")
log.info(f"Skills dir: {SKILLS_DIR}")
log.info(f"Workspace dir: {WORKSPACE_DIR}")
log.info(f"GitCode API token: {'YES' if GITCODE_API_TOKEN else 'NO'}")
log.info(f"GitCode webhook token: {'YES' if GITCODE_WEBHOOK_TOKEN else 'NO (webhook not verified)'}")
log.info(f"Feishu bot: {'CONFIGURED' if FEISHU_APP_ID else 'NOT CONFIGURED (set FEISHU_APP_ID)'}")
log.info(f"Routes: /webhook/gitcode /health /api/sessions")

server = HTTPServer(("", 8080), Handler)
server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

if FEISHU_APP_ID:
    threading.Thread(target=start_ws, args=(process_chat,), daemon=True, name="feishu-ws").start()
    threading.Thread(target=_sync_feishu_users, daemon=True, name="feishu-sync").start()

try:
    server.serve_forever()
except KeyboardInterrupt:
    stop_ws()
    server.server_close()
