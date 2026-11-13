"""跨库视图 DDL 的唯一构造实现（供 domain_view_freshness.py 与 sync_domain_views.py 复用）。

为什么单独抽出来（避免多处各拼一份 DDL 而漂移）：两处脚本各自拼 DDL 会在引号/校验/owner 判定上漂移。
标识符一律先过白名单（库名/表名/列名只允许字母数字下划线），再拼进反引号语句。
"""
import re

IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class ViewDDLError(ValueError):
    """库名/表名/列名非法，拒绝拼 DDL。"""


def check_ident(kind: str, value: str) -> str:
    if not value:
        raise ViewDDLError(f"{kind} 不得为空")
    if not IDENT_RE.match(value):
        raise ViewDDLError(f"{kind} 非法（只允许字母/数字/下划线）: {value!r}")
    return value


def build_view_ddl(owner_db: str, table: str, columns: list[str]) -> str:
    """生成 `CREATE OR REPLACE VIEW <table> AS SELECT <owner>.<table>.<col> AS <col> ...`。

    调用方负责先切到目标库（本函数不写 USE），视图名不带库前缀。
    """
    check_ident("owner 库名", owner_db)
    check_ident("表名", table)
    if not columns:
        raise ViewDDLError(f"{table} 没有列信息，无法建视图")
    for c in columns:
        check_ident("列名", c)
    select_list = ", ".join(f"`{owner_db}`.`{table}`.`{c}` AS `{c}`" for c in columns)
    return (f"CREATE OR REPLACE VIEW `{table}` AS SELECT {select_list} "
            f"FROM `{owner_db}`.`{table}`")