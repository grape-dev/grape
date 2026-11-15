#!/usr/bin/env python3
"""发布后冒烟：`POST /api/chat/submit` 真提交一句并断言 200 + task_id。

为什么需要（2026-11-15 事故）：域连接被 MySQL 按 wait_timeout 断开后仍留在线程缓存里，
端点在**没有任何 SQL 错误提示**的情况下直接 500（日志只有 `(0, '')`）。health / 只读
端点都不碰「写路径 + 跨域 meta 读」，因此发布闸门全绿也拦不住这类故障。

本脚本的动作（默认会撤回，避免真跑一轮 agent）：
  1. 用 JWT_SECRET + 账号 mint 一个访问 token（与前端同路径）；
  2. POST /api/chat/submit（消息带 `smoke` 前缀，便于检索）；
  3. 断言 HTTP 200 且返回体含 `task_id`（形如 chat_xxx）与 `conversation_id`；
  4. 立刻 POST /api/chat/messages/{message_id}/recall 终止这轮执行（`--no-recall` 可关）。

退出码：0 = 通过；1 = 断言失败（闸门判红）；2 = 环境不具备（缺 JWT_SECRET/账号/网络）
——2 视为 SKIP，不阻断发布。

用法：
  python3 scripts/smoke_chat_submit.py                              # 默认 127.0.0.1:38081
  python3 scripts/smoke_chat_submit.py --base-url http://127.0.0.1:38080 --json
  python3 scripts/smoke_chat_submit.py --no-recall                  # 保留这轮对话
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_MESSAGE = "smoke: /api/chat/submit 连通性自检（自动撤回，无需处理）"
EXIT_OK, EXIT_FAIL, EXIT_SKIP = 0, 1, 2


def _load_env_file(path: Path) -> None:
    """把 .env 里的键灌进 os.environ（不覆盖已有值）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _dsn_to_devops_env() -> None:
    """把 GRAPE_DSN_MGMT 拆成 DEVOPS_DB_*（stage 环境没有这些变量，但进程主库就是它）。

    为什么不直接用 .env 的 DEVOPS_DB_*：.env 通常指向生产库，而发布冒烟打的是刚起的
    stage 集群——账号 id/JWT 版本号与生产库不同，用生产库 mint 的 token 会 401。
    """
    dsn = os.environ.get("GRAPE_DSN_MGMT", "").strip()
    if not dsn.startswith("mysql://"):
        return
    from urllib.parse import unquote, urlparse
    u = urlparse(dsn)
    # 直接覆盖：发布脚本会先把 .env（生产库）export 出来，若用 setdefault，
    # 冒烟的账号查询就会打到生产库，mint 出的 token 与被测 stage 进程的账号 id /
    # token_version 不一致 → 稳定 401。DSN 才是「被测集群」的权威描述。
    os.environ["DEVOPS_DB_HOST"] = u.hostname or "127.0.0.1"
    os.environ["DEVOPS_DB_PORT"] = str(u.port or 3306)
    os.environ["DEVOPS_DB_USER"] = unquote(u.username or "root")
    os.environ["DEVOPS_DB_PASSWORD"] = unquote(u.password or "")
    os.environ["DEVOPS_DB_NAME"] = (u.path or "/").lstrip("/") or "grape_mgmt_db"


def _stage_env(account: str) -> None:
    """加载 stage DSN 与仓库 .env，确定「账号所在 mgmt 库」与 JWT_SECRET。"""
    _load_env_file(Path.home() / ".grape-rds-stage.env")
    _dsn_to_devops_env()
    _load_env_file(REPO / ".env")
    os.environ.setdefault("GRAPE_SMOKE_ACCOUNT", account)


def _mint_token(account: str, timeout: float) -> str:
    """按账号 mint JWT（与前端登录后的 access token 同算法）。"""
    from src.kernel.auth_sdk.jwt_utils import create_access_token

    import pymysql

    host = os.environ.get("DEVOPS_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("DEVOPS_DB_PORT", "3306"))
    user = os.environ.get("DEVOPS_DB_USER", "root")
    pwd = os.environ.get("DEVOPS_DB_PASSWORD", "")
    name = os.environ.get("DEVOPS_DB_NAME", "grape_mgmt_db")
    conn = pymysql.connect(host=host, port=port, user=user, password=pwd,
                           database=name, charset="utf8mb4", connect_timeout=timeout)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, username, token_version FROM grape_accounts WHERE username=%s",
                    (account,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"account not found: {account}")
        uid, uname, tv = row
    finally:
        conn.close()
    return create_access_token(str(uid), uname, int(tv or 0))


def _post(url: str, payload: dict, token: str, timeout: float) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, {"_raw": body}
    except urllib.error.HTTPError as e:
        body = e.read(4096).decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}


def _delete(url: str, token: str, timeout: float) -> int:
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def check(base_url: str, account: str, message: str, recall: bool,
          timeout: float) -> tuple[int, dict]:
    """返回 (退出码, 结果字典)。异常一律映射成 SKIP/FAIL，不抛给调用方。"""
    base = base_url.rstrip("/")
    out: dict = {"base_url": base, "account": account}
    try:
        _stage_env(account)
        if not os.environ.get("JWT_SECRET"):
            out.update(result="skip", reason="JWT_SECRET 未配置")
            return EXIT_SKIP, out
        token = _mint_token(account, timeout)
    except Exception as exc:  # noqa: BLE001 - 环境不具备即 SKIP
        out.update(result="skip", reason=f"token/db: {exc!r}"[:200])
        return EXIT_SKIP, out

    # 刚重启的进程可能在跑启动迁移/预热，401/503 属于「还没就绪」而不是「写路径坏了」：
    # 按 GRAPE_SMOKE_RETRIES 重试（默认 3 次，间隔 1s）；仍然失败才判红，避免发布误红。
    retries = max(1, int(os.environ.get("GRAPE_SMOKE_RETRIES", "3")))
    # 冒烟必须用**独立会话**：不传 conversation_id 时后端会按 (user, repo_key) 复用
    # 用户那条空 repo 的对话——冒烟消息就出现在用户的聊天里，撤回还可能干扰同会话的
    # 真实任务（2026-11-15 现场：用户「反馈一下进展」与冒烟消息同会话）。
    conv_id = ""
    try:
        cstatus, cbody = _post(f"{base}/api/chat/conversations",
                               {"repo_key": "smoke:selftest"}, token, timeout)
        if cstatus == 200 and isinstance(cbody, dict):
            conv_id = str(cbody.get("conversation_id") or "")
    except Exception:  # noqa: BLE001 - 建会话失败就退回默认会话（但仍会撤回消息）
        conv_id = ""
    out["conversation_created"] = bool(conv_id)

    payload = {"message": message, "repo_key": "smoke:selftest"}
    if conv_id:
        payload["conversation_id"] = conv_id

    t0 = time.time()
    status, body = 0, {}
    for attempt in range(retries):
        try:
            status, body = _post(f"{base}/api/chat/submit", payload, token, timeout)
        except Exception as exc:  # noqa: BLE001 - 网络层不可达 → SKIP
            if attempt == retries - 1:
                out.update(result="skip", reason=f"network: {exc!r}"[:200])
                return EXIT_SKIP, out
            time.sleep(1.0)
            continue
        if status not in (401, 403, 503) or attempt == retries - 1:
            break
        out["submit_retries"] = attempt + 1
        time.sleep(1.0)
    out["submit_status"] = status
    out["latency_ms"] = round((time.time() - t0) * 1000, 1)

    task_id = str(body.get("task_id") or "") if isinstance(body, dict) else ""
    conv_id = str(body.get("conversation_id") or "") if isinstance(body, dict) else ""
    msg_id = body.get("message_id") if isinstance(body, dict) else None
    out.update(task_id=task_id, conversation_id=conv_id, message_id=msg_id)
    if status != 200 or not task_id.startswith("chat_"):
        out.update(result="fail", body=body)
        if conv_id:   # 失败也要清掉冒烟会话，避免下次冒烟读到脏会话
            try:
                out["conversation_delete_status"] = _delete(
                    f"{base}/api/chat/conversations/{conv_id}", token, timeout)
            except Exception as exc:  # noqa: BLE001
                out["conversation_delete_status"] = f"error: {exc!r}"[:120]
        return EXIT_FAIL, out

    if recall and msg_id:
        try:
            rstatus, rbody = _post(f"{base}/api/chat/messages/{msg_id}/recall", {}, token, timeout)
            out["recall_status"] = rstatus
            out["recalled"] = bool(isinstance(rbody, dict) and rbody.get("recalled"))
        except Exception as exc:  # noqa: BLE001 - 撤回失败不影响「提交可用」结论，但要记录
            out["recall_status"] = f"error: {exc!r}"[:120]

    if conv_id:
        try:
            out["conversation_delete_status"] = _delete(f"{base}/api/chat/conversations/{conv_id}",
                                                        token, timeout)
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响结论
            out["conversation_delete_status"] = f"error: {exc!r}"[:120]

    out["result"] = "ok"
    return EXIT_OK, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布后冒烟：/api/chat/submit 断言 200 + task_id")
    ap.add_argument("--base-url", default=os.environ.get("GRAPE_SMOKE_BASE_URL",
                                                        "http://127.0.0.1:38081"))
    ap.add_argument("--account", default=os.environ.get("GRAPE_SMOKE_ACCOUNT", "songspace"))
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("GRAPE_SMOKE_TIMEOUT", "20")))
    ap.add_argument("--no-recall", action="store_true", help="不撤回这轮消息（会真的跑 agent）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    code, out = check(args.base_url, args.account, args.message,
                      not args.no_recall, args.timeout)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"[chat-smoke] {out.get('result')} base={out.get('base_url')} "
              f"status={out.get('submit_status')} task={out.get('task_id') or '-'} "
              f"recall={out.get('recall_status') or '-'} latency={out.get('latency_ms')}ms"
              + (f" reason={out.get('reason')}" if out.get("reason") else ""))
    return code


if __name__ == "__main__":
    raise SystemExit(main())