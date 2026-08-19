from .activities import ActivitiesFlow
from .base import BaseFlow, FlowResult

__all__ = ["FLOW_REGISTRY", "BaseFlow", "FlowResult", "ActivitiesFlow"]

FLOW_REGISTRY: dict[str, BaseFlow] = {
    "activities": ActivitiesFlow(),
}
