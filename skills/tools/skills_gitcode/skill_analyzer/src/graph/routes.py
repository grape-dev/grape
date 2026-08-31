from src.graph.state import AnalysisState
from src.models.enums import ImplType


def route_by_impl_type(state: AnalysisState) -> str:
    return "sdk_interface_analysis"