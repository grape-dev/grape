"""模板质量检查测试: 规则层(_rule_check_issue_quality) + LLM 层(_llm_check_template_quality)。"""
from unittest import mock

import gitcode_event_handler as h


# ========== 测试用模板数据 ==========

FEATURE_BODY_VALID = """### 你创建的需求涉及到哪个华为云服务【Which Huawei Cloud service? (ECS, VPC, OBS, RDS, BSS, etc.)】

ECS

### 该Skill主要为了解决哪些问题【 What should the Skill do? (Query, Diagnose, Deploy, Monitor, Manage)】

查询 ECS 实例列表

### 该Skill主要涉及到如下哪些操作【Which operations are involved? (List, Show, Create, Delete, Update)】

List, Show

### 你希望在什么时机触发Agent对Skill的调用【When would an Agent invoke this? (Daily inspection, troubleshooting)】

日常巡检

### 需求其他补充描述
"""

FEATURE_BODY_MISSING_TARGET = """### 你创建的需求涉及到哪个华为云服务【Which Huawei Cloud service? (ECS, VPC, OBS, RDS, BSS, etc.)】

### 该Skill主要为了解决哪些问题【 What should the Skill do? (Query, Diagnose, Deploy, Monitor, Manage)】

查询 ECS 实例列表

### 该Skill主要涉及到如下哪些操作【Which operations are involved? (List, Show, Create, Delete, Update)】

List, Show"""

FEATURE_BODY_DIGIT_TARGET = """### 你创建的需求涉及到哪个华为云服务【Which Huawei Cloud service? (ECS, VPC, OBS, RDS, BSS, etc.)】

33333333

### 该Skill主要为了解决哪些问题【 What should the Skill do? (Query, Diagnose, Deploy, Monitor, Manage)】

查询 ECS 实例列表

### 该Skill主要涉及到如下哪些操作【Which operations are involved? (List, Show, Create, Delete, Update)】

List, Show"""

FEATURE_BODY_REPEATED_TARGET = """### 你创建的需求涉及到哪个华为云服务【Which Huawei Cloud service? (ECS, VPC, OBS, RDS, BSS, etc.)】

aaaaaaaaa

### 该Skill主要为了解决哪些问题【 What should the Skill do? (Query, Diagnose, Deploy, Monitor, Manage)】

查询 ECS 实例列表

### 该Skill主要涉及到如下哪些操作【Which operations are involved? (List, Show, Create, Delete, Update)】

List, Show"""

FEATURE_BODY_ALL_FILLED = """### 你创建的需求涉及到哪个华为云服务【Which Huawei Cloud service? (ECS, VPC, OBS, RDS, BSS, etc.)】

ECS

### 该Skill主要为了解决哪些问题【 What should the Skill do? (Query, Diagnose, Deploy, Monitor, Manage)】

查询 ECS 实例列表

### 该Skill主要涉及到如下哪些操作【Which operations are involved? (List, Show, Create, Delete, Update)】

List, Show

### 附加说明

something"""

BUG_BODY_VALID = """### 问题描述

创建弹性IP时返回500错误

### 问题触发场景

VPC中创建EIP，参数正常

### 问题影响

用户无法创建公网IP"""

BUG_BODY_MISSING_IMPACT = """### 问题描述

创建弹性IP时返回500错误

### 问题触发场景

VPC中创建EIP，参数正常

### 问题影响"""


# ========== _parse_template_sections ==========

class TestParseTemplateSections:

    def test_empty_body(self):
        assert h._parse_template_sections("") == {}
        assert h._parse_template_sections(None) == {}
        assert h._parse_template_sections("  ") == {}

    def test_feature_template(self):
        sections = h._parse_template_sections(FEATURE_BODY_VALID)
        assert len(sections) >= 4
        assert any("华为云服务" in k for k in sections)
        assert any("哪些问题" in k for k in sections)
        assert any("哪些操作" in k for k in sections)

    def test_bug_template(self):
        sections = h._parse_template_sections(BUG_BODY_VALID)
        assert len(sections) == 3
        assert any("问题描述" in k for k in sections)
        assert any("触发场景" in k for k in sections)

    def test_plain_body(self):
        sections = h._parse_template_sections("这是一条普通的 issue")
        assert len(sections) == 1


# ========== _detect_template_type ==========

class TestDetectTemplateType:

    def test_feature_detected(self):
        sections = h._parse_template_sections(FEATURE_BODY_VALID)
        assert h._detect_template_type(sections) == "feature"

    def test_bug_detected(self):
        sections = h._parse_template_sections(BUG_BODY_VALID)
        assert h._detect_template_type(sections) == "bug"

    def test_empty_section(self):
        assert h._detect_template_type({}) is None

    def test_plain_text(self):
        sections = h._parse_template_sections("普通文字")
        assert h._detect_template_type(sections) is None


# ========== _rule_check_issue_quality ===========

class TestRuleCheckIssueQuality:

    def test_empty_body(self):
        ok, reason = h._rule_check_issue_quality("标题", "", "feature")
        assert ok is False
        assert "空" in reason

    def test_title_no_skill_name(self):
        ok, reason = h._rule_check_issue_quality(
            "[帮我创建一个skill] ", FEATURE_BODY_VALID, "feature")
        assert ok is False
        assert "未指定" in reason

    def test_title_digit_skill_name(self):
        ok, reason = h._rule_check_issue_quality(
            "[帮我创建一个skill] 333", FEATURE_BODY_VALID, "feature")
        assert ok is False
        assert "无效" in reason

    def test_title_repeated_skill_name(self):
        ok, reason = h._rule_check_issue_quality(
            "[帮我创建一个skill] aaaaa", FEATURE_BODY_VALID, "feature")
        assert ok is False
        assert "无效" in reason

    def test_feature_missing_target_service(self):
        ok, reason = h._rule_check_issue_quality(
            "[帮我创建一个skill] ecs-query", FEATURE_BODY_MISSING_TARGET, "feature")
        assert ok is False
        assert "华为云服务" in reason

    def test_feature_digit_target(self):
        ok, reason = h._rule_check_issue_quality(
            "[帮我创建一个skill] ecs-query", FEATURE_BODY_DIGIT_TARGET, "feature")
        assert ok is False
        assert "华为云服务" in reason or "未填写" in reason

    def test_feature_repeated_target(self):
        ok, reason = h._rule_check_issue_quality(
            "[帮我创建一个skill] ecs-query", FEATURE_BODY_REPEATED_TARGET, "feature")
        assert ok is False
        assert "华为云服务" in reason or "未填写" in reason

    def test_feature_valid(self):
        ok, reason = h._rule_check_issue_quality(
            "[帮我创建一个skill] ecs-query", FEATURE_BODY_ALL_FILLED, "feature")
        assert ok is True
        assert reason == ""

    def test_bug_missing_impact(self):
        ok, reason = h._rule_check_issue_quality(
            "huawei-cloud-ecs-有问题", BUG_BODY_MISSING_IMPACT, "bug")
        assert ok is False
        assert "问题影响" in reason

    def test_bug_valid(self):
        ok, reason = h._rule_check_issue_quality(
            "huawei-cloud-ecs-有问题", BUG_BODY_VALID, "bug")
        assert ok is True
        assert reason == ""

    def test_no_template_falls_through(self):
        ok, reason = h._rule_check_issue_quality(
            "标题", "这是一条普通的 issue 描述内容", "feature")
        assert ok is None


# ========== _llm_check_template_quality ==========

class TestLlmCheckTemplateQuality:

    def test_llm_not_configured(self):
        with mock.patch.object(h, "LLM_API_KEY", ""):
            ok, reason = h._llm_check_template_quality("标题", FEATURE_BODY_VALID, "feature")
        assert ok is True
        assert "未启用" in reason

    def test_llm_pass(self):
        with mock.patch.object(h, "LLM_API_KEY", "mock-key"):
            with mock.patch.object(h, "http_json", return_value={
                "choices": [{"message": {"content": '{"ok": true, "missing_fields": [], "reason": "全部已正确填写"}'}}]
            }):
                ok, reason = h._llm_check_template_quality(
                    "标题", FEATURE_BODY_VALID, "feature")
                assert ok is True

    def test_llm_fail(self):
        with mock.patch.object(h, "LLM_API_KEY", "mock-key"):
            with mock.patch.object(h, "http_json", return_value={
                "choices": [{"message": {"content": '{"ok": false, "missing_fields": ["涉及的操作"], "reason": "涉及的操作未填写实质内容"}'}}]
            }):
                ok, reason = h._llm_check_template_quality(
                    "标题", FEATURE_BODY_VALID, "feature")
                assert ok is False
                assert "涉及的操作" in reason or "未填写" in reason

    def test_llm_response_unparsable(self):
        with mock.patch.object(h, "LLM_API_KEY", "mock-key"):
            with mock.patch.object(h, "http_json", return_value={
                "choices": [{"message": {"content": "很抱歉，我无法处理这个请求。"}}]
            }):
                ok, reason = h._llm_check_template_quality(
                    "标题", FEATURE_BODY_VALID, "feature")
                assert ok is True

    def test_llm_exception(self):
        with mock.patch.object(h, "LLM_API_KEY", "mock-key"):
            with mock.patch.object(h, "http_json", side_effect=Exception("API unavailable")):
                ok, reason = h._llm_check_template_quality(
                    "标题", FEATURE_BODY_VALID, "feature")
                assert ok is True