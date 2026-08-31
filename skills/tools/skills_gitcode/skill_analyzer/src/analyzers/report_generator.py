import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.infra.file_utils import write_file
from src.infra.logger import get_step_logger
from src.models.enums import (
    Acceptability,
    AlternativeFeasibility,
    AnalysisStatus,
    CliCoveredStatus,
    CliInfoSource,
    CorrespondenceStatus,
    DimensionStatus,
    ImpactLevel,
    IsMandatory,
    ReplacementConclusion,
)
from src.models.report import ReportOutput, SkillAnalysisResult


SENSITIVE_PATTERNS = [
    re.compile(r"(ak|access_key|secret_key|password|token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"AK[A-Z0-9]{10,}", re.IGNORECASE),
    re.compile(r"SK[A-Z0-9]{10,}", re.IGNORECASE),
]


class ReportGenerator:
    def __init__(self, template_dir: str = "./templates", output_dir: str = "./reports"):
        self.template_dir = Path(template_dir)
        self.output_dir = Path(output_dir)
        self.log = get_step_logger("ReportGenerator")

    def generate(self, skill_result: SkillAnalysisResult, llm_analysis_results: list = None, tool_dependencies: list = None) -> ReportOutput:
        skill_name = skill_result.skill_info.skill_name
        generation_time = datetime.now(timezone.utc).isoformat()
        llm_analysis_results = llm_analysis_results or []

        openapi_usages = [asdict(u) for u in skill_result.sdk_result.openapi_usages]
        koocli_correspondences = [asdict(c) for c in skill_result.correspondence_result.correspondences] if skill_result.correspondence_result else []
        replacement_feasibility = asdict(skill_result.replacement_feasibility) if skill_result.replacement_feasibility else None
        stage_1 = asdict(skill_result.stage_1_result) if skill_result.stage_1_result else None
        stage_2 = asdict(skill_result.stage_2_result) if skill_result.stage_2_result else None
        stage_3 = asdict(skill_result.stage_3_result) if skill_result.stage_3_result else None
        incomplete_apis = skill_result.correspondence_result.incomplete_apis if skill_result.correspondence_result else []
        llm_status = "已启用" if llm_analysis_results else "未启用"

        business_scenario = self._build_business_scenario(skill_result)
        dependencies = self._build_dependencies(skill_result, tool_dependencies)
        implementation_logic = self._build_implementation_logic(skill_result)
        incomplete_explanation = self._build_incomplete_explanation(incomplete_apis, koocli_correspondences)
        stage_4_description = self._build_stage4_description(skill_result)

        raw_goal = skill_result.skill_info.business_goal or ""
        business_goal = self._summarize_business_goal(raw_goal)

        template = self._load_template()
        content = template.render(
            skill_name=skill_name,
            implementation_type=skill_result.skill_info.implementation_type.value,
            business_goal=business_goal,
            analysis_status=skill_result.analysis_status.value,
            generation_time=generation_time,
            sdk_evidence=skill_result.skill_info.sdk_evidence,
            cli_evidence=skill_result.skill_info.cli_evidence,
            api_interfaces=skill_result.sdk_result.api_interfaces,
            openapi_usages=openapi_usages,
            mapping_failures=skill_result.sdk_result.mapping_failures,
            koocli_correspondences=koocli_correspondences,
            incomplete_correspondence_apis=incomplete_apis,
            incomplete_explanation=incomplete_explanation,
            replacement_feasibility=replacement_feasibility,
            stage_1_result=stage_1,
            stage_2_result=stage_2,
            stage_3_result=stage_3,
            llm_enhancement_status=llm_status,
            errors=skill_result.errors,
            CliCoveredStatus=CliCoveredStatus,
            ImpactLevel=ImpactLevel,
            IsMandatory=IsMandatory,
            AlternativeFeasibility=AlternativeFeasibility,
            llm_analysis_results=llm_analysis_results,
            business_scenario=business_scenario,
            dependencies=dependencies,
            implementation_logic=implementation_logic,
            stage_4_description=stage_4_description,
        )

        content = self._desensitize(content)

        file_path = self.output_dir / f"{skill_name}_analysis_report.md"
        try:
            write_file(file_path, content)
            self.log.info(f"Report saved to: {file_path}")
        except Exception as e:
            self.log.info(f"Failed to save report file: {e}, outputting to console")
            print(content)

        return ReportOutput(
            report_title=f"Skill分析报告 - {skill_name}",
            generation_time=generation_time,
            content_md=content,
            file_path=str(file_path),
        )

    def _load_template(self):
        template_file = self.template_dir / "report_template.md.j2"
        if template_file.exists():
            env = Environment(
                loader=FileSystemLoader(str(self.template_dir), encoding="utf-8"),
                autoescape=select_autoescape(default=False),
            )
            return env.get_template("report_template.md.j2")
        else:
            return self._get_builtin_template()

    def _get_builtin_template(self):
        from jinja2 import Template
        builtin = """# Skill分析报告 - {{ skill_name }}

## 1. 基本信息
| 属性 | 值 |
|------|-----|
| Skill名称 | {{ skill_name }} |
| 实现方式 | {{ implementation_type }} |
| 业务目标 | {{ business_goal }} |
| 分析状态 | {{ analysis_status }} |
| 分析时间 | {{ generation_time }} |

{% if sdk_evidence %}
### SDK调用证据
{% for evidence in sdk_evidence %}
- `{{ evidence }}`
{% endfor %}
{% endif %}

{% if cli_evidence %}
### CLI调用证据
{% for evidence in cli_evidence %}
- `{{ evidence }}`
{% endfor %}
{% endif %}

## 2. SDK接口使用清单
{% if api_interfaces %}
| 序号 | 服务名 | 接口名 | HTTP方法 | API路径 | 必填参数 | 可选参数 |
|------|--------|--------|----------|---------|----------|----------|
{% for api in api_interfaces %}
| {{ loop.index }} | {{ api.service_name }} | {{ api.api_name }} | {{ api.http_method.value }} | {{ api.api_path }} | {{ api.required_params | length }} | {{ api.optional_params | length }} |
{% endfor %}
{% else %}
无SDK接口使用（KooCLI方式或未检测到SDK调用）
{% endif %}


## 3. CLI命令覆盖情况
{% if coverage_map %}
| 序号 | API接口 | 覆盖状态 | 匹配的CLI命令 | 说明 |
|------|---------|----------|--------------|------|
{% for api_name, status in coverage_map.items() %}
| {{ loop.index }} | {{ api_name }} | {{ status.is_covered.value }} | {{ status.matched_cli.cli_command if status.matched_cli else '-' }} | {{ status.uncertain_reason if status.uncertain_reason else '-' }} |
{% endfor %}
{% else %}
无API接口需要覆盖分析
{% endif %}

## 4. CLI能力缺口
{% if cli_gaps %}
| 序号 | 不支持接口 | 服务 | 业务作用 | 影响程度 | 是否必须 |
|------|-----------|------|---------|----------|---------|
{% for gap in cli_gaps %}
| {{ loop.index }} | {{ gap.unsupported_api }} | {{ gap.service_name }} | {{ gap.business_role }} | {{ gap.impact_level.value }} | {{ gap.is_mandatory.value }} |
{% endfor %}
{% else %}
无CLI能力缺口
{% endif %}

## 5. 替代方案建议
{% if alternatives %}
| 不支持接口 | 替代CLI命令 | 可行性 | 功能差异 |
|-----------|------------|--------|---------|
{% for alt in alternatives %}
| {{ alt.unsupported_api }} | {{ alt.alternative_cli if alt.alternative_cli else '-' }} | {{ alt.feasibility.value }} | {{ alt.alternative_difference }} |
{% endfor %}
{% else %}
无替代方案
{% endif %}

## 6. 分析结论
{% set covered_count = coverage_map.values() | selectattr('is_covered', 'equalto', CliCoveredStatus.COVERED) | list | length %}
{% set total_count = api_interfaces | length %}
{% if total_count > 0 %}
- CLI覆盖率：{{ covered_count }}/{{ total_count }} ({{ (covered_count / total_count * 100) | round(1) }}%)
- 该Skill {{ '可以' if covered_count == total_count else '无法' }} 完全通过CLI实现
{% else %}
- 无SDK API接口需要CLI覆盖
{% endif %}

{% if cli_gaps %}
- 关键能力缺口：{{ cli_gaps | selectattr('is_mandatory', 'equalto', IsMandatory.MANDATORY) | list | length }} 个必须接口缺失
{% endif %}

{% if errors %}
### 分析错误
{% for error in errors %}
- {{ error }}
{% endfor %}
{% endif %}
"""
        return Template(builtin)

    def _summarize_business_goal(self, raw_goal: str) -> str:
        if not raw_goal:
            return "未提供"
        if len(raw_goal) <= 300:
            return raw_goal

        return raw_goal[:297] + "..."

    def set_skip_llm(self, skip: bool = True):
        self._skip_llm = skip

    _skip_llm = False

    def _llm_summarize_goal(self, raw_goal: str) -> str:
        if self._skip_llm:
            return ""
        if len(raw_goal) <= 300:
            return ""
        try:
            import openai
            from config import ConfigManager
            config_mgr = ConfigManager()
            _, llm_config = config_mgr.parse_args()
            if not llm_config.is_enabled:
                return ""
            skill_names = config_mgr._config.skill_names if hasattr(config_mgr, '_config') else []
            if not skill_names:
                return ""
            client = openai.OpenAI(
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
            )
            prompt = (
                "请基于以下Skill描述，用中文简要概述该Skill的业务目标和功能，不超过300字。"
                "只输出概述内容，不要输出其他任何内容。\n\n"
                f"Skill描述：\n{raw_goal}"
            )
            response = client.chat.completions.create(
                model=llm_config.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=256,
                timeout=10,
            )
            text = response.choices[0].message.content.strip()
            if text and len(text) <= 350:
                return text
            return ""
        except Exception as e:
            self.log.info(f"LLM business goal summarization failed: {e}")
            return ""

    @staticmethod
    def _build_business_scenario(skill_result: SkillAnalysisResult) -> str:
        goal = skill_result.skill_info.business_goal
        if goal:
            return f"该Skill的业务目标为：{goal}。"
        impl = skill_result.skill_info.implementation_type.value
        svc_set = set()
        for usage in skill_result.sdk_result.openapi_usages:
            svc_set.add(usage.service_name)
        if svc_set:
            services = "、".join(sorted(svc_set))
            return f"该Skill基于{impl}方式实现，主要涉及华为云{services}服务的API调用。"
        return f"该Skill基于{impl}方式实现，具体业务场景需结合Skill源码进一步分析。"

    @staticmethod
    def _build_dependencies(skill_result: SkillAnalysisResult, tool_dependencies: list = None) -> List[dict]:
        if tool_dependencies:
            category_order = {"Language": 0, "SDK": 1, "KooCLI": 2, "Tool": 3, "Library": 4}
            result = []
            for td in sorted(tool_dependencies, key=lambda d: (category_order.get(d.get("category", ""), 9), d.get("name", ""))):
                name = td.get("name", "")
                ver = td.get("version", "")
                cat = td.get("category", "")
                confirmed = td.get("is_confirmed", False)
                if cat == "SDK":
                    label = f"SDK: {name}"
                elif cat == "KooCLI":
                    label = name
                elif cat == "Language":
                    label = name
                elif cat == "Tool":
                    label = f"Tool: {name}"
                else:
                    label = name
                ver_str = ver if ver else ("已确认" if confirmed else "未明确版本")
                desc = td.get("description", "")
                mandatory = td.get("is_mandatory", "mandatory")
                mandatory_label = "必须" if mandatory in ("mandatory", "必须") else "可选"
                result.append({"name": label, "version": ver_str, "description": desc, "is_mandatory": mandatory_label})
            return result if result else [{"name": "Python", "version": "3.x", "description": "运行时环境", "is_mandatory": "必须"}]

        deps = []
        impl = skill_result.skill_info.implementation_type.value
        if skill_result.skill_info.sdk_evidence:
            sdk_names = set()
            for e in skill_result.skill_info.sdk_evidence:
                m = re.match(r".*huaweicloudsdk(\w+)", e)
                if m:
                    sdk_names.add(f"huaweicloudsdk{m.group(1)}")
            if sdk_names:
                deps.append({"name": "Huawei Cloud Python SDK", "version": "、".join(sorted(sdk_names))})
        if skill_result.skill_info.cli_evidence:
            deps.append({"name": "KooCLI (hcloud)", "version": "已集成"})
        if not deps:
            if impl == "SDK":
                deps.append({"name": "Huawei Cloud Python SDK", "version": "未明确版本"})
            elif impl in ("KooCLI", "HYBRID"):
                deps.append({"name": "KooCLI (hcloud)", "version": "未明确版本"})
        deps.append({"name": "Python", "version": "3.x", "description": "运行时环境", "is_mandatory": "必须"})
        return deps

    @staticmethod
    def _build_implementation_logic(skill_result: SkillAnalysisResult) -> str:
        impl = skill_result.skill_info.implementation_type.value
        usages = skill_result.sdk_result.openapi_usages
        if not usages:
            return f"该Skill采用{impl}方式实现，未检测到明确的SDK API调用链，实现逻辑需结合源码进一步分析。"
        steps = []
        for i, usage in enumerate(usages, 1):
            action = usage.api_name
            svc = usage.service_name
            role = usage.business_role.business_role if usage.business_role else action
            steps.append(f"{i}. 调用{svc}服务的{action}接口（{role}）")
        logic = "\n".join(steps)
        return f"该Skill采用{impl}方式实现，主要执行流程如下：\n{logic}"

    @staticmethod
    def _build_incomplete_explanation(incomplete_apis: list, koocli_correspondences: list) -> str:
        if not incomplete_apis:
            return ""
        none_apis = []
        partial_apis = []
        for corr in koocli_correspondences:
            api_name = corr.get("api_name", "")
            status = corr.get("status", "")
            if api_name in incomplete_apis:
                if status == "无对应命令":
                    none_apis.append(api_name)
                else:
                    partial_apis.append(api_name)
        parts = []
        if none_apis:
            parts.append(f"通过在本地实际使用KooCLI命令查询，发现{','.join(none_apis)}确实没有与之对应的KooCLI命令。")
        if partial_apis:
            parts.append(f"{','.join(partial_apis)}存在KooCLI候选命令，但七维判定未达到效果完全一致，需要人工进一步验证。")
        return " ".join(parts) if parts else "以上接口需要人工进一步验证KooCLI命令的对应关系和效果一致性。"

    @staticmethod
    def _build_stage4_description(skill_result: SkillAnalysisResult) -> str:
        if not skill_result.replacement_feasibility:
            return "尚未进行KooCLI本地验证，无法给出第四阶段结论。"
        conclusion = skill_result.replacement_feasibility.conclusion
        reason = skill_result.replacement_feasibility.reason
        if conclusion == ReplacementConclusion.FULLY_REPLACEABLE:
            return f"基于规则分析，所有必须接口均存在效果完全一致的KooCLI命令，结论为可以完全替换。{reason}建议在实际替换前，通过本地KooCLI命令行工具对关键接口进行端到端验证，确认返回结果格式和业务流程无差异。"
        elif conclusion == ReplacementConclusion.NOT_REPLACEABLE:
            blocking = skill_result.replacement_feasibility.blocking_points
            bp_names = [bp.api_name for bp in blocking]
            return f"基于规则分析，存在必须接口无法通过KooCLI实现等效替换（{','.join(bp_names)}），结论为不能完全替换。{reason}建议保留这些接口的SDK实现方式，或探索KooCLI组合命令是否可达到近似业务效果。"
        elif conclusion == ReplacementConclusion.PARTIALLY_REPLACEABLE:
            replaceable = skill_result.replacement_feasibility.replaceable_scope
            return f"基于规则分析，部分接口可以替换为KooCLI实现（{','.join(replaceable)}），但存在差异需要评估。{reason}建议对可替换部分先行迁移，不可替换部分保留SDK实现，并通过本地KooCLI验证差异是否可接受。"
        else:
            return f"基于规则分析，{reason}建议通过本地KooCLI命令行工具对以下待确认接口逐一验证，确认KooCLI命令的实际效果与SDK调用是否一致。"

    @staticmethod
    def _desensitize(content: str) -> str:
        for pattern in SENSITIVE_PATTERNS:
            content = pattern.sub("[REDACTED]", content)
        return content