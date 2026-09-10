import logging
import traceback
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ServicesAPIConflict, ServicesAPIError
from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
EVENT_PREFIX = "evt_"
CONFIRM_YES = "evt_confirm_yes"
CONFIRM_NO = "evt_confirm_no"

ACTION_BROWSE = "evt_browse"
ACTION_WITHDRAW = "evt_withdraw"
WITHDRAW_PREFIX = "evtw_"
WITHDRAW_YES = "evt_withdraw_yes"
WITHDRAW_NO = "evt_withdraw_no"


class EventosStep(StrEnum):
    AWAITING_ACTION = "awaiting_action"
    AWAITING_CHOICE = "awaiting_choice"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_WITHDRAW_CHOICE = "awaiting_withdraw_choice"
    AWAITING_WITHDRAW_CONFIRM = "awaiting_withdraw_confirm"


@dataclass
class EventosState:
    step: EventosStep = EventosStep.AWAITING_ACTION
    available_events: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_event: dict[str, object] | None = None
    enrolled_events: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_withdrawal: dict[str, object] | None = None


class EventosFlow(BaseFlow[EventosState]):
    def create_state(self) -> EventosState:
        return EventosState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        state.step = EventosStep.AWAITING_ACTION
        await whatsapp_client.send_buttons(
            phone,
            body="¿Qué querés hacer?",
            buttons=[(ACTION_BROWSE, "Ver eventos"), (ACTION_WITHDRAW, "Cancelar inscripción")],
            session=session,
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == EventosStep.AWAITING_ACTION:
            return await self._handle_action(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_CHOICE:
            return await self._handle_choice(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_CONFIRM:
            return await self._handle_confirm(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_WITHDRAW_CHOICE:
            return await self._handle_withdraw_choice(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_WITHDRAW_CONFIRM:
            return await self._handle_withdraw_confirm(phone, session, state, msg)

        return FlowResult.DONE

    async def _handle_action(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == ACTION_BROWSE:
            await self._show_events(phone, session, state)
            return FlowResult.CONTINUE
        if msg.interactive_id == ACTION_WITHDRAW:
            return await self._show_withdrawable(phone, session, state)

        await whatsapp_client.send_text(phone, "Por favor, elegí Ver eventos o Cancelar inscripción.", session=session)
        return FlowResult.CONTINUE

    async def _show_events(self, phone: str, session: Session, state: EventosState) -> None:
        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])

        try:
            inscripciones = await svc.services_client.get_inscripciones_evento_socio(socio_id)
            eventos = await svc.services_client.get_eventos()
        except ServicesAPIError:
            logger.warning("Error fetching eventos or inscriptions for socio %s", socio_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            session.end_flow()
            return

        eventos_by_id: dict[object, dict[str, object]] = {e["id"]: e for e in eventos}
        inscriptas_ids = {i.get("eventoId") for i in inscripciones}

        if inscripciones:
            lines: list[str] = []
            for i in inscripciones:
                evento = eventos_by_id.get(i.get("eventoId"))
                if evento:
                    lines.append(f"- {evento['nombre']} ({evento.get('fecha', '')})")
                else:
                    lines.append("- Evento (detalle no disponible)")
            text = "Ya estás inscripto/a en:\n\n" + "\n".join(lines)
        else:
            text = "Todavía no estás inscripto/a en ningún evento."
        await whatsapp_client.send_text(phone, text, session=session)

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
            session.end_flow()
            return

        state.available_events = {str(e["id"]): e for e in disponibles}
        state.step = EventosStep.AWAITING_CHOICE

        note = " (mostrando los primeros 10)" if len(disponibles) > 10 else ""
        rows = [
            {
                "id": f"{EVENT_PREFIX}{e['id']}",
                "title": str(e["nombre"]),
                "description": f"{e.get('fecha', '')} - ${float(str(e.get('costoSocio', 0))):.0f}",
            }
            for e in disponibles[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body=f"Elegí un evento para ver más detalles{note}:",
            button_text="Ver eventos",
            rows=rows,
            section_title="Eventos disponibles",
            session=session,
        )

    async def _handle_choice(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
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
                await svc.services_client.post_inscripcion_evento(str(evento["id"]), str(socio["id"]))
            except ServicesAPIConflict as exc:
                await whatsapp_client.send_text(phone, str(exc), session=session)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.warning(
                    "Error creating evento inscription for socio %s to evento %s",
                    socio["id"],
                    evento["id"],
                )
                traceback.print_exc()
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

    async def _show_withdrawable(
        self, phone: str, session: Session, state: EventosState
    ) -> FlowResult:
        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])

        try:
            inscripciones = await svc.services_client.get_inscripciones_evento_socio(socio_id)
            eventos = await svc.services_client.get_eventos()
        except ServicesAPIError:
            logger.warning("Error fetching evento inscriptions for socio %s", socio_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE

        eventos_by_id: dict[object, dict[str, object]] = {e["id"]: e for e in eventos}
        activas = [i for i in inscripciones if i.get("estado") != "Cancelada"]

        if not activas:
            await whatsapp_client.send_text(
                phone, "No tenés inscripciones a eventos para cancelar.", session=session
            )
            return FlowResult.DONE

        state.enrolled_events = {}
        for i in activas:
            key = str(i.get("id", ""))
            evento = eventos_by_id.get(i.get("eventoId"))
            state.enrolled_events[key] = {
                "inscripcionId": i.get("id"),
                "eventoId": i.get("eventoId"),
                "eventoNombre": evento.get("nombre", "Evento") if evento else "Evento",
                "eventoFecha": evento.get("fecha", "") if evento else "",
            }

        state.step = EventosStep.AWAITING_WITHDRAW_CHOICE

        note = " (mostrando las primeras 10)" if len(activas) > 10 else ""
        rows = [
            {
                "id": f"{WITHDRAW_PREFIX}{key}",
                "title": str(info["eventoNombre"]),
                "description": str(info.get("eventoFecha", "")),
            }
            for key, info in list(state.enrolled_events.items())[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body=f"Elegí la inscripción que querés cancelar{note}:",
            button_text="Ver inscripciones",
            rows=rows,
            section_title="Tus inscripciones",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_withdraw_choice(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(WITHDRAW_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí una inscripción de la lista.", session=session)
            return FlowResult.CONTINUE

        key = iid[len(WITHDRAW_PREFIX) :]
        enrolled = state.enrolled_events.get(key)
        if not enrolled:
            await whatsapp_client.send_text(phone, "Esa inscripción ya no está disponible.", session=session)
            return FlowResult.DONE

        state.selected_withdrawal = enrolled
        state.step = EventosStep.AWAITING_WITHDRAW_CONFIRM
        await whatsapp_client.send_buttons(
            phone,
            body=f"¿Confirmás que querés cancelar tu inscripción a *{enrolled['eventoNombre']}*?",
            buttons=[(WITHDRAW_YES, "Sí, cancelar"), (WITHDRAW_NO, "Volver")],
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_withdraw_confirm(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == WITHDRAW_YES:
            info = state.selected_withdrawal
            assert info is not None
            try:
                await svc.services_client.delete_inscripcion_evento(
                    str(info["eventoId"]), str(info["inscripcionId"])
                )
            except ServicesAPIError:
                logger.warning(
                    "Error withdrawing from evento %s inscription %s",
                    info["eventoId"],
                    info["inscripcionId"],
                )
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE

            await whatsapp_client.send_text(
                phone,
                f"Inscripción a *{info['eventoNombre']}* cancelada.",
                session=session,
            )
            return FlowResult.DONE

        if msg.interactive_id == WITHDRAW_NO:
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Sí, cancelar o Volver.", session=session)
        return FlowResult.CONTINUE
