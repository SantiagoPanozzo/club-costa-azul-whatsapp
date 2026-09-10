import logging
import traceback
from dataclasses import dataclass, field
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ServicesAPIConflict, ServicesAPIError
from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
ACTIVITY_PREFIX = "act_"
CONFIRM_YES = "confirm_yes"
CONFIRM_NO = "confirm_no"
ACTION_ENROLL = "act_enroll"
ACTION_CANCEL = "act_cancel"
CANCEL_PREFIX = "cancel_"
CANCEL_CONFIRM_YES = "cancel_act_yes"
CANCEL_CONFIRM_NO = "cancel_act_no"


class ActivitiesStep(StrEnum):
    AWAITING_ACTION = "awaiting_action"
    AWAITING_CHOICE = "awaiting_choice"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_CANCEL_CHOICE = "awaiting_cancel_choice"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


@dataclass
class ActivitiesState:
    step: ActivitiesStep = ActivitiesStep.AWAITING_ACTION
    available_activities: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_activity: dict[str, object] | None = None
    active_inscriptions: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_inscription: dict[str, object] | None = None


class ActivitiesFlow(BaseFlow[ActivitiesState]):
    def create_state(self) -> ActivitiesState:
        return ActivitiesState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        state.step = ActivitiesStep.AWAITING_ACTION
        await whatsapp_client.send_buttons(
            phone,
            body="¿Qué querés hacer?",
            buttons=[(ACTION_ENROLL, "Inscribirme"), (ACTION_CANCEL, "Cancelar inscripción")],
            session=session,
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == ActivitiesStep.AWAITING_ACTION:
            return await self._handle_action(phone, session, state, msg)
        if state.step == ActivitiesStep.AWAITING_CHOICE:
            return await self._handle_choice(phone, session, state, msg)
        if state.step == ActivitiesStep.AWAITING_CONFIRM:
            return await self._handle_confirm(phone, session, state, msg)
        if state.step == ActivitiesStep.AWAITING_CANCEL_CHOICE:
            return await self._handle_cancel_choice(phone, session, state, msg)
        if state.step == ActivitiesStep.AWAITING_CANCEL_CONFIRM:
            return await self._handle_cancel_confirm(phone, session, state, msg)

        return FlowResult.DONE

    async def _handle_action(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == ACTION_ENROLL:
            await self._show_activities(phone, session, state)
            return FlowResult.CONTINUE
        if msg.interactive_id == ACTION_CANCEL:
            return await self._show_cancellable(phone, session, state)

        await whatsapp_client.send_text(
            phone, "Por favor, elegí Inscribirme o Cancelar inscripción.", session=session
        )
        return FlowResult.CONTINUE

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
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return

        actividades_by_id: dict[object, dict[str, object]] = {a["id"]: a for a in actividades}
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
            session.end_flow()
            return

        state.available_activities = {str(a["id"]): a for a in disponibles}
        state.step = ActivitiesStep.AWAITING_CHOICE

        note = " (mostrando las primeras 10)" if len(disponibles) > 10 else ""
        rows = [
            {
                "id": f"{ACTIVITY_PREFIX}{a['id']}",
                "title": str(a["nombre"]),
                "description": f"{a['diasHorario']} - ${a['costo']:.0f}",
            }
            for a in disponibles[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body=f"Elegí una actividad para inscribirte{note}:",
            button_text="Ver actividades",
            rows=rows,
            section_title="Actividades disponibles",
            session=session,
        )

    async def _handle_choice(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
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
                await svc.services_client.post_inscripcion(str(socio["id"]), str(activity["id"]))
            except ServicesAPIConflict as exc:
                await whatsapp_client.send_text(phone, str(exc), session=session)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.warning(
                    "Error creating inscription for socio %s to activity %s",
                    socio["id"],
                    activity["id"],
                )
                traceback.print_exc()
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

    async def _show_cancellable(
        self, phone: str, session: Session, state: ActivitiesState
    ) -> FlowResult:
        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])
        try:
            inscripciones = await svc.services_client.get_inscripciones_socio(socio_id)
            actividades = await svc.services_client.get_actividades()
        except ServicesAPIError:
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE

        actividades_by_id: dict[object, dict[str, object]] = {a["id"]: a for a in actividades}
        activas = [i for i in inscripciones if i.get("estado") == "Activa"]

        if not activas:
            await whatsapp_client.send_text(
                phone, "No tenés inscripciones activas para cancelar.", session=session
            )
            return FlowResult.DONE

        state.active_inscriptions = {str(i["id"]): i for i in activas}
        state.step = ActivitiesStep.AWAITING_CANCEL_CHOICE

        rows = []
        for i in activas[:10]:
            act = actividades_by_id.get(i.get("actividadId"))
            nombre = str(act["nombre"]) if act else "Actividad"
            rows.append({
                "id": f"{CANCEL_PREFIX}{i['id']}",
                "title": nombre,
                "description": act.get("diasHorario", "") if act else "",
            })

        await whatsapp_client.send_list(
            to=phone,
            body="Elegí la inscripción que querés cancelar:",
            button_text="Ver inscripciones",
            rows=rows,
            section_title="Inscripciones activas",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_cancel_choice(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(CANCEL_PREFIX):
            await whatsapp_client.send_text(
                phone, "Por favor, elegí una inscripción de la lista.", session=session
            )
            return FlowResult.CONTINUE

        insc_id = iid[len(CANCEL_PREFIX) :]
        insc = state.active_inscriptions.get(insc_id)
        if not insc:
            await whatsapp_client.send_text(
                phone, "Esa inscripción ya no está disponible.", session=session
            )
            return FlowResult.DONE

        state.selected_inscription = insc
        state.step = ActivitiesStep.AWAITING_CANCEL_CONFIRM
        await whatsapp_client.send_buttons(
            phone,
            body="¿Confirmás la cancelación de esta inscripción?",
            buttons=[(CANCEL_CONFIRM_YES, "Confirmar"), (CANCEL_CONFIRM_NO, "Volver")],
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_cancel_confirm(
        self, phone: str, session: Session, state: ActivitiesState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CANCEL_CONFIRM_YES:
            insc = state.selected_inscription
            assert insc is not None
            try:
                await svc.services_client.delete_inscripcion(str(insc["id"]))
            except ServicesAPIError:
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE
            await whatsapp_client.send_text(phone, "Inscripción cancelada.", session=session)
            return FlowResult.DONE

        if msg.interactive_id == CANCEL_CONFIRM_NO:
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Confirmar o Volver.", session=session)
        return FlowResult.CONTINUE
