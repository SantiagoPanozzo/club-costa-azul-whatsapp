import logging
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from .. import services_client as svc
from ..flow_ui import (
    BUTTON_BODY_LIMIT,
    MENU_ID,
    adaptive_summary,
    confirmation_buttons,
    list_body_limit,
    paginate_rows,
    with_notice,
)
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
EVENT_PAGE_PREFIX = "evt_page_"
WITHDRAW_PAGE_PREFIX = "evtw_page_"


class EventosStep(StrEnum):
    AWAITING_CHOICE = "awaiting_choice"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_WITHDRAW_CHOICE = "awaiting_withdraw_choice"
    AWAITING_WITHDRAW_CONFIRM = "awaiting_withdraw_confirm"


@dataclass
class EventosState:
    step: EventosStep = EventosStep.AWAITING_CHOICE
    available_events: dict[str, dict] = field(default_factory=dict)
    selected_event: dict | None = None
    enrolled_events: dict[str, dict] = field(default_factory=dict)
    selected_withdrawal: dict | None = None
    withdrawal_event: dict | None = None
    accepted_description: str | None = None
    inscriptions: list[dict] = field(default_factory=list)
    events: dict[str, dict] = field(default_factory=dict)
    page: int = 0
    withdrawal_page: int = 0


def _price(value: object) -> str:
    # Preserve cents (and API precision) instead of rounding the member price.
    return str(value) if value is not None else "Sin definir"


def _details(event: dict) -> str:
    start = event.get("horaInicio") or "Sin definir"
    end = event.get("horaFin")
    lines = [
        f"*{event.get('nombre') or 'Evento (detalle no disponible)'}*",
        f"📅 Fecha: {event.get('fecha') or 'Sin definir'}",
        f"🕐 Horario: {start}" + (f" - {end}" if end else ""),
        f"📍 Lugar: {event.get('nombreEspacio') or 'Sin definir'}",
        f"💲 Costo socio: ${_price(event.get('costoSocio'))}",
    ]
    if event.get("costoNoSocio") is not None:
        lines.append(f"Costo no socio: ${_price(event['costoNoSocio'])}")
    if event.get("cupoDisponible") is not None:
        lines.append(f"🎟️ Cupo disponible: {event['cupoDisponible']}")
    return "\n".join(lines)


def _enrollment_event(inscription: dict, events: dict[str, dict]) -> dict:
    return events.get(str(inscription.get("eventoId"))) or {
        "nombre": inscription.get("eventoNombre")
        or inscription.get("nombreEvento")
        or f"Evento {inscription.get('eventoId', '')} (detalle no disponible)",
        "fecha": inscription.get("eventoFecha") or inscription.get("fechaEvento"),
    }


def _enrollment_details(inscription: dict, events: dict[str, dict]) -> str:
    return (
        _details(_enrollment_event(inscription, events))
        + f"\nInscripción: {inscription.get('fechaInscripcion') or 'Fecha no disponible'}"
        + f"\nEstado: {inscription.get('estado') or 'Sin información'}"
    )


def _available(events: dict[str, dict], inscriptions: list[dict]) -> dict[str, dict]:
    enrolled = {str(i.get("eventoId")) for i in inscriptions}
    today = date.today().isoformat()
    return {
        key: event
        for key, event in events.items()
        if event.get("estado") == "Programado"
        and str(event.get("fecha") or "") >= today
        and (event.get("cupoDisponible") is None or event["cupoDisponible"] > 0)
        and key not in enrolled
    }


class EventosFlow(BaseFlow[EventosState]):
    def create_state(self) -> EventosState:
        return EventosState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        session.set_task("view")
        await self._show_events(phone, session, state, refresh=True)

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)
        if state.step == EventosStep.AWAITING_CHOICE:
            return await self._handle_choice(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_CONFIRM:
            return await self._handle_confirm(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_WITHDRAW_CHOICE:
            return await self._handle_withdraw_choice(phone, session, state, msg)
        if state.step == EventosStep.AWAITING_WITHDRAW_CONFIRM:
            return await self._handle_withdraw_confirm(phone, session, state, msg)
        session.finish(GENERIC_ERROR, outcome="error")
        return FlowResult.DONE

    async def _load(self, session: Session, state: EventosState) -> bool:
        assert session.socio is not None
        try:
            inscriptions = await svc.services_client.get_inscripciones_evento_socio(str(session.socio["id"]))
            events = await svc.services_client.get_eventos()
        except ServicesAPIError:
            logger.warning("Error fetching events or inscriptions", exc_info=True)
            session.finish(GENERIC_ERROR, outcome="error")
            return False
        # The API's enrollment timestamp determines recency, not the event date.
        state.inscriptions = sorted(inscriptions, key=lambda i: str(i.get("fechaInscripcion") or ""), reverse=True)
        state.events = {str(e["id"]): e for e in events}
        state.available_events = _available(state.events, state.inscriptions)
        state.enrolled_events = {str(i["id"]): i for i in state.inscriptions}
        return True

    async def _show_events(
        self, phone: str, session: Session, state: EventosState, *, refresh: bool = False, notice: str = ""
    ) -> FlowResult:
        session.clear_confirmation()
        if notice:
            session.mark_recovery()
        if refresh and not await self._load(session, state):
            return FlowResult.DONE
        state.step = EventosStep.AWAITING_CHOICE
        state.selected_event = None
        state.accepted_description = None
        if not state.available_events and not state.inscriptions:
            session.finish(with_notice("No hay eventos disponibles para inscribirte en este momento.", notice), "empty")
            return FlowResult.DONE
        prompt = (
            "Elegí un evento para ver los detalles e inscribirte. Mis inscripciones: detalle y cancelaciones."
            if state.available_events
            else "No hay otros eventos disponibles. Consultá el detalle o cancelá desde Mis inscripciones."
        )
        body = adaptive_summary(
            "Tus inscripciones a eventos:",
            [_enrollment_details(i, state.events) for i in state.inscriptions],
            prompt,
            empty="Todavía no estás inscripto/a en ningún evento.",
            limit=list_body_limit() - (len(notice) + 2 if notice else 0),
        )
        options = [
            {
                "id": f"{EVENT_PREFIX}{key}",
                "title": str(e["nombre"]),
                "description": f"{e.get('fecha', '')} - ${_price(e.get('costoSocio'))}",
            }
            for key, e in state.available_events.items()
        ]
        rows, state.page = paginate_rows(
            options,
            state.page,
            page_prefix=EVENT_PAGE_PREFIX,
            extra_rows=[{"id": ACTION_WITHDRAW, "title": "Mis inscripciones"}],
        )
        await whatsapp_client.send_list(
            phone,
            with_notice(body, notice),
            button_text="Ver eventos",
            rows=rows,
            section_title="Eventos disponibles",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_choice(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if iid == ACTION_WITHDRAW:
            session.set_task("view")
            return await self._show_withdrawable(phone, session, state, refresh=True)
        if iid.startswith(EVENT_PAGE_PREFIX) and iid[len(EVENT_PAGE_PREFIX) :].isdigit():
            state.page = int(iid[len(EVENT_PAGE_PREFIX) :])
            return await self._show_events(phone, session, state)
        key = iid[len(EVENT_PREFIX) :] if iid.startswith(EVENT_PREFIX) else ""
        if not key:
            return await self._show_events(phone, session, state, notice="Por favor, elegí una opción de la lista.")
        offered = key in state.available_events
        if not await self._load(session, state):
            return FlowResult.DONE
        event = state.available_events.get(key)
        if not offered or event is None:
            return await self._show_events(
                phone, session, state, notice="Ese evento ya no está disponible. Te muestro la lista actualizada."
            )
        session.set_task("enroll")
        state.selected_event = event
        state.step = EventosStep.AWAITING_CONFIRM
        await self._send_confirmation(phone, session, state)
        return FlowResult.CONTINUE

    async def _send_confirmation(
        self, phone: str, session: Session, state: EventosState, *, notice: str = "", repeat: bool = False
    ) -> None:
        if notice:
            session.mark_recovery()
        event = state.selected_event
        assert event is not None
        core = _details(event) + "\n\n¿Querés inscribirte en este evento?"
        description = event.get("descripcion") or ""
        full = with_notice(_details(event) + (f"\n\n{description}" if description else ""), notice)
        full += "\n\n¿Querés inscribirte en este evento?"
        if len(full) > BUTTON_BODY_LIMIT and description:
            # Exceptional long narratives overflow losslessly; all critical details
            # remain next to the explicit consent buttons, never behind another step.
            if state.accepted_description != description:
                if not await whatsapp_client.send_text(phone, str(description), session=session):
                    session.clear_confirmation()
                    return
                state.accepted_description = description
            full = with_notice(core, notice)
        await whatsapp_client.send_buttons(
            phone, full, confirmation_buttons(CONFIRM_YES, CONFIRM_NO, "Inscribirme", session=session), session=session
        )

    async def _handle_confirm(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CONFIRM_NO:
            return await self._show_events(phone, session, state, refresh=True)
        if not session.consume_confirmation(msg.interactive_id, CONFIRM_YES):
            await self._send_confirmation(
                phone,
                session,
                state,
                notice="Por favor, tocá Inscribirme, Volver o Menú principal.",
                repeat=True,
            )
            return FlowResult.CONTINUE
        event = state.selected_event
        assert event is not None and session.socio is not None
        if not await self._load(session, state):
            return FlowResult.DONE
        current = state.available_events.get(str(event["id"]))
        if current is None:
            return await self._show_events(
                phone, session, state, notice="Ese evento ya no está disponible. Te muestro la lista actualizada."
            )
        if current != event:
            state.selected_event = current
            await self._send_confirmation(
                phone, session, state, notice="Los detalles del evento cambiaron. Revisalos antes de confirmar."
            )
            return FlowResult.CONTINUE
        try:
            await svc.services_client.post_inscripcion_evento(str(event["id"]), str(session.socio["id"]))
        except ServicesAPIConflict as exc:
            session.finish(str(exc), outcome="conflict")
        except ServicesAPIError:
            logger.warning("Error creating event inscription", exc_info=True)
            session.finish(GENERIC_ERROR, outcome="error")
        else:
            session.finish(f"¡Listo! Te inscribiste en *{event['nombre']}*.")
        return FlowResult.DONE

    async def _show_withdrawable(
        self, phone: str, session: Session, state: EventosState, *, refresh: bool = False, notice: str = ""
    ) -> FlowResult:
        session.clear_confirmation()
        if notice:
            session.mark_recovery()
        if refresh and not await self._load(session, state):
            return FlowResult.DONE
        state.step = EventosStep.AWAITING_WITHDRAW_CHOICE
        state.selected_withdrawal = None
        options = [
            {
                "id": f"{WITHDRAW_PREFIX}{key}",
                "title": str(_enrollment_event(i, state.events)["nombre"]),
                "description": (
                    f"{_enrollment_event(i, state.events).get('fecha') or 'Sin fecha'} · {i.get('estado', '')}"
                ),
            }
            for key, i in state.enrolled_events.items()
        ]
        rows, state.withdrawal_page = paginate_rows(
            options,
            state.withdrawal_page,
            page_prefix=WITHDRAW_PAGE_PREFIX,
            extra_rows=[{"id": ACTION_BROWSE, "title": "Ver eventos"}],
        )
        body = (
            "Todas tus inscripciones, incluidas las históricas y canceladas. Elegí una para ver el detalle o cancelar."
            if options
            else "No tenés inscripciones a eventos. Podés volver a Ver eventos."
        )
        await whatsapp_client.send_list(
            phone,
            with_notice(body, notice),
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
        if iid == ACTION_BROWSE:
            session.set_task("enroll")
            return await self._show_events(phone, session, state, refresh=True)
        if iid.startswith(WITHDRAW_PAGE_PREFIX) and iid[len(WITHDRAW_PAGE_PREFIX) :].isdigit():
            state.withdrawal_page = int(iid[len(WITHDRAW_PAGE_PREFIX) :])
            return await self._show_withdrawable(phone, session, state)
        key = iid[len(WITHDRAW_PREFIX) :] if iid.startswith(WITHDRAW_PREFIX) else ""
        if not key:
            return await self._show_withdrawable(
                phone, session, state, notice="Por favor, elegí una inscripción de la lista."
            )
        offered = key in state.enrolled_events
        if not await self._load(session, state):
            return FlowResult.DONE
        inscription = state.enrolled_events.get(key)
        if not offered or inscription is None:
            return await self._show_withdrawable(
                phone, session, state, notice="Esa inscripción ya no está disponible. Te muestro la lista actualizada."
            )
        state.selected_withdrawal = inscription
        state.withdrawal_event = dict(_enrollment_event(inscription, state.events))
        state.step = EventosStep.AWAITING_WITHDRAW_CONFIRM
        session.set_task("view" if inscription.get("estado") == "Cancelada" else "withdraw")
        await self._send_withdraw_confirmation(phone, session, state)
        return FlowResult.CONTINUE

    async def _send_withdraw_confirmation(
        self, phone: str, session: Session, state: EventosState, *, notice: str = ""
    ) -> None:
        if notice:
            session.mark_recovery()
        inscription = state.selected_withdrawal
        assert inscription is not None
        body = _enrollment_details(inscription, state.events)
        if inscription.get("estado") == "Cancelada":
            session.clear_confirmation()
            body += "\n\nEsta inscripción ya está cancelada."
            buttons = [(WITHDRAW_NO, "Volver"), (MENU_ID, "Menú principal")]
        else:
            body += "\n\n¿Confirmás que querés cancelar esta inscripción?"
            buttons = confirmation_buttons(WITHDRAW_YES, WITHDRAW_NO, "Sí, cancelar", session=session)
        await whatsapp_client.send_buttons(phone, with_notice(body, notice), buttons, session=session)

    async def _handle_withdraw_confirm(
        self, phone: str, session: Session, state: EventosState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == WITHDRAW_NO:
            session.set_task("view")
            return await self._show_withdrawable(phone, session, state, refresh=True)
        inscription = state.selected_withdrawal
        assert inscription is not None
        if inscription.get("estado") == "Cancelada" or not session.consume_confirmation(
            msg.interactive_id, WITHDRAW_YES
        ):
            await self._send_withdraw_confirmation(
                phone, session, state, notice="Por favor, elegí una opción de los botones."
            )
            return FlowResult.CONTINUE
        if not await self._load(session, state):
            return FlowResult.DONE
        current = state.enrolled_events.get(str(inscription["id"]))
        if (
            current is None
            or current.get("estado") == "Cancelada"
            or current.get("eventoId") != inscription.get("eventoId")
        ):
            return await self._show_withdrawable(
                phone,
                session,
                state,
                notice="Esa inscripción ya no se puede cancelar. Te muestro la lista actualizada.",
            )
        event = _enrollment_event(inscription, state.events)
        if event != state.withdrawal_event:
            state.withdrawal_event = dict(event)
            state.selected_withdrawal = current
            await self._send_withdraw_confirmation(
                phone, session, state, notice="Los detalles del evento cambiaron. Revisalos antes de cancelar."
            )
            return FlowResult.CONTINUE
        try:
            await svc.services_client.delete_inscripcion_evento(str(inscription["eventoId"]), str(inscription["id"]))
        except ServicesAPIError:
            logger.warning("Error withdrawing event inscription", exc_info=True)
            session.finish(GENERIC_ERROR, outcome="error")
        else:
            session.finish(f"Inscripción a *{event['nombre']}* cancelada.")
        return FlowResult.DONE
