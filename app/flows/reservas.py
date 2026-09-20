import logging
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from .. import services_client as svc
from ..date_parser import parse_flexible_date
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
CONFIRM_YES = "reserva_yes"
CONFIRM_NO = "reserva_no"
RESERVA_PREFIX = "res_"
CANCEL_YES = "cancel_confirm"
CANCEL_NO = "cancel_deny"

MAX_DAYS_AHEAD = 90
TIME_FORMATS = ["%H:%M", "%H.%M", "%H"]


def _parse_time(text: str) -> str | None:
    text = text.strip()
    for fmt in TIME_FORMATS:
        try:
            t = datetime.strptime(text, fmt).time()
            return t.strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


class ReservasStep(StrEnum):
    AWAITING_ACTION = "awaiting_action"
    AWAITING_SPACE = "awaiting_space"
    AWAITING_DATE = "awaiting_date"
    AWAITING_START_TIME = "awaiting_start_time"
    AWAITING_END_TIME = "awaiting_end_time"
    AWAITING_PEOPLE = "awaiting_people"
    AWAITING_REASON = "awaiting_reason"
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
    cant_personas: int | None = None
    motivo: str | None = None
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
        if state.step == ReservasStep.AWAITING_START_TIME:
            return await self._handle_start_time(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_END_TIME:
            return await self._handle_end_time(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_PEOPLE:
            return await self._handle_people(phone, session, state, msg)
        if state.step == ReservasStep.AWAITING_REASON:
            return await self._handle_reason(phone, session, state, msg)
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
            logger.warning("Error fetching espacios")
            traceback.print_exc()
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

        note = " (mostrando los primeros 10)" if len(disponibles) > 10 else ""
        rows = [
            {
                "id": f"{SPACE_PREFIX}{e['id']}",
                "title": str(e["nombre"]),
                "description": f"Capacidad: {e.get('capacidad', '?')} - ${float(str(e.get('costo', 0))):.0f}",
            }
            for e in disponibles[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body=f"Elegí un espacio para reservar{note}:",
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
            "Escribí la fecha de tu reserva (ej: 15/09/2026, 15/9, 15):",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_date(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text:
            await whatsapp_client.send_text(
                phone,
                "Por favor, escribí la fecha de tu reserva (ej: 15/09/2026, 15/9, 15).",
                session=session,
            )
            return FlowResult.CONTINUE

        parsed = parse_flexible_date(msg.text)
        if parsed is None:
            await whatsapp_client.send_text(
                phone,
                "No pude entender esa fecha. Intentá con algo como 15/09/2026, 15/9 o simplemente 15.",
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
        state.step = ReservasStep.AWAITING_START_TIME
        await whatsapp_client.send_text(
            phone,
            "Escribí la hora de inicio (ej: 10:00):",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_start_time(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text:
            await whatsapp_client.send_text(phone, "Escribí la hora de inicio (ej: 10:00).", session=session)
            return FlowResult.CONTINUE
        parsed = _parse_time(msg.text)
        if parsed is None:
            await whatsapp_client.send_text(
                phone, "No pude entender la hora. Usá formato HH:MM (ej: 10:00).", session=session
            )
            return FlowResult.CONTINUE
        state.start_time = parsed
        state.step = ReservasStep.AWAITING_END_TIME
        await whatsapp_client.send_text(phone, "Escribí la hora de fin (ej: 12:00):", session=session)
        return FlowResult.CONTINUE

    async def _handle_end_time(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text:
            await whatsapp_client.send_text(phone, "Escribí la hora de fin (ej: 12:00).", session=session)
            return FlowResult.CONTINUE
        parsed = _parse_time(msg.text)
        if parsed is None:
            await whatsapp_client.send_text(
                phone, "No pude entender la hora. Usá formato HH:MM (ej: 12:00).", session=session
            )
            return FlowResult.CONTINUE
        if parsed <= state.start_time:
            await whatsapp_client.send_text(
                phone, "La hora de fin debe ser posterior a la de inicio. Escribí otra hora.", session=session
            )
            return FlowResult.CONTINUE
        state.end_time = parsed
        state.step = ReservasStep.AWAITING_PEOPLE
        await whatsapp_client.send_text(phone, "¿Cuántas personas van a asistir?", session=session)
        return FlowResult.CONTINUE

    async def _handle_people(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text or not msg.text.strip().isdigit():
            await whatsapp_client.send_text(phone, "Escribí un número (ej: 4).", session=session)
            return FlowResult.CONTINUE
        cant = int(msg.text.strip())
        if cant < 1 or cant > 500:
            await whatsapp_client.send_text(phone, "Ingresá un número válido de personas.", session=session)
            return FlowResult.CONTINUE
        state.cant_personas = cant
        state.step = ReservasStep.AWAITING_REASON
        await whatsapp_client.send_text(
            phone, "¿Cuál es el motivo de la reserva? (ej: cumpleaños, reunión, entrenamiento)", session=session
        )
        return FlowResult.CONTINUE

    async def _handle_reason(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text or not msg.text.strip():
            await whatsapp_client.send_text(phone, "Escribí el motivo de la reserva.", session=session)
            return FlowResult.CONTINUE
        state.motivo = msg.text.strip()
        state.step = ReservasStep.AWAITING_CONFIRM
        espacio = state.selected_space
        assert espacio is not None
        fecha_display = datetime.strptime(state.selected_date, "%Y-%m-%d").strftime("%d/%m/%Y")
        await whatsapp_client.send_buttons(
            phone,
            body=(
                f"¿Confirmás tu reserva?\n\n"
                f"📍 Espacio: *{espacio['nombre']}*\n"
                f"📅 Fecha: {fecha_display}\n"
                f"🕐 Horario: {state.start_time} - {state.end_time}\n"
                f"👥 Personas: {state.cant_personas}\n"
                f"📝 Motivo: {state.motivo}\n"
                f"💲 Costo: ${float(str(espacio.get('costo', 0))):.0f}"
            ),
            buttons=[(CONFIRM_YES, "Confirmar"), (CONFIRM_NO, "Cancelar")],
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_confirm(
        self, phone: str, session: Session, state: ReservasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CONFIRM_YES:
            espacio = state.selected_space
            assert espacio is not None
            socio = session.socio
            assert socio is not None

            try:
                await svc.services_client.post_reserva(
                    str(socio["id"]),
                    str(espacio["id"]),
                    state.selected_date,
                    state.start_time,
                    state.end_time,
                    state.cant_personas,
                    state.motivo,
                )
            except ServicesAPIConflict as exc:
                await whatsapp_client.send_text(phone, str(exc), session=session)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.warning(
                    "Error creating reserva (socio=%s, espacio=%s, fecha=%s)",
                    socio["id"],
                    espacio["id"],
                    state.selected_date,
                )
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE

            fecha_display = datetime.strptime(state.selected_date, "%Y-%m-%d").strftime("%d/%m/%Y")
            await whatsapp_client.send_text(
                phone,
                f"¡Reserva registrada! *{espacio['nombre']}* para el {fecha_display}. "
                f"Queda pendiente de aprobación por el club.",
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
            logger.warning("Error fetching reservas for socio %s", socio["id"])
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE

        activas = [r for r in reservas if r.get("estado") != "Cancelada"]
        if not activas:
            await whatsapp_client.send_text(phone, "No tenés reservas activas.", session=session)
            return FlowResult.DONE

        state.reservas = {str(r["id"]): r for r in activas}
        state.step = ReservasStep.VIEWING_RESERVAS

        note = " (mostrando las primeras 10)" if len(activas) > 10 else ""
        rows = [
            {
                "id": f"{RESERVA_PREFIX}{r['id']}",
                "title": str(r.get("nombreEspacio", "Espacio")),
                "description": f"{r.get('fecha', '')} - {r.get('estado', '')}",
            }
            for r in activas[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body=f"Tus reservas{note}. Elegí una para ver opciones:",
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
            socio = session.socio
            assert socio is not None

            try:
                await svc.services_client.delete_reserva(str(reserva["id"]), str(socio["id"]))
            except ServicesAPIError:
                logger.warning("Error cancelling reserva %s", reserva["id"])
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
                return FlowResult.DONE

            await whatsapp_client.send_text(phone, "Reserva cancelada.", session=session)
            return FlowResult.DONE

        if msg.interactive_id == CANCEL_NO:
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Cancelar reserva o Volver.", session=session)
        return FlowResult.CONTINUE
