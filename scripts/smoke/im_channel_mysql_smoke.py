"""IM 渠道层 MySQL 适配冒烟（Issue #742/#760/#825 + 本地 MySQL 适配）。

做法：用**临时库**（默认 im_adapt_check，跑完即删）走真实代码路径——
DEVOPS_DB_BACKEND=mysql 时 src.session_manager.db 就是 MySQLCompatDB，
engine_wiring 的仓储、迁移、去重、绑定、对账全部跑在这条连接上。

用法：
    cd <repo-root>
    DEVOPS_DB_BACKEND=mysql DEVOPS_DB_NAME=im_adapt_check \
      .venv/bin/python scripts/smoke/im_channel_mysql_smoke.py [--keep]

退出码 0 = 全部通过。
"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
except Exception:
    pass

import pymysql  # noqa: E402

DB_NAME = os.environ.get("DEVOPS_DB_NAME", "im_adapt_check")

LEGACY_DDL = [
    # A2 Phase B 起不再种 legacy im_* 行（列已 DROP，回填移到 002 一次性迁移）；
    # 这里只保留渠道装配需要的历史表。
    """CREATE TABLE IF NOT EXISTS unified_user_mapping (
        feishu_open_id VARCHAR(191) PRIMARY KEY,
        user_id INT NOT NULL DEFAULT 0
    )""",
]

_FAILS: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {extra}" if extra else ""))
    if not ok:
        _FAILS.append(label)


def main() -> int:
    from src.mysql_compat import mysql_config
    from src.im.channel.types import ChatType, MsgType, Scope, Type

    cfg = mysql_config()
    assert os.environ.get("DEVOPS_DB_BACKEND", "").lower() == "mysql", \
        "必须显式设置 DEVOPS_DB_BACKEND=mysql 才能跑本冒烟"

    admin = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                            password=cfg["password"], autocommit=True)
    ac = admin.cursor()
    ac.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
    ac.execute(f"CREATE DATABASE {DB_NAME} DEFAULT CHARSET utf8mb4")
    print(f"[1] 临时库 {DB_NAME} 已建（{cfg['host']}:{cfg['port']}）")

    # 用临时库的 compat 连接建历史表（A2 Phase B：不再种 legacy im_* 行）
    from src.mysql_compat import MySQLCompatDB
    tmp_cfg = dict(cfg, database=DB_NAME)
    conn = MySQLCompatDB(**tmp_cfg)
    for ddl in LEGACY_DDL:
        conn.execute(ddl)
    conn.commit()
    print("[2] 历史表就绪（legacy im_* 行不再种：列已 DROP）")

    # 切到真实代码路径：session_manager.db 绑到临时库
    from src.im import engine_wiring as W

    stats = W.run_startup_migration(W.default_db_path())
    check("启动迁移不跑回填（A2 Phase B 已移到 002 一次性迁移）",
          stats.get("backfill") == "moved-to-002_drop_legacy_im",
          f"backfill={stats.get('backfill')}")

    # 直接走写穿桥建 3 条安装：1 user（app 去重语义由安装表唯一索引保证）+ 2 repo（app 共享）
    W.sync_installation("user", owner_user_id="7", app_id="cli_user_app", app_secret="sec_u")
    W.sync_installation("repo", repo_key="grape-dev/grape", app_id="cli_repo_app", app_secret="sec_r")
    W.sync_installation("repo", repo_key="XOcto/hello-blog", app_id="cli_repo_app", app_secret="sec_r")
    print("[3] 安装行就绪：user app×1 / repo 2 仓（app 共享）")

    shares = W._shared_db()
    total = shares.execute("SELECT COUNT(*) FROM grape_t_channel_installation").fetchone()[0]
    check("grape_t_channel_installation 行数 = 3（1 user + 2 repo）", total == 3, f"rows={total}")

    # 4 表 + wiring 自有消息表 + 唯一索引
    names = {r[0] for r in shares.execute("SHOW TABLES").fetchall()}
    want = {"grape_t_channel_installation", "grape_t_channel_chat_session_binding", "grape_t_channel_inbound_dedup",
            "grape_t_channel_sender_identity", "grape_t_channel_inbound_message"}
    check("5 张表齐全", want.issubset(names), f"missing={sorted(want - names)}")
    idx = shares.execute(
        "SELECT COUNT(*) FROM information_schema.statistics WHERE TABLE_SCHEMA=DATABASE()"
        " AND TABLE_NAME='grape_t_channel_installation' AND INDEX_NAME='uk_channel_installation_legacy'"
    ).fetchone()[0]
    check("表达式唯一索引 uk_channel_installation_legacy 存在", idx >= 1, f"parts={idx}")

    # 唯一索引语义：同 (legacy_app_id, scope, coalesce(repo_key,owner)) 重复插入必须被拒
    try:
        shares.execute(
            "INSERT INTO grape_t_channel_installation (id,channel_type,scope,owner_user_id,repo_key,"
            "config,config_fingerprint,status,legacy_app_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("dup-id", "feishu", "user", "7", "", "{}", "x", "active", "cli_user_app"))
        shares.commit()
        dup_ok = False
        dup_note = "重复行未被唯一索引拒绝"
    except Exception as e:
        try:
            shares.rollback()
        except Exception:
            pass
        dup_ok = "1062" in str(e) or "Duplicate" in str(e)
        dup_note = str(e)[:60]
    check("唯一索引拦截重复安装", dup_ok, dup_note)

    # 安装提供者枚举（Supervisor 的真实数据源）
    from src.im.engine.installations import SqliteInstallationProvider
    prov = SqliteInstallationProvider(lambda: shares)
    import asyncio
    rows = asyncio.run(prov())
    check("provider 枚举 active 安装", len(rows) == 3, f"rows={len(rows)}")

    # 旧 IM 下线 A2 M2：双轨对账机制已删除（不再有 legacy 镜像需要核对）

    # 两阶段去重
    store = W.SqliteDedupStore("ignored-mysql")
    first = store.try_claim("inst-1", "mid-1", "tok-a")
    second = store.try_claim("inst-1", "mid-1", "tok-b")
    check("去重 claim 首次成功/二次失败", first is True and second is False, f"{first}/{second}")
    store.mark("inst-1", "mid-1")
    check("去重 mark 后 seen=True", store.seen("inst-1", "mid-1") is True)
    store.release("inst-1", "mid-1")
    check("去重 release 后 seen=False", store.seen("inst-1", "mid-1") is False)

    # 会话绑定 upsert（同 key 只一行，last_active_at 更新）
    from src.im.engine.session import SessionBinding
    bind = W.SqliteBindingStore("ignored-mysql")
    b = SessionBinding(installation_id="inst-1", binding_key="chat-9", chat_type="group",
                       scope=Scope.USER, repo_key="", agent_type="chat", runtime_session_id="s1",
                       config={"k": "v"})
    bind.upsert(b)
    b2 = SessionBinding(installation_id="inst-1", binding_key="chat-9", chat_type="group",
                        scope=Scope.USER, repo_key="grape-dev/grape", agent_type="chat",
                        runtime_session_id="s2", config={"k": "v2"})
    bind.upsert(b2)
    got = bind.get("inst-1", "chat-9")
    n = shares.execute("SELECT COUNT(*) FROM grape_t_channel_chat_session_binding").fetchone()[0]
    # 注意：上游 upsert 的 ON CONFLICT DO UPDATE 列表不含 runtime_session_id（既有行为，
    # 本次只做方言适配不改语义），故断言 repo_key/config 更新、单行、session_id 保持原值。
    check("绑定 upsert 单行且字段更新",
          n == 1 and got is not None and got.repo_key == "grape-dev/grape"
          and got.config.get("k") == "v2" and got.runtime_session_id == "s1",
          f"rows={n} repo={getattr(got, 'repo_key', None)} sid={getattr(got, 'runtime_session_id', None)}")

    # ⑦ 持久化 + dedup mark 同事务（含 datetime('now') 翻译）
    from src.im.channel.message import InboundMessage, Source
    msg = InboundMessage(
        event_id="ev-7", message_id="mid-7", type=MsgType.TEXT, text="hello",
        source=Source(channel_type=Type.FEISHU, chat_id="chat-9",
                      chat_type=ChatType.GROUP, sender_id="ou_1"))
    W._append_message_sync("ignored-mysql", "inst-1", msg)
    W._append_message_sync("ignored-mysql", "inst-1", msg)   # 重投：INSERT IGNORE 不炸
    nm = shares.execute("SELECT COUNT(*) FROM grape_t_channel_inbound_message").fetchone()[0]
    state = shares.execute(
        "SELECT state FROM grape_t_channel_inbound_dedup WHERE installation_id=? AND message_id=?",
        ("inst-1", "mid-7")).fetchone()
    check("消息持久化幂等 + dedup 置 marked",
          nm == 1 and state is not None and state[0] == "marked", f"rows={nm} state={state}")

    # 身份解析（grape_t_channel_sender_identity 优先 → unified_user_mapping 回退）
    shares.execute("INSERT INTO unified_user_mapping (feishu_open_id,user_id) VALUES (?,?)",
                   ("ou_1", 42))
    shares.commit()
    check("身份回退映射命中 user 42", W._lookup_user_sync("ignored-mysql", "inst-1", "ou_1") == "42")

    # 写穿桥（UI 保存/解绑 → grape_t_channel_installation）：MySQL 上同样幂等 + 可回滚
    ok = W.sync_installation("repo", repo_key="gitcode:acme/api",
                                    app_id="cli_ui_bridge", app_secret="sec_bridge")
    row = shares.execute("SELECT status, config FROM grape_t_channel_installation "
                         "WHERE legacy_app_id=? AND scope='repo'", ("cli_ui_bridge",)).fetchone()
    check("写穿桥 sync → active 且带 secret",
          ok and row is not None and row[0] == "active"
          and json.loads(row[1]).get("app_secret") == "sec_bridge",
          f"ok={ok} row={row}")

    ok2 = W.sync_installation("repo", repo_key="gitcode:acme/api", app_id="cli_ui_bridge")
    rows2 = shares.execute("SELECT COUNT(*), config FROM grape_t_channel_installation "
                          "WHERE legacy_app_id=? AND scope='repo'", ("cli_ui_bridge",)).fetchone()
    check("写穿桥幂等 + 空 secret 沿用旧值",
          ok2 and rows2[0] == 1 and json.loads(rows2[1]).get("app_secret") == "sec_bridge",
          f"rows={rows2[0]}")

    ok3 = W.revoke_installation("repo", repo_key="gitcode:acme/api", app_id="cli_ui_bridge")
    st = shares.execute("SELECT status FROM grape_t_channel_installation WHERE legacy_app_id=?",
                        ("cli_ui_bridge",)).fetchone()
    check("写穿桥 revoke → status=revoked（不硬删）", ok3 and st and st[0] == "revoked", f"st={st}")

    ok4 = W.sync_installation("repo", repo_key="gitcode:acme/api",
                                     app_id="cli_ui_bridge", app_secret="sec_bridge")
    st = shares.execute("SELECT status FROM grape_t_channel_installation WHERE legacy_app_id=?",
                        ("cli_ui_bridge",)).fetchone()
    check("写穿桥重新绑定 → 回到 active", ok4 and st and st[0] == "active", f"st={st}")

    # 引擎只枚举 active：revoked 行对 provider 不可见
    W.revoke_installation("repo", repo_key="gitcode:acme/api", app_id="cli_ui_bridge")
    rows_live = asyncio.run(prov())
    check("provider 只枚举 active（revoked 不可见）",
          all(r["installation_id"] != "cli_ui_bridge" for r in rows_live)
          and len(rows_live) == 3, f"rows={len(rows_live)}")

    # [B-1] TTL 清理必须走 MySQL 可用的写法：参数化 cutoff，验证过期行真的被删
    from src.runtime import cleanup as _cleanup
    shares.execute("INSERT INTO grape_t_channel_inbound_dedup (installation_id, message_id, created_at)"
                   " VALUES ('smoke','old',?)", ("2020-01-01 00:00:00",))
    shares.execute("INSERT INTO grape_t_channel_inbound_dedup (installation_id, message_id, created_at)"
                   " VALUES ('smoke','fresh',?)", ("2099-01-01 00:00:00",))
    shares.execute("INSERT INTO grape_t_channel_inbound_message (installation_id, message_id, created_at)"
                   " VALUES ('smoke','old',?)", ("2020-01-01 00:00:00",))
    shares.execute("INSERT INTO grape_t_channel_inbound_message (installation_id, message_id, created_at)"
                   " VALUES ('smoke','fresh',?)", ("2099-01-01 00:00:00",))
    shares.execute("INSERT INTO grape_t_channel_outbound_card (out_track_id, installation_id, created_at)"
                   " VALUES ('smoke-old','smoke',?)", ("2020-01-01 00:00:00",))
    shares.execute("INSERT INTO grape_t_channel_outbound_card (out_track_id, installation_id, created_at)"
                   " VALUES ('smoke-fresh','smoke',?)", ("2099-01-01 00:00:00",))
    cleaned = _cleanup._sweep_im_tables()
    left = {
        "dedup": sorted(r[0] for r in shares.execute(
            "SELECT message_id FROM grape_t_channel_inbound_dedup WHERE installation_id='smoke'").fetchall()),
        "msg": sorted(r[0] for r in shares.execute(
            "SELECT message_id FROM grape_t_channel_inbound_message WHERE installation_id='smoke'").fetchall()),
        "card": sorted(r[0] for r in shares.execute(
            "SELECT out_track_id FROM grape_t_channel_outbound_card WHERE installation_id='smoke'").fetchall()),
    }
    check("TTL 清理在 MySQL 真生效（过期行删除、未过期行保留）",
          cleaned >= 3 and left["dedup"] == ["fresh"] and left["msg"] == ["fresh"]
          and left["card"] == ["smoke-fresh"], f"cleaned={cleaned} left={left}")

    print()
    if _FAILS:
        print(f"结果：{len(_FAILS)} 项失败 → {_FAILS}")
    else:
        print("结果：全部通过 ✅")

    adm2 = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                           password=cfg["password"], autocommit=True)
    if os.environ.get("KEEP_SMOKE_DB") == "1":
        print(f"保留临时库 {DB_NAME} 供检查（KEEP_SMOKE_DB=1）")
    else:
        adm2.cursor().execute(f"DROP DATABASE {DB_NAME}")
        print(f"临时库 {DB_NAME} 已清理")
    return 0 if not _FAILS else 1


if __name__ == "__main__":
    sys.exit(main())