#!/usr/bin/env python3
"""从 MySQL 只读导出全部表数据到本地 SQLite，用于端到端验证。"""
import sys, os, pymysql, sqlite3
sys.path.insert(0, '.')

MYSQL_CONFIG = {
    'host': '127.0.0.1', 'port': 3306,
    'user': 'grape', 'password': 'YOUR_SSH_PASSWORD',
    'database': 'rds-grape-dev', 'charset': 'utf8mb4'
}
SQLITE_PATH = 'data/grape-test.db'

def main():
    # 1. 用 SQLite 模式初始化 grape 让它建表
    os.environ['DEVOPS_DB_BACKEND'] = 'sqlite'
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # 补缺失列（MySQL 有但 SQLite 建表时没加的）
        for col_sql in [
            "ALTER TABLE user_accounts ADD COLUMN gitcode_publish_repo TEXT DEFAULT ''",
            "ALTER TABLE user_accounts ADD COLUMN github_publish_repo TEXT DEFAULT ''",
        ]:
            try: c.execute(col_sql)
            except: pass
        db.commit()
    print("[1/3] SQLite 表结构初始化完成")

    # 2. 从 MySQL 导数据
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    mysql_cur = mysql_conn.cursor()
    mysql_cur.execute("SHOW TABLES")
    tables = [r[0] for r in mysql_cur.fetchall()]
    
    total_rows = 0
    for table in tables:
        # 获取 MySQL 列名
        mysql_cur.execute(f"SHOW COLUMNS FROM `{table}`")
        mysql_cols = [r[0] for r in mysql_cur.fetchall()]
        if not mysql_cols:
            continue
        
        # 获取 SQLite 列名
        with db_lock:
            c = db.cursor()
            c.execute(f"PRAGMA table_info({table})")
            sqlite_cols = [r[1] for r in c.fetchall()]
        
        # 取交集
        common_cols = [col for col in mysql_cols if col in sqlite_cols]
        if not common_cols:
            continue
        
        # 补缺失列
        for col in mysql_cols:
            if col not in sqlite_cols:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                    common_cols.append(col)
                except:
                    pass
        with db_lock:
            c = db.cursor()
            c.execute(f"DELETE FROM {table}")
        
        # 导数据
        col_list = ",".join(f"`{col}`" for col in common_cols)
        mysql_cur.execute(f"SELECT {col_list} FROM `{table}`")
        rows = mysql_cur.fetchall()
        if rows:
            placeholders = ",".join("?" for _ in common_cols)
            col_list_sq = ",".join(common_cols)
            with db_lock:
                c = db.cursor()
                c.executemany(f"INSERT INTO {table} ({col_list_sq}) VALUES ({placeholders})", rows)
                db.commit()
            total_rows += len(rows)
        print(f"  {table}: {len(rows)} rows")
    
    mysql_conn.close()
    print(f"[2/3] 数据导入完成: {total_rows} rows, {len(tables)} tables")
    
    # 3. 重置卡住的事件状态
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE events SET status='failed' WHERE status='running' AND seq < 1146")
        c.execute("UPDATE agent_tasks SET status='2' WHERE status='0' AND issue_session_id IN (SELECT id FROM issue_agent_session WHERE status!='idle')")
        c.execute("UPDATE issue_agent_session SET status='idle', runtime_session_id='' WHERE status!='idle'")
        db.commit()
    print("[3/3] 卡住的事件状态已重置")

if __name__ == "__main__":
    main()
