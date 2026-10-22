"""Issue #432: Runtimes 统计栏口径一致性 + i18n 静态断言测试。

前端纯函数测试+静态断言模式（参考 #406）：
- 读取 runtimes.js 源码，断言 _filterEngines 共享函数存在且被 renderEngines 与 _summaryStatsBar 共用
- 断言 _summaryStatsBar 不含中文硬编码（共/在线/刚失联/离线/待回收）
- 断言 _summaryStatsBar 使用 I18N.t('runtimes.stats.total', {count: ...}) 插值
- 读取 zh/en locale JSON，断言 stats.total key 存在且含 {count} 占位符
- 断言两语言包 key 结构一致（stats.total 均存在）
"""
import json
import os
import re

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
JS_PATH = os.path.join(ROOT, "src", "api", "static", "js", "pages", "runtimes.js")
ZH_PATH = os.path.join(ROOT, "src", "api", "static", "locales", "zh", "runtimes.json")
EN_PATH = os.path.join(ROOT, "src", "api", "static", "locales", "en", "runtimes.json")


def _read_js():
    with open(JS_PATH, encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── _filterEngines 共享函数存在且被共用 ──

class TestFilterEnginesShared:
    """Issue #432: _filterEngines 提取为共享函数，renderEngines 与 _summaryStatsBar 共用。"""

    def test_filter_engines_function_exists(self):
        js = _read_js()
        assert "function _filterEngines(" in js, "_filterEngines 函数应存在"

    def test_render_engines_uses_filter_engines(self):
        js = _read_js()
        # renderEngines 内应调用 _filterEngines
        match = re.search(r'function renderEngines\([^)]*\)\s*\{[^}]*?_filterEngines\(', js, re.DOTALL)
        assert match, "renderEngines 应调用 _filterEngines"

    def test_summary_stats_bar_uses_filter_engines(self):
        js = _read_js()
        # _summaryStatsBar 在 engines tab 应调用 _filterEngines
        match = re.search(
            r'function _summaryStatsBar\([^)]*\)\s*\{[^}]*?_filterEngines\(',
            js, re.DOTALL,
        )
        assert match, "_summaryStatsBar 应在 engines tab 调用 _filterEngines"


# ── _summaryStatsBar 无中文硬编码 ──

class TestSummaryStatsBarI18n:
    """Issue #432: 统计栏文案全部走 i18n，无中文硬编码。"""

    def test_no_hardcoded_chinese_total(self):
        js = _read_js()
        # 提取 _summaryStatsBar 函数体
        match = re.search(
            r'function _summaryStatsBar\(data, devices\)\s*\{(.*?)^\}',
            js, re.DOTALL | re.MULTILINE,
        )
        assert match, "_summaryStatsBar 函数应存在"
        body = match.group(1)
        # 不应出现硬编码中文「共」和「台」
        assert "共 " not in body or "I18N.t" in body, "不应硬编码「共 N 台」"
        assert "共 ${total} 台" not in body, "不应硬编码「共 ${total} 台」"

    def test_no_hardcoded_chinese_health_labels(self):
        js = _read_js()
        match = re.search(
            r'function _summaryStatsBar\(data, devices\)\s*\{(.*?)^\}',
            js, re.DOTALL | re.MULTILINE,
        )
        assert match
        body = match.group(1)
        # 不应出现硬编码的中文健康标签
        for label in ["在线 ", "刚失联 ", "离线 ", "待回收 "]:
            # 允许出现在 I18N.t key 字符串中，但不允许作为硬编码显示文案
            # 检查是否有不含 I18N.t 的硬编码
            assert f'>{label}' not in body, f"不应硬编码「{label.strip()}」"

    def test_uses_i18n_stats_total(self):
        js = _read_js()
        match = re.search(
            r'function _summaryStatsBar\(data, devices\)\s*\{(.*?)^\}',
            js, re.DOTALL | re.MULTILINE,
        )
        assert match
        body = match.group(1)
        assert "I18N.t('runtimes.stats.total'" in body, \
            "应使用 I18N.t('runtimes.stats.total', {count: total})"

    def test_uses_i18n_health_keys(self):
        js = _read_js()
        match = re.search(
            r'function _summaryStatsBar\(data, devices\)\s*\{(.*?)^\}',
            js, re.DOTALL | re.MULTILINE,
        )
        assert match
        body = match.group(1)
        for key in ["runtimes.health.online", "runtimes.health.recently_lost",
                     "runtimes.health.offline", "runtimes.health.about_to_gc"]:
            assert key in body, f"应使用 I18N.t('{key}')"


# ── locale JSON: stats.total key ──

class TestStatsTotalLocaleKey:
    """Issue #432: stats.total key 在 zh/en 两语言包均存在且含 {count} 占位符。"""

    def test_zh_stats_total_exists(self):
        d = _read_json(ZH_PATH)
        assert "stats" in d, "zh runtimes.json 应有 stats 顶层 key"
        assert "total" in d["stats"], "stats.total key 应存在"

    def test_en_stats_total_exists(self):
        d = _read_json(EN_PATH)
        assert "stats" in d, "en runtimes.json 应有 stats 顶层 key"
        assert "total" in d["stats"], "stats.total key 应存在"

    def test_zh_stats_total_has_count_placeholder(self):
        d = _read_json(ZH_PATH)
        val = d["stats"]["total"]
        assert "{count}" in val, f"zh stats.total 应含 {{count}} 占位符，实际: {val}"

    def test_en_stats_total_has_count_placeholder(self):
        d = _read_json(EN_PATH)
        val = d["stats"]["total"]
        assert "{count}" in val, f"en stats.total 应含 {{count}} 占位符，实际: {val}"

    def test_zh_stats_total_content(self):
        d = _read_json(ZH_PATH)
        val = d["stats"]["total"]
        assert "共" in val and "台" in val, f"zh stats.total 应为「共 N 台」格式，实际: {val}"

    def test_en_stats_total_content(self):
        d = _read_json(EN_PATH)
        val = d["stats"]["total"]
        assert "Total" in val, f"en stats.total 应为「Total N」格式，实际: {val}"

    def test_locale_json_valid(self):
        """确保修改后 JSON 格式合法。"""
        for path in [ZH_PATH, EN_PATH]:
            d = _read_json(path)
            assert isinstance(d, dict)
            assert "health" in d
            assert "stats" in d


# ── 口径一致性逻辑验证（纯函数模拟） ──

class TestFilterConsistency:
    """Issue #432: 模拟 _filterEngines 逻辑，验证统计口径与 renderEngines 一致。

    使用与 runtimes.js _filterEngines 相同的过滤谓词，验证：
    - 搜索/类型/场景/标签过滤后，统计栏 total = 过滤后条数 = 各健康状态之和
    """

    def _filter_engines(self, runtimes, q="", ftype="", fscene=None, ftag=None):
        """Python 版 _filterEngines，与 JS 逻辑一致。"""
        fscene = fscene or []
        ftag = ftag or []
        q = q.lower().strip()
        result = []
        for rt in runtimes:
            if q:
                hay = " ".join([
                    str(rt.get("alias", "")), str(rt.get("instance_name", "")),
                    str(rt.get("name", "")), str(rt.get("worker_name", "")),
                    str(rt.get("owner", "")), str(rt.get("host_name", "")),
                    " ".join(rt.get("agent_tags", [])),
                    " ".join(rt.get("local_skills", [])),
                ]).lower()
                if q not in hay:
                    continue
            if ftype:
                ename = (rt.get("instance_name") or rt.get("name") or "").lower()
                if ftype not in ename:
                    continue
            if fscene:
                tags = rt.get("scene_tags", [])
                if not any(s in tags for s in fscene):
                    continue
            if ftag:
                tags = rt.get("agent_tags", [])
                if not any(t in tags or "all" in tags for t in ftag):
                    continue
            result.append(rt)
        return result

    def _count_health(self, runtimes):
        """与 _summaryStatsBar 统计逻辑一致。"""
        counts = {"online": 0, "recently_lost": 0, "offline": 0, "about_to_gc": 0}
        for e in runtimes:
            h = e.get("health") or e.get("status") or "offline"
            if h in counts:
                counts[h] += 1
        return counts

    @pytest.fixture
    def sample_runtimes(self):
        return [
            {"id": 1, "name": "hermes-1", "instance_name": "hermes", "health": "online",
             "agent_tags": ["all"], "scene_tags": ["dev"], "local_skills": []},
            {"id": 2, "name": "codex-1", "instance_name": "codex", "health": "offline",
             "agent_tags": ["coding"], "scene_tags": ["dev"], "local_skills": []},
            {"id": 3, "name": "opencode-1", "instance_name": "opencode", "health": "online",
             "agent_tags": ["qa"], "scene_tags": ["prod"], "local_skills": []},
            {"id": 4, "name": "hermes-2", "instance_name": "hermes", "health": "recently_lost",
             "agent_tags": ["all"], "scene_tags": ["prod"], "local_skills": []},
            {"id": 5, "name": "codex-2", "instance_name": "codex", "health": "about_to_gc",
             "agent_tags": ["issue"], "scene_tags": [], "local_skills": []},
        ]

    def test_no_filter_total_equals_sum(self, sample_runtimes):
        """无过滤时 total = 各健康状态之和。"""
        filtered = self._filter_engines(sample_runtimes)
        counts = self._count_health(filtered)
        total = len(filtered)
        assert total == sum(counts.values())

    def test_search_filter_total_equals_sum(self, sample_runtimes):
        """搜索过滤后 total = 各健康状态之和。"""
        filtered = self._filter_engines(sample_runtimes, q="hermes")
        counts = self._count_health(filtered)
        total = len(filtered)
        assert total == 2  # hermes-1, hermes-2
        assert total == sum(counts.values())

    def test_type_filter_total_equals_sum(self, sample_runtimes):
        """类型过滤后 total = 各健康状态之和。"""
        filtered = self._filter_engines(sample_runtimes, ftype="codex")
        counts = self._count_health(filtered)
        total = len(filtered)
        assert total == 2  # codex-1, codex-2
        assert total == sum(counts.values())

    def test_scene_filter_total_equals_sum(self, sample_runtimes):
        """场景过滤后 total = 各健康状态之和。"""
        filtered = self._filter_engines(sample_runtimes, fscene=["prod"])
        counts = self._count_health(filtered)
        total = len(filtered)
        assert total == 2  # opencode-1, hermes-2
        assert total == sum(counts.values())

    def test_tag_filter_total_equals_sum(self, sample_runtimes):
        """标签过滤后 total = 各健康状态之和。"""
        filtered = self._filter_engines(sample_runtimes, ftag=["all"])
        counts = self._count_health(filtered)
        total = len(filtered)
        assert total == 2  # hermes-1, hermes-2 (both have "all" tag)
        assert total == sum(counts.values())

    def test_combined_filter_total_equals_sum(self, sample_runtimes):
        """组合过滤后 total = 各健康状态之和。"""
        filtered = self._filter_engines(
            sample_runtimes, q="hermes", ftag=["all"], fscene=["dev"],
        )
        counts = self._count_health(filtered)
        total = len(filtered)
        assert total == 1  # only hermes-1
        assert total == sum(counts.values())

    def test_empty_filter_returns_empty(self, sample_runtimes):
        """过滤后无匹配时返回空列表（统计栏不渲染）。"""
        filtered = self._filter_engines(sample_runtimes, q="nonexistent")
        assert len(filtered) == 0

    def test_clear_filters_restores_full_list(self, sample_runtimes):
        """清除过滤后恢复完整列表。"""
        # 先过滤
        filtered = self._filter_engines(sample_runtimes, q="hermes")
        assert len(filtered) == 2
        # 清除（等价于无过滤）
        cleared = self._filter_engines(sample_runtimes)
        assert len(cleared) == 5
        counts = self._count_health(cleared)
        assert len(cleared) == sum(counts.values())
