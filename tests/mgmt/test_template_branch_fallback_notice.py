"""模板推送落到一次性分支时必须告知用户（不能假报"创建并托管成功"）。

回归背景：push_template_to_repo 已返回 {"pushed_to": ...}，但调用点丢弃返回值，
默认分支受保护时模板代码只在 feat/init-from-template-* 上，用户仍看到成功。
"""
from __future__ import annotations

import inspect


def test_create_from_template_surfaces_pushed_to():
    from src.domains.mgmt.routes import templates as t

    src = inspect.getsource(t.create_from_template)
    # 返回值被接收
    assert "pushed_to" in src
    # 响应里透出落点
    assert '"pushed_to": pushed_to' in src
    # 非 main 时必须给出明确告知
    assert "模板代码已推送到分支" in src
    assert 'if pushed_to != "main":' in src


def test_branch_note_mentions_default_branch_is_empty():
    from src.domains.mgmt.routes import templates as t

    src = inspect.getsource(t.create_from_template)
    assert "默认分支" in src