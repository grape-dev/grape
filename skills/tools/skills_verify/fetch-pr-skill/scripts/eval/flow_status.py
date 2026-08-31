#!/usr/bin/env python3
import os
from typing import Optional

import pymysql

from .config import DB_CONFIG

VALID_STATUSES = {"processing", "done", "failed"}
WORKSPACE_SLUG = "skills-prod"
APP_URL = "https://multica.clouddeveloper.club"


def _get_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset=DB_CONFIG.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_multica_issue_id(skill_path: str) -> Optional[str]:
    for start in (os.getcwd(), os.path.abspath(skill_path)):
        current = start
        while True:
            ctx_file = os.path.join(current, ".multica", "daemon_task_context.json")
            if os.path.exists(ctx_file):
                try:
                    import json

                    with open(ctx_file, "r") as f:
                        data = json.load(f)
                    return data.get("issue_id") or None
                except Exception:
                    break  # 该起点损坏，跳到下一起点
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def build_verify_url(issue_id: str, pr_url: Optional[str] = None) -> str:
    return f"{APP_URL}/{WORKSPACE_SLUG}/issues/{issue_id}"


def update_verify_status(issue_id: str, status: str, url: Optional[str] = None) -> bool:
    if status not in VALID_STATUSES:
        print(f"    Warning: invalid verify status '{status}', skipped")
        return False
    if not issue_id:
        return False
    try:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE `t_skills_dev_flow` "
                    "SET node_verify_status = %s, node_verify_time = NOW(), node_verify_url = %s "
                    "WHERE multica_issue_id = %s",
                    (status, url, issue_id),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        print(f"    Warning: update_verify_status failed: {e}")
        return False
