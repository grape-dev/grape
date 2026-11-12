"""D102-O 同族守卫：create_task 同步流水线触发禁用 int() 强转 issue number/iid。

实弹回归（2026-11-09）：gitee 字符串 ident IKTVZQ 建需求后同步触发流水线
被 int(issue_num) 炸掉（create_task: sync pipeline trigger failed），
守卫固化防回潮。
"""
import re


def test_no_int_cast_on_issue_num_in_pipeline_trigger():
    src = open("src/api/issues_center.py", encoding="utf-8").read()
    assert not re.search(r'int\(issue_num\)', src), \
        "issues_center.py 出现 int(issue_num) 强转——字符串 ident 平台建单后同步触发会炸"


def test_no_bare_order_by_issue_number():
    """ident 混型后裸按 issue_number 排序是字典序（"9">"24"）——业务排序必须
    用 LENGTH 双键（session_store 既有形态）或 id/created_at。db_setup.py 的
    迁移/守卫文件与 LENGTH 双键形态豁免。"""
    import os
    hits = []
    for root, dirs, files in os.walk("src"):
        if "__pycache__" in root:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            for i, line in enumerate(open(path, encoding="utf-8"), 1):
                if re.search(r'ORDER BY\s+issue_number\b', line) and "LENGTH" not in line:
                    hits.append(f"{path}:{i}")
    assert not hits, f"裸 ORDER BY issue_number 回潮: {hits}"
