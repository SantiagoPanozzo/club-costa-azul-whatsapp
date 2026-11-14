import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from .. import services_client as svc
from ..flow_ui import (
    MENU_ID,
    adaptive_summary,
    confirmation_buttons,
    list_body_limit,
    paginate_rows,
    with_notice,
)
from ..formatting import format_date, format_date_weekday, format_schedule, format_time_range
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
    return format_schedule(actividad)


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
    return f"{format_date_weekday(inicio)} {format_time_range(inicio.time(), clase.get('horaFin'))}"


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
    """Incluye detalles de solo lectura; las escrituras comprueban _cancelable aparte."""
    resultado: dict[str, dict] = {}
    for inscripcion in sorted(inscripciones, key=lambda i: bool(i.get("instanciaId"))):
        actividad = (actividades or {}).get(str(inscripcion.get("actividadId")), {})
        nombre = inscripcion.get("nombreActividad") or actividad.get("nombre") or "Actividad"
        if not inscripcion.get("instanciaId"):
            resultado[f"general_{inscripcion['id']}"] = {
                **inscripcion,
                "nombreActividad": nombre,
                "instanciaId": None,
            }
        clases = _clases_inscripcion(inscripcion)
        if not clases and inscripcion.get("instanciaId"):
            clases = [inscripcion]
        for clase in clases:
            key = f"clase_{clase['instanciaId']}"
            candidate = {
                **inscripcion,
                **clase,
                "nombreActividad": nombre,
            }
            previous = resultado.get(key)
            if previous and previous["id"] != candidate["id"]:
                if _cancelable(previous) and not _cancelable(candidate):
                    resultado[f"history_{candidate['id']}_{key}"] = candidate
                    continue
                if not _cancelable(previous):
                    resultado[f"history_{previous['id']}_{key}"] = previous
            resultado[key] = candidate
    return resultado


def _cancelable(inscripcion: dict) -> bool:
    return _vigente(inscripcion) and (
        not inscripcion.get("instanciaId")
        or (
            _es_futura(inscripcion)
            and inscripcion.get("estadoClase") == "Inscripto"
            and inscripcion.get("estadoInstancia") == "Programada"
            and not inscripcion.get("liquidada")
        )
    )


def _detalle_inscripcion(inscripcion: dict) -> str:
    detalle = _formato_clase(inscripcion) if inscripcion.get("instanciaId") else "Inscripción general"
    estado = inscripcion.get("estadoClase") or inscripcion.get("estado") or "Sin estado"
    etiqueta = "baja registrada" if estado == "Baja" else "suspendida" if estado == "Suspendida" else estado
    extras = []
    if inscripcion.get("estado") not in (None, "Activa", estado):
        extras.append(str(inscripcion["estado"]))
    if inscripcion.get("estadoInstancia") not in (None, "Programada", estado):
        extras.append(str(inscripcion["estadoInstancia"]))
    if not inscripcion.get("instanciaId"):
        for campo, titulo in (("fechaInicio", "Desde"), ("fechaFin", "Hasta")):
            if inscripcion.get(campo):
                extras.append(f"{titulo}: {format_date(inscripcion[campo])}")
    return " · ".join([detalle, etiqueta, *extras])


def _fecha_creacion(inscripcion: dict) -> tuple[float, str]:
    try:
        fecha = datetime.fromisoformat(str(inscripcion.get("fechaInscripcion")).replace("Z", "+00:00"))
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=_ZONA_CLUB)
        return fecha.timestamp(), str(inscripcion.get("id", ""))
    except (ValueError, TypeError):
        return float("-inf"), str(inscripcion.get("id", ""))


def _entradas_resumen(inscripciones: list[dict], actividades: dict) -> list[str]:
    entradas = []
    for inscripcion in sorted(inscripciones, key=_fecha_creacion, reverse=True):
        opciones = _opciones_inscripciones([inscripcion], actividades)
        nombre = inscripcion.get("nombreActividad") or actividades.get(str(inscripcion.get("actividadId")), {}).get(
            "nombre", "Actividad"
        )
        detalles = "; ".join(_detalle_inscripcion(i) for i in opciones.values())
        entradas.append(f"- {nombre}: {detalles}")
    return entradas


class ActivitiesStep(StrEnum):
    AWAITING_CHOICE = "awaiting_choice"
    ELEGIR_INSTANCIA = "elegir_instancia"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_CANCEL_CHOICE = "awaiting_cancel_choice"
    AWAITING_CANCEL_CONFIRM = "awaiting_cancel_confirm"


@dataclass
class ActivitiesState:
    step: ActivitiesStep = ActivitiesStep.AWAITING_CHOICE
    available_activities: dict[str, dict] = field(default_factory=dict)
    selected_activity: dict | None = None
    active_inscriptions: dict[str, dict] = field(default_factory=dict)
    selected_inscription: dict | None = None
    instancias_disponibles: dict[str, dict] = field(default_factory=dict)
    instancia_elegida: dict | None = None
    paginas: dict[str, int] = field(default_factory=dict)
    summary_entries: list[str] = field(default_factory=list)


class ActivitiesFlow(BaseFlow[ActivitiesState]):
    def create_state(self) -> ActivitiesState:
        return ActivitiesState()

    async def enter(self, phone: str, session: Session) -> None:
        session.flow_state = self.create_state()
        if not session.socio:
            session.finish(SESION_VENCIDA, "error")
            return
        await self._show_activities(phone, session, self.get_state(session))

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        if not isinstance(session.flow_state, ActivitiesState) or not session.socio:
            session.finish(SESION_VENCIDA, "error")
            return FlowResult.DONE
        estado = self.get_state(session)
        pasos = {
            ActivitiesStep.AWAITING_CHOICE: self._handle_choice,
            ActivitiesStep.ELEGIR_INSTANCIA: self._elegir_instancia,
            ActivitiesStep.AWAITING_CONFIRM: self._handle_confirm,
            ActivitiesStep.AWAITING_CANCEL_CHOICE: self._handle_cancel_choice,
            ActivitiesStep.AWAITING_CANCEL_CONFIRM: self._handle_cancel_confirm,
        }
        return await pasos[estado.step](phone, session, estado, msg)

    async def _enviar_pagina(self, phone, session, estado, tipo: str, pagina: int = 0, notice: str = "") -> None:
        session.clear_confirmation()
        if notice:
            session.mark_recovery()
        if tipo == "actividades":
            opciones = [
                {"id": f"{ACTIVITY_PREFIX}{id_}", "title": a["nombre"], "description": _format_horario(a)}
                for id_, a in estado.available_activities.items()
            ]
            titulo = "Actividades disponibles"
            cuerpo = adaptive_summary(
                "Mis inscripciones:",
                estado.summary_entries,
                "Elegí una actividad o consultá Mis inscripciones:",
                empty="No tenés inscripciones.",
                limit=list_body_limit() - (len(notice) + 2 if notice else 0),
            )
            extras = [{"id": ACTION_CANCEL, "title": "Mis inscripciones"}]
        elif tipo == "clases":
            opciones = [
                {
                    "id": f"{INSTANCIA_PREFIX}{id_}",
                    "title": f"Clase {format_date(c['fecha'])}",
                    "description": f"{_formato_clase(c)} · {c['cupoDisponible']} lugares",
                }
                for id_, c in estado.instancias_disponibles.items()
            ]
            titulo, cuerpo = "Clases disponibles", "Elegí la clase a la que querés anotarte:"
            extras = [{"id": ACTION_ENROLL, "title": "Volver a actividades"}]
        else:
            opciones = [
                {
                    "id": f"{CANCEL_PREFIX}{id_}",
                    "title": i["nombreActividad"],
                    "description": _detalle_inscripcion(i),
                }
                for id_, i in estado.active_inscriptions.items()
            ]
            titulo = "Mis inscripciones"
            cuerpo = (
                "Elegí una inscripción para ver el detalle o darte de baja:"
                if opciones
                else "No tenés inscripciones. Podés volver al catálogo."
            )
            extras = [{"id": ACTION_ENROLL, "title": "Volver a actividades"}]
        filas, pagina = paginate_rows(opciones, pagina, page_prefix=f"{PAGINA_PREFIX}{tipo}:", extra_rows=extras)
        estado.paginas[tipo] = pagina
        for fila in filas:
            fila["title"] = _recortar(str(fila["title"]), 24)
            fila["description"] = _recortar(str(fila.get("description", "")), 72)
        await whatsapp_client.send_list(
            to=phone,
            body=with_notice(cuerpo, notice),
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
            await self._enviar_pagina(
                phone, session, estado, tipo, estado.paginas.get(tipo, 0), "Elegí una página de la lista actual."
            )
            return True
        await self._enviar_pagina(phone, session, estado, tipo, pagina)
        return True

    async def _show_activities(self, phone, session, estado) -> FlowResult:
        self._clear_selection(estado)
        session.set_task("view")
        try:
            inscripciones = await svc.services_client.get_inscripciones_socio(str(session.socio["id"]))
            actividades = await svc.services_client.get_actividades()
        except ServicesAPIError:
            logger.warning("No se pudieron consultar actividades e inscripciones")
            session.finish(GENERIC_ERROR, "error")
            return FlowResult.DONE
        estado.summary_entries = _entradas_resumen(inscripciones, {str(a["id"]): a for a in actividades})
        # El cupo de actividad es genérico: no determina el cupo de sus clases.
        # Tener otra inscripción en la actividad tampoco impide elegir otra clase.
        estado.available_activities = {str(a["id"]): a for a in actividades if a.get("estado") == "Activa"}
        if not estado.available_activities and not inscripciones:
            session.finish("No hay actividades disponibles en este momento.", "empty")
            return FlowResult.DONE
        estado.step = ActivitiesStep.AWAITING_CHOICE
        await self._enviar_pagina(phone, session, estado, "actividades")
        return FlowResult.CONTINUE

    @staticmethod
    def _clear_selection(estado) -> None:
        estado.selected_activity = None
        estado.instancia_elegida = None
        estado.instancias_disponibles = {}
        estado.selected_inscription = None

    async def _handle_choice(self, phone, session, estado, msg) -> FlowResult:
        if msg.interactive_id == ACTION_CANCEL:
            return await self._show_inscriptions(phone, session, estado)
        if msg.interactive_id == ACTION_ENROLL:
            return await self._show_activities(phone, session, estado)
        if await self._paginar(phone, session, estado, msg, "actividades"):
            return FlowResult.CONTINUE
        identificador = msg.interactive_id or ""
        actividad = estado.available_activities.get(identificador.removeprefix(ACTIVITY_PREFIX))
        if not identificador.startswith(ACTIVITY_PREFIX) or actividad is None:
            await self._enviar_pagina(
                phone,
                session,
                estado,
                "actividades",
                estado.paginas.get("actividades", 0),
                "Elegí una actividad de la lista actual.",
            )
            return FlowResult.CONTINUE
        estado.selected_activity = actividad
        estado.instancia_elegida = None
        try:
            instancias = await svc.services_client.get_instancias_actividad(str(actividad["id"]))
            inscripciones = await svc.services_client.get_inscripciones_socio(str(session.socio["id"]))
        except ServicesAPIError:
            session.finish(GENERIC_ERROR, "error")
            return FlowResult.DONE
        programadas = [c for c in instancias if c.get("estado") == "Programada" and _es_futura(c)]
        session.set_task("enroll_class" if programadas else "enroll_general")
        cubiertas = {str(c["instanciaId"]) for i in inscripciones if _vigente(i) for c in _clases_inscripcion(i)}
        estado.instancias_disponibles = {
            str(c["id"]): c
            for c in sorted(programadas, key=lambda c: (c["fecha"], c["horaInicio"]))
            if not c.get("liquidada") and (c.get("cupoDisponible") or 0) > 0 and str(c["id"]) not in cubiertas
        }
        if estado.instancias_disponibles:
            if len(estado.instancias_disponibles) == 1:
                estado.instancia_elegida = next(iter(estado.instancias_disponibles.values()))
                await self._pedir_confirmacion(phone, session, estado)
            else:
                estado.step = ActivitiesStep.ELEGIR_INSTANCIA
                await self._enviar_pagina(phone, session, estado, "clases")
        elif programadas:
            # No ofrecer una genérica como atajo para entrar en clases llenas o ya cubiertas.
            session.finish(
                "No hay otras clases futuras con cupo: están llenas, cerradas o ya estás inscripto/a.",
                "unavailable",
            )
            return FlowResult.DONE
        elif any(
            _vigente(i) and i.get("actividadId") == actividad["id"] and not i.get("instanciaId") for i in inscripciones
        ):
            session.finish("Ya tenés una inscripción general en esta actividad.", "unavailable")
            return FlowResult.DONE
        elif (actividad.get("cupoDisponible") or 0) <= 0:
            session.finish("La actividad no tiene cupo disponible.", "unavailable")
            return FlowResult.DONE
        else:
            await self._pedir_confirmacion(phone, session, estado)
        return FlowResult.CONTINUE

    async def _elegir_instancia(self, phone, session, estado, msg) -> FlowResult:
        if msg.interactive_id == ACTION_ENROLL:
            return await self._show_activities(phone, session, estado)
        if await self._paginar(phone, session, estado, msg, "clases"):
            return FlowResult.CONTINUE
        identificador = msg.interactive_id or ""
        clase = estado.instancias_disponibles.get(identificador.removeprefix(INSTANCIA_PREFIX))
        if not identificador.startswith(INSTANCIA_PREFIX) or clase is None:
            await self._enviar_pagina(
                phone, session, estado, "clases", estado.paginas.get("clases", 0), "Elegí una clase de la lista actual."
            )
            return FlowResult.CONTINUE
        estado.instancia_elegida = clase
        await self._pedir_confirmacion(phone, session, estado)
        return FlowResult.CONTINUE

    async def _pedir_confirmacion(self, phone, session, estado, notice="") -> None:
        if notice:
            session.mark_recovery()
        actividad = estado.selected_activity
        clase = estado.instancia_elegida
        detalle = (
            _formato_clase(clase)
            if clase
            else f"Inscripción general a la actividad, sin clase puntual.\n{_format_horario(actividad)}"
        )
        costo = (clase or actividad).get("costo", 0)
        estado.step = ActivitiesStep.AWAITING_CONFIRM
        await whatsapp_client.send_buttons(
            phone,
            body=with_notice(
                f"¿Confirmás tu inscripción a *{actividad['nombre']}*?\n{detalle}\n"
                f"Costo base: ${Decimal(str(costo)):.2f}",
                notice,
            ),
            buttons=confirmation_buttons(CONFIRM_YES, CONFIRM_NO, session=session),
            session=session,
        )

    async def _handle_confirm(self, phone, session, estado, msg) -> FlowResult:
        if estado.selected_activity is None:
            session.finish(SESION_VENCIDA, "error")
            return FlowResult.DONE
        if msg.interactive_id == CONFIRM_NO:
            estado.instancia_elegida = None
            if len(estado.instancias_disponibles) > 1:
                estado.step = ActivitiesStep.ELEGIR_INSTANCIA
                await self._enviar_pagina(phone, session, estado, "clases", estado.paginas.get("clases", 0))
                return FlowResult.CONTINUE
            return await self._show_activities(phone, session, estado)
        if not session.consume_confirmation(msg.interactive_id, CONFIRM_YES):
            await self._pedir_confirmacion(phone, session, estado, "Tocá Confirmar, Volver o Menú principal.")
            return FlowResult.CONTINUE
        actividad = estado.selected_activity
        clase = estado.instancia_elegida
        try:
            actividades = await svc.services_client.get_actividades()
            actividad_actual = next((a for a in actividades if str(a["id"]) == str(actividad["id"])), None)
            inscripciones = await svc.services_client.get_inscripciones_socio(str(session.socio["id"]))
            if actividad_actual is None or actividad_actual.get("estado") != "Activa":
                session.finish(NO_DISPONIBLE, "unavailable")
                return FlowResult.DONE
            if clase:
                actuales = await svc.services_client.get_instancias_actividad(str(actividad["id"]))
                actual = next((c for c in actuales if str(c["id"]) == str(clase["id"])), None)
                if (
                    actual is None
                    or not _es_futura(actual)
                    or actual.get("estado") != "Programada"
                    or actual.get("liquidada")
                    or str(actual.get("actividadId", actividad["id"])) != str(actividad["id"])
                ):
                    session.finish(NO_DISPONIBLE, "unavailable")
                    return FlowResult.DONE
                if (actual.get("cupoDisponible") or 0) <= 0:
                    session.finish(SIN_CUPO, "unavailable")
                    return FlowResult.DONE
                if any(
                    str(c.get("instanciaId")) == str(clase["id"])
                    for i in inscripciones
                    if _vigente(i)
                    for c in _clases_inscripcion(i)
                ):
                    session.finish(NO_DISPONIBLE, "unavailable")
                    return FlowResult.DONE
                if actividad_actual.get("nombre") != actividad.get("nombre") or any(
                    actual.get(campo) != clase.get(campo) for campo in ("fecha", "horaInicio", "horaFin", "costo")
                ):
                    estado.selected_activity = actividad_actual
                    estado.instancia_elegida = actual
                    await self._pedir_confirmacion(
                        phone, session, estado, "La clase cambió. Revisá los datos antes de confirmar."
                    )
                    return FlowResult.CONTINUE
                await svc.services_client.post_inscripcion(
                    str(session.socio["id"]), str(actividad["id"]), instancia_id=str(clase["id"])
                )
            else:
                actuales = await svc.services_client.get_instancias_actividad(str(actividad["id"]))
                if any(c.get("estado") == "Programada" and _es_futura(c) for c in actuales) or any(
                    _vigente(i) and str(i.get("actividadId")) == str(actividad["id"]) and not i.get("instanciaId")
                    for i in inscripciones
                ):
                    session.finish(NO_DISPONIBLE, "unavailable")
                    return FlowResult.DONE
                if (actividad_actual.get("cupoDisponible") or 0) <= 0:
                    session.finish("La actividad no tiene cupo disponible.", "unavailable")
                    return FlowResult.DONE
                if any(
                    actividad_actual.get(campo) != actividad.get(campo)
                    for campo in ("nombre", "costo", "horaInicio", "horaFin", "diasSemana", "horarios")
                ):
                    estado.selected_activity = actividad_actual
                    await self._pedir_confirmacion(
                        phone, session, estado, "La actividad cambió. Revisá los datos antes de confirmar."
                    )
                    return FlowResult.CONTINUE
                # POST también revalida actividad/socio, duplicados y cupo en la API.
                await svc.services_client.post_inscripcion(str(session.socio["id"]), str(actividad["id"]))
        except ServicesAPICupoAgotado:
            session.finish(SIN_CUPO, "unavailable")
            return FlowResult.DONE
        except (ServicesAPIConflict, ServicesAPIBadRequest, ServicesAPINoEncontrado):
            session.finish(NO_DISPONIBLE, "unavailable")
            return FlowResult.DONE
        except ServicesAPIError:
            logger.warning("No se pudo confirmar la inscripción")
            session.finish(GENERIC_ERROR, "error")
            return FlowResult.DONE
        detalle = f" · {_formato_clase(clase)}" if clase else " (inscripción general)"
        session.finish(f"¡Listo! Quedaste inscripto/a en *{actividad['nombre']}*{detalle}.")
        return FlowResult.DONE

    async def _show_inscriptions(self, phone, session, estado) -> FlowResult:
        self._clear_selection(estado)
        session.set_task("view")
        try:
            inscripciones = await svc.services_client.get_inscripciones_socio(str(session.socio["id"]))
        except ServicesAPIError:
            session.finish(GENERIC_ERROR, "error")
            return FlowResult.DONE
        estado.active_inscriptions = _opciones_inscripciones(inscripciones, estado.available_activities)
        estado.step = ActivitiesStep.AWAITING_CANCEL_CHOICE
        await self._enviar_pagina(phone, session, estado, "inscripciones")
        return FlowResult.CONTINUE

    async def _handle_cancel_choice(self, phone, session, estado, msg) -> FlowResult:
        if msg.interactive_id == ACTION_ENROLL:
            return await self._show_activities(phone, session, estado)
        if await self._paginar(phone, session, estado, msg, "inscripciones"):
            return FlowResult.CONTINUE
        identificador = msg.interactive_id or ""
        inscripcion = estado.active_inscriptions.get(identificador.removeprefix(CANCEL_PREFIX))
        if not identificador.startswith(CANCEL_PREFIX) or inscripcion is None:
            await self._enviar_pagina(
                phone,
                session,
                estado,
                "inscripciones",
                estado.paginas.get("inscripciones", 0),
                "Elegí una inscripción de la lista actual.",
            )
            return FlowResult.CONTINUE
        estado.selected_inscription = inscripcion
        session.set_task(
            ("withdraw_class" if inscripcion.get("instanciaId") else "withdraw_general")
            if _cancelable(inscripcion)
            else "view"
        )
        await self._pedir_baja(phone, session, estado)
        return FlowResult.CONTINUE

    async def _pedir_baja(self, phone, session, estado, notice="") -> None:
        if notice:
            session.mark_recovery()
        inscripcion = estado.selected_inscription
        estado.step = ActivitiesStep.AWAITING_CANCEL_CONFIRM
        detalle = _detalle_inscripcion(inscripcion)
        if inscripcion.get("costo") is not None:
            detalle += f"\nCosto base: ${Decimal(str(inscripcion['costo'])):.2f}"
        if _cancelable(inscripcion):
            cuerpo = f"¿Confirmás la baja de *{inscripcion['nombreActividad']}*?\n{detalle}"
            botones = confirmation_buttons(CANCEL_CONFIRM_YES, CANCEL_CONFIRM_NO, session=session)
        else:
            session.clear_confirmation()
            cuerpo = (
                f"*{inscripcion['nombreActividad']}*\n{detalle}\nEsta inscripción no está disponible para dar de baja."
            )
            botones = [(CANCEL_CONFIRM_NO, "Volver"), (MENU_ID, "Menú principal")]
        await whatsapp_client.send_buttons(
            phone,
            body=with_notice(cuerpo, notice),
            buttons=botones,
            session=session,
        )

    async def _handle_cancel_confirm(self, phone, session, estado, msg) -> FlowResult:
        if msg.interactive_id == CANCEL_CONFIRM_NO:
            estado.selected_inscription = None
            session.set_task("view")
            estado.step = ActivitiesStep.AWAITING_CANCEL_CHOICE
            await self._enviar_pagina(phone, session, estado, "inscripciones", estado.paginas.get("inscripciones", 0))
            return FlowResult.CONTINUE
        inscripcion = estado.selected_inscription
        if inscripcion is None:
            session.finish(SESION_VENCIDA, "error")
            return FlowResult.DONE
        if not _cancelable(inscripcion) or not session.consume_confirmation(msg.interactive_id, CANCEL_CONFIRM_YES):
            await self._pedir_baja(phone, session, estado, "Elegí una de las opciones disponibles.")
            return FlowResult.CONTINUE
        try:
            actual = await svc.services_client.get_inscripcion(str(inscripcion["id"]))
            actual = {**actual, "nombreActividad": actual.get("nombreActividad") or inscripcion["nombreActividad"]}
            if (
                str(actual.get("socioId")) != str(session.socio["id"])
                or str(actual.get("actividadId")) != str(inscripcion.get("actividadId"))
                or not _vigente(actual)
            ):
                session.finish(NO_DISPONIBLE, "unavailable")
                return FlowResult.DONE
            instancia_id = inscripcion.get("instanciaId")
            if instancia_id:
                clase = next((c for c in _clases_inscripcion(actual) if c.get("instanciaId") == instancia_id), None)
                if (
                    clase is None
                    or not _es_futura(clase)
                    or clase.get("estadoClase") != "Inscripto"
                    or clase.get("estadoInstancia") != "Programada"
                    or clase.get("liquidada")
                ):
                    session.finish(NO_DISPONIBLE, "unavailable")
                    return FlowResult.DONE
                actualizada = {
                    **actual,
                    **clase,
                    "nombreActividad": actual.get("nombreActividad") or inscripcion["nombreActividad"],
                }
                if any(
                    actualizada.get(campo) != inscripcion.get(campo)
                    for campo in ("nombreActividad", "fecha", "horaInicio", "horaFin", "costo")
                ):
                    estado.selected_inscription = actualizada
                    await self._pedir_baja(
                        phone, session, estado, "La clase cambió. Revisá los datos antes de confirmar."
                    )
                    return FlowResult.CONTINUE
                await svc.services_client.dar_baja_instancia(str(instancia_id), str(session.socio["id"]))
            else:
                if actual.get("instanciaId"):
                    session.finish(NO_DISPONIBLE, "unavailable")
                    return FlowResult.DONE
                if any(
                    actual.get(campo) != inscripcion.get(campo)
                    for campo in ("nombreActividad", "fechaInicio", "fechaFin", "costo")
                ):
                    estado.selected_inscription = {
                        **actual,
                        "nombreActividad": actual.get("nombreActividad") or inscripcion["nombreActividad"],
                    }
                    await self._pedir_baja(
                        phone, session, estado, "La inscripción cambió. Revisá los datos antes de confirmar."
                    )
                    return FlowResult.CONTINUE
                await svc.services_client.delete_inscripcion(str(inscripcion["id"]))
        except (ServicesAPINoEncontrado, ServicesAPIBadRequest, ServicesAPIConflict):
            session.finish(NO_DISPONIBLE, "unavailable")
            return FlowResult.DONE
        except ServicesAPIError:
            logger.warning("No se pudo confirmar la baja de la inscripción")
            session.finish(GENERIC_ERROR, "error")
            return FlowResult.DONE
        mensaje = (
            "Te diste de baja solo de esa clase. Tus otras inscripciones siguen activas."
            if instancia_id
            else "Inscripción general cancelada."
        )
        session.finish(mensaje)
        return FlowResult.DONE
