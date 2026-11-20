#!/usr/bin/env python3
"""审计「凭据密文解不开」的行（只读）。

背景（2026-11-18）：凭据密文由 `JWT_SECRET` 派生的密钥加密（kid 恒为 `derived`）。
某段时间内的进程若用了另一份 `JWT_SECRET`，它写进去的凭据在换回正确密钥后就**永久
解不开**——而读侧此前的实现把解密失败吞成 `{}`，用户面表现为"这个平台没配凭据"，
项目列表整块消失（dev 实测：某账号 6 条 PAT + 81 条仓库缓存都在，页面却是引导页）。

本脚本只做三件事（不写库）：
  1. 统计 `user_credentials` / `external_identities` 里解不开的行；
  2. 按账号/平台打印，并给出处置建议；
  3. 提示可用的自愈路径（重配 PAT / 重新 OAuth 授权）。

用法（必须在部署树上跑，且要带上运行时的环境变量，否则你自己也会 `Credential key not found`）：

    set -a && . /etc/grape/grape.env && set +a
    /root/grape/.venv/bin/python3 scripts/audit_credential_decrypt.py
    # 只看某个账号：--user <user_id>
    # 机器可读：--json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db_config():
    """拿当前进程该连的库（与运行中的 mgmt 进程同源）。"""
    from src.kernel.session_manager import db  # noqa: F401  （导入即初始化连接配置）
    return None


def _rows(sql: str, args: tuple):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(sql, args)
        return c.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="审计解不开的凭据密文（只读）")
    ap.add_argument("--user", default="", help="只看某个 user_id")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if not os.environ.get("JWT_SECRET"):
        print("!! 环境里没有 JWT_SECRET：请先 `set -a && . /etc/grape/grape.env && set +a`，"
              "否则所有密文都会被误判为解不开", file=sys.stderr)
        return 2

    from src.domains.mgmt.auth.credential_crypto import decrypt

    broken_creds, broken_identities = [], []
    cred_sql = ("SELECT user_id, provider, COALESCE(kind,''), config_json, COALESCE(updated_at,'') "
                "FROM user_credentials")
    ident_sql = ("SELECT id, grape_user_id, provider, status, access_token, refresh_token "
                 "FROM external_identities WHERE LENGTH(access_token) > 0")
    creds = _rows(cred_sql + (" WHERE user_id=?" if args.user else ""), (args.user,) if args.user else ())
    idents = _rows(ident_sql + (" AND grape_user_id=?" if args.user else ""),
                   (args.user,) if args.user else ())

    for user_id, provider, kind, raw, updated_at in creds:
        if not raw:
            continue
        try:
            decrypt(raw)
        except Exception as e:
            broken_creds.append({"user_id": str(user_id), "provider": str(provider),
                                 "kind": str(kind or ""), "updated_at": str(updated_at or ""),
                                 "error": str(e)[:60] or type(e).__name__})

    for rid, user_id, provider, status, at, rt in idents:
        try:
            decrypt(at)
            if rt:
                decrypt(rt)
        except Exception as e:
            broken_identities.append({"id": str(rid), "user_id": str(user_id),
                                      "provider": str(provider), "status": int(status or 0),
                                      "error": str(e)[:60] or type(e).__name__})

    if args.json:
        print(json.dumps({"user_credentials": broken_creds,
                          "external_identities": broken_identities},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"user_credentials 总行数={len(creds)}  解不开={len(broken_creds)}")
    for r in broken_creds:
        print(f"  - user={r['user_id']} provider={r['provider']} kind={r['kind'] or '-'} "
              f"updated_at={r['updated_at']} ({r['error']})")
    print(f"external_identities(有 token) 总行数={len(idents)}  解不开={len(broken_identities)}")
    for r in broken_identities:
        print(f"  - id={r['id']} user={r['user_id']} provider={r['provider']} "
              f"status={r['status']} ({r['error']})")

    if broken_creds or broken_identities:
        users = sorted({r["user_id"] for r in broken_creds + broken_identities})
        print("\n处置建议（明文不可恢复：换过密钥且没有旧密钥副本）：")
        print(f"  1) 让这些账号在「账号 → 凭据」页重新保存一次 PAT（会按当前密钥重写密文），"
              f"影响账号: {', '.join(users)}")
        print("  2) OAuth 平台也可在原账号页重新授权一次（等价）")
        print("  3) 修复后（凭据可见性修复分支）这些行会显示为「PAT 已损坏，请重配」，"
              "而不再静默消失")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
