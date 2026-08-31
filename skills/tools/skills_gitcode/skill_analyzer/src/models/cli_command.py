from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.models.enums import (
    Acceptability,
    CliCoveredStatus,
    CliInfoSource,
    CorrespondenceStatus,
    DimensionStatus,
)


@dataclass
class CliCommand:
    cli_command: str
    corresponding_api: str
    cli_service: str
    parameter_mapping: Dict[str, str] = field(default_factory=dict)


@dataclass
class CliCoverageStatus:
    api_name: str
    is_covered: CliCoveredStatus
    matched_cli: Optional[CliCommand] = None
    uncertain_reason: str = ""


@dataclass
class KooCliParameter:
    name: str
    description: str = ""
    required: bool = False
    value_type: str = "str"


@dataclass
class KooCliCommand:
    command: str
    service_name: str
    operation_name: str
    resource_object: str = ""
    parameters: List[KooCliParameter] = field(default_factory=list)
    output_fields: List[str] = field(default_factory=list)
    info_source: CliInfoSource = CliInfoSource.LOCAL_CLI


@dataclass
class EquivalenceDimensionResult:
    dimension_name: str
    status: DimensionStatus
    basis: str = ""
    difference: str = ""
    impact: str = ""


@dataclass
class EffectEquivalenceResult:
    usage_id: str
    api_name: str
    candidate_command: Optional[KooCliCommand]
    dimensions: List[EquivalenceDimensionResult] = field(default_factory=list)
    overall_status: CorrespondenceStatus = CorrespondenceStatus.UNKNOWN
    confidence: float = 0.0
    consistency_basis: str = ""
    difference_description: str = ""
    impact_description: str = ""
    acceptable: Acceptability = Acceptability.UNKNOWN
    manual_verification_items: List[str] = field(default_factory=list)
    llm_assisted: bool = False


@dataclass
class KooCliCorrespondence:
    usage_id: str
    api_name: str
    service_name: str
    candidate_commands: List[KooCliCommand] = field(default_factory=list)
    selected_command: Optional[KooCliCommand] = None
    status: CorrespondenceStatus = CorrespondenceStatus.UNKNOWN
    equivalence_result: Optional[EffectEquivalenceResult] = None
    difference_description: str = ""
    impact_description: str = ""
    acceptable: Acceptability = Acceptability.UNKNOWN
    info_source: CliInfoSource = CliInfoSource.NONE
    manual_verification_items: List[str] = field(default_factory=list)
    api_explorer_url: str = ""
