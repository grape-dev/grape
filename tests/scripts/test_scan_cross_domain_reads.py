"""Issue #2504：跨域读清单扫描器（scripts/p4/scan_cross_domain_reads.py）单测。

两层 seam：
1. 公开接口 `scan_source`（合成样例）——验证判定口径：
   裸读 / 兜底（else、if-is-None、早退守卫覆盖尾段）/ 同域不计 /
   动态列名 / 动态 IN / 非 fetch 消费 / auto（静态 SQL + fetch 赋值）/
   api-kernel 共享桶。
2. 仓库级 `scan_repo` + CLI——清单可复现（与基线快照一致）、
   `--check` 回归门禁、fail-closed 退出码 2。

基线快照在纯净 base（arch-weekly-11）上重新生成：total/bare 全 0
（base 已用 public lib 具名查询把跨域读清零）。仓库级用例因此只测
棘轮机制本身（快照一致 / 篡改必红 / 状态翻转必红 / 截断提示），
不绑定任何历史实现的读点数量与分布。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "p4" / "scan_cross_domain_reads.py"
_BASELINE = _REPO / "scripts" / "p4" / "read_inventory_baseline.json"

spec = importlib.util.spec_from_file_location("scan_cross_domain_reads", _SCRIPT)
assert spec is not None and spec.loader is not None
scanmod = importlib.util.module_from_spec(spec)
sys.modules["scan_cross_domain_reads"] = scanmod
spec.loader.exec_module(scanmod)

#: 合成样例用的最小表归属：mgmt 表 + runtime 表 + insight 表
OWNERSHIP = {
    "mgmt_table": "mgmt",
    "skill": "runtime",
    "agent_tasks": "runtime",
    "obs_metric": "insight",
}

BARE_SNIPPET = '''
def read_tasks(limit):
    c = db.cursor()
    c.execute("SELECT id FROM agent_tasks WHERE status=?", (limit,))
    rows = c.fetchall()
    return rows
'''

CONVERTED_ELSE_SNIPPET = '''
from src.kernel.domain_read import runtime_lib as _rt


def read_skill(name):
    lib = _rt(foreign_only=True)
    c = db.cursor()
    if lib is not None:
        row = lib.read_one("skill_x", (name,))
    else:
        c.execute("SELECT id FROM skill WHERE name=?", (name,))
        row = c.fetchone()
    return row
'''

CONVERTED_IF_NONE_SNIPPET = '''
from src.kernel.domain_read import runtime_lib


def count_tasks():
    lib = runtime_lib()
    c = db.cursor()
    if lib is None:
        c.execute("SELECT COUNT(*) FROM agent_tasks")
        return c.fetchone()[0]
    return lib.read_one("agent_tasks_count", ())
'''

CONVERTED_EARLY_RETURN_SNIPPET = '''
from src.domains.runtime import public as _rt


def skill_hash(name, scope):
    if _rt.available():
        return _rt.get_skill_content_hash(name, scope)
    c = db.cursor()
    c.execute("SELECT id, content_hash, file_path FROM skill WHERE name=? AND scope=? LIMIT 1",
              (name, scope))
    return c.fetchone()
'''

SAME_DOMAIN_SNIPPET = '''
def read_own():
    c = db.cursor()
    c.execute("SELECT id FROM mgmt_table WHERE id=?", (1,))
    return c.fetchall()
'''

DYNAMIC_IN_SNIPPET = '''
def tasks_of_keys(keys):
    ph = ",".join("?" * len(keys))
    c = db.cursor()
    c.execute("SELECT id FROM agent_tasks WHERE repo_key IN (" + ph + ")", tuple(keys))
    rows = c.fetchall()
    return rows
'''

DYNAMIC_COLUMN_SNIPPET = '''
def user_field(col, uid):
    c = db.cursor()
    c.execute(f"SELECT {col} FROM skill WHERE id=?", (uid,))
    return c.fetchall()
'''

NON_FETCH_SNIPPET = '''
def iter_tasks():
    c = db.cursor()
    c.execute("SELECT id FROM agent_tasks")
    for r in c.fetchall():
        yield r
'''

PLAIN_VAR_SQL_SNIPPET = '''
def read_dyn(sql_var):
    c = db.cursor()
    c.execute(sql_var)
    return c.fetchall()
'''

KERNEL_SHARED_SNIPPET = '''
def shared_read():
    c = db.cursor()
    c.execute("SELECT id FROM skill WHERE id=?", (1,))
    return c.fetchall()
'''


def _scan(snippet: str, filename: str):
    return scanmod.scan_source(snippet, filename, OWNERSHIP)


def _fake_site(i: int, status: str = "converted") -> dict:
    """构造与基线快照同构的伪造读点（棘轮负例用，不依赖任何真实读点）。"""
    return {
        "file": f"src/fake_{i}.py", "line": i + 1, "func": "f",
        "host": "mgmt", "status": status, "classification": "auto",
        "tables": ["skill"], "dynamic": False, "sql": "SELECT 1",
    }


class TestScanSourceBareConverted:
    def test_bare_read_in_domain_file(self):
        sites = _scan(BARE_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert len(sites) == 1
        s = sites[0]
        assert s.status == "bare"
        assert s.host == "mgmt"
        assert s.tables == ["agent_tasks"]

    def test_guarded_else_fallback_is_converted(self):
        sites = _scan(CONVERTED_ELSE_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert len(sites) == 1
        assert sites[0].status == "converted"

    def test_if_is_none_body_is_converted(self):
        sites = _scan(CONVERTED_IF_NONE_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert len(sites) == 1
        assert sites[0].status == "converted"

    def test_early_return_guard_covers_tail(self):
        """`if lib.available(): return ...` 后的同域 execute 属兜底。"""
        sites = _scan(CONVERTED_EARLY_RETURN_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert len(sites) == 1
        assert sites[0].status == "converted"

    def test_same_domain_read_not_counted(self):
        assert _scan(SAME_DOMAIN_SNIPPET, "src/domains/mgmt/routes/x.py") == []

    def test_unregistered_table_not_counted(self):
        snippet = "def f():\n    c = db.cursor()\n" \
                  "    c.execute('SELECT id FROM unknown_table WHERE id=?', (1,))\n" \
                  "    return c.fetchall()\n"
        assert _scan(snippet, "src/domains/mgmt/routes/x.py") == []

    def test_write_sql_not_counted(self):
        snippet = "def f(c, x):\n    c.execute('INSERT INTO skill (id) VALUES (?)', (x,))\n"
        assert _scan(snippet, "src/domains/mgmt/routes/x.py") == []

    def test_plain_variable_sql_skipped(self):
        """`c.execute(sql_var)` 无法静态提取表名 → 不计入（口径文档已登记）。"""
        assert _scan(PLAIN_VAR_SQL_SNIPPET, "src/domains/mgmt/routes/x.py") == []

    def test_nested_function_not_double_counted(self):
        snippet = '''
def outer():
    def inner():
        c = db.cursor()
        c.execute("SELECT id FROM skill WHERE id=?", (1,))
        return c.fetchall()
    return inner()
'''
        sites = _scan(snippet, "src/domains/mgmt/routes/x.py")
        assert len(sites) == 1
        assert sites[0].func == "inner"


class TestHostBuckets:
    def test_kernel_file_goes_to_shared_bucket(self):
        sites = _scan(KERNEL_SHARED_SNIPPET, "src/kernel/session_manager.py")
        assert len(sites) == 1
        assert sites[0].host == "kernel"
        # 共享代码域未知：mgmt 域注册表在 kernel 文件里也算跨域
        assert sites[0].status == "bare"

    def test_api_file_goes_to_shared_bucket(self):
        sites = _scan(KERNEL_SHARED_SNIPPET, "src/api/session_store.py")
        assert sites[0].host == "api"


class TestClassification:
    def test_plain_fetch_assign_is_auto(self):
        sites = _scan(BARE_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert sites[0].classification == "auto"

    def test_dynamic_in_classified(self):
        sites = _scan(DYNAMIC_IN_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert sites[0].classification == "manual_dynamic_in"
        assert sites[0].dynamic is True

    def test_fstring_dynamic_in_classified(self):
        snippet = '''
def f(keys):
    c = db.cursor()
    c.execute(f"SELECT id FROM agent_tasks WHERE repo_key IN ({','.join('?' * len(keys))})", tuple(keys))
    return c.fetchall()
'''
        sites = _scan(snippet, "src/domains/mgmt/routes/x.py")
        assert len(sites) == 1
        assert sites[0].classification == "manual_dynamic_in"

    def test_dynamic_column_classified(self):
        sites = _scan(DYNAMIC_COLUMN_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert sites[0].classification == "manual_dynamic_column"

    def test_non_fetch_consumption_classified(self):
        sites = _scan(NON_FETCH_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert sites[0].classification == "manual_non_fetch"


@pytest.fixture(scope="module")
def repo_inventory():
    return scanmod.scan_repo([_REPO / "src"])


class TestRepoInventory:
    """仓库级 seam：清单必须与基线快照一致（--json 可复现）。

    基线为纯净 base 上重新生成的零清单（total/bare 全 0）；本组只看护
    棘轮机制本身，不绑定具体读点数量。
    """

    def test_baseline_file_exists(self):
        assert _BASELINE.is_file(), "交付物：scripts/p4/read_inventory_baseline.json"

    def test_repo_scan_matches_baseline_snapshot(self, repo_inventory):
        base = json.loads(_BASELINE.read_text(encoding="utf-8"))
        assert repo_inventory.files_scanned == base["files_scanned"]
        base_keys = set(scanmod.site_keys(base["sites"]))
        cur_keys = set(scanmod.site_keys(repo_inventory.sites))
        assert cur_keys == base_keys, (
            f"读点清单漂移：+{len(cur_keys - base_keys)} / -{len(base_keys - cur_keys)}"
        )

    def test_stats_reproducible(self, repo_inventory):
        base = json.loads(_BASELINE.read_text(encoding="utf-8"))
        assert repo_inventory.stats() == base["stats"]

    def test_w1_scope_present(self, repo_inventory):
        """统计分两层：进程归属固定的 W1 域桶 + api/kernel 共享桶。

        W1 域桶恒输出三域键（0 计也保留），共享桶键域 ⊆ {api, kernel}，
        总量守恒。
        """
        st = repo_inventory.stats()
        assert set(st["w1_scope"]["bare_by_host"]) == {"insight", "mgmt", "runtime"}
        assert set(st["shared_scope"]["by_host"]) <= {"api", "kernel"}
        assert st["total"] == st["w1_scope"]["bare"] + st["w1_scope"]["converted"] \
            + st["shared_scope"]["total"]

    def test_check_passes(self, repo_inventory):
        ok, diffs = scanmod.compare_baseline(repo_inventory)
        assert ok, diffs

    def test_check_detects_regression(self, tmp_path):
        """基线快照被改动（读点凭空多出/消失）→ check FAIL。"""
        base = json.loads(_BASELINE.read_text(encoding="utf-8"))
        base["sites"].append(_fake_site(0, status="bare"))  # 基线伪造一个仓库没有的读点
        tampered = tmp_path / "baseline.json"
        tampered.write_text(json.dumps(base), encoding="utf-8")
        repo = scanmod.scan_repo([_REPO / "src"])
        ok, diffs = scanmod.compare_baseline(repo, tampered)
        assert not ok
        assert any("消失的读点" in d for d in diffs)

    def test_check_detects_status_flip(self, tmp_path):
        """同位置 converted→bare 翻转必须 FAIL（QA 两轴审查最重项）。

        删除 guard 不动行号时，读点键 (file,line,tables) 不变但 status 翻转——
        门禁若只比键不比状态，裸读即可静默绕过 --check。
        基线为零清单，用合成 converted 清单驱动同一致命路径。
        """
        sites = _scan(CONVERTED_ELSE_SNIPPET, "src/domains/mgmt/routes/x.py")
        assert sites and sites[0].status == "converted"
        inv = scanmod.Inventory(files_scanned=1, sites=sites)
        base = {"sites": [dict(s.as_dict(), status="bare") for s in sites]}
        tampered = tmp_path / "baseline.json"
        tampered.write_text(json.dumps(base), encoding="utf-8")
        ok, diffs = scanmod.compare_baseline(inv, tampered)
        assert not ok, "同位置状态翻转未被门禁拦截"
        assert any("状态翻转" in d for d in diffs)


class TestCli:
    def test_fail_closed_exit_2_when_no_files(self, tmp_path, capsys):
        rc = scanmod.main([str(tmp_path)])
        assert rc == 2

    def test_json_mode_prints_sites(self, tmp_path, capsys):
        """--json 模式：src 相对路径口径产出 sites；未知前缀（tmp 绝对路径）显式跳过。"""
        snippet_dir = tmp_path / "src" / "domains" / "mgmt" / "routes"
        snippet_dir.mkdir(parents=True)
        (snippet_dir / "x.py").write_text(
            "def read_tasks():\n"
            "    c = db.cursor()\n"
            "    c.execute('SELECT id FROM skill WHERE id=?', (1,))\n"
            "    return c.fetchall()\n",
            encoding="utf-8")
        # CLI 对 tmp 绝对路径：host_of 判为未知前缀 → 0 读点（不静默入 kernel 桶）
        rc = scanmod.main([str(snippet_dir), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["files_scanned"] == 1
        assert payload["sites"] == []
        # 同一代码按 src 相对路径口径 → 必须产出 sites
        sites = scanmod.scan_source(
            (snippet_dir / "x.py").read_text(encoding="utf-8"),
            "src/domains/mgmt/routes/x.py",
            {"skill": "runtime"},
        )
        assert sites, "合成跨域读点（src 相对路径口径）必须产出 sites"

    def test_unknown_prefix_skipped_not_kernel(self):
        """host_of：scripts/、tests/ 等未知前缀不入 kernel 桶（QA 判断项 4）。"""
        for rel in ("scripts/p4/foo.py", "tests/api/foo.py", "src/toplevel.py"):
            assert scanmod.host_of(rel) == (None, None)
        assert scanmod.host_of("src/domains/contracts/local/mgmt_local.py") == ("kernel", None)

    def test_check_mode_exit_code_0(self, capsys):
        assert scanmod.main(["src", "--check"]) == 0

    def test_missing_manifest_exit_2(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(scanmod, "OWNERSHIP_MANIFEST", tmp_path / "missing.json")
        assert scanmod.main(["src"]) == 2


class TestTableQuery:
    """清单独表可查（seam = CLI 公开接口）。

    `--table` 只输出引用该表的读点；与门禁/基线维护旗标互斥
    （过滤视图不得喂 --check / --update-baseline）。基线为零清单，
    正向过滤行为由合成样例（TestScanSource*）覆盖，这里看护
    零读点合法、未知表 fail-closed、旗标互斥三条边界。
    """

    def test_table_zero_reads_is_a_valid_query(self, capsys):
        """已登记但零跨域读的表 → rc=0 空清单（合法查询结果，非失败）。"""
        rc = scanmod.main(["src", "--table", "admin_agent_alert", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sites"] == []

    def test_table_unknown_fail_closed(self, capsys):
        rc = scanmod.main(["src", "--table", "not_a_registered_table"])
        assert rc == 2

    def test_table_conflicts_with_gate_flags(self):
        """--table 与 --check / --update-baseline 互斥：过滤视图不得喂门禁。"""
        for extra in (["--check"], ["--update-baseline"]):
            with pytest.raises(SystemExit) as ei:
                scanmod.main(["src", "--table", "skill", *extra])
            assert ei.value.code == 2


class TestW1DerivedStats:
    """stats 派生不变式（对任意清单成立，零清单同样看护）：
    裸读分类计数之和 = W1 裸读数；逐表分类合计与 by_table 一致。"""

    def test_w1_bare_classification_derived_stats(self, repo_inventory):
        st = repo_inventory.stats()["w1_scope"]
        cls = st["bare_classification"]
        assert sum(cls.values()) == st["bare"], "裸读分类计数之和必须等于 W1 裸读数"
        by_table = st["bare_by_table"]
        assert sum(by_table.values()) >= st["bare"], (
            "一处读点可引用多表：表-点对合计 ≥ 裸读数")
        assert st["bare_tables"] == len(by_table)
        for table, per_cls in st["bare_classification_by_table"].items():
            assert sum(per_cls.values()) == by_table[table]
        assert set(st["bare_classification_by_table"]) == set(by_table)


class TestCliFlagHygiene:
    """QA 复审判断项收口（seam = CLI 公开接口）。

    - `--json --update-baseline`：json 分支先 return 0 会静默不更新基线
      → 必须 fail-fast 拒绝；
    - `--check --update-baseline`：先写基线再比对必然 PASS
      → 必须 fail-fast 拒绝；
    - 非 JSON --check 差异 >50 条只截断不提示 → 须标注截断。
    """

    def test_json_update_baseline_fail_fast(self, tmp_path, monkeypatch):
        """--json 与 --update-baseline 互斥：拒绝执行且不得改动基线。"""
        monkeypatch.setattr(scanmod, "BASELINE", tmp_path / "baseline.json")
        with pytest.raises(SystemExit) as ei:
            scanmod.main(["src", "--json", "--update-baseline"])
        assert ei.value.code == 2
        assert not (tmp_path / "baseline.json").exists(), "互斥拒绝时不得写基线"

    def test_check_update_baseline_fail_fast(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scanmod, "BASELINE", tmp_path / "baseline.json")
        with pytest.raises(SystemExit) as ei:
            scanmod.main(["src", "--check", "--update-baseline"])
        assert ei.value.code == 2
        assert not (tmp_path / "baseline.json").exists()

    def test_check_truncation_note_when_many_diffs(self, tmp_path, monkeypatch,
                                                   capsys):
        """差异 >50 条时非 JSON --check 须标注截断（共 N 条 / 仅显示前 50）。"""
        base = json.loads(_BASELINE.read_text(encoding="utf-8"))
        total = len(base["sites"])
        base["sites"] = []
        # 基线为零清单 → 伪造 60 条「基线有而仓库没有」的读点撑过截断阈值
        # （门禁对 diff 结构敏感，对读点真伪不敏感）。
        for i in range(60):
            base["sites"].append(_fake_site(i))
        tampered = tmp_path / "baseline.json"
        tampered.write_text(json.dumps(base), encoding="utf-8")
        monkeypatch.setattr(scanmod, "BASELINE", tampered)
        rc = scanmod.main(["src", "--check"])
        assert rc == 1
        out = capsys.readouterr().out
        assert f"共 {total + 60} 条差异" in out
        assert "仅显示前 50" in out
