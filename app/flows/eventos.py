import logging
from dataclasses import dataclass, field
from datetime import date
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
EVENT_PREFIX = "evt_"
CONFIRM_YES = "evt_confirm_yes"
CONFIRM_NO = "evt_confirm_no"
CANCEL_PREFIX = "evt_cancel_"
CANCEL_YES = "evt_cancel_yes"
CANCEL_NO = "evt_cancel_no"


class EventosStep(StrEnum):
    AWAITING_CHOICE = "awaiting_choice"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


@dataclass
class EventosState:
    step: EventosStep = EventosStep.AWAITING_CHOICE
    available_events: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_event: dict[str, object] | None = None
    events_by_id: dict[str, dict[str, object]] = field(default_factory=dict)
    confirmed_inscriptions: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_inscription: dict[str, object] | None = None


class EventosFlow(BaseFlow[EventosState]):
    def create_state(self) -> EventosState:
        return EventosState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        await self._show_events(phone, session, state)

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == EventosStep.AWAITING_CHOICE:
            return await self._handle_choice(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_CONFIRM:
            return await self._handle_confirm(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_CANCEL_CONFIRM:
            return await self._handle_cancel_confirm(phone, session, state, msg)

        return FlowResult.DONE

    async def _show_events(self, phone: str, session: Session, state: EventosState) -> None:
        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])

        try:
            inscripciones = await svc.services_client.get_inscripciones_evento_socio(socio_id)
            eventos = await svc.services_client.get_eventos()
        except ServicesAPIError:
            logger.exception("Error fetching events or member enrollments")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            session.end_flow()
            return

        eventos_by_id: dict[object, dict[str, object]] = {e["id"]: e for e in eventos}
        state.events_by_id = {str(e["id"]): e for e in eventos}
        confirmadas = [i for i in inscripciones if i.get("estado") == "Confirmada"]
        inscriptas_ids = {i.get("eventoId") for i in confirmadas}

        if confirmadas:
            lines: list[str] = []
            for i in confirmadas:
                evento = eventos_by_id.get(i.get("eventoId"))
                if evento:
                    lines.append(f"- {evento['nombre']} ({evento.get('fecha', '')})")
                else:
                    lines.append("- Evento (detalle no disponible)")
            text = "Ya estás inscripto/a en:\n\n" + "\n".join(lines)
        else:
            text = "Todavía no estás inscripto/a en ningún evento."
        await whatsapp_client.send_text(phone, text, session=session)

        if confirmadas:
            state.confirmed_inscriptions = {str(i["id"]): i for i in confirmadas}
            rows = []
            for inscription in confirmadas:
                event = eventos_by_id.get(inscription.get("eventoId"), {})
                rows.append(
                    {
                        "id": f"{CANCEL_PREFIX}{inscription['id']}",
                        "title": str(event.get("nombre", "Evento")),
                        "description": f"{event.get('fecha', '')} - cancelar inscripción",
                    }
                )
            await send_list_pages(
                phone,
                body="Si necesitás darte de baja de un evento, elegilo acá:",
                button_text="Mis inscripciones",
                rows=rows,
                section_title="Eventos inscriptos",
                session=session,
            )

        today = date.today().isoformat()
        disponibles = [
            e
            for e in eventos
            if e.get("estado") == "Programado"
            and str(e.get("fecha", "")) >= today
            and (e.get("cupoDisponible") is None or e.get("cupoDisponible", 0) > 0)
            and e["id"] not in inscriptas_ids
        ]

        if not disponibles:
            await whatsapp_client.send_text(
                phone,
                "No hay otros eventos disponibles para inscribirte en este momento.",
                session=session,
            )
            if not confirmadas:
                session.end_flow()
            return

        state.available_events = {str(e["id"]): e for e in disponibles}
        state.step = EventosStep.AWAITING_CHOICE

        rows = [
            {
                "id": f"{EVENT_PREFIX}{e['id']}",
                "title": str(e["nombre"]),
                "description": f"{e.get('fecha', '')} - ${float(str(e.get('costoSocio', 0))):.0f}",
            }
            for e in disponibles
        ]
        await send_list_pages(
            phone,
            body="Elegí un evento para ver más detalles:",
            button_text="Ver eventos",
            rows=rows,
            section_title="Eventos disponibles",
            session=session,
        )

    async def _handle_choice(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if iid.startswith(CANCEL_PREFIX):
            inscription = state.confirmed_inscriptions.get(iid[len(CANCEL_PREFIX) :])
            if not inscription:
                await whatsapp_client.send_text(phone, "Esa inscripción ya no está disponible.", session=session)
                return FlowResult.DONE
            state.selected_inscription = inscription
            state.step = EventosStep.AWAITING_CANCEL_CONFIRM
            event = state.events_by_id.get(str(inscription.get("eventoId")))
            event_name = event.get("nombre", "el evento") if event else "el evento"
            await whatsapp_client.send_buttons(
                phone,
                body=(
                    f"¿Confirmás que querés darte de baja de *{event_name}*? "
                    "Los pagos registrados no se reembolsan automáticamente."
                ),
                buttons=[(CANCEL_YES, "Dar de baja"), (CANCEL_NO, "Volver")],
                session=session,
            )
            return FlowResult.CONTINUE
        if not iid.startswith(EVENT_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí un evento de la lista.", session=session)
            return FlowResult.CONTINUE

        event_id = iid[len(EVENT_PREFIX) :]
        evento = state.available_events.get(event_id)
        if not evento:
            await whatsapp_client.send_text(
                phone,
                "Ese evento ya no está disponible. Te muestro la lista actualizada.",
                session=session,
            )
            await self._show_events(phone, session, state)
            return FlowResult.CONTINUE

        state.selected_event = evento
        state.step = EventosStep.AWAITING_CONFIRM

        nombre = evento.get("nombre", "")
        descripcion = evento.get("descripcion") or ""
        fecha = evento.get("fecha", "")
        hora_inicio = evento.get("horaInicio") or ""
        hora_fin = evento.get("horaFin") or ""
        lugar = evento.get("nombreEspacio") or "Sin definir"
        costo = float(str(evento.get("costoSocio", 0)))
        cupo = evento.get("cupoDisponible")

        lines = [f"*{nombre}*"]
        if descripcion:
            lines.append(descripcion)
        lines.append(f"📅 Fecha: {fecha}")
        if hora_inicio:
            horario = f"{hora_inicio} - {hora_fin}" if hora_fin else hora_inicio
            lines.append(f"🕐 Horario: {horario}")
        lines.append(f"📍 Lugar: {lugar}")
        lines.append(f"💲 Costo socio: ${costo:.0f}")
        if cupo is not None:
            lines.append(f"🎟️ Cupo disponible: {cupo}")

        await whatsapp_client.send_text(phone, "\n".join(lines), session=session)
        await whatsapp_client.send_buttons(
            phone,
            body="¿Querés inscribirte en este evento?",
            buttons=[(CONFIRM_YES, "Inscribirme"), (CONFIRM_NO, "Volver")],
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_confirm(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CONFIRM_YES:
            evento = state.selected_event
            assert evento is not None
            socio = session.socio
            assert socio is not None
            try:
                operation = "create-event-enrollment"
                if not await sessions.was_mutation_applied(msg.wamid, operation):
                    await svc.services_client.post_inscripcion_evento(str(evento["id"]), str(socio["id"]))
                    await sessions.mark_mutation_applied(msg.wamid, operation)
            except ServicesAPIConflict as exc:
                await whatsapp_client.send_text(phone, str(exc), session=session)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.exception("Error creating event enrollment")
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE
            await whatsapp_client.send_text(
                phone,
                f"¡Listo! Te inscribiste en *{evento['nombre']}*.",
                session=session,
            )
            return FlowResult.DONE

        if msg.interactive_id == CONFIRM_NO:
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Inscribirme o Volver.", session=session)
        return FlowResult.CONTINUE

    async def _handle_cancel_confirm(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CANCEL_NO:
            return FlowResult.DONE
        if msg.interactive_id != CANCEL_YES:
            await whatsapp_client.send_text(phone, "Por favor, tocá Dar de baja o Volver.", session=session)
            return FlowResult.CONTINUE

        inscription = state.selected_inscription
        assert inscription is not None
        try:
            operation = "cancel-event-enrollment"
            if not await sessions.was_mutation_applied(msg.wamid, operation):
                await svc.services_client.delete_inscripcion_evento(
                    str(inscription["eventoId"]),
                    str(inscription["id"]),
                )
                await sessions.mark_mutation_applied(msg.wamid, operation)
        except ServicesAPIConflict as exc:
            await whatsapp_client.send_text(phone, str(exc), session=session)
            return FlowResult.DONE
        except ServicesAPIError:
            logger.exception("Error cancelling event enrollment")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Tu inscripción al evento fue cancelada.", session=session)
        return FlowResult.DONE
