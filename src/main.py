"""
DevOps Bot v2 - NDJSON streaming agent executor
Architecture: Bot = shell, Agent = brain.
Bot listens events → builds prompt → spawns opencode with NDJSON streaming
Agent autonomously reads skills, calls GitCode API, takes actions
Bot monitors & logs the Agent's execution process
"""
import socket, threading
from http.server import HTTPServer
from src.config import log, SKILLS_DIR, WORKSPACE_DIR, GITCODE_API_TOKEN, GITCODE_WEBHOOK_TOKEN
from src.feishu_handler import (
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BOT_NAME,
    start_ws, stop_ws
)
from src.chat_handler import process_chat
from src.webhook_handler import Handler

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

try:
    server.serve_forever()
except KeyboardInterrupt:
    stop_ws()
    server.server_close()
