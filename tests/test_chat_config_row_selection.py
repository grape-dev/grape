"""chat_agent_session 配置行读取优先级（第二步最小止血）测试。

背景：同一用户可能有**多条**全局配置行（本表历史上移除过 `UNIQUE(repo_key)`、id 与 user_id
经迁移后错位）。写路径只写 `id='sess-cfg-{user_id}'`，而原读路径是
`... WHERE user_id=? ... LIMIT 1`（**无 ORDER BY**）→ 读到哪行由存储顺序决定，
出现"界面改了配置读不回来"。

新行为（本次）：
  ① 写路径那一行优先 → 保证"写进去的配置一定读得回来"；
  ② 该行引擎不可用（引擎行不存在/未知引擎）时**不把可用配置降级**，退回最近活跃的遗留行；
  ③ 全部不可用 → 保持既有兜底语义（runtime_id 置零 + 本机直连/空兜底）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, get_chat_config  # noqa: E402

R_OK = 9401      # 可用引擎（本机直连 credential_id=0）
R_GONE = 9499    # 不存在的引擎 id（幽灵引擎）
UID = 94001      # 隔离用的 user_id（不与真实数据冲突）
UID2 = 94002


def _reset_engine_cache():
    """清掉 `resolve_local_engine()` 的 30s 模块级缓存。

    为什么需要：无配置/引擎不可用分支会调它，而它把结果缓存 30 秒（模块级）。
    本文件若先跑，会把"当时的 min(runtime_id) 本机引擎"缓存下来，同批其他用例
    再调用时读到的是缓存值而不是自己种子里的引擎（实测会让
    `test_chat_backend_resolution.py::test_unconfigured_user_materializes_default`
    断言到 1 而不是它种的 902）。每个用例前后都清一次。
    """
    try:
        from src.runtime import engine_select
        engine_select._resolve_local_cache.clear()
    except Exception:
        pass


def _snapshot_runtime_ids():
    """记录当前 agent_runtimes 的 id 集合。

    为什么需要：`_chat_config_with_local_backend(无配置)` 会调 `resolve_local_engine()`，
    在没有可用本机直连引擎时可能**自动物化**一行引擎（副作用）。同批混跑时会污染其他用例
    （实测：本文件先跑会让 `test_chat_backend_resolution.py::test_unconfigured_user_materializes_default`
    断言到自动物化的 id 而不是它自己种的 902），故每个用例结束后按快照回滚。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM agent_runtimes")
        return {r[0] for r in c.fetchall()}


def _restore_runtimes(snapshot):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM agent_runtimes")
        for (rid,) in c.fetchall():
            if rid not in snapshot:
                c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        db.commit()


def _seed_engine():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (R_OK,))
        c.execute(
            "INSERT OR REPLACE INTO agent_runtimes "
            "(id, credential_id, name, installed, healthy, status, scope, owner_id, last_seen_at) "
            "VALUES (?,?,?,1,1,?,?,?,datetime('now'))",
            (R_OK, 0, "hermes", "online", "global", ""))
        db.commit()


def _row(uid, row_id, runtime_id, last_active, im_app_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session "
            "(id, user_id, repo_key, runtime_id, skill, skill_id, im_app_id, im_app_secret, "
            " im_provider, created_at, last_active_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),?)",
            (row_id, uid, "", runtime_id, "chat", 0, im_app_id, "", "", last_active))
        db.commit()


def _clean(uid=UID):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE user_id=?", (uid,))
        db.commit()


def _clean_engine():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (R_OK,))
        db.commit()


# ---------------------------------------------------------------- ① 写路径那行优先

def test_canonical_row_wins_over_lexicographically_smaller_legacy():
    """老行 id 字符串序在前（'sess-cfg-1' < 'sess-cfg-3'）时，仍必须读到写路径那一行。

    这正是线上"改了不生效"的形态：写 sess-cfg-{uid}、读老行；修复后写读同向。
    """
    _snap = _snapshot_runtime_ids(); _seed_engine(); _clean()
    _row(UID, "sess-cfg-1", 0, "2026-08-30 01:11:38")              # 老行（无引擎，比下面那行"小"）
    _row(UID, f"sess-cfg-{UID}", R_OK, "2026-10-17 21:29:23")      # 写路径行
    try:
        cfg = get_chat_config(UID)
        assert cfg["runtime_id"] == R_OK, f"应读到写路径那一行，实际 {cfg}"
        assert cfg["backend"] == "hermes", f"引擎名应为 hermes，实际 {cfg}"
    finally:
        _clean(); _clean_engine(); _restore_runtimes(_snap); _reset_engine_cache()


def test_canonical_row_wins_even_if_legacy_row_is_more_recent():
    """写路径行优先于"更晚活跃"的历史遗留行（写入即真理，不靠时间猜）。"""
    _snap = _snapshot_runtime_ids(); _seed_engine(); _clean()
    _row(UID, "sess-cfg-77701", 0, "2026-10-18 09:00:00")          # 遗留行，活跃时间更晚
    _row(UID, f"sess-cfg-{UID}", R_OK, "2026-10-01 09:00:00")      # 写路径行，活跃时间更早
    try:
        cfg = get_chat_config(UID)
        assert cfg["runtime_id"] == R_OK, f"写路径行应优先于更晚活跃的遗留行，实际 {cfg}"
    finally:
        _clean(); _clean_engine(); _restore_runtimes(_snap); _reset_engine_cache()


# ---------------------------------------------------------------- ② 退回遗留行（最近活跃优先）

def test_legacy_row_used_when_canonical_missing_most_recent_first():
    """写路径行不存在（历史遗留用户）：退回**最近活跃**的遗留行，而不是存储顺序上的任意一行。"""
    _snap = _snapshot_runtime_ids(); _seed_engine(); _clean()
    _row(UID, "sess-cfg-90001", 0, "2026-09-01 10:00:00")          # 旧
    _row(UID, "sess-cfg-90002", R_OK, "2026-10-14 11:40:29")       # 新（应命中，且引擎可用）
    try:
        cfg = get_chat_config(UID)
        assert cfg["runtime_id"] == R_OK, f"应读到最近活跃的遗留行，实际 {cfg}"
    finally:
        _clean(); _clean_engine(); _restore_runtimes(_snap); _reset_engine_cache()


def test_unusable_canonical_row_does_not_downgrade_usable_legacy():
    """写路径行指向已删除引擎时，不把可用配置降级为"未配置"，而是退回可用的遗留行。"""
    _snap = _snapshot_runtime_ids(); _seed_engine(); _clean()
    _row(UID2, f"sess-cfg-{UID2}", R_GONE, "2026-10-17 21:29:23")  # 写路径行 → 引擎已删（幽灵）
    _row(UID2, "sess-cfg-80001", R_OK, "2026-10-14 11:40:29")      # 遗留行 → 引擎可用
    try:
        cfg = get_chat_config(UID2)
        assert cfg["runtime_id"] == R_OK, f"应退回可用的遗留行而不是降级，实际 {cfg}"
    finally:
        _clean(UID2); _clean_engine(); _restore_runtimes(_snap); _reset_engine_cache()


# ---------------------------------------------------------------- ③ 全不可用：保持既有兜底

def test_all_candidates_unusable_keeps_existing_fallback():
    """所有候选行的引擎都不可用 → 不再返回幽灵 runtime_id（保持"按未配置处理"的既有语义）。"""
    _snap = _snapshot_runtime_ids(); _clean(UID2)
    _row(UID2, f"sess-cfg-{UID2}", R_GONE, "2026-10-17 21:29:23")
    _row(UID2, "sess-cfg-80002", R_GONE, "2026-10-14 11:40:29")
    try:
        cfg = get_chat_config(UID2)
        assert cfg["runtime_id"] != R_GONE, f"不应再把幽灵引擎当配置返回，实际 {cfg}"
    finally:
        _clean(UID2); _restore_runtimes(_snap); _reset_engine_cache()


def test_cross_space_row_belonging_to_other_user_is_not_picked():
    """跨空间错位行：`id='sess-cfg-{uid}'` 但 `user_id` 是别人 → **绝不能**当成自己的配置。

    云端真实形态：id='sess-cfg-37' 属于 user_id=1。若只按 id 取，user 37 会读到 user 1 的行
    （引擎/技能/飞书 app_id+secret 全是别人的）——这是真实发生过的回归，必须钉住。
    """
    _snap = _snapshot_runtime_ids(); _seed_engine(); _clean(UID); _clean(UID2)
    # UID2 的行名是 UID2 的 canonical 名，但 user_id 是 UID（别人）
    _row(UID, f"sess-cfg-{UID2}", R_OK, "2026-10-17 09:00:00", im_app_id="cli_someone_else")
    # UID2 自己真正的行（非 canonical 名）
    _row(UID2, "sess-cfg-77001", R_OK, "2026-10-14 09:00:00")
    try:
        cfg = get_chat_config(UID2)
        assert cfg["im_app_id"] != "cli_someone_else", \
            f"不得读到别人的行（拿到了别人的飞书 app_id），实际 {cfg}"
    finally:
        _clean(UID); _clean(UID2); _clean_engine(); _restore_runtimes(_snap); _reset_engine_cache()


def test_no_rows_still_materializes_default():
    """一行都没有时保持原行为：本机直连兜底（并可物化默认行）。"""
    _snap = _snapshot_runtime_ids(); _clean(UID2)
    try:
        cfg = get_chat_config(UID2)
        assert "runtime_id" in cfg and "backend" in cfg, f"返回结构应保持，实际 {cfg}"
    finally:
        _clean(UID2); _restore_runtimes(_snap); _reset_engine_cache()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM chat_agent_session WHERE id=?", (f"sess-cfg-{UID2}",))
            db.commit()
