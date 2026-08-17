#!/usr/bin/env python3
"""
AI 助手代理脚本（移植自 VOD vod-system/backend/ai_hermes_query.py，grape 适配版）
通过 Hermes Agent CLI 执行查询，支持流式输出和会话续接。
输出 JSON 事件（每行一个）:
  {"type":"token","data":"..."} / {"type":"session","data":"<id>"} / {"type":"done","data":""} / {"type":"error","data":"..."}
"""
import sys
import os
import json
import argparse
import subprocess
import re

CHART_HINT = (
    "\n\n[输出要求] 查询结果涉及统计数据或分布信息时，必须使用 ```chart 代码块包裹 echarts 配置 JSON 输出图表，"
    "禁止只用文字描述统计结果（文字分析可保留，但图表必须输出）。"
    "JSON 必须是完整的 echarts option 对象，包含 title/tooltip/xAxis/yAxis/series 等字段。"
    "可以同时输出文字分析和多个图表。示例格式:\n"
    "```chart\n"
    '{"title":{"text":"标题"},"tooltip":{},"xAxis":{"data":["A","B","C"]},'
    '"yAxis":{},"series":[{"type":"bar","data":[10,20,30]}]}\n'
    "```\n"
    "请确保 JSON 格式正确，不要有多余的逗号或缺少引号。"
)

def db_hint() -> str:
    """共享库只读访问提示。凭据全部来自环境变量；未配置则不加提示（不留硬编码默认值）。"""
    host = os.environ.get("GRAPE_DB_HOST", "")
    user = os.environ.get("GRAPE_DB_USER", "")
    password = os.environ.get("GRAPE_DB_PASSWORD", "")
    name = os.environ.get("GRAPE_DB_NAME", "")
    if not (host and user and password and name):
        return ""
    return (
        "\n\n[数据库只读访问] 你可以用以下命令查询共享库（只读，禁止任何修改）:\n"
        f"mysql -h{host} -u{user} -p'{password}' {name} -e \"SELECT ...\"\n"
        f"注意：此账号只有 SELECT 权限，禁止执行 INSERT/UPDATE/DELETE/CREATE/DROP 等写操作。"
    )

def resolve_hermes_bin() -> str:
    hermes_bin = os.environ.get("HERMES_BIN", "")
    if hermes_bin and os.path.exists(hermes_bin):
        return hermes_bin
    expanded = os.path.expanduser("~/.local/bin/hermes")
    if os.path.exists(expanded):
        return expanded
    return "hermes"

def emit(event: dict):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main():
    parser = argparse.ArgumentParser(description="Grape admin AI 助手代理")
    parser.add_argument("--query", required=True, help="用户查询")
    parser.add_argument("--session-id", default=None, help="会话ID（用于续接）")
    args = parser.parse_args()

    hermes_bin = resolve_hermes_bin()
    session_id = args.session_id

    # 首次查询附加图表指引 + 共享库只读提示；续接会话不加
    full_query = args.query
    if not session_id:
        full_query = args.query + CHART_HINT + db_hint()

    try:
        # AI 助手专属工作区（固定目录——会话 workspace 隔离——不与其他服务混）
        WORKSPACE_DIR = "/tmp/ai-hermes-ws"
        os.makedirs(WORKSPACE_DIR, exist_ok=True)

        # 会话绑定：--continue <前端会话 id>——hermes 按名称创建/续接会话——
        # 前端不感知 hermes 内部 sessionId（只管历史记录——同名续接自动绑定）
        cmd = [hermes_bin, "-z", full_query, "--yolo", "--continue", session_id] if session_id else \
              [hermes_bin, "-z", full_query, "--yolo"]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=WORKSPACE_DIR, env=os.environ.copy(), text=True)

        new_session_id = session_id   # 绑定名 = 前端会话 id（--continue 创建/续接同一会话）
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            emit({"type": "token", "data": line})

        process.wait()
        err = process.stderr.read()
        if process.returncode != 0 and err:
            emit({"type": "error", "data": err.strip()[:1000]})

        emit({"type": "done", "data": ""})
    except Exception as e:
        emit({"type": "error", "data": str(e)[:1000]})

if __name__ == "__main__":
    main()
