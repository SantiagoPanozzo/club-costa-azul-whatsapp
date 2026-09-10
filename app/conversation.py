"""
Conversation router.

Handles sign-in, global keywords (middleware), main menu rendering,
and dispatches to the active flow.
"""

import logging
import traceback

from . import message_store
from . import services_client as svc
from .flows import FLOW_REGISTRY, FlowResult
from .services_client import ServicesAPIError
from .state import Session, sessions
from .storing_client import storing_client as whatsapp_client
from .webhook_parser import IncomingMessage

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
NOT_REGISTERED = (
    "No encontramos tu número registrado como socio del Club Costa Azul. "
    "Comunicate con administración para verificar o actualizar tu WhatsApp."
)

MENU_OPTIONS: list[dict[str, str]] = [
    {
        "id": "activities",
        "title": "Actividades",
        "description": "Ver tus actividades e inscribirte",
    },
    {
        "id": "cuotas",
        "title": "Cuotas y pagos",
        "description": "Ver estado de tus cuotas",
    },
    {
        "id": "reservas",
        "title": "Reservas",
        "description": "Reservar espacios del club",
    },
    {
        "id": "eventos",
        "title": "Eventos",
        "description": "Ver eventos e inscribirte",
    },
    {
        "id": "datos_personales",
        "title": "Datos personales",
        "description": "Consultar tus datos",
    },
    {
        "id": "comunicados",
        "title": "Comunicados",
        "description": "Novedades y comunicados",
    },
    {
        "id": "ayuda",
        "title": "Ayuda",
        "description": "Contactar al club o pedir ayuda",
    },
]

GLOBAL_KEYWORDS: dict[str, str] = {
    "menu": "menu",
    "menú": "menu",
    "inicio": "menu",
    "volver": "menu",
    "ayuda": "ayuda",
    "help": "ayuda",
}

_TEXT_TO_FLOW: dict[str, str] = {
    "1": "activities",
    "2": "cuotas",
    "3": "reservas",
    "4": "eventos",
    "5": "datos_personales",
    "6": "comunicados",
    "7": "ayuda",
    "actividades": "activities",
    "cuotas": "cuotas",
    "pagos": "cuotas",
    "reservas": "reservas",
    "eventos": "eventos",
    "datos": "datos_personales",
    "datos personales": "datos_personales",
    "comunicados": "comunicados",
    "ayuda": "ayuda",
    "help": "ayuda",
}

HELP_TEXT = (
    "Si necesitás ayuda o tenés algún problema, podés comunicarte con "
    "la administración del Club Costa Azul:\n\n"
    "📞 Teléfono: (consultar en recepción)\n"
    "📧 Email: clubcostaazul@gmail.com\n"
    "🌐 Web: https://clubcostaazul.com\n\n"
    "Escribí *menu* para volver al menú principal."
)


async def handle_message(incoming: IncomingMessage) -> None:
    phone = incoming.phone

    async with sessions.lock_for(phone):
        session = sessions.get(phone)

        is_new = await message_store.store_incoming(incoming, session)
        if not is_new:
            return

        if session.socio is None:
            await _sign_in(phone, session, incoming)
            return

        global_action = _handle_global_keyword(incoming, session)
        if global_action == "menu":
            await _send_main_menu(phone, session)
            return
        if global_action == "ayuda":
            session.end_flow()
            await whatsapp_client.send_text(phone, HELP_TEXT, session=session)
            return

        if session.active_flow is not None:
            await _dispatch_to_flow(phone, session, incoming)
            return

        if incoming.interactive_id and incoming.interactive_id == "ayuda":
            await whatsapp_client.send_text(phone, HELP_TEXT, session=session)
            return

        if incoming.interactive_id and incoming.interactive_id in FLOW_REGISTRY:
            await _enter_flow(phone, session, incoming.interactive_id)
            return

        text_flow = _match_text_to_flow(incoming)
        if text_flow == "ayuda":
            await whatsapp_client.send_text(phone, HELP_TEXT, session=session)
            return
        if text_flow:
            await _enter_flow(phone, session, text_flow)
            return

        await _send_main_menu(phone, session)


def _handle_global_keyword(incoming: IncomingMessage, session: Session) -> str | None:
    """Return the action name if a global keyword was matched, else None."""
    if incoming.text is None:
        return None
    keyword = incoming.text.strip().lower()
    action = GLOBAL_KEYWORDS.get(keyword)
    if action:
        session.end_flow()
        return action
    return None


def _match_text_to_flow(incoming: IncomingMessage) -> str | None:
    """Match typed text (number or name) to a flow when no flow is active."""
    if incoming.text is None:
        return None
    return _TEXT_TO_FLOW.get(incoming.text.strip().lower())


async def _sign_in(phone: str, session: Session, incoming: IncomingMessage) -> None:
    try:
        socio = await svc.services_client.get_socio_by_whatsapp(phone)
    except ServicesAPIError:
        logger.warning("Error looking up socio for phone %s", phone)
        traceback.print_exc()
        await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
        return

    if socio is None:
        await whatsapp_client.send_text(phone, NOT_REGISTERED, session=session)
        return

    session.socio = socio
    nombre = socio.get("nombre", "")
    saludo = f"¡Hola, {nombre}!" if nombre else "¡Hola!"
    await whatsapp_client.send_text(
        phone,
        f"{saludo} Bienvenido/a al bot del Club Costa Azul. Te ayudo a gestionar tus actividades.",
        session=session,
    )
    await _send_main_menu(phone, session)


async def _send_main_menu(phone: str, session: Session) -> None:
    session.end_flow()
    await whatsapp_client.send_list(
        to=phone,
        body="¿Qué querés hacer?",
        button_text="Ver opciones",
        rows=MENU_OPTIONS,
        section_title="Menú",
        session=session,
    )


async def _enter_flow(phone: str, session: Session, flow_name: str) -> None:
    flow = FLOW_REGISTRY[flow_name]
    session.active_flow = flow_name
    await flow.enter(phone, session)
    if session.active_flow is None:
        await _send_main_menu(phone, session)


async def _dispatch_to_flow(phone: str, session: Session, incoming: IncomingMessage) -> None:
    flow = FLOW_REGISTRY.get(session.active_flow)  # type: ignore[arg-type]
    if flow is None:
        logger.warning("Unknown active flow %r for phone %s, resetting", session.active_flow, phone)
        await _send_main_menu(phone, session)
        return

    result = await flow.handle(phone, session, incoming)
    if result == FlowResult.DONE:
        await _send_main_menu(phone, session)
