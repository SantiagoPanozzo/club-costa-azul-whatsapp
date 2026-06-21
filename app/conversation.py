"""
Conversation flow for the bot.

Steps:
  (no socio yet)        -> sign-in lookup, then welcome + main menu
  MAIN_MENU             -> waiting for a menu option
  ACTIVITY_AWAITING_CHOICE -> waiting for the user to pick an activity from the list
  AWAITING_CONFIRM       -> waiting for Confirmar/Cancelar
"""
import logging
import traceback

from . import services_client as svc
from .services_client import ServicesAPIError
from .state import Session, sessions
from .webhook_parser import IncomingMessage
from .whatsapp_client import whatsapp_client

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
NOT_REGISTERED = (
    "No encontramos tu número registrado como socio del Club Costa Azul. "
    "Comunicate con administración para verificar o actualizar tu WhatsApp."
)

MENU_ACTIVIDADES = "menu_actividades"
CONFIRM_YES = "confirm_yes"
CONFIRM_NO = "confirm_no"
ACTIVITY_PREFIX = "act_"


async def handle_message(incoming: IncomingMessage) -> None:
    phone = incoming.phone
    session = sessions.get(phone)

    if session.socio is None:
        await _sign_in(phone, session)
        return

    if session.step == "MAIN_MENU":
        await _handle_main_menu(phone, session, incoming)
    elif session.step == "ACTIVITY_AWAITING_CHOICE":
        await _handle_activity_choice(phone, session, incoming)
    elif session.step == "AWAITING_CONFIRM":
        await _handle_confirm(phone, session, incoming)
    else:
        await _send_main_menu(phone, session)


async def _sign_in(phone: str, session: Session) -> None:
    try:
        socio = await svc.services_client.get_socio_by_whatsapp(phone)
    except ServicesAPIError as sae:
        logger.warning("Error looking up socio for phone %s", phone)
        traceback.print_exc()
        await whatsapp_client.send_text(phone, GENERIC_ERROR)
        return

    if socio is None:
        await whatsapp_client.send_text(phone, NOT_REGISTERED)
        return

    session.socio = socio
    nombre = socio.get("nombre", "")
    saludo = f"¡Hola, {nombre}!" if nombre else "¡Hola!"
    await whatsapp_client.send_text(
        phone,
        f"{saludo} Bienvenido/a al bot del Club Costa Azul. Te ayudo a gestionar tus actividades.",
    )
    await _send_main_menu(phone, session)


async def _send_main_menu(phone: str, session: Session) -> None:
    session.step = "MAIN_MENU"
    rows = [
        {
            "id": MENU_ACTIVIDADES,
            "title": "Actividades",
            "description": "Ver tus actividades e inscribirte a nuevas",
        },
    ]
    await whatsapp_client.send_list(
        to=phone,
        body="¿Qué querés hacer?",
        button_text="Ver opciones",
        rows=rows,
        section_title="Menú",
    )


async def _handle_main_menu(phone: str, session: Session, incoming: IncomingMessage) -> None:
    if incoming.interactive_id == MENU_ACTIVIDADES:
        await _show_activities_entry(phone, session)
    else:
        await whatsapp_client.send_text(phone, "Elegí una opción del menú, por favor. 👇")
        await _send_main_menu(phone, session)


async def _show_activities_entry(phone: str, session: Session) -> None:
    socio_id = session.socio["id"]
    try:
        inscripciones = await svc.services_client.get_inscripciones_socio(socio_id)
        actividades = await svc.services_client.get_actividades()
    except ServicesAPIError:
        logger.warning("Error fetching activities or inscriptions for socio %s", socio_id)
        traceback.print_exc()
        await whatsapp_client.send_text(phone, GENERIC_ERROR)
        return

    actividades_by_id = {a["id"]: a for a in actividades}
    activas = [i for i in inscripciones if i.get("estado") == "Activa"]

    if activas:
        lines = []
        for i in activas:
            act = actividades_by_id.get(i.get("actividadId"))
            if act:
                lines.append(f"• {act['nombre']} ({act['diasHorario']})")
            else:
                lines.append("• Actividad (detalle no disponible)")
        text = "Ya estás inscripto/a en:\n\n" + "\n".join(lines)
    else:
        text = "Todavía no estás inscripto/a en ninguna actividad."
    await whatsapp_client.send_text(phone, text)

    inscriptas_ids = {i.get("actividadId") for i in activas}
    disponibles = [
        a
        for a in actividades
        if a.get("estado") == "Activa"
        and a.get("cupoDisponible", 0) > 0
        and a["id"] not in inscriptas_ids
    ]

    if not disponibles:
        await whatsapp_client.send_text(
            phone, "No hay otras actividades disponibles para inscribirte en este momento."
        )
        await _send_main_menu(phone, session)
        return

    session.available_activities = {a["id"]: a for a in disponibles}
    session.step = "ACTIVITY_AWAITING_CHOICE"

    note = " (mostrando las primeras 10)" if len(disponibles) > 10 else ""
    rows = [
        {
            "id": f"{ACTIVITY_PREFIX}{a['id']}",
            "title": a["nombre"],
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
    )


async def _handle_activity_choice(phone: str, session: Session, incoming: IncomingMessage) -> None:
    iid = incoming.interactive_id or ""
    if not iid.startswith(ACTIVITY_PREFIX):
        await whatsapp_client.send_text(phone, "Por favor, elegí una actividad de la lista. 👇")
        return

    activity_id = iid[len(ACTIVITY_PREFIX):]
    activity = session.available_activities.get(activity_id)
    if not activity:
        await whatsapp_client.send_text(
            phone, "Esa actividad ya no está disponible. Te muestro la lista actualizada."
        )
        await _show_activities_entry(phone, session)
        return

    session.selected_activity = activity
    session.step = "AWAITING_CONFIRM"
    await whatsapp_client.send_buttons(
        phone,
        body=(
            f"¿Confirmás tu inscripción a *{activity['nombre']}*?\n"
            f"Horario: {activity['diasHorario']}\n"
            f"Costo: ${activity['costo']:.0f}"
        ),
        buttons=[(CONFIRM_YES, "Confirmar"), (CONFIRM_NO, "Cancelar")],
    )


async def _handle_confirm(phone: str, session: Session, incoming: IncomingMessage) -> None:
    if incoming.interactive_id == CONFIRM_YES:
        activity = session.selected_activity
        try:
            await svc.services_client.post_inscripcion(session.socio["id"], activity["id"])
        except ServicesAPIError:
            logger.warning("Error creating inscription for socio %s to activity %s", session.socio["id"], activity["id"])
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR)
            await _send_main_menu(phone, session)
            return
        await whatsapp_client.send_text(phone, f"¡Listo! Quedaste inscripto/a en *{activity['nombre']}*.")
        await _send_main_menu(phone, session)
    elif incoming.interactive_id == CONFIRM_NO:
        await whatsapp_client.send_text(phone, "Inscripción cancelada.")
        await _send_main_menu(phone, session)
    else:
        await whatsapp_client.send_text(phone, "Por favor, tocá Confirmar o Cancelar. 👇")
