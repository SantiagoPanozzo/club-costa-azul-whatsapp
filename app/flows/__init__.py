from .activities import ActivitiesFlow
from .base import BaseFlow, FlowResult
from .comunicados import ComunicadosFlow
from .cuotas import CuotasFlow
from .datos_personales import DatosPersonalesFlow
from .eventos import EventosFlow
from .reservas import ReservasFlow

__all__ = ["FLOW_REGISTRY", "BaseFlow", "FlowResult"]

FLOW_REGISTRY: dict[str, BaseFlow] = {
    "activities": ActivitiesFlow(),
    "cuotas": CuotasFlow(),
    "reservas": ReservasFlow(),
    "eventos": EventosFlow(),
    "datos_personales": DatosPersonalesFlow(),
    "comunicados": ComunicadosFlow(),
}
