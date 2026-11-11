import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from .. import services_client as svc
from ..date_parser import parse_flexible_date
from ..flow_ui import MENU_ID, confirmation_buttons, paginate_rows, with_notice
from ..formatting import format_date, format_time_range
from ..services_client import ServicesAPIConflict, ServicesAPIError
from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."

ACTION_NEW = "reserva_new"
ACTION_LIST = "reserva_list"
SPACE_PREFIX = "esp_"
SPACE_PAGE_PREFIX = "esp_page_"
CONFIRM_YES = "reserva_yes"
CONFIRM_NO = "reserva_no"
RESERVA_PREFIX = "res_"
RESERVA_PAGE_PREFIX = "res_page_"
CANCEL_YES = "cancel_confirm"
CANCEL_NO = "cancel_deny"

MAX_DAYS_AHEAD = 90
TIME_TOKEN = r"[0-9]{1,2}(?:[:.][0-9]{2})?"
RANGE_PATTERN = re.compile(
    rf"\s*(?:de\s*)?({TIME_TOKEN})(?:\s*[-–—/,]\s*|\s*(?:hasta|a)\s*|\s+)({TIME_TOKEN})\s*",
    re.IGNORECASE,
)
RANGE_PROMPT = "Escribí el horario de inicio y fin en formato de 24 horas (ej: 10-12 o 10:30 a 12:30):"


def _parse_time(text: str) -> str | None:
    text = text.strip()
    if not re.fullmatch(TIME_TOKEN, text):
        return None
    parts = re.split(r"[:.]", text)
    hour, minute = int(parts[0]), int(parts[1]) if len(parts) == 2 else 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}:00"


def _parse_time_range(text: str) -> tuple[str, str] | None:
    """Accept exactly two explicit 24-hour times; never infer AM/PM or another day."""
    match = RANGE_PATTERN.fullmatch(text)
    if match is None:
        return None
    start, end = (_parse_time(token) for token in match.groups())
    if start is None or end is None or end <= start:
        return None
    return start, end


def _reservable(space: dict) -> bool:
    # Missing reservable is allowed for older API responses, independently of estado.
    return space.get("estado") == "Disponible" and space.get("reservable") is not False


def _capacity(space: dict) -> int | None:
    value = space.get("capacidad")
    if isinstance(value, bool) or value is None:
        return None
    try:
        capacity = int(str(value))
    except ValueError:
        return None
    return capacity if capacity >= 0 else None


def _page(iid: str, prefix: str) -> int | None:
    suffix = iid.removeprefix(prefix)
    return int(suffix) if iid.startswith(prefix) and re.fullmatch(r"[0-9]{1,6}", suffix) else None


class ReservasStep(StrEnum):
    AWAITING_SPACE = "awaiting_space"
    AWAITING_DATE = "awaiting_date"
    AWAITING_TIME_RANGE = "awaiting_time_range"
    AWAITING_PEOPLE = "awaiting_people"
    AWAITING_REASON = "awaiting_reason"
    AWAITING_CONFIRM = "awaiting_confirm"
    VIEWING_RESERVAS = "viewing_reservas"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


@dataclass
class ReservasState:
    step: ReservasStep = ReservasStep.AWAITING_SPACE
    available_spaces: dict[str, dict] = field(default_factory=dict)
    space_page: int = 0
    selected_space: dict | None = None
    selected_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    cant_personas: int | None = None
    motivo: str | None = None
    reservas: dict[str, dict] = field(default_factory=dict)
    reserva_page: int = 0
    selected_reserva: dict | None = None

    def clear_selection(self) -> None:
        self.selected_space = None
        self.selected_date = None
        self.start_time = None
        self.end_time = None
        self.cant_personas = None
        self.motivo = None
        self.selected_reserva = None


class ReservasFlow(BaseFlow[ReservasState]):
    def create_state(self) -> ReservasState:
        return ReservasState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        await self._show_spaces(phone, session, state)

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)
        handlers = {
            ReservasStep.AWAITING_SPACE: self._handle_space,
            ReservasStep.AWAITING_DATE: self._handle_date,
            ReservasStep.AWAITING_TIME_RANGE: self._handle_time_range,
            ReservasStep.AWAITING_PEOPLE: self._handle_people,
            ReservasStep.AWAITING_REASON: self._handle_reason,
            ReservasStep.AWAITING_CONFIRM: self._handle_confirm,
            ReservasStep.VIEWING_RESERVAS: self._handle_reserva_selection,
            ReservasStep.AWAITING_CANCEL_CONFIRM: self._handle_cancel_confirm,
        }
        return await handlers[state.step](phone, session, state, msg)

    async def _load_spaces(self, session: Session, state: ReservasState) -> bool:
        try:
            spaces = await svc.services_client.get_espacios()
        except ServicesAPIError:
            logger.exception("Error fetching espacios")
            session.finish(GENERIC_ERROR, outcome="error")
            return False
        state.available_spaces = {str(e["id"]): e for e in spaces if _reservable(e)}
        return True

    async def _show_spaces(self, phone: str, session: Session, state: ReservasState, notice: str = "") -> FlowResult:
        session.set_task("create")
        state.clear_selection()
        state.space_page = 0
        if not await self._load_spaces(session, state):
            return FlowResult.DONE
        return await self._render_spaces(phone, session, state, notice)

    async def _render_spaces(self, phone: str, session: Session, state: ReservasState, notice: str = "") -> FlowResult:
        session.clear_confirmation()
        if notice:
            session.mark_recovery()
        state.step = ReservasStep.AWAITING_SPACE
        rows, state.space_page = paginate_rows(
            [
                {
                    "id": f"{SPACE_PREFIX}{e['id']}",
                    "title": str(e["nombre"]),
                    "description": f"Capacidad: {e.get('capacidad', '?')} - ${e.get('costo', 0)}",
                }
                for e in state.available_spaces.values()
            ],
            state.space_page,
            page_prefix=SPACE_PAGE_PREFIX,
            extra_rows=[{"id": ACTION_LIST, "title": "Mis reservas"}, {"id": MENU_ID, "title": "Menú principal"}],
        )
        await whatsapp_client.send_list(
            to=phone,
            body=with_notice(
                "Elegí un espacio para reservar:"
                if state.available_spaces
                else "No hay espacios disponibles para nuevas reservas. Podés consultar Mis reservas.",
                notice,
            ),
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
        if iid == ACTION_LIST:
            return await self._show_reservas(phone, session, state)
        if iid == ACTION_NEW:
            return await self._show_spaces(phone, session, state)
        page = _page(iid, SPACE_PAGE_PREFIX)
        if page is not None:
            state.space_page = page
            return await self._render_spaces(phone, session, state)
        if not iid.startswith(SPACE_PREFIX) or iid.startswith(SPACE_PAGE_PREFIX):
            return await self._render_spaces(phone, session, state, "Por favor, elegí un espacio de la lista.")
        space_id = iid[len(SPACE_PREFIX) :]
        was_listed = space_id in state.available_spaces
        if not await self._load_spaces(session, state):
            return FlowResult.DONE
        space = state.available_spaces.get(space_id)
        if not was_listed or space is None:
            state.clear_selection()
            return await self._render_spaces(
                phone, session, state, "Ese espacio ya no está disponible. Te muestro la lista actualizada."
            )
        state.clear_selection()
        state.selected_space = space
        state.step = ReservasStep.AWAITING_DATE
        await whatsapp_client.send_text(
            phone, "Escribí la fecha de tu reserva (ej: 15/09/2026, 15/9, 15):", session=session
        )
        return FlowResult.CONTINUE

    async def _handle_date(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        parsed = parse_flexible_date(msg.text) if msg.text else None
        today = date.today()
        if parsed is None:
            error = "No pude entender esa fecha. Intentá con algo como 15/09/2026, 15/9 o simplemente 15."
        elif parsed < today:
            error = "La fecha no puede ser anterior a hoy. Escribí otra fecha."
        elif parsed > today + timedelta(days=MAX_DAYS_AHEAD):
            error = f"Solo podés reservar con hasta {MAX_DAYS_AHEAD} días de anticipación. Escribí otra fecha."
        else:
            state.selected_date = parsed.isoformat()
            state.start_time = state.end_time = None
            state.cant_personas = state.motivo = None
            state.step = ReservasStep.AWAITING_TIME_RANGE
            await whatsapp_client.send_text(phone, RANGE_PROMPT, session=session)
            return FlowResult.CONTINUE
        session.mark_recovery()
        await whatsapp_client.send_text(phone, error, session=session)
        return FlowResult.CONTINUE

    async def _handle_time_range(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        parsed = _parse_time_range(msg.text or "")
        if parsed is None:
            session.mark_recovery()
            await whatsapp_client.send_text(
                phone,
                with_notice(RANGE_PROMPT, "Indicá exactamente dos horas válidas, con el fin posterior al inicio."),
                session=session,
            )
            return FlowResult.CONTINUE
        state.start_time, state.end_time = parsed
        state.cant_personas = state.motivo = None
        state.step = ReservasStep.AWAITING_PEOPLE
        await whatsapp_client.send_text(phone, self._people_prompt(state), session=session)
        return FlowResult.CONTINUE

    @staticmethod
    def _people_prompt(state: ReservasState) -> str:
        capacity = _capacity(state.selected_space or {})
        detail = f" (capacidad: {capacity})" if capacity is not None else ""
        return f"¿Cuántas personas van a asistir{detail}?"

    async def _handle_people(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        text = (msg.text or "").strip()
        capacity = _capacity(state.selected_space or {})
        maximum = min(500, capacity) if capacity is not None else 500
        if not re.fullmatch(r"[0-9]{1,6}", text) or not 1 <= int(text) <= maximum:
            session.mark_recovery()
            await whatsapp_client.send_text(
                phone,
                with_notice(
                    self._people_prompt(state), "Ingresá un número válido de personas, sin superar la capacidad."
                ),
                session=session,
            )
            return FlowResult.CONTINUE
        state.cant_personas = int(text)
        state.motivo = None
        state.step = ReservasStep.AWAITING_REASON
        await whatsapp_client.send_text(
            phone, "¿Cuál es el motivo de la reserva? (ej: cumpleaños, reunión, entrenamiento)", session=session
        )
        return FlowResult.CONTINUE

    async def _handle_reason(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text or not msg.text.strip():
            session.mark_recovery()
            await whatsapp_client.send_text(phone, "Escribí el motivo de la reserva.", session=session)
            return FlowResult.CONTINUE
        state.motivo = msg.text.strip()
        return await self._show_confirm(phone, session, state)

    async def _show_confirm(self, phone: str, session: Session, state: ReservasState, notice: str = "") -> FlowResult:
        if notice:
            session.mark_recovery()
        space = state.selected_space
        assert space is not None and state.selected_date is not None
        state.step = ReservasStep.AWAITING_CONFIRM
        body = (
            "¿Confirmás tu reserva?\n\n"
            f"📍 Espacio: *{space['nombre']}*\n"
            f"📅 Fecha: {format_date(state.selected_date)}\n"
            f"🕐 Horario: {format_time_range(state.start_time, state.end_time)}\n"
            f"👥 Personas: {state.cant_personas} (capacidad: {space.get('capacidad', '?')})\n"
            f"📝 Motivo: {state.motivo}\n"
            f"💲 Costo: ${space.get('costo', 0)}"
        )
        await whatsapp_client.send_buttons(
            phone,
            body=with_notice(body, notice),
            buttons=confirmation_buttons(CONFIRM_YES, CONFIRM_NO, session=session),
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_confirm(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CONFIRM_NO:
            return await self._show_spaces(phone, session, state)
        if not session.consume_confirmation(msg.interactive_id, CONFIRM_YES):
            return await self._show_confirm(
                phone, session, state, "Por favor, elegí Confirmar, Volver o Menú principal."
            )
        space = state.selected_space
        socio = session.socio
        assert space is not None and socio is not None and state.selected_date is not None
        if not await self._load_spaces(session, state):
            return FlowResult.DONE
        fresh = state.available_spaces.get(str(space["id"]))
        if fresh is None:
            return await self._show_spaces(phone, session, state, "Ese espacio ya no está disponible para reservar.")
        capacity = _capacity(fresh)
        if capacity is not None and state.cant_personas > capacity:
            state.selected_space = fresh
            state.cant_personas = state.motivo = None
            state.step = ReservasStep.AWAITING_PEOPLE
            await whatsapp_client.send_text(
                phone, with_notice(self._people_prompt(state), "La capacidad del espacio cambió."), session=session
            )
            return FlowResult.CONTINUE
        today = date.today()
        if not today <= date.fromisoformat(state.selected_date) <= today + timedelta(days=MAX_DAYS_AHEAD):
            state.selected_date = state.start_time = state.end_time = None
            state.cant_personas = state.motivo = None
            state.step = ReservasStep.AWAITING_DATE
            await whatsapp_client.send_text(phone, "La fecha ya no es válida. Escribí otra fecha.", session=session)
            return FlowResult.CONTINUE
        state.selected_space = fresh
        if any(fresh.get(key) != space.get(key) for key in ("nombre", "costo", "capacidad")):
            return await self._show_confirm(
                phone, session, state, "Los datos del espacio cambiaron. Revisalos antes de confirmar."
            )
        try:
            await svc.services_client.post_reserva(
                str(socio["id"]),
                str(fresh["id"]),
                state.selected_date,
                state.start_time,
                state.end_time,
                state.cant_personas,
                state.motivo,
            )
        except ServicesAPIConflict as exc:
            session.finish(str(exc), outcome="conflict")
            return FlowResult.DONE
        except ServicesAPIError:
            logger.exception("Error creating reserva")
            session.finish(GENERIC_ERROR, outcome="error")
            return FlowResult.DONE
        session.finish(
            f"¡Reserva registrada! *{fresh['nombre']}* para el {format_date(state.selected_date)}. "
            "Queda pendiente de aprobación por el club."
        )
        return FlowResult.DONE

    async def _load_reservas(self, session: Session, state: ReservasState) -> bool:
        assert session.socio is not None
        try:
            reservas = await svc.services_client.get_reservas_socio(str(session.socio["id"]))
        except ServicesAPIError:
            logger.exception("Error fetching reservas")
            session.finish(GENERIC_ERROR, outcome="error")
            return False
        state.reservas = {str(r["id"]): r for r in reservas if r.get("estado") != "Cancelada"}
        return True

    async def _show_reservas(self, phone: str, session: Session, state: ReservasState, notice: str = "") -> FlowResult:
        session.set_task("view")
        state.clear_selection()
        if not await self._load_reservas(session, state):
            return FlowResult.DONE
        return await self._render_reservas(phone, session, state, notice)

    async def _render_reservas(
        self, phone: str, session: Session, state: ReservasState, notice: str = ""
    ) -> FlowResult:
        session.clear_confirmation()
        if notice:
            session.mark_recovery()
        if not state.reservas:
            session.finish(with_notice("No tenés reservas activas.", notice))
            return FlowResult.DONE
        state.step = ReservasStep.VIEWING_RESERVAS
        rows, state.reserva_page = paginate_rows(
            [
                {
                    "id": f"{RESERVA_PREFIX}{r['id']}",
                    "title": str(r.get("nombreEspacio", "Espacio")),
                    "description": f"{format_date(r.get('fecha'))} {self._reservation_time(r)} - {r.get('estado', '')}",
                }
                for r in state.reservas.values()
            ],
            state.reserva_page,
            page_prefix=RESERVA_PAGE_PREFIX,
            extra_rows=[{"id": ACTION_NEW, "title": "Volver a espacios"}, {"id": MENU_ID, "title": "Menú principal"}],
        )
        await whatsapp_client.send_list(
            to=phone,
            body=with_notice("Tus reservas. Elegí una para ver opciones:", notice),
            button_text="Ver reservas",
            rows=rows,
            section_title="Mis reservas",
            session=session,
        )
        return FlowResult.CONTINUE

    @staticmethod
    def _reservation_time(reserva: dict) -> str:
        return format_time_range(reserva.get("horaInicio"), reserva.get("horaFin"))

    async def _handle_reserva_selection(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if iid == ACTION_NEW:
            return await self._show_spaces(phone, session, state)
        page = _page(iid, RESERVA_PAGE_PREFIX)
        if page is not None:
            state.reserva_page = page
            return await self._render_reservas(phone, session, state)
        if not iid.startswith(RESERVA_PREFIX) or iid.startswith(RESERVA_PAGE_PREFIX):
            return await self._render_reservas(phone, session, state, "Por favor, elegí una reserva de la lista.")
        reserva_id = iid[len(RESERVA_PREFIX) :]
        was_listed = reserva_id in state.reservas
        if not await self._load_reservas(session, state):
            return FlowResult.DONE
        reserva = state.reservas.get(reserva_id)
        if not was_listed or reserva is None:
            state.selected_reserva = None
            return await self._render_reservas(
                phone, session, state, "Esa reserva ya no está disponible. Lista actualizada."
            )
        session.set_task("cancel")
        state.selected_reserva = reserva
        return await self._show_cancel_confirm(phone, session, state)

    async def _show_cancel_confirm(
        self, phone: str, session: Session, state: ReservasState, notice: str = ""
    ) -> FlowResult:
        if notice:
            session.mark_recovery()
        reserva = state.selected_reserva
        assert reserva is not None
        state.step = ReservasStep.AWAITING_CANCEL_CONFIRM
        body = (
            f"📍 *{reserva.get('nombreEspacio', 'Espacio')}*\n"
            f"📅 Fecha: {format_date(reserva.get('fecha'))}\n"
            f"🕐 Horario: {self._reservation_time(reserva)}\n"
            f"Estado: {reserva.get('estado', '')}\n\n"
            "¿Querés cancelar esta reserva?"
        )
        await whatsapp_client.send_buttons(
            phone,
            body=with_notice(body, notice),
            buttons=confirmation_buttons(CANCEL_YES, CANCEL_NO, label="Cancelar reserva", session=session),
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_cancel_confirm(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CANCEL_NO:
            return await self._show_reservas(phone, session, state)
        if not session.consume_confirmation(msg.interactive_id, CANCEL_YES):
            return await self._show_cancel_confirm(
                phone, session, state, "Por favor, elegí Cancelar reserva, Volver o Menú principal."
            )
        reserva = state.selected_reserva
        socio = session.socio
        assert reserva is not None and socio is not None
        if not await self._load_reservas(session, state):
            return FlowResult.DONE
        fresh = state.reservas.get(str(reserva["id"]))
        if fresh is None:
            return await self._show_reservas(
                phone, session, state, "Esa reserva ya no está disponible. Lista actualizada."
            )
        state.selected_reserva = fresh
        if any(
            fresh.get(key) != reserva.get(key) for key in ("nombreEspacio", "fecha", "horaInicio", "horaFin", "estado")
        ):
            return await self._show_cancel_confirm(
                phone, session, state, "La reserva cambió. Revisá los datos antes de cancelar."
            )
        try:
            await svc.services_client.delete_reserva(str(fresh["id"]), str(socio["id"]))
        except ServicesAPIError:
            logger.exception("Error cancelling reserva %s", fresh["id"])
            session.finish(GENERIC_ERROR, outcome="error")
            return FlowResult.DONE
        session.finish("Reserva cancelada.")
        return FlowResult.DONE
