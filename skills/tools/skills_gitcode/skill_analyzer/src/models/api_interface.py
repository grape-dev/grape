from dataclasses import dataclass, field
from typing import Dict, List

from src.models.enums import CliCoveredStatus, HttpMethod, IsMandatory


@dataclass
class ApiParameter:
    name: str
    param_type: str
    is_required: bool
    description: str = ""


@dataclass
class SourceSdkCall:
    service_name: str
    client_class: str
    method_name: str
    source_file: str = ""
    line_number: int = 0
    call_arguments: Dict[str, str] = field(default_factory=dict)


@dataclass
class ApiBusinessRole:
    usage_id: str
    business_role: str
    business_stage: str = ""
    is_mandatory: IsMandatory = IsMandatory.MANDATORY
    upstream_dependencies: List[str] = field(default_factory=list)
    downstream_dependencies: List[str] = field(default_factory=list)
    state_impact: str = ""
    rule_basis: str = ""
    llm_suggestion: str = ""


@dataclass
class OpenAPIUsage:
    usage_id: str
    service_name: str
    api_name: str
    api_path: str
    http_method: HttpMethod
    required_params: List[ApiParameter] = field(default_factory=list)
    optional_params: List[ApiParameter] = field(default_factory=list)
    actual_params: Dict[str, str] = field(default_factory=dict)
    source_sdk_call: SourceSdkCall = field(default_factory=lambda: SourceSdkCall("", "", ""))
    response_fields_used: List[str] = field(default_factory=list)
    error_branches: List[str] = field(default_factory=list)
    business_role: ApiBusinessRole = None


@dataclass
class ApiInterface:
    service_name: str
    api_name: str
    api_path: str
    http_method: HttpMethod
    required_params: List[ApiParameter] = field(default_factory=list)
    optional_params: List[ApiParameter] = field(default_factory=list)
    cli_covered: CliCoveredStatus = CliCoveredStatus.NOT_COVERED
    is_mandatory: IsMandatory = IsMandatory.OPTIONAL


@dataclass
class SdkCallInfo:
    service_name: str
    client_class: str
    method_name: str
    call_arguments: Dict[str, str] = field(default_factory=dict)
    source_file: str = ""
    line_number: int = 0
