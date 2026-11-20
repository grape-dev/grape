# -*- coding: utf-8 -*-
"""池化连接必须 READ COMMITTED（OAuth 登录 state 失明的根因守护）。

autocommit=False + REPEATABLE READ 下，只读线程借出的池化连接长期不 commit，
其读快照冻结在首次 SELECT——另一线程 INSERT+commit 的行（如 oauth_states 的
登录 state）对它永远不可见，GitCode/GitHub 登录 100% state_invalid。
(dev 双主实测: save OK、行在表、3 秒后 callback NOT FOUND，fresh 线程全通过。)
"""


def test_pool_init_command_sets_read_committed():
    from pathlib import Path
    src = Path("src/kernel/mysql_compat.py").read_text(encoding="utf-8")
    assert "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED" in src, (
        "池参数缺 init_command=READ COMMITTED → 冻结快照会让池化连接读不到"
        "其他线程已提交的行（OAuth state 校验失明）")
    # 且要挂在 PooledDB(creator=pymysql...) 的构造上，而不是裸连接 fallback
    import re
    m = re.search(r"self\._pool = PooledDB\((.*?)\)\n", src, re.S)
    assert m and "READ COMMITTED" in m.group(1), (
        "READ COMMITTED 必须作为池 init_command 对每条池化连接生效")
