import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from .. import services_client as svc
from ..services_client import (
    ServicesAPIBadRequest,
    ServicesAPIConflict,
    ServicesAPICupoAgotado,
    ServicesAPIError,
    ServicesAPINoEncontrado,
)
from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
SESION_VENCIDA = "La selección venció. Volvé a Actividades para consultar los datos actuales."
NO_DISPONIBLE = "Esa clase o inscripción ya no está disponible. Revisá Mis inscripciones."
SIN_CUPO = "La clase ya no tiene cupo disponible. Elegí otra clase."
_ZONA_CLUB = ZoneInfo("America/Montevideo")
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_DIA_LABELS = {0: "Dom", 1: "Lun", 2: "Mar", 3: "Mié", 4: "Jue", 5: "Vie", 6: "Sáb"}

ACTIVITY_PREFIX = "act_"
INSTANCIA_PREFIX = "clase_"
CONFIRM_YES = "confirm_yes"
CONFIRM_NO = "confirm_no"
ACTION_ENROLL = "act_enroll"
ACTION_CANCEL = "act_cancel"
CANCEL_PREFIX = "cancel_"
CANCEL_CONFIRM_YES = "cancel_act_yes"
CANCEL_CONFIRM_NO = "cancel_act_no"
PAGINA_PREFIX = "act_pagina_"


def _format_horario(actividad: dict) -> str:
    dias = ", ".join(_DIA_LABELS.get(d, str(d)) for d in actividad.get("diasSemana") or [])
    inicio = str(actividad.get("horaInicio") or "")[:5]
    fin = str(actividad.get("horaFin") or "")[:5]
    horario = f"{inicio}-{fin}" if inicio and fin else inicio
    return " ".join(p for p in (dias, horario) if p) or "Horario no definido"


def _inicio_clase(clase: dict) -> datetime | None:
    try:
        return datetime.combine(
            datetime.fromisoformat(str(clase["fecha"])).date(),
            time.fromisoformat(str(clase["horaInicio"])),
            tzinfo=_ZONA_CLUB,
        )
    except (KeyError, ValueError, TypeError):
        return None


def _es_futura(clase: dict) -> bool:
    inicio = _inicio_clase(clase)
    return inicio is not None and inicio > datetime.now(_ZONA_CLUB)


def _formato_clase(clase: dict) -> str:
    inicio = _inicio_clase(clase)
    if inicio is None:
        return "Clase sin fecha disponible"
    fin = str(clase.get("horaFin") or "")[:5]
    return f"{_DIAS[inicio.weekday()]} {inicio:%d/%m/%Y} {inicio:%H:%M}-{fin}"


def _recortar(texto: str, limite: int) -> str:
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def _clases_inscripcion(inscripcion: dict) -> list[dict]:
    if inscripcion.get("clases") is not None:
        return inscripcion["clases"]
    # Tolera respuestas que solo tienen la clase en el nivel superior.
    return [inscripcion] if inscripcion.get("instanciaId") else []


def _vigente(inscripcion: dict) -> bool:
    if inscripcion.get("estado") != "Activa":
        return False
    if inscripcion.get("instanciaId"):
        return any(_es_futura(clase) for clase in _clases_inscripcion(inscripcion))
    fin = inscripcion.get("fechaFin")
    return not fin or str(fin) >= datetime.now(_ZONA_CLUB).date().isoformat()


def _opciones_inscripciones(inscripciones: list[dict], actividades: dict | None = None) -> dict[str, dict]:
    """Una opción por clase y otra para la genérica, sin duplicar clases superpuestas."""
    resultado: dict[str, dict] = {}
    for inscripcion in sorted(inscripciones, key=lambda i: bool(i.get("instanciaId"))):
        if not _vigente(inscripcion):
            continue
        actividad = (actividades or {}).get(inscripcion.get("actividadId"), {})
        nombre = inscripcion.get("nombreActividad") or actividad.get("nombre") or "Actividad"
        if not inscripcion.get("instanciaId"):
            resultado[f"general_{inscripcion['id']}"] = {
                **inscripcion,
                "nombreActividad": nombre,
                "instanciaId": None,
            }
        for clase in _clases_inscripcion(inscripcion):
            if not _es_futura(clase):
                continue
            resultado[f"clase_{clase['instanciaId']}"] = {
                **inscripcion,
                **clase,
                "nombreActividad": nombre,
            }
    return resultado


class ActivitiesStep(StrEnum):
    AWAITING_ACTION = "awaiting_action"
    AWAITING_CHOICE = "awaiting_choice"
    ELEGIR_INSTANCIA = "elegir_instancia"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_CANCEL_CHOICE = "awaiting_cancel_choice"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


@dataclass
class ActivitiesState:
    step: ActivitiesStep = ActivitiesStep.AWAITING_ACTION
    available_activities: dict[str, dict] = field(default_factory=dict)
    selected_activity: dict | None = None
    active_inscriptions: dict[str, dict] = field(default_factory=dict)
    selected_inscription: dict | None = None
    instancias_disponibles: dict[str, dict] = field(default_factory=dict)
    instancia_elegida: dict | None = None
    paginas: dict[str, int] = field(default_factory=dict)


class ActivitiesFlow(BaseFlow[ActivitiesState]):
    def create_state(self) -> ActivitiesState:
        return ActivitiesState()

    async def enter(self, phone: str, session: Session) -> None:
        session.flow_state = self.create_state()
        await whatsapp_client.send_buttons(
            phone,
            body="¿Qué querés hacer?",
            buttons=[(ACTION_ENROLL, "Inscribirme"), (ACTION_CANCEL, "Mis inscripciones")],
            session=session,
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        if not isinstance(session.flow_state, ActivitiesState) or not session.socio:
            await whatsapp_client.send_text(phone, SESION_VENCIDA, session=session)
            return FlowResult.DONE
        estado = self.get_state(session)
        pasos = {
            ActivitiesStep.AWAITING_ACTION: self._handle_action,
            ActivitiesStep.AWAITING_CHOICE: self._handle_choice,
            ActivitiesStep.ELEGIR_INSTANCIA: self._elegir_instancia,
            ActivitiesStep.AWAITING_CONFIRM: self._handle_confirm,
            ActivitiesStep.AWAITING_CANCEL_CHOICE: self._handle_cancel_choice,
            ActivitiesStep.AWAITING_CANCEL_CONFIRM: self._handle_cancel_confirm,
        }
        return await pasos[estado.step](phone, session, estado, msg)

    async def _handle_action(self, phone, session, estado, msg) -> FlowResult:
        if msg.interactive_id == ACTION_ENROLL:
            return await self._show_activities(phone, session, estado)
        if msg.interactive_id == ACTION_CANCEL:
            return await self._show_cancellable(phone, session, estado)
        await whatsapp_client.send_text(phone, "Elegí Inscribirme o Mis inscripciones.", session=session)
        return FlowResult.CONTINUE

    async def _enviar_pagina(self, phone, session, estado, tipo: str, pagina: int = 0) -> None:
        if tipo == "actividades":
            opciones = [
                {"id": f"{ACTIVITY_PREFIX}{id_}", "title": a["nombre"], "description": _format_horario(a)}
                for id_, a in estado.available_activities.items()
            ]
            titulo, cuerpo = "Actividades disponibles", "Elegí una actividad:"
        elif tipo == "clases":
            opciones = [
                {
                    "id": f"{INSTANCIA_PREFIX}{id_}",
                    "title": f"Clase {str(c['fecha'])[8:10]}/{str(c['fecha'])[5:7]}",
                    "description": f"{_formato_clase(c)} · {c['cupoDisponible']} lugares",
                }
                for id_, c in estado.instancias_disponibles.items()
            ]
            titulo, cuerpo = "Clases disponibles", "Elegí la clase a la que querés anotarte:"
        else:
            opciones = [
                {
                    "id": f"{CANCEL_PREFIX}{id_}",
                    "title": i["nombreActividad"],
                    "description": _formato_clase(i)
                    if i.get("instanciaId")
                    else "Inscripción general (toda la actividad)",
                }
                for id_, i in estado.active_inscriptions.items()
            ]
            titulo, cuerpo = "Mis inscripciones", "Elegí una clase para darte de baja, o la inscripción general:"
        pagina = max(0, min(pagina, max(0, (len(opciones) - 1) // 8)))
        estado.paginas[tipo] = pagina
        filas = opciones[pagina * 8 : (pagina + 1) * 8]
        if pagina:
            filas.append({"id": f"{PAGINA_PREFIX}{tipo}:{pagina - 1}", "title": "Anteriores"})
        if (pagina + 1) * 8 < len(opciones):
            filas.append({"id": f"{PAGINA_PREFIX}{tipo}:{pagina + 1}", "title": "Más opciones"})
        for fila in filas:
            fila["title"] = _recortar(str(fila["title"]), 24)
            fila["description"] = _recortar(str(fila.get("description", "")), 72)
        await whatsapp_client.send_list(
            to=phone,
            body=cuerpo,
            button_text="Ver opciones",
            rows=filas,
            section_title=titulo,
            session=session,
        )

    async def _paginar(self, phone, session, estado, msg, tipo) -> bool:
        prefijo = f"{PAGINA_PREFIX}{tipo}:"
        if not (msg.interactive_id or "").startswith(prefijo):
            return False
        try:
            pagina = int(msg.interactive_id[len(prefijo) :])
        except ValueError:
            pagina = 0
        await self._enviar_pagina(phone, session, estado, tipo, pagina)
        return True

    async def _resumen(self, phone, session, inscripciones, actividades=None) -> None:
        opciones = _opciones_inscripciones(inscripciones, actividades)
        lineas = []
        for inscripcion in opciones.values():
            detalle = _formato_clase(inscripcion) if inscripcion.get("instanciaId") else "inscripción general"
            estado = inscripcion.get("estadoClase") or "Inscripto"
            etiqueta = " · baja registrada" if estado == "Baja" else " · suspendida" if estado == "Suspendida" else ""
            lineas.append(f"- {inscripcion['nombreActividad']}: {detalle}{etiqueta}")
        # Los mensajes de texto también tienen límite. No se pierden clases por truncar una lista.
        texto = "Mis inscripciones:\n\n" if lineas else "No tenés inscripciones vigentes."
        for linea in lineas:
            linea = _recortar(linea, 500)
            if len(texto) + len(linea) > 3500:
                await whatsapp_client.send_text(phone, texto, session=session)
                texto = "Mis inscripciones (continúa):\n\n"
            texto += linea + "\n"
        await whatsapp_client.send_text(phone, texto.rstrip(), session=session)

    async def _show_activities(self, phone, session, estado) -> FlowResult:
        try:
            inscripciones = await svc.services_client.get_inscripciones_socio(str(session.socio["id"]))
            actividades = await svc.services_client.get_actividades()
        except ServicesAPIError:
            logger.warning("No se pudieron consultar actividades e inscripciones")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE
        await self._resumen(phone, session, inscripciones, {a["id"]: a for a in actividades})
        # El cupo de actividad es genérico: no determina el cupo de sus clases.
        # Tener otra inscripción en la actividad tampoco impide elegir otra clase.
        estado.available_activities = {str(a["id"]): a for a in actividades if a.get("estado") == "Activa"}
        if not estado.available_activities:
            await whatsapp_client.send_text(phone, "No hay actividades disponibles en este momento.", session=session)
            return FlowResult.DONE
        estado.step = ActivitiesStep.AWAITING_CHOICE
        await self._enviar_pagina(phone, session, estado, "actividades")
        return FlowResult.CONTINUE

    async def _handle_choice(self, phone, session, estado, msg) -> FlowResult:
        if await self._paginar(phone, session, estado, msg, "actividades"):
            return FlowResult.CONTINUE
        identificador = msg.interactive_id or ""
        actividad = estado.available_activities.get(identificador.removeprefix(ACTIVITY_PREFIX))
        if not identificador.startswith(ACTIVITY_PREFIX) or actividad is None:
            await whatsapp_client.send_text(phone, "Elegí una actividad de la lista actual.", session=session)
            return FlowResult.CONTINUE
        estado.selected_activity = actividad
        estado.instancia_elegida = None
        try:
            instancias = await svc.services_client.get_instancias_actividad(str(actividad["id"]))
            inscripciones = await svc.services_client.get_inscripciones_socio(str(session.socio["id"]))
        except ServicesAPIError:
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE
        programadas = [c for c in instancias if c.get("estado") == "Programada" and _es_futura(c)]
        cubiertas = {str(c["instanciaId"]) for i in inscripciones if _vigente(i) for c in _clases_inscripcion(i)}
        estado.instancias_disponibles = {
            str(c["id"]): c
            for c in sorted(programadas, key=lambda c: (c["fecha"], c["horaInicio"]))
            if not c.get("liquidada") and (c.get("cupoDisponible") or 0) > 0 and str(c["id"]) not in cubiertas
        }
        if estado.instancias_disponibles:
            estado.step = ActivitiesStep.ELEGIR_INSTANCIA
            await self._enviar_pagina(phone, session, estado, "clases")
        elif programadas:
            # No ofrecer una genérica como atajo para entrar en clases llenas o ya cubiertas.
            await whatsapp_client.send_text(
                phone,
                "No hay otras clases futuras con cupo: están llenas, cerradas o ya estás inscripto/a.",
                session=session,
            )
            return FlowResult.DONE
        elif any(
            _vigente(i) and i.get("actividadId") == actividad["id"] and not i.get("instanciaId") for i in inscripciones
        ):
            await whatsapp_client.send_text(
                phone, "Ya tenés una inscripción general en esta actividad.", session=session
            )
            return FlowResult.DONE
        elif (actividad.get("cupoDisponible") or 0) <= 0:
            await whatsapp_client.send_text(phone, "La actividad no tiene cupo disponible.", session=session)
            return FlowResult.DONE
        else:
            await self._pedir_confirmacion(phone, session, estado)
        return FlowResult.CONTINUE

    async def _elegir_instancia(self, phone, session, estado, msg) -> FlowResult:
        if await self._paginar(phone, session, estado, msg, "clases"):
            return FlowResult.CONTINUE
        identificador = msg.interactive_id or ""
        clase = estado.instancias_disponibles.get(identificador.removeprefix(INSTANCIA_PREFIX))
        if not identificador.startswith(INSTANCIA_PREFIX) or clase is None:
            await whatsapp_client.send_text(phone, "Elegí una clase de la lista actual.", session=session)
            return FlowResult.CONTINUE
        estado.instancia_elegida = clase
        await self._pedir_confirmacion(phone, session, estado)
        return FlowResult.CONTINUE

    async def _pedir_confirmacion(self, phone, session, estado) -> None:
        actividad = estado.selected_activity
        clase = estado.instancia_elegida
        detalle = _formato_clase(clase) if clase else "Inscripción general a la actividad, sin clase puntual."
        costo = (clase or actividad).get("costo", 0)
        estado.step = ActivitiesStep.AWAITING_CONFIRM
        await whatsapp_client.send_buttons(
            phone,
            body=f"¿Confirmás tu inscripción a *{actividad['nombre']}*?\n{detalle}\nCosto base: ${float(costo):.0f}",
            buttons=[(CONFIRM_YES, "Confirmar"), (CONFIRM_NO, "Cancelar")],
            session=session,
        )

    async def _handle_confirm(self, phone, session, estado, msg) -> FlowResult:
        if msg.interactive_id == CONFIRM_NO:
            await whatsapp_client.send_text(phone, "Inscripción no realizada.", session=session)
            return FlowResult.DONE
        if msg.interactive_id != CONFIRM_YES:
            await whatsapp_client.send_text(phone, "Tocá Confirmar o Cancelar.", session=session)
            return FlowResult.CONTINUE
        actividad = estado.selected_activity
        clase = estado.instancia_elegida
        if actividad is None:
            await whatsapp_client.send_text(phone, SESION_VENCIDA, session=session)
            return FlowResult.DONE
        try:
            if clase:
                actuales = await svc.services_client.get_instancias_actividad(str(actividad["id"]))
                actual = next((c for c in actuales if str(c["id"]) == str(clase["id"])), None)
                if (
                    actual is None
                    or not _es_futura(actual)
                    or actual.get("estado") != "Programada"
                    or actual.get("liquidada")
                ):
                    await whatsapp_client.send_text(phone, NO_DISPONIBLE, session=session)
                    return FlowResult.DONE
                if (actual.get("cupoDisponible") or 0) <= 0:
                    await whatsapp_client.send_text(phone, SIN_CUPO, session=session)
                    return FlowResult.DONE
                if any(actual.get(campo) != clase.get(campo) for campo in ("fecha", "horaInicio", "horaFin", "costo")):
                    estado.instancia_elegida = actual
                    await whatsapp_client.send_text(
                        phone, "La clase cambió. Revisá los datos antes de confirmar.", session=session
                    )
                    await self._pedir_confirmacion(phone, session, estado)
                    return FlowResult.CONTINUE
                await svc.services_client.post_inscripcion(
                    str(session.socio["id"]), str(actividad["id"]), instancia_id=str(clase["id"])
                )
            else:
                # POST también revalida actividad/socio, duplicados y cupo en la API.
                await svc.services_client.post_inscripcion(str(session.socio["id"]), str(actividad["id"]))
        except ServicesAPICupoAgotado:
            await whatsapp_client.send_text(phone, SIN_CUPO, session=session)
            return FlowResult.DONE
        except (ServicesAPIConflict, ServicesAPIBadRequest, ServicesAPINoEncontrado):
            await whatsapp_client.send_text(phone, NO_DISPONIBLE, session=session)
            return FlowResult.DONE
        except ServicesAPIError:
            logger.warning("No se pudo confirmar la inscripción")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE
        detalle = f" · {_formato_clase(clase)}" if clase else " (inscripción general)"
        await whatsapp_client.send_text(
            phone, f"¡Listo! Quedaste inscripto/a en *{actividad['nombre']}*{detalle}.", session=session
        )
        return FlowResult.DONE

    async def _show_cancellable(self, phone, session, estado) -> FlowResult:
        try:
            inscripciones = await svc.services_client.get_inscripciones_socio(str(session.socio["id"]))
        except ServicesAPIError:
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE
        await self._resumen(phone, session, inscripciones)
        estado.active_inscriptions = {
            id_: i
            for id_, i in _opciones_inscripciones(inscripciones).items()
            if not i.get("instanciaId")
            or (i.get("estadoClase") == "Inscripto" and i.get("estadoInstancia") == "Programada")
        }
        if not estado.active_inscriptions:
            await whatsapp_client.send_text(
                phone, "No tenés inscripciones disponibles para dar de baja.", session=session
            )
            return FlowResult.DONE
        estado.step = ActivitiesStep.AWAITING_CANCEL_CHOICE
        await self._enviar_pagina(phone, session, estado, "inscripciones")
        return FlowResult.CONTINUE

    async def _handle_cancel_choice(self, phone, session, estado, msg) -> FlowResult:
        if await self._paginar(phone, session, estado, msg, "inscripciones"):
            return FlowResult.CONTINUE
        identificador = msg.interactive_id or ""
        inscripcion = estado.active_inscriptions.get(identificador.removeprefix(CANCEL_PREFIX))
        if not identificador.startswith(CANCEL_PREFIX) or inscripcion is None:
            await whatsapp_client.send_text(phone, "Elegí una inscripción de la lista actual.", session=session)
            return FlowResult.CONTINUE
        estado.selected_inscription = inscripcion
        estado.step = ActivitiesStep.AWAITING_CANCEL_CONFIRM
        detalle = _formato_clase(inscripcion) if inscripcion.get("instanciaId") else "la inscripción general completa"
        await whatsapp_client.send_buttons(
            phone,
            body=f"¿Confirmás la baja de *{inscripcion['nombreActividad']}*?\n{detalle}",
            buttons=[(CANCEL_CONFIRM_YES, "Confirmar"), (CANCEL_CONFIRM_NO, "Volver")],
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_cancel_confirm(self, phone, session, estado, msg) -> FlowResult:
        if msg.interactive_id == CANCEL_CONFIRM_NO:
            return FlowResult.DONE
        if msg.interactive_id != CANCEL_CONFIRM_YES:
            await whatsapp_client.send_text(phone, "Tocá Confirmar o Volver.", session=session)
            return FlowResult.CONTINUE
        inscripcion = estado.selected_inscription
        if inscripcion is None:
            await whatsapp_client.send_text(phone, SESION_VENCIDA, session=session)
            return FlowResult.DONE
        try:
            actual = await svc.services_client.get_inscripcion(str(inscripcion["id"]))
            if actual.get("socioId") != str(session.socio["id"]) or not _vigente(actual):
                await whatsapp_client.send_text(phone, NO_DISPONIBLE, session=session)
                return FlowResult.DONE
            instancia_id = inscripcion.get("instanciaId")
            if instancia_id:
                clase = next((c for c in _clases_inscripcion(actual) if c.get("instanciaId") == instancia_id), None)
                if (
                    clase is None
                    or not _es_futura(clase)
                    or clase.get("estadoClase") != "Inscripto"
                    or clase.get("estadoInstancia") != "Programada"
                    or any(clase.get(campo) != inscripcion.get(campo) for campo in ("fecha", "horaInicio", "horaFin"))
                ):
                    await whatsapp_client.send_text(phone, NO_DISPONIBLE, session=session)
                    return FlowResult.DONE
                await svc.services_client.dar_baja_instancia(str(instancia_id), str(session.socio["id"]))
            else:
                if actual.get("instanciaId"):
                    await whatsapp_client.send_text(phone, NO_DISPONIBLE, session=session)
                    return FlowResult.DONE
                await svc.services_client.delete_inscripcion(str(inscripcion["id"]))
        except (ServicesAPINoEncontrado, ServicesAPIBadRequest, ServicesAPIConflict):
            await whatsapp_client.send_text(phone, NO_DISPONIBLE, session=session)
            return FlowResult.DONE
        except ServicesAPIError:
            logger.warning("No se pudo confirmar la baja de la inscripción")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.DONE
        mensaje = (
            "Te diste de baja solo de esa clase. Tus otras inscripciones siguen activas."
            if instancia_id
            else "Inscripción general cancelada."
        )
        await whatsapp_client.send_text(phone, mensaje, session=session)
        return FlowResult.DONE
