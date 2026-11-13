"""D105 MySQL 实弹复验探针（挂账清偿：设计稿 9.3 自动化）。

用法：DEVOPS_DB_BACKEND=mysql DEVOPS_DB_URL='mysql://grape:***@127.0.0.1:3306/grape_dialect_ci' \
      ./venv/bin/python scripts/mysql_livefire_probe.py
退出码 0 = 全过。覆盖：S2 建表/写入、S5 时间函数、IK* 编号双格式行为
（含 issue_number > 0 数值比较雷、CAST 陷阱、_issue_number_key 路径）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def main():
    import pymysql  # noqa: F401  (直连探针，不经翻译层；归一口径另测)

    conn = pymysql.connect(host="127.0.0.1", port=3306, user="grape",
                           password=os.environ["DEVOPS_DB_PW"], database="grape_dialect_ci",
                           charset="utf8mb4")
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS grape_t_probe_issue")
    c.execute("""CREATE TABLE grape_t_probe_issue (
        id INTEGER PRIMARY KEY AUTO_INCREMENT,
        repo_key VARCHAR(200) NOT NULL DEFAULT '',
        issue_number VARCHAR(64) NOT NULL DEFAULT '',
        title VARCHAR(200) DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    c.executemany(
        "INSERT INTO grape_t_probe_issue (repo_key, issue_number, title) VALUES (%s,%s,%s)",
        [("gitee:demo/r1", "99", "数字旧格式"),
         ("gitee:demo/r1", "IKTXF9", "IK 新格式"),
         ("gitee:demo/r1", "0", "零号")])
    conn.commit()

    # 雷1（扫描发现）：issue_number > 0 数值比较——MySQL 下 TEXT 转数值，'IKTXF9'→0
    c.execute("SELECT issue_number FROM grape_t_probe_issue WHERE issue_number > 0")
    rows_gt = [r[0] for r in c.fetchall()]
    check("trap: issue_number>0 在 MySQL 下漏掉 IK* 行", "IKTXF9" not in rows_gt,
          f"返回={rows_gt}（SQLite 恒真 vs MySQL 假，移植雷实锤）" if "IKTXF9" not in rows_gt else "意外包含")

    # 正确写法：长度/类型安全谓词
    c.execute("SELECT issue_number FROM grape_t_probe_issue WHERE issue_number <> '' AND issue_number <> '0'")
    rows_safe = sorted(r[0] for r in c.fetchall())
    check("fix: 改用字符串谓词后 IK* 与数字都在", rows_safe == ["99", "IKTXF9"], f"返回={rows_safe}")

    # 雷2：CAST 对 IK* 归零（issue_state 迁移对账同款写法）
    c.execute("SELECT CAST(REPLACE(issue_number,'#','') AS SIGNED) FROM grape_t_probe_issue WHERE issue_number='IKTXF9'")
    cast_val = c.fetchone()[0]
    check("trap: CAST(IK*)=0（迁移对账误判 orphan 的根源）", cast_val == 0, f"CAST={cast_val}")

    # 归一函数：写读两侧统一
    from src.api.session_store import _issue_number_key
    check("norm: _issue_number_key('IKTXF9')", _issue_number_key("IKTXF9") == "IKTXF9")
    check("norm: _issue_number_key('#99')", _issue_number_key("#99") == "99")
    check("norm: _issue_number_key('#IKTXF9')", _issue_number_key("#IKTXF9") == "IKTXF9")

    # 等值参数化查询（主链路实际用法）：IK* 作为 ? 参数
    c.execute("SELECT title FROM grape_t_probe_issue WHERE repo_key=%s AND issue_number=%s",
              ("gitee:demo/r1", "IKTXF9"))
    check("主链路: 参数化等值查询命中 IK* 行", c.fetchone() == ("IK 新格式",))

    # 时间函数（S5 族）：翻译层 datetime('now')
    c.execute("SELECT created_at IS NOT NULL FROM grape_t_probe_issue LIMIT 1")
    check("time: DATETIME DEFAULT CURRENT_TIMESTAMP 生效", c.fetchone()[0] == 1)

    # LIKE 前缀（S1 族，规则2：常量前缀参数化，无 ESCAPE 反斜杠）
    c.execute("SELECT issue_number, repo_key FROM grape_t_probe_issue")
    print("  rows:", c.fetchall())
    c.execute("SELECT COUNT(*) FROM grape_t_probe_issue WHERE repo_key LIKE %s", ("gitee:demo/%",))
    _like_n = c.fetchone()[0]
    print("  like count:", _like_n)
    check("dialect: 常量前缀 LIKE 参数化", _like_n == 3)

    c.execute("DROP TABLE grape_t_probe_issue")
    conn.commit()
    conn.close()

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{'ALL PASS' if not failed else 'FAILED'}: {len(CHECKS)-len(failed)}/{len(CHECKS)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
