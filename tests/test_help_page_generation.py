"""Tests for src/help_page_gen.py (Issue #97 follow-up: help.html content from help/ dir).

Verifies the generator renders help/usage + help/api markdown into src/api/static/help.html:
- output is UTF-8 without BOM, cleanly decodable
- every usage doc's H1 title appears in the page
- every api doc's H1 title appears in the page (TOC is title-based)
- rendered structure survives (code fences / tables)
- every TOC anchor resolves to a heading id in the body
- generation is deterministic (byte-identical on re-run)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import src.help_page_gen as gen

REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_HTML = REPO_ROOT / "src" / "api" / "static" / "help.html"
API_FILES = sorted(p.name for p in gen.API_DIR.glob("*.md"))


def _first_h1(path: Path) -> str:
    md = path.read_text(encoding="utf-8")
    line = next((l for l in md.splitlines() if l.startswith("# ")), "")
    return line.lstrip("# ").strip()


@pytest.fixture()
def page_text():
    return HELP_HTML.read_text(encoding="utf-8")


class TestOutputFile:
    def test_file_exists_and_utf8_no_bom(self):
        raw = HELP_HTML.read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", "help.html has BOM"
        raw.decode("utf-8")  # must decode cleanly

    def test_no_replacement_chars(self, page_text):
        assert "\ufffd" not in page_text

    def test_contains_migration_note(self, page_text):
        assert "生成产物" in page_text and "help/" in page_text


class TestUsageContent:
    @pytest.mark.parametrize("filename", gen.USAGE_ORDER)
    def test_usage_first_h1_present(self, filename, page_text):
        title = _first_h1(gen.USAGE_DIR / filename)
        assert title in page_text, f"H1 '{title}' of {filename} missing from help.html"


class TestApiContent:
    @pytest.mark.parametrize("filename", API_FILES if API_FILES else ["README.md"])
    def test_api_first_h1_present(self, filename, page_text):
        title = _first_h1(gen.API_DIR / filename)
        assert title in page_text, f"H1 '{title}' of {filename} missing from help.html"

    def test_api_section_marker_present(self, page_text):
        assert "API 参考" in page_text

    def test_all_thirteen_api_files(self):
        assert len(API_FILES) == 13, f"expected 13 api files, got {len(API_FILES)}"


class TestRenderedStructure:
    def test_code_block_rendered(self, page_text):
        assert "<pre><code" in page_text

    def test_table_rendered(self, page_text):
        assert "<table>" in page_text

    def test_anchors_all_resolve(self, page_text):
        hrefs = set(re.findall(r'<a href="#([^"]+)"', page_text))
        ids = set(re.findall(r'id="([^"]+)"', page_text))
        assert hrefs, "no TOC anchors generated"
        assert hrefs - ids == set(), f"broken anchors: {sorted(hrefs - ids)[:5]}"


class TestGenerate:
    def test_source_count(self):
        n = gen.generate()
        assert n == len(gen.USAGE_ORDER) + len(API_FILES), f"expected {len(gen.USAGE_ORDER)+len(API_FILES)} sources, got {n}"

    def test_repeatable_bytes(self):
        gen.generate()
        first = HELP_HTML.read_bytes()
        gen.generate()
        second = HELP_HTML.read_bytes()
        assert first == second, "generation not deterministic"