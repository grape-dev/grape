from dataclasses import dataclass

from src.models.enums import StepStatus


@dataclass
class CheckpointInfo:
    thread_id: str
    skill_name: str
    created_at: str
    last_step: str
    status: StepStatus
    node_count: int