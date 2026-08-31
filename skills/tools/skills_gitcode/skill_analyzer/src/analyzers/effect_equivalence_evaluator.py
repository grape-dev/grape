import re
from typing import List, Optional

from src.models.api_interface import ApiBusinessRole, OpenAPIUsage
from src.models.cli_command import (
    EffectEquivalenceResult,
    EquivalenceDimensionResult,
    KooCliCommand,
)
from src.models.enums import Acceptability, CorrespondenceStatus, DimensionStatus


class EffectEquivalenceEvaluator:
    DIMENSIONS = [
        "目标服务一致",
        "操作语义一致",
        "资源作用对象一致",
        "参数表达完整",
        "返回信息满足流程",
        "错误语义一致",
        "业务状态影响一致",
    ]

    def evaluate(
        self,
        api_usage: OpenAPIUsage,
        business_role: Optional[ApiBusinessRole],
        candidate_command: Optional[KooCliCommand],
    ) -> EffectEquivalenceResult:
        if candidate_command is None:
            return EffectEquivalenceResult(
                usage_id=api_usage.usage_id,
                api_name=api_usage.api_name,
                candidate_command=None,
                dimensions=[
                    EquivalenceDimensionResult(
                        dimension_name=name,
                        status=DimensionStatus.NOT_SATISFIED,
                        basis="无候选KooCLI命令",
                        difference="不存在可用于比较的命令",
                        impact="无法仅通过KooCLI实现该API对应能力",
                    )
                    for name in self.DIMENSIONS
                ],
                overall_status=CorrespondenceStatus.NONE,
                confidence=1.0,
                consistency_basis="未发现对应KooCLI命令",
                difference_description="无对应命令",
                impact_description="该接口无法被KooCLI完全替换",
                acceptable=Acceptability.NOT_ACCEPTABLE,
                manual_verification_items=[],
            )

        dimensions = [
            self._service_dimension(api_usage, candidate_command),
            self._operation_dimension(api_usage, candidate_command),
            self._resource_dimension(api_usage, candidate_command),
            self._parameter_dimension(api_usage, candidate_command),
            self._response_dimension(api_usage, candidate_command),
            self._error_dimension(api_usage, candidate_command),
            self._state_dimension(api_usage, business_role, candidate_command),
        ]
        overall = self._overall_status(dimensions)
        failed = [d for d in dimensions if d.status == DimensionStatus.NOT_SATISFIED]
        unknown = [d for d in dimensions if d.status == DimensionStatus.UNKNOWN]
        confidence = round((len(dimensions) - len(failed) - len(unknown) * 0.5) / len(dimensions), 2)
        acceptable = Acceptability.ACCEPTABLE if overall == CorrespondenceStatus.EXACT else Acceptability.NOT_ACCEPTABLE
        if overall == CorrespondenceStatus.UNKNOWN:
            acceptable = Acceptability.UNKNOWN

        return EffectEquivalenceResult(
            usage_id=api_usage.usage_id,
            api_name=api_usage.api_name,
            candidate_command=candidate_command,
            dimensions=dimensions,
            overall_status=overall,
            confidence=confidence,
            consistency_basis="；".join(d.basis for d in dimensions if d.basis),
            difference_description="；".join(d.difference for d in dimensions if d.difference),
            impact_description="；".join(d.impact for d in dimensions if d.impact),
            acceptable=acceptable,
            manual_verification_items=[d.dimension_name for d in unknown],
        )

    def _service_dimension(self, api_usage: OpenAPIUsage, command: KooCliCommand) -> EquivalenceDimensionResult:
        matched = self._norm(api_usage.service_name) == self._norm(command.service_name)
        return EquivalenceDimensionResult(
            "目标服务一致",
            DimensionStatus.SATISFIED if matched else DimensionStatus.NOT_SATISFIED,
            f"API服务={api_usage.service_name}, CLI服务={command.service_name}",
            "" if matched else "目标服务不一致",
            "" if matched else "可能操作不同云服务，不能判为完全一致",
        )

    def _operation_dimension(self, api_usage: OpenAPIUsage, command: KooCliCommand) -> EquivalenceDimensionResult:
        api_action = self._action(api_usage.api_name)
        cli_op = self._strip_version(command.operation_name or command.command)
        cli_action = self._action(cli_op)
        matched = api_action and api_action == cli_action
        return EquivalenceDimensionResult(
            "操作语义一致",
            DimensionStatus.SATISFIED if matched else DimensionStatus.NOT_SATISFIED,
            f"API动作={api_action}, CLI动作={cli_action}",
            "" if matched else "操作语义不一致或无法识别",
            "" if matched else "业务流程可能执行不同动作",
        )

    def _resource_dimension(self, api_usage: OpenAPIUsage, command: KooCliCommand) -> EquivalenceDimensionResult:
        api_resource = self._resource_from_path(api_usage.api_path)
        cli_resource = self._norm(command.resource_object or command.operation_name)
        matched = bool(api_resource and (api_resource in cli_resource or cli_resource in api_resource))
        if not matched and api_resource and cli_resource:
            api_sing = api_resource.rstrip("s")
            cli_sing = cli_resource.rstrip("s")
            if api_sing == cli_sing or api_sing in cli_sing or cli_sing in api_sing:
                matched = True
        if not matched:
            service_matched = self._norm(api_usage.service_name) == self._norm(command.service_name)
            api_action = self._action(api_usage.api_name)
            cli_action = self._action(self._strip_version(command.operation_name or command.command))
            action_matched = api_action and api_action == cli_action
            if service_matched and action_matched:
                matched = True
        if not matched and self._norm(api_usage.service_name) == "obs":
            matched = True
        if not cli_resource:
            matched = False
        return EquivalenceDimensionResult(
            "资源作用对象一致",
            DimensionStatus.SATISFIED if matched else DimensionStatus.UNKNOWN,
            f"API资源={api_resource}, CLI资源={cli_resource}",
            "" if matched else "资源对象无法完全确认",
            "" if matched else "可能需要人工确认CLI命令作用对象是否一致",
        )

    def _parameter_dimension(self, api_usage: OpenAPIUsage, command: KooCliCommand) -> EquivalenceDimensionResult:
        cli_params = {self._norm(p.name) for p in command.parameters}
        actual_params = {self._norm(k) for k in api_usage.actual_params.keys()}
        required_params = {self._norm(p.name) for p in api_usage.required_params}
        required_actual = actual_params or required_params
        missing = [p for p in required_actual if p and p not in cli_params]
        if not required_actual:
            status = DimensionStatus.SATISFIED
        elif missing:
            status = DimensionStatus.NOT_SATISFIED
        else:
            status = DimensionStatus.SATISFIED
        return EquivalenceDimensionResult(
            "参数表达完整",
            status,
            f"实际/必需参数={sorted(required_actual)}, CLI参数={sorted(cli_params)}",
            "" if not missing else f"CLI缺少参数表达能力: {missing}",
            "" if not missing else "Skill实际使用参数无法完整迁移到KooCLI",
        )

    def _response_dimension(self, api_usage: OpenAPIUsage, command: KooCliCommand) -> EquivalenceDimensionResult:
        if not api_usage.response_fields_used:
            return EquivalenceDimensionResult("返回信息满足流程", DimensionStatus.SATISFIED, "Skill未检测到返回字段依赖")
        cli_outputs = {self._norm(x) for x in command.output_fields}
        required = {self._norm(x) for x in api_usage.response_fields_used}
        missing = [x for x in required if x not in cli_outputs]
        return EquivalenceDimensionResult(
            "返回信息满足流程",
            DimensionStatus.SATISFIED if not missing else DimensionStatus.UNKNOWN,
            f"返回依赖={sorted(required)}, CLI输出={sorted(cli_outputs)}",
            "" if not missing else f"CLI输出字段待确认或缺失: {missing}",
            "" if not missing else "可能影响后续流程数据传递",
        )

    def _error_dimension(self, api_usage: OpenAPIUsage, command: KooCliCommand) -> EquivalenceDimensionResult:
        if not api_usage.error_branches:
            return EquivalenceDimensionResult("错误语义一致", DimensionStatus.SATISFIED, "Skill未检测到错误分支依赖")
        return EquivalenceDimensionResult(
            "错误语义一致",
            DimensionStatus.UNKNOWN,
            f"检测到错误分支: {api_usage.error_branches}",
            "CLI退出码/错误文本与SDK异常需要人工确认",
            "错误语义差异可能改变业务决策分支",
        )

    def _state_dimension(
        self,
        api_usage: OpenAPIUsage,
        business_role: Optional[ApiBusinessRole],
        command: KooCliCommand,
    ) -> EquivalenceDimensionResult:
        state_impact = business_role.state_impact if business_role else ""
        if not state_impact:
            return EquivalenceDimensionResult("业务状态影响一致", DimensionStatus.SATISFIED, "未提取到业务状态影响，默认满足")
        return EquivalenceDimensionResult("业务状态影响一致", DimensionStatus.SATISFIED, state_impact)

    def _overall_status(self, dimensions: List[EquivalenceDimensionResult]) -> CorrespondenceStatus:
        if any(d.status == DimensionStatus.NOT_SATISFIED for d in dimensions):
            return CorrespondenceStatus.PARTIAL
        if any(d.status == DimensionStatus.UNKNOWN for d in dimensions):
            return CorrespondenceStatus.UNKNOWN
        return CorrespondenceStatus.EXACT

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    @staticmethod
    def _strip_version(name: str) -> str:
        return re.sub(r"/v\d+$", "", name or "")

    @staticmethod
    def _action(value: str) -> str:
        lower = (value or "").lower()
        for action in ["create", "delete", "update", "show", "get", "list", "batch", "set", "put", "check", "count", "associate", "disassociate", "grant", "revoke", "enable", "disable", "download", "upload", "copy", "move", "rename", "search", "query", "validate", "verify", "login", "logout", "register", "unregister"]:
            if action in lower:
                return action
        return ""

    @staticmethod
    def _resource_from_path(path: str) -> str:
        parts = [p for p in (path or "").split("/") if p and not p.startswith("{") and not p.startswith("v")]
        return re.sub(r"[^a-z0-9]", "", (parts[-1] if parts else "").lower())