"""Tests for src/help_page_gen.py (v2: help.html SPA shell; content from help/ dir).

Verifies the generator renders help/usage + help/api markdown into src/api/static/help.html:
- output is UTF-8 without BOM, cleanly decodable
- embedded data parses; usage(9) + api(13) docs with html/toc/title/id
- landing offers 使用文档 / API 文档 ; section scoping labels present
- every doc's first H1 appears; heading ids unique across all docs
- same-level sub-TOC items share the same level (2.1/2.2 not nested)
- rendered structures (code fence / table) survive; generation deterministic
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import src.help_page_gen as gen

REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_HTML = REPO_ROOT / "src" / "api" / "static" / "help.html"
API_FILES = sorted(p.name for p in gen.API_DIR.glob("*.md"))


def _page_text() -> str:
    return HELP_HTML.read_text(encoding="utf-8")


def _load_data() -> dict:
    text = _page_text()
    m = re.search(
        r'<script type="application/json" id="help-data">(.*?)</script>', text, re.DOTALL
    )
    assert m, "help-data script missing"
    return json.loads(m.group(1))


@pytest.fixture(name="page_text")
def _page_text_fixture() -> str:
    return _page_text()


def _first_h1(path: Path) -> str:
    md = path.read_text(encoding="utf-8")
    line = next((l for l in md.splitlines() if l.startswith("# ")), "")
    return line.lstrip("# ").strip()


@pytest.fixture(name="data")
def _data_fixture() -> dict:
    return _load_data()


class TestOutputFile:
    def test_file_exists_and_utf8_no_bom(self):
        raw = HELP_HTML.read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", "help.html has BOM"
        raw.decode("utf-8")

    def test_no_replacement_chars(self, page_text):
        assert "\ufffd" not in page_text

    def test_contains_migration_note(self, page_text):
        assert ("生成产物" in page_text or "生成物" in page_text) and "help/" in page_text


class TestShell:
    def test_landing_two_options(self, page_text):
        assert "使用文档" in page_text and "API 文档" in page_text

    def test_routes_present(self, page_text):
        assert "#/usage" in page_text and "#/api" in page_text

    def test_section_scoping_labels(self, page_text):
        assert "使用指南" in page_text and "API 参考" in page_text

    def test_openapi_download_link_fixed(self, page_text):
        """契约下载 URL 固定为 /static/openapi.json（生成物，启动期落盘）。"""
        assert "下载 openapi.json" in page_text
        assert "/static/openapi.json" in page_text

    def test_data_parses_with_correct_counts(self, data):
        assert len(data["usage"]) == 9, f"expected 9 usage docs, got {len(data['usage'])}"
        assert len(data["api"]) == 13, f"expected 13 api docs, got {len(data['api'])}"

    def test_every_doc_has_fields(self, data):
        for section in ("usage", "api"):
            for d in data[section]:
                assert d["id"] and d["title"], f"bad id/title in {d}"
                assert isinstance(d["html"], str) and len(d["html"]) > 50
                assert isinstance(d["toc"], list)


class TestContent:
    @pytest.mark.parametrize("filename", gen.USAGE_ORDER)
    def test_usage_first_h1_present(self, filename, data):
        title = _first_h1(gen.USAGE_DIR / filename)
        html_all = "".join(d["html"] for d in data["usage"])
        assert title in html_all, f"H1 '{title}' of {filename} missing"

    @pytest.mark.parametrize("filename", API_FILES if API_FILES else ["README.md"])
    def test_api_first_h1_present(self, filename, data):
        title = _first_h1(gen.API_DIR / filename)
        html_all = "".join(d["html"] for d in data["api"])
        assert title in html_all, f"H1 '{title}' of {filename} missing"

    def test_heading_ids_unique_across_docs(self, data):
        ids = []
        for section in ("usage", "api"):
            for d in data[section]:
                ids += re.findall(r'id="([^"]+)"', d["html"])
        assert ids, "no heading ids found"
        assert len(ids) == len(set(ids)), "duplicate heading ids across docs"

    def test_sub_toc_same_level_siblings(self, data):
        """quickstart 的 2.1/2.2/2.3/2.4 必须同级（level==2），不得相互嵌套。"""
        doc = next(d for d in data["usage"] if d["id"] == "quickstart")
        levels = [r["level"] for r in doc["toc"]]
        assert levels, "quickstart has no sub-toc"
        assert all(l == 2 for l in levels), f"quickstart sub-headings not all sibling level2: {levels}"


class TestRenderedStructure:
    def test_code_block_rendered(self, data):
        html_all = "".join(d["html"] for sec in data.values() for d in sec)
        assert "<pre><code" in html_all

    def test_table_rendered(self, data):
        html_all = "".join(d["html"] for sec in data.values() for d in sec)
        assert "<table>" in html_all


class TestGenerate:
    def test_source_count(self):
        n = gen.generate()
        assert n == len(gen.USAGE_ORDER) + len(API_FILES)

    def test_repeatable_bytes(self):
        gen.generate()
        first = HELP_HTML.read_bytes()
        gen.generate()
        second = HELP_HTML.read_bytes()
        assert first == second, "generation not deterministic"