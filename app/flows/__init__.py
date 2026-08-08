from .activities import ActivitiesFlow
from .base import BaseFlow

FLOW_REGISTRY: dict[str, BaseFlow] = {
    "activities": ActivitiesFlow(),
}
