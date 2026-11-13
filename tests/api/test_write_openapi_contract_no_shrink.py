"""Issue #1931 C 项（新架构移植版）：write_openapi_contract 的「不缩小契约」保护。

背景：四进程拆分后任一进程的 schema 都只是并集的子集。若调用方用单进程 schema 覆盖，
磁盘上的全量契约会被静默缩小（CLI 随之丢命令组，2026-11-10 事故形态）。保护规则：
磁盘已有契约里存在本次 schema 缺失的路径时不覆盖；allow_partial=True 可显式放行。
"""
import json
import logging
from pathlib import Path

import pytest

from src.kernel.help_page_gen import write_openapi_contract


def _schema(*paths):
    return {"openapi": "3.1.0", "info": {"title": "t", "version": "2.2.2"},
            "paths": {p: {"get": {"summary": p}} for p in paths}}


@pytest.fixture
def targets(tmp_path):
    return tmp_path / "help" / "openapi.json", tmp_path / "static" / "openapi.json"


def test_fresh_write_when_no_contract_on_disk(targets):
    help_path, static_path = targets
    n = write_openapi_contract(_schema("/api/a", "/api/b"), help_path=help_path, static_path=static_path)
    assert n == 2
    assert json.loads(help_path.read_text(encoding="utf-8"))["paths"].keys() == {"/api/a", "/api/b"}
    assert static_path.exists()


def test_refuses_to_shrink_contract(targets, caplog):
    help_path, static_path = targets
    write_openapi_contract(_schema("/api/a", "/api/b", "/api/tasks"), help_path=help_path, static_path=static_path)
    before = help_path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        n = write_openapi_contract(_schema("/api/a"), help_path=help_path, static_path=static_path)

    assert n == 0
    assert help_path.read_text(encoding="utf-8") == before, "磁盘契约不得被单进程 schema 缩小"
    assert any("skip contract write" in r.message and "already on disk" in r.message
               for r in caplog.records)


def test_superset_write_allowed(targets):
    help_path, static_path = targets
    write_openapi_contract(_schema("/api/a"), help_path=help_path, static_path=static_path)
    n = write_openapi_contract(_schema("/api/a", "/api/b"), help_path=help_path, static_path=static_path)
    assert n == 2
    assert set(json.loads(help_path.read_text(encoding="utf-8"))["paths"]) == {"/api/a", "/api/b"}


def test_allow_partial_writes_anyway(targets):
    help_path, static_path = targets
    write_openapi_contract(_schema("/api/a", "/api/b"), help_path=help_path, static_path=static_path)
    n = write_openapi_contract(_schema("/api/a"), help_path=help_path,
                               static_path=static_path, allow_partial=True)
    assert n == 1
    assert set(json.loads(help_path.read_text(encoding="utf-8"))["paths"]) == {"/api/a"}


def test_corrupted_disk_contract_is_overwritten(targets):
    help_path, static_path = targets
    help_path.parent.mkdir(parents=True, exist_ok=True)
    help_path.write_text("{not json", encoding="utf-8")
    n = write_openapi_contract(_schema("/api/a"), help_path=help_path, static_path=static_path)
    assert n == 1
    assert json.loads(help_path.read_text(encoding="utf-8"))["paths"].keys() == {"/api/a"}