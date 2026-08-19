import logging
import traceback
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ConflictError, ServicesAPIError
from ..state import Session
from ..webhook_parser import IncomingMessage
from ..whatsapp_client import whatsapp_client
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
FEATURE_UNAVAILABLE = "Las reservas no están disponibles en este momento. Intentá más tarde."

ACTION_VIEW = "res_view"
ACTION_NEW = "res_new"
ESPACIO_PREFIX = "esp_"
CANCEL_PREFIX = "cancel_"
CONFIRM_YES = "res_confirm_yes"
CONFIRM_NO = "res_confirm_no"
CANCEL_CONFIRM_YES = "res_cancel_yes"
CANCEL_CONFIRM_NO = "res_cancel_no"

DAY_NAMES = {
    "lunes": 0,
    "martes": 1,
    "miércoles": 2,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sábado": 5,
    "sabado": 5,
    "domingo": 6,
}


class ReservationsStep(StrEnum):
    AWAITING_ACTION = "awaiting_action"
    AWAITING_CANCEL_CHOICE = "awaiting_cancel_choice"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"
    AWAITING_DATE = "awaiting_date"
    AWAITING_ESPACIO_CHOICE = "awaiting_espacio_choice"
    AWAITING_RESERVA_CONFIRM = "awaiting_reserva_confirm"


@dataclass
class ReservationsState:
    step: ReservationsStep = ReservationsStep.AWAITING_ACTION
    reservas: dict[str, dict] = field(default_factory=dict)
    espacios: dict[str, dict] = field(default_factory=dict)
    selected_fecha: str | None = None
    selected_espacio_id: str | None = None
    selected_reserva_id: str | None = None


class ReservationsFlow(BaseFlow[ReservationsState]):
    def create_state(self) -> ReservationsState:
        return ReservationsState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        state.step = ReservationsStep.AWAITING_ACTION
        await whatsapp_client.send_buttons(
            phone,
            body="¿Qué querés hacer con tus reservas?",
            buttons=[(ACTION_VIEW, "Mis reservas"), (ACTION_NEW, "Nueva reserva")],
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == ReservationsStep.AWAITING_ACTION:
            return await self._handle_action(phone, session, state, msg)
        if state.step == ReservationsStep.AWAITING_CANCEL_CHOICE:
            return await self._handle_cancel_choice(phone, session, state, msg)
        if state.step == ReservationsStep.AWAITING_CANCEL_CONFIRM:
            return await self._handle_cancel_confirm(phone, session, state, msg)
        if state.step == ReservationsStep.AWAITING_DATE:
            return await self._handle_date(phone, session, state, msg)
        if state.step == ReservationsStep.AWAITING_ESPACIO_CHOICE:
            return await self._handle_espacio_choice(phone, session, state, msg)
        if state.step == ReservationsStep.AWAITING_RESERVA_CONFIRM:
            return await self._handle_reserva_confirm(phone, session, state, msg)

        return FlowResult.DONE

    async def _handle_action(
        self, phone: str, session: Session, state: ReservationsState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == ACTION_VIEW:
            return await self._show_reservas(phone, session, state)
        if msg.interactive_id == ACTION_NEW:
            state.step = ReservationsStep.AWAITING_DATE
            await whatsapp_client.send_text(
                phone, "Ingresá la fecha para la reserva (ej: 25/08/2026, mañana, lunes):"
            )
            return FlowResult.CONTINUE
        await whatsapp_client.send_text(phone, "Por favor, tocá una de las opciones.")
        return FlowResult.CONTINUE

    async def _show_reservas(
        self, phone: str, session: Session, state: ReservationsState
    ) -> FlowResult:
        socio = session.socio
        assert socio is not None

        try:
            reservas = await svc.services_client.get_reservas_socio(str(socio["id"]))
        except ServicesAPIError as exc:
            if "no configuradas" in str(exc):
                await whatsapp_client.send_text(phone, FEATURE_UNAVAILABLE)
            else:
                logger.warning("Error fetching reservas for socio %s", socio["id"])
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR)
            return FlowResult.DONE

        today = date.today()
        activas = [
            r for r in reservas
            if r.get("estado") == "Confirmada" and r.get("fecha", "") >= today.isoformat()
        ]

        if not activas:
            await whatsapp_client.send_text(phone, "No tenés reservas activas.")
            state.step = ReservationsStep.AWAITING_ACTION
            await whatsapp_client.send_buttons(
                phone,
                body="¿Qué querés hacer?",
                buttons=[(ACTION_VIEW, "Ver mis reservas"), (ACTION_NEW, "Nueva reserva")],
            )
            return FlowResult.CONTINUE

        lines = ["*Tus reservas:*"]
        for r in activas:
            lines.append(f"- {r.get('nombreEspacio', 'Espacio')} — {_format_date(r['fecha'])}")
        await whatsapp_client.send_text(phone, "\n".join(lines))

        min_cancel_date = (today + timedelta(days=1)).isoformat()
        cancelables = [r for r in activas if r.get("fecha", "") > min_cancel_date]

        if not cancelables:
            await whatsapp_client.send_text(
                phone, "No hay reservas que se puedan cancelar (se requiere al menos 1 día de anticipación)."
            )
            return FlowResult.DONE

        state.reservas = {str(r["id"]): r for r in cancelables}
        state.step = ReservationsStep.AWAITING_CANCEL_CHOICE

        rows = [
            {
                "id": f"{CANCEL_PREFIX}{r['id']}",
                "title": str(r.get("nombreEspacio", "Espacio"))[:24],
                "description": _format_date(r["fecha"]),
            }
            for r in cancelables[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body="Seleccioná una reserva para cancelar (o escribí 'menu' para volver):",
            button_text="Ver reservas",
            rows=rows,
            section_title="Cancelar reserva",
        )
        return FlowResult.CONTINUE

    async def _handle_cancel_choice(
        self, phone: str, session: Session, state: ReservationsState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(CANCEL_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí una reserva de la lista.")
            return FlowResult.CONTINUE

        reserva_id = iid[len(CANCEL_PREFIX):]
        reserva = state.reservas.get(reserva_id)
        if not reserva:
            await whatsapp_client.send_text(phone, "Esa reserva ya no está disponible.")
            return FlowResult.DONE

        state.selected_reserva_id = reserva_id
        state.step = ReservationsStep.AWAITING_CANCEL_CONFIRM
        await whatsapp_client.send_buttons(
            phone,
            body=(
                f"¿Cancelar la reserva de *{reserva.get('nombreEspacio', 'Espacio')}* "
                f"para el {_format_date(reserva['fecha'])}?"
            ),
            buttons=[(CANCEL_CONFIRM_YES, "Confirmar"), (CANCEL_CONFIRM_NO, "No cancelar")],
        )
        return FlowResult.CONTINUE

    async def _handle_cancel_confirm(
        self, phone: str, session: Session, state: ReservationsState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CANCEL_CONFIRM_YES:
            assert state.selected_reserva_id is not None
            try:
                await svc.services_client.delete_reserva_admin(state.selected_reserva_id)
            except ServicesAPIError as exc:
                await whatsapp_client.send_text(phone, str(exc))
                return FlowResult.DONE
            await whatsapp_client.send_text(phone, "Reserva cancelada.")
            return FlowResult.DONE

        if msg.interactive_id == CANCEL_CONFIRM_NO:
            await whatsapp_client.send_text(phone, "Cancelación descartada.")
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Confirmar o No cancelar.")
        return FlowResult.CONTINUE

    async def _handle_date(
        self, phone: str, session: Session, state: ReservationsState, msg: IncomingMessage
    ) -> FlowResult:
        if not msg.text:
            await whatsapp_client.send_text(
                phone, "Por favor, escribí una fecha (ej: 25/08/2026, mañana, lunes)."
            )
            return FlowResult.CONTINUE

        parsed = parse_date(msg.text.strip())
        if parsed is None:
            await whatsapp_client.send_text(
                phone,
                "No entendí la fecha. Probá con formato DD/MM/AAAA, o escribí 'mañana', 'lunes', etc.",
            )
            return FlowResult.CONTINUE

        if parsed <= date.today():
            await whatsapp_client.send_text(phone, "La fecha debe ser futura. Probá con otra fecha.")
            return FlowResult.CONTINUE

        state.selected_fecha = parsed.isoformat()

        try:
            disponibilidad = await svc.services_client.get_disponibilidad_espacios(state.selected_fecha)
        except ServicesAPIError as exc:
            if "no configuradas" in str(exc):
                await whatsapp_client.send_text(phone, FEATURE_UNAVAILABLE)
            else:
                logger.warning("Error fetching disponibilidad for %s", state.selected_fecha)
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR)
            return FlowResult.DONE

        disponibles = [
            e for e in disponibilidad
            if e.get("estado") == "Activo"
            and (e.get("capacidad", 0) - len(e.get("reservasConfirmadas", []))) > 0
        ]

        if not disponibles:
            await whatsapp_client.send_text(
                phone,
                f"No hay espacios disponibles para el {_format_date(state.selected_fecha)}. Probá con otra fecha.",
            )
            state.step = ReservationsStep.AWAITING_DATE
            return FlowResult.CONTINUE

        state.espacios = {str(e["id"]): e for e in disponibles}
        state.step = ReservationsStep.AWAITING_ESPACIO_CHOICE

        rows = [
            {
                "id": f"{ESPACIO_PREFIX}{e['id']}",
                "title": str(e["nombre"])[:24],
                "description": f"Disponible - ${e.get('costo', 0):.0f}",
            }
            for e in disponibles[:10]
        ]
        note = " (mostrando los primeros 10)" if len(disponibles) > 10 else ""
        await whatsapp_client.send_list(
            to=phone,
            body=f"Espacios disponibles para el {_format_date(state.selected_fecha)}{note}:",
            button_text="Ver espacios",
            rows=rows,
            section_title="Espacios",
        )
        return FlowResult.CONTINUE

    async def _handle_espacio_choice(
        self, phone: str, session: Session, state: ReservationsState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(ESPACIO_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí un espacio de la lista.")
            return FlowResult.CONTINUE

        espacio_id = iid[len(ESPACIO_PREFIX):]
        espacio = state.espacios.get(espacio_id)
        if not espacio:
            await whatsapp_client.send_text(phone, "Ese espacio ya no está disponible.")
            return FlowResult.DONE

        state.selected_espacio_id = espacio_id
        state.step = ReservationsStep.AWAITING_RESERVA_CONFIRM

        costo_text = f"\nCosto: ${espacio.get('costo', 0):.0f}" if espacio.get("costo", 0) > 0 else ""
        await whatsapp_client.send_buttons(
            phone,
            body=(
                f"¿Reservar *{espacio['nombre']}* para el "
                f"{_format_date(state.selected_fecha or '')}?{costo_text}"
            ),
            buttons=[(CONFIRM_YES, "Confirmar"), (CONFIRM_NO, "Cancelar")],
        )
        return FlowResult.CONTINUE

    async def _handle_reserva_confirm(
        self, phone: str, session: Session, state: ReservationsState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == CONFIRM_YES:
            socio = session.socio
            assert socio is not None
            assert state.selected_espacio_id is not None
            assert state.selected_fecha is not None

            try:
                await svc.services_client.post_reserva_admin(
                    str(socio["id"]), state.selected_espacio_id, state.selected_fecha
                )
            except ConflictError as exc:
                await whatsapp_client.send_text(phone, exc.detail)
                return FlowResult.DONE
            except ServicesAPIError:
                logger.warning("Error creating reserva")
                traceback.print_exc()
                await whatsapp_client.send_text(phone, GENERIC_ERROR)
                return FlowResult.DONE

            espacio = state.espacios.get(state.selected_espacio_id, {})
            await whatsapp_client.send_text(
                phone,
                f"¡Reserva confirmada! *{espacio.get('nombre', 'Espacio')}* para el "
                f"{_format_date(state.selected_fecha)}.",
            )
            return FlowResult.DONE

        if msg.interactive_id == CONFIRM_NO:
            await whatsapp_client.send_text(phone, "Reserva cancelada.")
            return FlowResult.DONE

        await whatsapp_client.send_text(phone, "Por favor, tocá Confirmar o Cancelar.")
        return FlowResult.CONTINUE


def parse_date(text: str) -> date | None:
    normalized = text.lower().strip()

    today = date.today()

    if normalized in ("hoy",):
        return today
    if normalized in ("mañana", "manana"):
        return today + timedelta(days=1)
    if normalized in ("pasado mañana", "pasado manana"):
        return today + timedelta(days=2)

    if normalized in DAY_NAMES:
        target_weekday = DAY_NAMES[normalized]
        days_ahead = (target_weekday - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    # DD/MM/YYYY
    if "/" in normalized:
        parts = normalized.split("/")
        try:
            day = int(parts[0])
            month = int(parts[1])
            year = int(parts[2]) if len(parts) > 2 else today.year
            return date(year, month, day)
        except (ValueError, IndexError):
            return None

    # YYYY-MM-DD
    if "-" in normalized and len(normalized) >= 8:
        try:
            return date.fromisoformat(normalized)
        except ValueError:
            return None

    return None


def _format_date(iso_str: str) -> str:
    if not iso_str:
        return "—"
    date_part = iso_str[:10]
    try:
        parts = date_part.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except (IndexError, ValueError):
        return date_part
