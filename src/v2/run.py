"""Agentic DevOps v2 entry point — starts both legacy server and new API server"""
import sys, os, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def start_v2_api():
    """启动 v2 FastAPI 服务（端口 3000）"""
    import uvicorn
    from src.v2.api.main import app
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")


if __name__ == "__main__":
    from src.config import log
    log.info("Starting Agentic DevOps v2...")

    # 启动 v2 API 服务
    t = threading.Thread(target=start_v2_api, daemon=True, name="v2-api")
    t.start()
    time.sleep(2)
    log.info("v2 API server started on :3000")

    # 启动原有系统（端口 80 + 8080）
    from src.admin.server import start_admin_server
    from src.im.feishu import FEISHU_APP_ID, FEISHU_BOT_NAME, start_ws, ensure_configured
    from src.im.handler import process_chat
    from http.server import HTTPServer
    from src.config import ACCOUNTS
    from src.webhook_handler import Handler
    from src.session_manager import db, db_lock
    import socket

    threading.Thread(target=start_admin_server, daemon=True, name="admin").start()
    log.info("Admin server started on :80")

    server = HTTPServer(("", 8080), Handler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    for name, acc in ACCOUNTS.items():
        app_id = acc.get("feishu_app_id", "")
        if app_id:
            threading.Thread(target=start_ws, args=(process_chat, name),
                             daemon=True, name=f"fs-ws-{name}").start()

    log.info("Webhook server started on :8080")
    server.serve_forever()
