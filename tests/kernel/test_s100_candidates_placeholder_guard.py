"""S100 用例（D75，先测后改）：D16/S25"参数动态化、占位符半截工程"收口——
candidates 数 == 占位符数守卫 + 五站点源码守卫。

缺陷（全场景扫描报告，36ca1cfb4 引入）：
- P0 engine_select.query_engines：OR 占位符写死 3 个 vs candidates 5 元组
  → ProgrammingError → except 吞掉 → scope=repo 引擎查询静默空；
- P0 dashboard.get_dashboard：_rk_placeholders 只用 1 处，其余 9 处
  IN (?,?) 配 5 元组 → 全部静默空（44 个 except 吞噬）；
- P1 skills 歧义守卫 len(rows)==2 写死：≥3 平台同名仓绕过 strict。
"""
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_SRC = Path(__file__).resolve().parent.parent.parent / "src"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def test_s100_engine_select_dynamic_placeholders():
    """守卫 ①：engine_select 关单站点零固定 OR 链，走 repo_key_in_params 原子产物。"""
    src = _read("domains/runtime/engine/engine_select.py")
    assert '"rc.owner_id=? OR rc.owner_id=? OR rc.owner_id=?"' not in src, \
        "owner_id OR 占位符必须由候选集派生"
    assert "repo_key_in_params(repo)" in src, \
        "占位符/参数必须走 repo_key_in_params 原子产物（同源构造，arity 由构造保证）"
    assert 'repo_key_candidates(repo)' not in src, \
        "engine_select 不得直接调用 repo_key_candidates（绕过原子产物=两次调用漂移风险）"


def test_s100_dashboard_no_fixed_in_clauses():
    """守卫 ②：get_dashboard 区域零 IN (?,?) 字面量、零 _repo_keys 位置解包、
    _rk_placeholders 接线数 = IN 站点数。"""
    src = _read("domains/insight/routes/dashboard.py")
    start = src.index("def get_dashboard")
    _nxt = src.find("\ndef ", start + 10)
    end = _nxt if _nxt != -1 else len(src)
    fn = src[start:end]
    assert "IN (?,?)" not in fn, "get_dashboard 不得残留硬编码 IN (?,?)"
    assert "_repo_keys[0]" not in fn and "_repo_keys[1]" not in fn, \
        "参数不得按位置解包（占位符数动态化后即错位）"
    n_in = len(re.findall(r"IN \(\" \+ _rk_placeholders", fn))
    n_ph = fn.count("_rk_placeholders")
    assert n_in >= 9, f"动态占位符接线站点数异常: {n_in}"
    assert n_ph == n_in + 1, f"_rk_placeholders 定义(1) + 接线({n_in}) 应与出现次数({n_ph})一致"


def test_s100_skills_strict_ge_two():
    """守卫 ③：skills 歧义守卫 >= 2，报错文案零双平台硬编码。"""
    src = _read("domains/mgmt/routes/skills.py")
    assert "len(rows) == 2" not in src, "歧义守卫必须 >= 2（≥3 平台同名仓不得绕过 strict）"
    assert "len(rows) >= 2" in src
    assert "同时存在于 gitcode 与 github" not in src, "报错文案必须动态列出命中平台"


def test_s100_engine_select_live_bindings():
    """行为锁：修复后的 engine_select SQL 形态在真实 sqlite 下 5 候选不炸、命中可见。
    （修复前本用例红：ProgrammingError → except → []，即 test_query_scopes 0==1 的根因）"""
    from src.kernel.repo_key import repo_key_candidates
    cands = tuple(repo_key_candidates("owner/repo"))
    assert len(cands) == 5  # 默认部署 4 卡 + 裸键
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE agent_runtimes (id INTEGER PRIMARY KEY, name TEXT, status TEXT, "
                 "healthy INTEGER, installed INTEGER, credential_id INTEGER, last_seen_at TEXT, "
                 "alias TEXT DEFAULT '', scene_tags TEXT DEFAULT '', org_id INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE runtime_credentials (id INTEGER PRIMARY KEY, scope TEXT, status TEXT, "
                 "owner_id TEXT, device_name TEXT DEFAULT '', owner_name TEXT DEFAULT '')")
    conn.execute("CREATE TABLE engine_capabilities (runtime_id INTEGER, tag TEXT, enabled INTEGER)")
    conn.execute("INSERT INTO runtime_credentials VALUES (1,'repo','active','owner/repo','','')")
    conn.execute("INSERT INTO agent_runtimes (id,name,status,healthy,installed,credential_id) "
                 "VALUES (1,'eng','active',1,1,1)")
    sql = ("SELECT r.id FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
           "WHERE r.credential_id>0 AND rc.scope='repo' AND rc.status='active' "
           "AND (" + " OR ".join(["rc.owner_id=?"] * len(cands)) + ") ORDER BY r.id")
    rows = conn.execute(sql, cands).fetchall()
    assert len(rows) == 1, "5 候选与动态占位符必须严格配对且命中 owner_id=裸键 的行"


def test_s100_dashboard_live_bindings():
    """行为锁：get_dashboard 修复后 SQL 形态（5 候选 + 追加参数）真实 sqlite 不炸。"""
    from src.kernel.repo_key import repo_key_candidates
    _rk = repo_key_candidates("o/r")
    ph = ",".join("?" * len(_rk))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE events (seq INTEGER, event_type TEXT, agent_type TEXT, title TEXT, "
                 "summary TEXT, status TEXT, created_at TEXT, repo_key TEXT, issue_id TEXT, duration INTEGER)")
    conn.execute("INSERT INTO events (repo_key, seq, status, created_at, issue_id) "
                 "VALUES ('gitcode:o/r', 1, 'completed', '2026-11-05 00:00:00', '5')")
    # 含追加参数的两种形态（AND agent_type=? / AND created_at >= ?）
    rows = conn.execute("SELECT seq FROM events WHERE repo_key IN (" + ph + ") ORDER BY seq DESC LIMIT 5",
                        tuple(_rk)).fetchall()
    assert len(rows) == 1
    n = conn.execute("SELECT COUNT(*) FROM events WHERE repo_key IN (" + ph + ") AND created_at >= ?",
                     tuple(_rk) + ("2026-01-01 00:00:00",)).fetchone()[0]
    assert n == 1


# ===================== S101/D76：repo_key_in_params 原子产物契约 =====================

def test_s101_in_params_arity_by_construction():
    """契约：占位符数 == 参数数 == 候选数，由同一构造保证（同源无漂移）。"""
    from src.kernel.repo_key import repo_key_in_params, repo_key_candidates
    ph, args = repo_key_in_params("owner/repo")
    assert len(args) == 5  # 默认 4 卡 + 裸键
    assert ph == ",".join("?" * len(args))
    assert args == tuple(repo_key_candidates("owner/repo"))


def test_s101_in_params_empty_set_always_false():
    """空候选集恒假防御：IN (NULL) 不匹配任何行（含空串行——区别于 IN ('') 语义漂移）。"""
    from src.kernel.repo_key import repo_key_in_params
    ph, args = repo_key_in_params("")
    assert (ph, args) == ("NULL", ())
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (repo_key TEXT)")
    conn.execute("INSERT INTO t VALUES (''), ('gitcode:o/r'), (NULL)")
    n = conn.execute(f"SELECT COUNT(*) FROM t WHERE repo_key IN ({ph})", args).fetchone()[0]
    assert n == 0, "IN (NULL) 必须恒不匹配（含空串行）"


def test_s101_in_params_live_hit():
    """行为：真实 sqlite 下 5 候选绑定命中（S100 行为锁的原子产物形态）。"""
    from src.kernel.repo_key import repo_key_in_params
    ph, args = repo_key_in_params("owner/repo")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE rc (owner_id TEXT)")
    conn.execute("INSERT INTO rc VALUES ('owner/repo'), ('gitcode:owner/repo'), ('other')")
    rows = conn.execute(f"SELECT owner_id FROM rc WHERE owner_id IN ({ph})", args).fetchall()
    assert len(rows) == 2


def test_s101_grep_guard_handwritten_placeholder_family():
    """grep 守卫（评审建议的轻量形态）：全 src 除 repo_key.py 外，
    `",".join("?" * len(` 手写占位符 + repo_key_candidates 同函数共存即非法。
    现存合法站点（dashboard/_mirror_repo_keys 等待换批）白名单登记，
    名单只减不增——新站点一律走 repo_key_in_params。"""
    import re as _re
    allowed = {"domains/insight/routes/dashboard.py", "api/issues_center.py"}  # 待换批白名单
    bad = []
    for f in (_SRC / "src").rglob("*.py"):
        rel = str(f.relative_to(_SRC / "src"))
        if rel == "repo_key.py" or rel in allowed:
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        if '"?" * len(' in t and "repo_key_candidates" in t:
            bad.append(rel)
    assert not bad, f"以下文件手写占位符与 candidates 共存，必须改走 repo_key_in_params: {bad}"
