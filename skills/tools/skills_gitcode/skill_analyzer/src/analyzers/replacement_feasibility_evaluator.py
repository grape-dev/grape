from typing import List

from src.models.api_interface import ApiBusinessRole, OpenAPIUsage
from src.models.cli_command import KooCliCorrespondence
from src.models.enums import CorrespondenceStatus, IsMandatory, LlmEnhancementStatus, ReplacementConclusion
from src.models.replacement import BlockingPoint, KooCliReplacementFeasibility
from src.models.report import ReportStageResult


class ReplacementFeasibilityEvaluator:
    def evaluate(
        self,
        openapi_usages: List[OpenAPIUsage],
        api_business_roles: List[ApiBusinessRole],
        correspondences: List[KooCliCorrespondence],
        equivalence_results: List = None,
    ) -> tuple[KooCliReplacementFeasibility, ReportStageResult]:
        role_map = {r.usage_id: r for r in api_business_roles}
        corr_map = {c.usage_id: c for c in correspondences}
        blocking_points = []
        replaceable_scope = []
        non_replaceable_scope = []
        stats = {
            "total": len(openapi_usages),
            "has_correspondence": 0,
            "no_correspondence": 0,
            "mandatory_total": 0,
        }

        has_mandatory_block = False

        for usage in openapi_usages:
            role = role_map.get(usage.usage_id)
            mandatory = not role or role.is_mandatory == IsMandatory.MANDATORY
            if mandatory:
                stats["mandatory_total"] += 1
            corr = corr_map.get(usage.usage_id)
            has_match = corr and corr.status == CorrespondenceStatus.EXACT
            if has_match:
                stats["has_correspondence"] += 1
                replaceable_scope.append(usage.api_name)
            else:
                stats["no_correspondence"] += 1
                non_replaceable_scope.append(usage.api_name)
                if mandatory:
                    has_mandatory_block = True
                blocking_points.append(self._blocking_point(usage.api_name, "无对应KooCLI命令", "无法完全迁移该接口能力"))

        if not openapi_usages:
            conclusion = ReplacementConclusion.NEEDS_CONFIRMATION
            reason = "未提取到Open API接口，无法判断KooCLI替换可行性。"
        elif has_mandatory_block:
            conclusion = ReplacementConclusion.NOT_REPLACEABLE
            reason = "存在必须接口无对应KooCLI命令。"
        elif non_replaceable_scope:
            conclusion = ReplacementConclusion.PARTIALLY_REPLACEABLE
            reason = "部分可选接口无对应KooCLI命令，必须接口均可替换。"
        else:
            conclusion = ReplacementConclusion.FULLY_REPLACEABLE
            reason = "所有接口均存在对应的KooCLI命令。"

        feasibility = KooCliReplacementFeasibility(
            conclusion=conclusion,
            reason=reason,
            blocking_points=blocking_points,
            replaceable_scope=replaceable_scope,
            non_replaceable_scope=non_replaceable_scope,
            statistics=stats,
            llm_status=LlmEnhancementStatus.DISABLED,
        )
        stage_3 = ReportStageResult(
            stage_name="第三阶段：KooCLI替换可行性",
            summary=reason,
            details={
                "conclusion": conclusion.value,
                "statistics": stats,
                "blocking_points": len(blocking_points),
                "replaceable_scope": replaceable_scope,
                "non_replaceable_scope": non_replaceable_scope,
            },
        )
        return feasibility, stage_3

    @staticmethod
    def _blocking_point(api_name: str, reason: str, impact: str) -> BlockingPoint:
        return BlockingPoint(
            api_name=api_name,
            reason=reason,
            business_impact=impact or "可能影响Skill原有业务效果",
            recommendation="保留SDK实现或人工验证KooCLI组合命令是否可接受",
        )
