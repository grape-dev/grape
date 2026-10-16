"""Tests for help directory structure (Issue #97 / #96).

Verifies:
- help/ directory exists with usage/ and api/ subdirectories
- help/usage/ contains 9 expected markdown files
- help/usage/ files contain no residual HTML tags
- help/api/ contains 18 files matching docs/openapi/ source
- help.html retained with migration comment
- file naming is lowercase + hyphens
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repository root = tests/ parent
REPO_ROOT = Path(__file__).resolve().parent.parent
HELP_DIR = REPO_ROOT / "help"
USAGE_DIR = HELP_DIR / "usage"
API_DIR = HELP_DIR / "api"
OPENAPI_DIR = REPO_ROOT / "docs" / "openapi"
HELP_HTML = REPO_ROOT / "src" / "api" / "static" / "help.html"

EXPECTED_USAGE_FILES = [
    "overview.md",
    "quickstart.md",
    "agents.md",
    "agent-environment-variables.md",
    "pipeline.md",
    "skills.md",
    "runtimes.md",
    "ai-shell-devbridge-deploy.md",
    "monitoring.md",
    "knowledge-base.md",
    "faq.md",
]

EXPECTED_REFERENCE_FILES = [
    "event-mapping.md",
]

EXPECTED_API_FILES = [
    "00-auth.md",
    "01-tasks.md",
    "02-repos.md",
    "03-agents.md",
    "04-chat.md",
    "05-skills.md",
    "06-scenes.md",
    "07-knowledge.md",
    "08-events.md",
    "09-task-logs.md",
    "10-git-repos.md",
    "11-issues.md",
    "12-a2a.md",
    "13-settings.md",
    "14-runtime.md",
    "15-devstage.md",
    "16-auth-and-misc.md",
    "README.md",
]

# help/api 面向用户，允许与 docs/openapi 源文件不一致（docs 保留完整设计/实现说明，help 只留 API 参考）。
# 对这类文件仍校验存在性 + 非空，但不要求字节一致。
API_FILES_DIVERGENT_FROM_SOURCE = {
    "10-git-repos.md",  # GitHub 等都透传；docs 版含方案③/token 不可感知等内部实现，help 版精简为纯 API 参考
}


class TestHelpDirectoryStructure:
    """Verify help/ directory structure."""

    def test_help_dir_exists(self):
        assert HELP_DIR.is_dir(), f"{HELP_DIR} does not exist"

    def test_usage_subdir_exists(self):
        assert USAGE_DIR.is_dir(), f"{USAGE_DIR} does not exist"

    def test_api_subdir_exists(self):
        assert API_DIR.is_dir(), f"{API_DIR} does not exist"


class TestUsageFiles:
    """Verify help/usage/ markdown files."""

    def test_all_nine_files_exist(self):
        files = {f.name for f in USAGE_DIR.iterdir() if f.is_file()}
        for name in EXPECTED_USAGE_FILES:
            assert name in files, f"Missing {name} in help/usage/"

    def test_file_count(self):
        files = [f for f in USAGE_DIR.iterdir() if f.is_file()]
        assert len(files) == len(EXPECTED_USAGE_FILES), \
            f"Expected {len(EXPECTED_USAGE_FILES)} files, got {len(files)}"

    @pytest.mark.parametrize("filename", EXPECTED_USAGE_FILES)
    def test_no_residual_html_tags(self, filename):
        """Markdown files must not contain residual HTML tags.

        We strip fenced code blocks AND inline code (backtick segments)
        before scanning, because ``<owner>`` / ``<repo>`` style placeholders
        inside inline code are content, not HTML.
        """
        content = (USAGE_DIR / filename).read_text(encoding="utf-8")
        # Strip fenced code blocks
        cleaned = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        # Strip inline code spans
        cleaned = re.sub(r"`[^`]*`", "", cleaned)
        # Find HTML tags (not markdown)
        tags = re.findall(r"<(?!/)[a-zA-Z][^>]*>", cleaned)
        # Allow none — markdown should be pure
        assert not tags, f"{filename} contains residual HTML tags: {tags[:5]}"

    @pytest.mark.parametrize("filename", EXPECTED_USAGE_FILES)
    def test_files_are_nonempty(self, filename):
        content = (USAGE_DIR / filename).read_text(encoding="utf-8").strip()
        assert len(content) > 50, f"{filename} is suspiciously short ({len(content)} chars)"

    def test_naming_lowercase_hyphens(self):
        for name in EXPECTED_USAGE_FILES:
            stem = name.replace(".md", "")
            assert stem == stem.lower(), f"{name} not lowercase"
            assert " " not in name, f"{name} contains spaces"
            assert re.match(r"^[a-z0-9-]+\.md$", name), f"{name} violates naming convention"


class TestApiFiles:
    """Verify help/api/ matches docs/openapi/."""

    def test_all_expected_files_exist(self):
        files = {f.name for f in API_DIR.iterdir() if f.is_file()}
        for name in EXPECTED_API_FILES:
            assert name in files, f"Missing {name} in help/api/"

    def test_file_count(self):
        # 仅统计 markdown 文档（help/api/openapi.json 为启动期生成的契约生成物，不计入）
        files = [f for f in API_DIR.iterdir() if f.is_file() and f.suffix == ".md"]
        assert len(files) == len(EXPECTED_API_FILES), f"Expected {len(EXPECTED_API_FILES)} md files, got {len(files)}"

    @pytest.mark.parametrize("filename", EXPECTED_API_FILES)
    def test_api_matches_openapi_source(self, filename):
        """help/api/ files must be identical to docs/openapi/ source.

        豁免 API_FILES_DIVERGENT_FROM_SOURCE：这些文件 help 版面向用户、docs 版含完整设计/实现说明，
        仅校验存在性 + 非空（见 _test_api_help_usable）。
        """
        if filename in API_FILES_DIVERGENT_FROM_SOURCE:
            self._test_api_help_usable(filename)
            return
        src = OPENAPI_DIR / filename
        dst = API_DIR / filename
        assert src.exists(), f"Source {src} not found"
        assert dst.exists(), f"Destination {dst} not found"
        assert src.read_bytes() == dst.read_bytes(), f"{filename} differs from source"

    def _test_api_help_usable(self, filename):
        src = OPENAPI_DIR / filename
        dst = API_DIR / filename
        assert src.exists(), f"Source {src} not found"
        assert dst.exists(), f"Destination {dst} not found"
        content = dst.read_text(encoding="utf-8").strip()
        assert len(content) > 200, f"{filename} help 版为空或过短（面向用户不可用）"


class TestHelpHtmlMigration:
    """Verify help.html retained with migration comment."""

    def test_help_html_exists(self):
        assert HELP_HTML.exists(), "help.html was deleted"

    def test_migration_comment_present(self):
        content = HELP_HTML.read_text(encoding="utf-8")
        assert "迁移说明" in content or "migration" in content.lower(), \
            "help.html missing migration comment"

    def test_migration_comment_in_head(self):
        content = HELP_HTML.read_text(encoding="utf-8")
        head_end = content.find("</head>")
        assert head_end > 0, "Cannot find </head> in help.html"
        head_section = content[:head_end]
        assert "迁移" in head_section or "help/" in head_section, \
            "Migration comment not in <head> section"
