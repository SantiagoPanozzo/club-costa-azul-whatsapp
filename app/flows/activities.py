import logging
import traceback
from dataclasses import dataclass, field
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ConflictError, ServicesAPIError
from ..state import Session
from ..webhook_parser import IncomingMessage
from ..whatsapp_client import whatsapp_client
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
ACTIVITY_PREFIX = "act_"
CONFIRM_YES = "confirm_yes"
CONFIRM_NO = "confirm_no"


class ActivitiesStep(StrEnum):
    AWAITING_CHOICE = "awaiting_choice"
    AWAITING_CONFIRM = "awaiting_confirm"


@dataclass
class ActivitiesState:
    step: ActivitiesStep = ActivitiesStep.AWAITING_CHOICE
    available_activities: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_activity: dict[str, object] | None = None


class ActivitiesFlow(BaseFlow[ActivitiesState]):
    def create_state(self) -> ActivitiesState:
        return ActivitiesState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        await self._show_activities(phone, session, state)

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == ActivitiesStep.AWAITING_CHOICE:
            return await self._handle_choice(phone, session, state, msg)
        if state.step == ActivitiesStep.AWAITING_CONFIRM:
            return await self._handle_confirm(phone, session, state, msg)

        return FlowResult.DONE

    async def _show_activities(self, phone: str, session: Session, state: ActivitiesState) -> None:
        socio = session.socio
        assert socio is not None
        socio_id = socio["id"]
        try:
            inscripciones = await svc.services_client.get_inscripciones_socio(str(socio_id))
            actividades = await svc.services_client.get_actividades()
        except ServicesAPIError:
            logger.warning("Error fetching activities or inscriptions for socio %s", socio_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR)
            return

        actividades_by_id: dict[object, dict[str, object]] = {a["id"]: a for a in actividades}
        activas = [i for i in inscripciones if i.get("estado") == "Activa"]

        if activas:
            lines: list[str] = []
            for i in activas:
                act = actividades_by_id.get(i.get("actividadId"))
                if act:
                    lines.append(f"- {act['nombre']}")
                else:
                    lines.append("- Actividad (detalle no disponible)")
            text = "Ya estás inscripto/a en:\n\n" + "\n".join(lines)
        else:
            text = "Todavía no estás inscripto/a en ninguna actividad."
        await whatsapp_client.send_text(phone, text)

        inscriptas_ids = {i.get("actividadId") for i in activas}
        disponibles = [
            a
            for a in actividades
            if a.get("estado") == "Activa" and a.get("cupoDisponible", 0) > 0 and a["id"] not in inscriptas_ids
        ]

        if not disponibles:
            await whatsapp_client.send_text(
                phone, "No hay otras actividades disponibles para inscribirte en este momento."
            )
            session.end_flow()
            return

        state.available_activities = {str(a["id"]): a for a in disponibles}
        state.step = ActivitiesStep.AWAITING_CHOICE

        note = " (mostrando las primeras 10)" if len(disponibles) > 10 else ""
        rows = [
            {
                "id": f"{ACTIVITY_PREFIX}{a['id']}",
                "title": str(a["nombre"]),
                "description": f"${a['costo']:.0f} - {(a.get('descripcion') or '')[:50]}",
            }
            for a in disponibles[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body=f"Elegí una actividad para inscribirte{note}:",
            button_text="Ver actividades",
            rows=rows,
            section_title="Actividades disponibles",
        )

    async def _handle_choice(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(ACTIVITY_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí una actividad de la lista.")
            return FlowResult.CONTINUE

        activity_id = iid[len(ACTIVITY_PREFIX) :]
        activity = state.available_activities.get(activity_id)
        if not activity:
            await whatsapp_client.send_text(
                phone, "Esa actividad ya no está disponible. Te muestro la lista actualizada."
            )
            await self._show_activities(phone, session, state)
            return FlowResult.CONTINUE

        state.selected_activity = activity
        state.step = ActivitiesStep.AWAITING_CONFIRM

        horario = ""
        try:
            instancias = await svc.services_client.get_instancias_actividad(str(activity["id"]))
            if instancias:
                inst = instancias[0]
                horario = f"\nHorario: {inst['horaInicio'][:5]}-{inst['horaFin'][:5]}"
        except ServicesAPIError:
            pass

        await whatsapp_client.send_buttons(
            phone,
            body=(
                f"¿Confirmás tu inscripción a *{activity['nombre']}*?"
                f"{horario}\n"
                f"Costo: ${float(str(activity['costo'])):.0f}"
            ),
            buttons=[(CONFIRM_YES, "Confirmar"), (CONFIRM_NO, "Cancelar")],
        )
        return FlowResult.CONTINUE

    async def _handle_confirm(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CONFIRM_YES:
            activity = state.selected_activity
            assert activity is not None
            socio = session.socio
            assert socio is not None
            try:
                await svc.services_client.post_inscripcion(str(socio["id"]), str(activity["id"]))
            except ConflictError as exc:
                await whatsapp_client.send_text(phone, exc.detail)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.warning(
                    "Error creating inscription for socio %s to activity %s",
                    socio["id"],
                    activity["id"],
                )
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR)
                return FlowResult.DONE
            await whatsapp_client.send_text(phone, f"¡Listo! Quedaste inscripto/a en *{activity['nombre']}*.")
            return FlowResult.DONE

        if msg.interactive_id == CONFIRM_NO:
            await whatsapp_client.send_text(phone, "Inscripción cancelada.")
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Confirmar o Cancelar.")
        return FlowResult.CONTINUE
