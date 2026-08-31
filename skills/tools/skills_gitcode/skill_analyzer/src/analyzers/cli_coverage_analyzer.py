import re
from typing import Dict, List, Optional, Set

from src.infra.logger import get_step_logger
from src.models.api_interface import ApiInterface
from src.models.cli_command import CliCommand, CliCoverageStatus
from src.models.cli_gap import AlternativeScheme, CliGap
from src.models.enums import (
    AlternativeFeasibility,
    CliCoveredStatus,
    ImpactLevel,
    IsMandatory,
)
from src.models.report import CliCoverageResult


class CliCoverageAnalyzer:
    def __init__(
        self,
        cli_tool_available: bool = False,
        cli_tool_path: str = "",
    ):
        self.cli_tool_available = cli_tool_available
        self.cli_tool_path = cli_tool_path
        self.log = get_step_logger("CliCoverageAnalyzer")
        self._cli_commands: Dict[str, List[CliCommand]] = {}

    def analyze(self, api_interfaces: List[ApiInterface]) -> CliCoverageResult:
        if not self._cli_commands:
            self._cli_commands = self._get_cli_command_map()

        coverage_map: Dict[str, CliCoverageStatus] = {}
        cli_gaps: List[CliGap] = []
        alternatives: List[AlternativeScheme] = []

        for api in api_interfaces:
            status = self._check_coverage(api)
            coverage_map[api.api_name] = status

            if status.is_covered in (CliCoveredStatus.NOT_COVERED, CliCoveredStatus.UNCERTAIN):
                gap = self._identify_gap(api, status)
                cli_gaps.append(gap)

                alt = self._find_alternative(api)
                if alt:
                    alternatives.append(alt)

        info_source = "guidance" if self._cli_commands else "none"

        self.log.info(
            f"CLI coverage: {sum(1 for s in coverage_map.values() if s.is_covered == CliCoveredStatus.COVERED)}/{len(api_interfaces)} covered, "
            f"{len(cli_gaps)} gaps found"
        )

        return CliCoverageResult(
            coverage_map=coverage_map,
            cli_gaps=cli_gaps,
            alternatives=alternatives,
            info_source=info_source,
        )

    def _get_cli_command_map(self) -> Dict[str, List[CliCommand]]:
        commands = self._get_commands_from_guidance()
        if commands:
            self.log.info("Got CLI commands from guidance skill")
            return commands

        if self.cli_tool_available:
            from src.resources.cli_tool_manager import CliToolManager
            cli_mgr = CliToolManager(self.cli_tool_path)
            raw = cli_mgr.get_supported_commands()
            for service, cmd_list in raw.items():
                commands[service] = [
                    CliCommand(
                        cli_command=f"hcloud {service} {cmd}",
                        corresponding_api=cmd,
                        cli_service=service,
                    )
                    for cmd in cmd_list
                ]
            if commands:
                self.log.info("Got CLI commands from local CLI tool")

        return commands

    def _get_commands_from_guidance(self) -> Dict[str, List[CliCommand]]:
        try:
            from src.infra.logger import get_step_logger
            log = get_step_logger("cli_guidance")
            log.info("Attempting to get CLI info from huawei-cloud-cli-guidance skill")
        except Exception:
            pass
        return {}

    def _check_coverage(self, api: ApiInterface) -> CliCoverageStatus:
        service_lower = api.service_name.lower()
        service_commands = self._cli_commands.get(service_lower, [])

        for cli_cmd in service_commands:
            if self._exact_match(api.api_name, cli_cmd.corresponding_api):
                return CliCoverageStatus(
                    api_name=api.api_name,
                    is_covered=CliCoveredStatus.COVERED,
                    matched_cli=cli_cmd,
                )

        for cli_cmd in service_commands:
            if self._fuzzy_match(api.api_name, cli_cmd.corresponding_api):
                return CliCoverageStatus(
                    api_name=api.api_name,
                    is_covered=CliCoveredStatus.UNCERTAIN,
                    matched_cli=cli_cmd,
                    uncertain_reason="Matched by fuzzy name matching",
                )

        for svc, cmds in self._cli_commands.items():
            for cli_cmd in cmds:
                if self._fuzzy_match(api.api_name, cli_cmd.corresponding_api):
                    return CliCoverageStatus(
                        api_name=api.api_name,
                        is_covered=CliCoveredStatus.UNCERTAIN,
                        matched_cli=cli_cmd,
                        uncertain_reason=f"Cross-service match in {svc}",
                    )

        return CliCoverageStatus(
            api_name=api.api_name,
            is_covered=CliCoveredStatus.NOT_COVERED,
        )

    @staticmethod
    def _exact_match(api_name: str, cli_api_name: str) -> bool:
        return api_name.lower() == cli_api_name.lower()

    @staticmethod
    def _fuzzy_match(api_name: str, cli_api_name: str) -> bool:
        api_lower = api_name.lower().replace("_", "")
        cli_lower = cli_api_name.lower().replace("_", "")
        if api_lower == cli_lower:
            return True

        api_snake = CliCoverageAnalyzer._to_snake(api_name)
        cli_snake = CliCoverageAnalyzer._to_snake(cli_api_name)
        if api_snake == cli_snake:
            return True

        if api_lower in cli_lower or cli_lower in api_lower:
            return True

        return False

    @staticmethod
    def _to_snake(name: str) -> str:
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    def _identify_gap(self, api: ApiInterface, status: CliCoverageStatus) -> CliGap:
        impact = ImpactLevel.IMPORTANT
        mandatory = IsMandatory.MANDATORY

        if api.is_mandatory == IsMandatory.OPTIONAL:
            mandatory = IsMandatory.OPTIONAL
            impact = ImpactLevel.NORMAL

        business_role = f"{api.http_method.value} {api.api_path}" if api.api_path else api.api_name

        return CliGap(
            unsupported_api=api.api_name,
            service_name=api.service_name,
            business_role=business_role,
            impact_level=impact,
            is_mandatory=mandatory,
        )

    def _find_alternative(self, api: ApiInterface) -> Optional[AlternativeScheme]:
        service_lower = api.service_name.lower()
        service_commands = self._cli_commands.get(service_lower, [])

        for cli_cmd in service_commands:
            api_base = self._get_base_action(api.api_name)
            cmd_base = self._get_base_action(cli_cmd.corresponding_api)
            if api_base and cmd_base and api_base == cmd_base:
                return AlternativeScheme(
                    unsupported_api=api.api_name,
                    alternative_cli=cli_cmd.cli_command,
                    feasibility=AlternativeFeasibility.PARTIAL,
                    alternative_difference=f"CLI command {cli_cmd.corresponding_api} may not fully support all parameters of {api.api_name}",
                )

        for svc, cmds in self._cli_commands.items():
            if svc == service_lower:
                continue
            for cli_cmd in cmds:
                if self._fuzzy_match(api.api_name, cli_cmd.corresponding_api):
                    return AlternativeScheme(
                        unsupported_api=api.api_name,
                        alternative_cli=cli_cmd.cli_command,
                        feasibility=AlternativeFeasibility.PARTIAL,
                        alternative_difference=f"Cross-service alternative from {svc}",
                    )

        return AlternativeScheme(
            unsupported_api=api.api_name,
            alternative_cli="",
            feasibility=AlternativeFeasibility.NONE,
            alternative_difference="No alternative CLI command found",
        )

    @staticmethod
    def _get_base_action(name: str) -> str:
        for prefix in ("List", "Create", "Delete", "Update", "Show", "Get", "Batch"):
            if name.startswith(prefix):
                return prefix
        return ""