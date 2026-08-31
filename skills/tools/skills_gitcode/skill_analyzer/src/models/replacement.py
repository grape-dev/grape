from dataclasses import dataclass, field
from typing import Dict, List

from src.models.enums import LlmEnhancementStatus, ReplacementConclusion


@dataclass
class BlockingPoint:
    api_name: str
    reason: str
    business_impact: str
    recommendation: str = ""


@dataclass
class KooCliReplacementFeasibility:
    conclusion: ReplacementConclusion
    reason: str
    blocking_points: List[BlockingPoint] = field(default_factory=list)
    replaceable_scope: List[str] = field(default_factory=list)
    non_replaceable_scope: List[str] = field(default_factory=list)
    statistics: Dict[str, int] = field(default_factory=dict)
    llm_status: LlmEnhancementStatus = LlmEnhancementStatus.DISABLED