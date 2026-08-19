from .activities import ActivitiesFlow
from .base import BaseFlow, FlowResult
from .payments import PaymentsFlow
from .reservations import ReservationsFlow

__all__ = [
    "FLOW_REGISTRY",
    "BaseFlow",
    "FlowResult",
    "ActivitiesFlow",
    "PaymentsFlow",
    "ReservationsFlow",
]

FLOW_REGISTRY: dict[str, BaseFlow] = {
    "activities": ActivitiesFlow(),
    "payments": PaymentsFlow(),
    "reservations": ReservationsFlow(),
}
