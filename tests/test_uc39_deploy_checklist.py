"""UC39：生产部署 Checklist——MySQL 首启回填观察（D96，2026-11-08）。

场景↔用例映射（requirements UC39，五断言面）：
- UC39① 已迁移库（版本表齐全/密文形态/零空id）→ 脚本 PASS exit=0
- UC39② 明文残留命中：JSON 形态 + 裸 token 形态（ghp_/gho_ 真实前缀样本）
  双双 FAIL（修1 正向断言回归锁——v1 的 `{` 开头判据会漏裸 token）
- UC39③ 空 id 残留行 → FAIL 精确命中；缺盖章 → FAIL
- UC39④ 脚本只读静态守卫：扫描全部字符串常量禁含写语句关键词
  （AST 节点类型看不到 SQL 语义——字符串常量扫描才是有效断言）
- UC39⑤ checklist 文档守卫：四条回填日志锚点 + 第 0 项备份 + 4 周判据
  + 滚动观察表在册（文档不随人走）
"""
import ast
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_deploy_backfill.py"
_CHECKLIST = _ROOT / "docs" / "weekly-09" / "multi-platform-decouple" / "deploy-checklist-mysql-backfill.md"


def _run_script(db_url: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["GRAPE_CHECK_DB_URL"] = db_url
    r = subprocess.run([sys.executable, str(_SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=60,
                       cwd=str(_ROOT))
    return r.returncode, r.stdout + r.stderr


def _mk_db(tmp_path: Path, migrated: bool = True) -> str:
    """构造独立探针库：migrated=True 按迁移后 schema 建最小表集。"""
    db = tmp_path / "uc39.db"
    conn = sqlite3.connect(str(db))
    if migrated:
        conn.execute("CREATE TABLE grape_t_schema_migrations (version TEXT PRIMARY KEY, name TEXT, applied_at TEXT)")
        for v in ("202611070001", "202611070002", "202611070003"):
            conn.execute("INSERT INTO grape_t_schema_migrations VALUES (?,?,datetime('now'))", (v, v))
        conn.execute("CREATE TABLE user_credentials (id TEXT, user_id INT, provider TEXT, config_json TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE grape_t_repo_credentials (id TEXT, repo_key TEXT, cred_type TEXT, value TEXT)")
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


# ──────────────── UC39① 已迁移库全绿 ────────────────

def test_uc39_1_migrated_db_pass(tmp_path):
    """密文行 + 盖章齐全 + 零空id → PASS exit=0。"""
    url = _mk_db(tmp_path)
    conn = sqlite3.connect(url.replace("sqlite:///", ""))
    # 密文形态样本：由 credential_crypto.encrypt 真实生成（{key_id}:{nonce_b64}:{ct_b64}）
    os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")
    from src.domains.mgmt.auth.credential_crypto import encrypt as _enc
    conn.execute("INSERT INTO user_credentials VALUES (?,?,?,?,?,?)",
                 ("u1", 1, "gitee", _enc("uc39-probe-secret"), "t", "t"))
    conn.commit()
    conn.close()
    code, out = _run_script(url)
    assert code == 0, f"已迁移库应 PASS:\n{out}"
    assert "PASS" in out


# ──────────────── UC39② 明文残留双形态命中（修1 回归锁） ────────────────

def test_uc39_2_plaintext_both_shapes_fail(tmp_path):
    """JSON 明文 + 裸 token（ghp_ 前缀）都 FAIL——裸 token 是 v1 判据漏报形态。"""
    url = _mk_db(tmp_path)
    dbf = url.replace("sqlite:///", "")
    conn = sqlite3.connect(dbf)
    # JSON 形态明文
    conn.execute("INSERT INTO user_credentials VALUES (?,?,?,?,?,?)",
                 ("u2", 1, "probe_json", '{"token": "x"}', "t", "t"))
    # 裸 token 形态明文（真实 GitHub PAT 前缀）  secrets-check: allow
    conn.execute("INSERT INTO user_credentials VALUES (?,?,?,?,?,?)",
                 ("u3", 1, "probe_bare", "ghp_AbCdEf1234567890abcdefABCDEF1234", "t", "t"))  # secrets-check: allow
    # grape_t_repo_credentials 裸 token
    conn.execute("INSERT INTO grape_t_repo_credentials VALUES (?,?,?,?)",
                 ("uc39-r1-uuid", "gitee:o/r", "bot", "gho_Xx...cdef"))  # D99: id+值域归一
    conn.commit()
    conn.close()
    code, out = _run_script(url)
    assert code == 1
    assert "probe_json" in out and "probe_bare" in out, f"裸 token 未命中（v1 漏报形态回归）:\n{out}"
    assert "gitee:o/r" in out, "grape_t_repo_credentials 裸 token 未命中"
    assert out.count("明文/形态异常") == 2


def test_uc39_2_empty_id_fail(tmp_path):
    """空 id 残留 → FAIL 精确命中（审计锚点悬空）。"""
    url = _mk_db(tmp_path)
    conn = sqlite3.connect(url.replace("sqlite:///", ""))
    conn.execute("INSERT INTO user_credentials VALUES (?,?,?,?,?,?)",
                 ("", 1, "probe_emptyid", "dev:test:QUJD:REVG", "t", "t"))
    conn.commit()
    conn.close()
    code, out = _run_script(url)
    assert code == 1
    assert "空 id 残留 1 行" in out
    assert "empty_id" in out


def test_uc39_3_missing_stamp_fail(tmp_path):
    """缺盖章（迁移未跑/跑一半）→ FAIL 而非 exit=2。"""
    url = _mk_db(tmp_path, migrated=False)
    # 无表库：全部检查项 FAIL（表不存在=迁移未跑，属 fail）
    code, out = _run_script(url)
    assert code == 1
    assert "missing_stamp" in out


# ──────────────── UC39④ 只读静态守卫（字符串常量扫描） ────────────────

def test_uc39_4_script_readonly_string_constant_guard():
    """脚本全部字符串常量禁含写语句关键词（AST 节点类型看不到 SQL 语义）。"""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    banned = ("INSERT", "UPDATE ", "DELETE ", "REPLACE INTO", "ALTER ", "DROP ")
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            up = node.value.upper()
            for kw in banned:
                if kw in up:
                    offenders.append((node.lineno, kw, node.value[:60]))
    assert not offenders, f"校验脚本含写语句字符串常量（只读承诺破坏）: {offenders}"


def test_uc39_4_exit2_vs_fail_distinction():
    """exit=2（无法检查）与 exit=1（发现失败）严格区分——docstring 判定清单在册。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "GRAPE_CHECK_DB_URL 未设置" in src
    # 表不存在=1 的语义在 docstring 与 FAIL 输出双侧写明
    assert "迁移未跑或跑了一半" in src
    # 三态 exit 全部出现
    assert "sys.exit(0)" in src and "sys.exit(1)" in src and "sys.exit(2)" in src


# ──────────────── UC39⑤ checklist 文档守卫 ────────────────

def test_uc39_5_checklist_document_anchors():
    """checklist 覆盖：四条回填日志锚点 + 第0项备份 + 4周判据 + 滚动观察表 + 处置指引。"""
    doc = _CHECKLIST.read_text(encoding="utf-8")
    # 四条回填日志锚点（credentials_backfill_n1 的四条 info 日志——
    # 两条 UUID 回填 + 两条加密回填；checklist 以函数名锚定处置路径）
    for anchor in ("backfill_uuid", "backfill_encryption",
                   "backfill_repo_credentials_encrypt",
                   "backfill_repo_credentials_uuid"):
        assert anchor in doc, f"checklist 缺回填函数锚点: {anchor}"
    assert "backfill: N rows" in doc, "缺回填日志形态锚点"
    # 第 0 项备份（破坏性步骤前置条件独立勾选）
    assert "第 0 项" in doc and "备份" in doc and "破坏性" in doc
    # 4 周时序判据 + 滚动观察表结构（不是"观察 4 周"四个字）
    assert "连续 4 周" in doc
    assert "| W1 |" in doc and "| W4 |" in doc, "滚动观察表缺失"
    assert "观察期重新起算" in doc
    # 完成判定权重（版本表=辅助信号）
    assert "辅助信号" in doc and "最危险状态" in doc
    # 判定树失败分支（首启 warning → 二启允许 N>0 → 三启起必须零）
    assert "幂等续跑，不是幂等失败" in doc
    # 残余风险声明
    assert "唯一防线" in doc and "静默" in doc
