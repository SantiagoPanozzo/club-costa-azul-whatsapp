import logging
from dataclasses import dataclass, field
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ServicesAPIConflict, ServicesAPIError
from ..state import Session, sessions
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from .base import BaseFlow, FlowResult
from .utils import send_list_pages

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
ACTIVITY_PREFIX = "act_"
CONFIRM_YES = "confirm_yes"
CONFIRM_NO = "confirm_no"
CANCEL_PREFIX = "act_cancel_"
CANCEL_YES = "act_cancel_yes"
CANCEL_NO = "act_cancel_no"


class ActivitiesStep(StrEnum):
    AWAITING_CHOICE = "awaiting_choice"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


@dataclass
class ActivitiesState:
    step: ActivitiesStep = ActivitiesStep.AWAITING_CHOICE
    available_activities: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_activity: dict[str, object] | None = None
    activities_by_id: dict[str, dict[str, object]] = field(default_factory=dict)
    active_inscriptions: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_inscription: dict[str, object] | None = None


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
        if state.step == ActivitiesStep.AWAITING_CANCEL_CONFIRM:
            return await self._handle_cancel_confirm(phone, session, state, msg)

        return FlowResult.DONE

    async def _show_activities(self, phone: str, session: Session, state: ActivitiesState) -> None:
        socio = session.socio
        assert socio is not None
        socio_id = socio["id"]
        try:
            inscripciones = await svc.services_client.get_inscripciones_socio(str(socio_id))
            actividades = await svc.services_client.get_actividades()
        except ServicesAPIError:
            logger.exception("Error fetching activities or enrollments")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            session.end_flow()
            return

        actividades_by_id: dict[object, dict[str, object]] = {a["id"]: a for a in actividades}
        state.activities_by_id = {str(a["id"]): a for a in actividades}
        activas = [i for i in inscripciones if i.get("estado") == "Activa"]

        if activas:
            lines: list[str] = []
            for i in activas:
                act = actividades_by_id.get(i.get("actividadId"))
                if act:
                    lines.append(f"- {act['nombre']} ({act['diasHorario']})")
                else:
                    lines.append("- Actividad (detalle no disponible)")
            text = "Ya estás inscripto/a en:\n\n" + "\n".join(lines)
        else:
            text = "Todavía no estás inscripto/a en ninguna actividad."
        await whatsapp_client.send_text(phone, text, session=session)

        if activas:
            state.active_inscriptions = {str(i["id"]): i for i in activas}
            rows = []
            for inscription in activas:
                activity = state.activities_by_id.get(str(inscription.get("actividadId")), {})
                rows.append(
                    {
                        "id": f"{CANCEL_PREFIX}{inscription['id']}",
                        "title": str(activity.get("nombre", "Actividad")),
                        "description": "Cancelar inscripción",
                    }
                )
            await send_list_pages(
                phone,
                body="Si necesitás cancelar una inscripción, elegila acá:",
                button_text="Mis inscripciones",
                rows=rows,
                section_title="Actividades inscriptas",
                session=session,
            )

        inscriptas_ids = {i.get("actividadId") for i in activas}
        disponibles = [
            a
            for a in actividades
            if a.get("estado") == "Activa" and a.get("cupoDisponible", 0) > 0 and a["id"] not in inscriptas_ids
        ]

        if not disponibles:
            await whatsapp_client.send_text(
                phone,
                "No hay otras actividades disponibles para inscribirte en este momento.",
                session=session,
            )
            if not activas:
                session.end_flow()
            return

        state.available_activities = {str(a["id"]): a for a in disponibles}
        state.step = ActivitiesStep.AWAITING_CHOICE

        rows = [
            {
                "id": f"{ACTIVITY_PREFIX}{a['id']}",
                "title": str(a["nombre"]),
                "description": f"{a['diasHorario']} - ${a['costo']:.0f}",
            }
            for a in disponibles
        ]
        await send_list_pages(
            phone,
            body="Elegí una actividad para inscribirte:",
            button_text="Ver actividades",
            rows=rows,
            section_title="Actividades disponibles",
            session=session,
        )

    async def _handle_choice(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if iid.startswith(CANCEL_PREFIX):
            inscription = state.active_inscriptions.get(iid[len(CANCEL_PREFIX) :])
            if not inscription:
                await whatsapp_client.send_text(phone, "Esa inscripción ya no está disponible.", session=session)
                return FlowResult.DONE
            state.selected_inscription = inscription
            state.step = ActivitiesStep.AWAITING_CANCEL_CONFIRM
            activity = state.activities_by_id.get(str(inscription.get("actividadId")), {})
            await whatsapp_client.send_buttons(
                phone,
                body=f"¿Confirmás que querés cancelar tu inscripción a *{activity.get('nombre', 'la actividad')}*?",
                buttons=[(CANCEL_YES, "Dar de baja"), (CANCEL_NO, "Volver")],
                session=session,
            )
            return FlowResult.CONTINUE
        if not iid.startswith(ACTIVITY_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí una actividad de la lista.", session=session)
            return FlowResult.CONTINUE

        activity_id = iid[len(ACTIVITY_PREFIX) :]
        activity = state.available_activities.get(activity_id)
        if not activity:
            await whatsapp_client.send_text(
                phone,
                "Esa actividad ya no está disponible. Te muestro la lista actualizada.",
                session=session,
            )
            await self._show_activities(phone, session, state)
            return FlowResult.CONTINUE

        state.selected_activity = activity
        state.step = ActivitiesStep.AWAITING_CONFIRM
        await whatsapp_client.send_buttons(
            phone,
            body=(
                f"¿Confirmás tu inscripción a *{activity['nombre']}*?\n"
                f"Horario: {activity['diasHorario']}\n"
                f"Costo: ${float(str(activity['costo'])):.0f}"
            ),
            buttons=[(CONFIRM_YES, "Confirmar"), (CONFIRM_NO, "Cancelar")],
            session=session,
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
                operation = "create-activity-enrollment"
                if not await sessions.was_mutation_applied(msg.wamid, operation):
                    await svc.services_client.post_inscripcion(str(socio["id"]), str(activity["id"]))
                    await sessions.mark_mutation_applied(msg.wamid, operation)
            except ServicesAPIConflict as exc:
                await whatsapp_client.send_text(phone, str(exc), session=session)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.exception("Error creating activity enrollment")
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE
            await whatsapp_client.send_text(
                phone, f"¡Listo! Quedaste inscripto/a en *{activity['nombre']}*.", session=session
            )
            return FlowResult.DONE

        if msg.interactive_id == CONFIRM_NO:
            await whatsapp_client.send_text(phone, "Inscripción cancelada.", session=session)
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Confirmar o Cancelar.", session=session)
        return FlowResult.CONTINUE

    async def _handle_cancel_confirm(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CANCEL_NO:
            return FlowResult.DONE
        if msg.interactive_id != CANCEL_YES:
            await whatsapp_client.send_text(phone, "Por favor, tocá Dar de baja o Volver.", session=session)
            return FlowResult.CONTINUE

        inscription = state.selected_inscription
        assert inscription is not None
        try:
            operation = "cancel-activity-enrollment"
            if not await sessions.was_mutation_applied(msg.wamid, operation):
                await svc.services_client.delete_inscripcion(str(inscription["id"]))
                await sessions.mark_mutation_applied(msg.wamid, operation)
        except ServicesAPIError as exc:
            logger.exception("Error cancelling activity enrollment")
            message = str(exc) if isinstance(exc, ServicesAPIConflict) else GENERIC_ERROR
            await whatsapp_client.send_text(phone, message, session=session)
            return FlowResult.DONE
        await whatsapp_client.send_text(phone, "Tu inscripción fue cancelada.", session=session)
        return FlowResult.DONE
