import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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

ACTION_NEW = "reserva_new"
ACTION_LIST = "reserva_list"
SPACE_PREFIX = "esp_"
CONFIRM_YES = "reserva_yes"
CONFIRM_NO = "reserva_no"
RESERVA_PREFIX = "res_"
CANCEL_YES = "cancel_confirm"
CANCEL_NO = "cancel_deny"

MAX_DAYS_AHEAD = 90
DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y"]
TIME_FORMAT = "%H:%M"


def _parse_date(text: str) -> date | None:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time_range(text: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in text.replace(" a ", "-").split("-", 1)]
    if len(parts) != 2:
        return None
    try:
        start = datetime.strptime(parts[0], TIME_FORMAT).time()
        end = datetime.strptime(parts[1], TIME_FORMAT).time()
    except ValueError:
        return None
    if end <= start:
        return None
    return start.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)


class ReservasStep(StrEnum):
    AWAITING_ACTION = "awaiting_action"
    AWAITING_SPACE = "awaiting_space"
    AWAITING_DATE = "awaiting_date"
    AWAITING_TIME = "awaiting_time"
    AWAITING_PEOPLE = "awaiting_people"
    AWAITING_REASON = "awaiting_reason"
    AWAITING_NOTES = "awaiting_notes"
    AWAITING_CONFIRM = "awaiting_confirm"
    VIEWING_RESERVAS = "viewing_reservas"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


@dataclass
class ReservasState:
    step: ReservasStep = ReservasStep.AWAITING_ACTION
    available_spaces: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_space: dict[str, object] | None = None
    selected_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    people_count: int | None = None
    reason: str | None = None
    notes: str | None = None
    reservas: dict[str, dict[str, object]] = field(default_factory=dict)
    selected_reserva: dict[str, object] | None = None


class ReservasFlow(BaseFlow[ReservasState]):
    def create_state(self) -> ReservasState:
        return ReservasState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        state.step = ReservasStep.AWAITING_ACTION
        await whatsapp_client.send_buttons(
            phone,
            body="¿Qué querés hacer?",
            buttons=[(ACTION_NEW, "Nueva reserva"), (ACTION_LIST, "Mis reservas")],
            session=session,
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == ReservasStep.AWAITING_ACTION:
            return await self._handle_action(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_SPACE:
            return await self._handle_space(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_DATE:
            return await self._handle_date(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_TIME:
            return await self._handle_time(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_PEOPLE:
            return await self._handle_people(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_REASON:
            return await self._handle_reason(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_NOTES:
            return await self._handle_notes(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_CONFIRM:
            return await self._handle_confirm(phone, session, state, msg)
        if state.step == ReservasStep.VIEWING_RESERVAS:
            return await self._handle_reserva_selection(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_CANCEL_CONFIRM:
            return await self._handle_cancel_confirm(phone, session, state, msg)

        return FlowResult.DONE

    async def _handle_action(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == ACTION_NEW:
            return await self._show_spaces(phone, session, state)
        if msg.interactive_id == ACTION_LIST:
            return await self._show_reservas(phone, session, state)

        await whatsapp_client.send_text(phone, "Por favor, elegí Nueva reserva o Mis reservas.", session=session)
        return FlowResult.CONTINUE

    async def _show_spaces(self, phone: str, session: Session, state: ReservasState) -> FlowResult:
        try:
            espacios = await svc.services_client.get_espacios()
        except ServicesAPIError:
            logger.exception("Error fetching spaces")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE

        disponibles = [e for e in espacios if e.get("estado") == "Disponible"]
        if not disponibles:
            await whatsapp_client.send_text(
                phone,
                "No hay espacios disponibles para reservar en este momento.",
                session=session,
            )
            return FlowResult.DONE

        state.available_spaces = {str(e["id"]): e for e in disponibles}
        state.step = ReservasStep.AWAITING_SPACE

        rows = [
            {
                "id": f"{SPACE_PREFIX}{e['id']}",
                "title": str(e["nombre"]),
                "description": f"Capacidad: {e.get('capacidad', '?')} - ${float(str(e.get('costo', 0))):.0f}",
            }
            for e in disponibles
        ]
        await send_list_pages(
            phone,
            body="Elegí un espacio para reservar:",
            button_text="Ver espacios",
            rows=rows,
            section_title="Espacios disponibles",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_space(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(SPACE_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí un espacio de la lista.", session=session)
            return FlowResult.CONTINUE

        space_id = iid[len(SPACE_PREFIX) :]
        espacio = state.available_spaces.get(space_id)
        if not espacio:
            await whatsapp_client.send_text(
                phone,
                "Ese espacio ya no está disponible. Te muestro la lista actualizada.",
                session=session,
            )
            return await self._show_spaces(phone, session, state)

        state.selected_space = espacio
        state.step = ReservasStep.AWAITING_DATE
        await whatsapp_client.send_text(
            phone,
            "Escribí la fecha para tu reserva en formato DD/MM/AAAA (ej: 15/09/2026):",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_date(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text:
            await whatsapp_client.send_text(
                phone,
                "Por favor, escribí la fecha en formato DD/MM/AAAA (ej: 15/09/2026).",
                session=session,
            )
            return FlowResult.CONTINUE

        parsed = _parse_date(msg.text)
        if parsed is None:
            await whatsapp_client.send_text(
                phone,
                "No pude entender esa fecha. Usá el formato DD/MM/AAAA (ej: 15/09/2026).",
                session=session,
            )
            return FlowResult.CONTINUE

        today = date.today()
        if parsed < today:
            await whatsapp_client.send_text(
                phone,
                "La fecha no puede ser anterior a hoy. Escribí otra fecha.",
                session=session,
            )
            return FlowResult.CONTINUE

        if parsed > today + timedelta(days=MAX_DAYS_AHEAD):
            await whatsapp_client.send_text(
                phone,
                f"Solo podés reservar con hasta {MAX_DAYS_AHEAD} días de anticipación. Escribí otra fecha.",
                session=session,
            )
            return FlowResult.CONTINUE

        state.selected_date = parsed.isoformat()
        state.step = ReservasStep.AWAITING_TIME
        await whatsapp_client.send_text(
            phone,
            "Escribí el horario de inicio y fin como HH:MM-HH:MM (ej: 18:00-20:00):",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_time(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        parsed = _parse_time_range(msg.text or "")
        if parsed is None:
            await whatsapp_client.send_text(
                phone,
                "No pude entender el horario. Escribilo como HH:MM-HH:MM y asegurate de que el fin sea posterior.",
                session=session,
            )
            return FlowResult.CONTINUE
        state.start_time, state.end_time = parsed
        state.step = ReservasStep.AWAITING_PEOPLE
        await whatsapp_client.send_text(phone, "¿Para cuántas personas es la reserva?", session=session)
        return FlowResult.CONTINUE

    async def _handle_people(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        try:
            people = int((msg.text or "").strip())
        except ValueError:
            people = 0
        espacio = state.selected_space
        assert espacio is not None
        capacity = int(espacio.get("capacidad", 0))
        if people <= 0 or people > capacity:
            await whatsapp_client.send_text(
                phone,
                f"Indicá una cantidad entre 1 y {capacity}.",
                session=session,
            )
            return FlowResult.CONTINUE
        state.people_count = people
        state.step = ReservasStep.AWAITING_REASON
        await whatsapp_client.send_text(phone, "Contanos brevemente el motivo de la reserva:", session=session)
        return FlowResult.CONTINUE

    async def _handle_reason(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        reason = (msg.text or "").strip()
        if not reason:
            await whatsapp_client.send_text(
                phone, "El motivo es obligatorio. Escribí una breve descripción.", session=session
            )
            return FlowResult.CONTINUE
        state.reason = reason
        state.step = ReservasStep.AWAITING_NOTES
        await whatsapp_client.send_text(
            phone,
            "Si querés agregar notas, escribilas ahora. Si no, respondé *omitir*.",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_notes(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        notes = (msg.text or "").strip()
        state.notes = None if notes.lower() in {"omitir", "no", "ninguna"} else notes or None
        espacio = state.selected_space
        assert espacio is not None and state.selected_date is not None
        fecha_display = datetime.strptime(state.selected_date, "%Y-%m-%d").strftime("%d/%m/%Y")
        await whatsapp_client.send_buttons(
            phone,
            body=(
                f"¿Confirmás la solicitud?\n\n"
                f"📍 Espacio: *{espacio['nombre']}*\n"
                f"📅 Fecha: {fecha_display}\n"
                f"🕐 Horario: {state.start_time}-{state.end_time}\n"
                f"👥 Personas: {state.people_count}\n"
                f"📝 Motivo: {state.reason}\n"
                f"💲 Costo: ${float(str(espacio.get('costo', 0))):.0f}\n\n"
                "La solicitud quedará pendiente de aprobación."
            ),
            buttons=[(CONFIRM_YES, "Confirmar"), (CONFIRM_NO, "Cancelar")],
            session=session,
        )
        state.step = ReservasStep.AWAITING_CONFIRM
        return FlowResult.CONTINUE

    async def _handle_confirm(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CONFIRM_YES:
            espacio = state.selected_space
            assert espacio is not None
            socio = session.socio
            assert socio is not None
            assert state.selected_date is not None
            assert state.start_time is not None
            assert state.end_time is not None
            assert state.people_count is not None
            assert state.reason is not None

            try:
                operation = "create-reservation"
                if not await sessions.was_mutation_applied(msg.wamid, operation):
                    await svc.services_client.post_reserva(
                        str(socio["id"]),
                        str(espacio["id"]),
                        state.selected_date,
                        state.start_time,
                        state.end_time,
                        state.people_count,
                        state.reason,
                        state.notes,
                    )
                    await sessions.mark_mutation_applied(msg.wamid, operation)
            except ServicesAPIConflict as exc:
                await whatsapp_client.send_text(phone, str(exc), session=session)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.exception("Error creating reservation request")
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE

            fecha_display = datetime.strptime(state.selected_date, "%Y-%m-%d").strftime("%d/%m/%Y")
            await whatsapp_client.send_text(
                phone,
                f"¡Solicitud enviada! *{espacio['nombre']}* para el {fecha_display}. "
                "Queda pendiente de aprobación por la directiva.",
                session=session,
            )
            return FlowResult.DONE

        if msg.interactive_id == CONFIRM_NO:
            await whatsapp_client.send_text(phone, "Reserva cancelada.", session=session)
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Confirmar o Cancelar.", session=session)
        return FlowResult.CONTINUE

    async def _show_reservas(self, phone: str, session: Session, state: ReservasState) -> FlowResult:
        socio = session.socio
        assert socio is not None

        try:
            reservas = await svc.services_client.get_reservas_socio(str(socio["id"]))
        except ServicesAPIError:
            logger.exception("Error fetching member reservations")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE

        activas = [r for r in reservas if r.get("estado") != "Cancelada"]
        if not activas:
            await whatsapp_client.send_text(phone, "No tenés reservas activas.", session=session)
            return FlowResult.DONE

        state.reservas = {str(r["id"]): r for r in activas}
        state.step = ReservasStep.VIEWING_RESERVAS

        rows = [
            {
                "id": f"{RESERVA_PREFIX}{r['id']}",
                "title": str(r.get("nombreEspacio", "Espacio")),
                "description": f"{r.get('fecha', '')} - {r.get('estado', '')}",
            }
            for r in activas
        ]
        await send_list_pages(
            phone,
            body="Tus reservas. Elegí una para ver opciones:",
            button_text="Ver reservas",
            rows=rows,
            section_title="Mis reservas",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_reserva_selection(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(RESERVA_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí una reserva de la lista.", session=session)
            return FlowResult.CONTINUE

        reserva_id = iid[len(RESERVA_PREFIX) :]
        reserva = state.reservas.get(reserva_id)
        if not reserva:
            await whatsapp_client.send_text(phone, "Esa reserva ya no está disponible.", session=session)
            return FlowResult.DONE

        state.selected_reserva = reserva
        estado = reserva.get("estado", "")

        await whatsapp_client.send_buttons(
            phone,
            body=(
                f"📍 *{reserva.get('nombreEspacio', 'Espacio')}*\n"
                f"📅 Fecha: {reserva.get('fecha', '')}\n"
                f"Estado: {estado}\n\n"
                "¿Querés cancelar esta reserva?"
            ),
            buttons=[(CANCEL_YES, "Cancelar reserva"), (CANCEL_NO, "Volver")],
            session=session,
        )
        state.step = ReservasStep.AWAITING_CANCEL_CONFIRM
        return FlowResult.CONTINUE

    async def _handle_cancel_confirm(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CANCEL_YES:
            reserva = state.selected_reserva
            assert reserva is not None

            try:
                socio = session.socio
                assert socio is not None
                operation = "cancel-reservation"
                if not await sessions.was_mutation_applied(msg.wamid, operation):
                    await svc.services_client.delete_reserva(str(reserva["id"]), str(socio["id"]))
                    await sessions.mark_mutation_applied(msg.wamid, operation)
            except ServicesAPIConflict as exc:
                await whatsapp_client.send_text(phone, str(exc), session=session)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.exception("Error cancelling reservation")
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE

            await whatsapp_client.send_text(phone, "Reserva cancelada.", session=session)
            return FlowResult.DONE

        if msg.interactive_id == CANCEL_NO:
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Cancelar reserva o Volver.", session=session)
        return FlowResult.CONTINUE
