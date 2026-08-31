from typing import List, Optional

from src.infra.logger import get_step_logger
from src.llm.llm_client import LlmClient
from src.llm.prompts import (
    ALTERNATIVE_FEASIBILITY_PROMPT,
    ANALYSIS_SUMMARY_PROMPT,
    BUSINESS_LOGIC_PROMPT,
    SUGGEST_ALTERNATIVES_PROMPT,
)
from src.models.api_interface import ApiInterface
from src.models.cli_command import CliCommand
from src.models.cli_gap import AlternativeScheme, CliGap
from src.models.llm_config import LlmAnalysisResult
from src.models.report import SkillAnalysisResult
from src.models.skill import SkillImplResult


class LlmEnhancedAnalyzer:
    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client
        self.log = get_step_logger("LlmEnhancedAnalyzer")

    def analyze_business_logic(
        self,
        skill_info: SkillImplResult,
        api_interfaces: List[ApiInterface],
    ) -> Optional[LlmAnalysisResult]:
        if not self.llm_client.is_available:
            return None

        api_list = "\n".join(
            f"- {api.service_name}.{api.api_name} ({api.http_method.value} {api.api_path})"
            for api in api_interfaces
        ) or "No API interfaces found"

        sdk_evidence = "\n".join(f"- {e}" for e in skill_info.sdk_evidence) or "None"
        cli_evidence = "\n".join(f"- {e}" for e in skill_info.cli_evidence) or "None"

        prompt = BUSINESS_LOGIC_PROMPT.format(
            skill_name=skill_info.skill_name,
            impl_type=skill_info.implementation_type.value,
            business_goal=skill_info.business_goal or "Not provided",
            sdk_evidence=sdk_evidence,
            cli_evidence=cli_evidence,
            api_interfaces=api_list,
        )

        return self.llm_client.analyze(prompt, "business_logic")

    def evaluate_alternative_feasibility(
        self,
        cli_gap: CliGap,
        alternative: AlternativeScheme,
    ) -> Optional[LlmAnalysisResult]:
        if not self.llm_client.is_available:
            return None

        prompt = ALTERNATIVE_FEASIBILITY_PROMPT.format(
            api_name=cli_gap.unsupported_api,
            service_name=cli_gap.service_name,
            business_role=cli_gap.business_role,
            alternative_cli=alternative.alternative_cli or "None",
            current_feasibility=alternative.feasibility.value,
        )

        return self.llm_client.analyze(prompt, "alternative_feasibility")

    def suggest_alternatives(
        self,
        cli_gap: CliGap,
        available_cli_commands: List[CliCommand],
    ) -> Optional[LlmAnalysisResult]:
        if not self.llm_client.is_available:
            return None

        cmd_list = "\n".join(
            f"- {cmd.cli_command} (API: {cmd.corresponding_api})"
            for cmd in available_cli_commands
        ) or "No CLI commands available"

        prompt = SUGGEST_ALTERNATIVES_PROMPT.format(
            api_name=cli_gap.unsupported_api,
            service_name=cli_gap.service_name,
            business_role=cli_gap.business_role,
            available_commands=cmd_list,
        )

        return self.llm_client.analyze(prompt, "suggest_alternatives")

    def summarize_analysis(
        self,
        skill_result: SkillAnalysisResult,
    ) -> Optional[LlmAnalysisResult]:
        if not self.llm_client.is_available:
            return None

        api_count = len(skill_result.sdk_result.api_interfaces)
        total = api_count
        covered = sum(
            1 for s in skill_result.cli_result.coverage_map.values()
            if s.is_covered.value == "covered"
        )
        coverage_rate = round(covered / total * 100, 1) if total > 0 else 0

        critical_gaps = [
            g for g in skill_result.cli_result.cli_gaps
            if g.impact_level.value == "critical"
        ]

        gap_details = "\n".join(
            f"- {g.unsupported_api} ({g.service_name}): {g.business_role} [{g.impact_level.value}]"
            for g in skill_result.cli_result.cli_gaps
        ) or "No gaps"

        prompt = ANALYSIS_SUMMARY_PROMPT.format(
            skill_name=skill_result.skill_info.skill_name,
            impl_type=skill_result.skill_info.implementation_type.value,
            business_goal=skill_result.skill_info.business_goal or "Not provided",
            api_count=api_count,
            coverage_rate=coverage_rate,
            gap_count=len(skill_result.cli_result.cli_gaps),
            critical_gap_count=len(critical_gaps),
            gap_details=gap_details,
            alternative_count=len(skill_result.cli_result.alternatives),
        )

        return self.llm_client.analyze(prompt, "analysis_summary")