"""
Conversation router.

Handles sign-in, global keywords (middleware), main menu rendering,
and dispatches to the active flow.
"""

import logging
import time

from . import message_store
from . import services_client as svc
from .config import settings
from .flows import FLOW_REGISTRY, FlowResult
from .privacy import log_reference
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
        "id": "help",
        "title": "Ayuda y contacto",
        "description": "Hablar con administración",
    },
]

GLOBAL_KEYWORDS: dict[str, str] = {
    "menu": "menu",
    "menú": "menu",
    "inicio": "menu",
    "volver": "menu",
    "ayuda": "help",
    "humano": "help",
    "contacto": "help",
}

MENU_TEXT_ALIASES: dict[str, str] = {
    "1": "activities",
    "actividades": "activities",
    "2": "cuotas",
    "cuotas": "cuotas",
    "pagos": "cuotas",
    "3": "reservas",
    "reservas": "reservas",
    "4": "eventos",
    "eventos": "eventos",
    "5": "datos_personales",
    "datos": "datos_personales",
    "datos personales": "datos_personales",
    "6": "comunicados",
    "comunicados": "comunicados",
    "novedades": "comunicados",
    "7": "help",
}


async def handle_message(incoming: IncomingMessage) -> None:
    phone = incoming.phone
    async with sessions.locked(phone) as session:
        if not await sessions.claim_message(incoming.wamid):
            logger.info("Skipping already processed WhatsApp message_ref=%s", log_reference(incoming.wamid))
            return
        await _handle_locked(incoming, session)
        await sessions.mark_message_processed(incoming.wamid)


async def _handle_locked(incoming: IncomingMessage, session: Session) -> None:
    phone = incoming.phone
    await message_store.store_incoming(incoming, session)

    if session.socio is None:
        if await _refresh_member(phone, session, greet=True):
            # The first message starts the authenticated session; showing the
            # menu is less surprising than treating an old button as current.
            await _send_main_menu(phone, session)
        return

    if time.time() - session.member_verified_at >= settings.member_revalidate_seconds:
        if not await _refresh_member(phone, session, greet=False):
            return

    global_action = _global_action(incoming)
    if global_action == "menu":
        session.end_flow()
        await _send_main_menu(phone, session)
        return
    if global_action == "help":
        session.end_flow()
        await _send_help(phone, session)
        await _send_main_menu(phone, session)
        return

    if session.active_flow is not None:
        await _dispatch_to_flow(phone, session, incoming)
        return

    menu_choice = incoming.interactive_id
    if menu_choice is None and incoming.text:
        menu_choice = MENU_TEXT_ALIASES.get(incoming.text.strip().lower())
    if menu_choice == "help":
        await _send_help(phone, session)
        await _send_main_menu(phone, session)
        return
    if menu_choice in FLOW_REGISTRY:
        await _enter_flow(phone, session, menu_choice)
        return

    await _send_main_menu(phone, session)


def _global_action(incoming: IncomingMessage) -> str | None:
    if incoming.text is None:
        return None
    keyword = incoming.text.strip().lower()
    return GLOBAL_KEYWORDS.get(keyword)


async def _send_help(phone: str, session: Session) -> None:
    await whatsapp_client.send_text(phone, settings.club_contact_text, session=session)


async def _refresh_member(phone: str, session: Session, *, greet: bool) -> bool:
    try:
        socio = await svc.services_client.get_socio_by_whatsapp(phone)
    except ServicesAPIError:
        logger.exception("Error looking up socio for member_ref=%s", log_reference(phone))
        await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
        return False

    if socio is None:
        session.socio = None
        session.member_verified_at = 0
        session.end_flow()
        await whatsapp_client.send_text(phone, NOT_REGISTERED, session=session)
        return False

    session.socio = socio
    session.member_verified_at = time.time()
    if greet:
        nombre = socio.get("nombre", "")
        saludo = f"¡Hola, {nombre}!" if nombre else "¡Hola!"
        await whatsapp_client.send_text(
            phone,
            f"{saludo} Bienvenido/a al bot del Club Costa Azul.",
            session=session,
        )
    return True


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
        logger.warning(
            "Unknown active flow %r for member_ref=%s, resetting",
            session.active_flow,
            log_reference(phone),
        )
        await _send_main_menu(phone, session)
        return

    result = await flow.handle(phone, session, incoming)
    if result == FlowResult.DONE:
        await _send_main_menu(phone, session)
