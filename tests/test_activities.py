from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.flows import activities as flujo
from app.flows.base import FlowResult
from app.services_client import (
    ServicesAPIBadRequest,
    ServicesAPIConflict,
    ServicesAPICupoAgotado,
    ServicesAPIError,
    ServicesAPINoEncontrado,
)
from app.state import Session
from app.webhook_parser import IncomingMessage

TELEFONO = "numero-sintetico-sin-envios"
ACTIVIDAD = {
    "id": "a1",
    "nombre": "Yoga inicial",
    "estado": "Activa",
    "cupoDisponible": 5,
    "costo": 100,
    "diasSemana": [1, 3],
    "horaInicio": "09:00:00",
    "horaFin": "10:00:00",
}
CLASE = {
    "id": "c1",
    "actividadId": "a1",
    "fecha": "2099-05-11",
    "horaInicio": "09:00:00",
    "horaFin": "10:00:00",
    "estado": "Programada",
    "liquidada": False,
    "cupoDisponible": 2,
    "costo": 100,
}


def inscripcion(id_="i1", clase=None):
    clase = clase or CLASE
    detalle = {
        "instanciaId": clase["id"],
        "fecha": clase["fecha"],
        "horaInicio": clase["horaInicio"],
        "horaFin": clase["horaFin"],
        "estadoInstancia": clase["estado"],
        "estadoClase": "Inscripto",
    }
    return {
        "id": id_,
        "socioId": "s1",
        "actividadId": "a1",
        "nombreActividad": "Yoga inicial",
        "estado": "Activa",
        "fechaFin": clase["fecha"],
        **detalle,
        "clases": [detalle],
    }


@pytest.fixture
def entorno(monkeypatch):
    api = SimpleNamespace(
        get_actividades=AsyncMock(return_value=[deepcopy(ACTIVIDAD)]),
        get_inscripciones_socio=AsyncMock(return_value=[]),
        get_instancias_actividad=AsyncMock(return_value=[deepcopy(CLASE)]),
        post_inscripcion=AsyncMock(return_value={"mensaje": "Inscripción exitosa"}),
        get_inscripcion=AsyncMock(return_value=inscripcion()),
        dar_baja_instancia=AsyncMock(return_value={"mensaje": "Baja registrada"}),
        delete_inscripcion=AsyncMock(return_value={"mensaje": "Cancelada"}),
    )
    mensajes = SimpleNamespace(send_text=AsyncMock(), send_buttons=AsyncMock(), send_list=AsyncMock())
    monkeypatch.setattr(flujo.svc, "services_client", api)
    monkeypatch.setattr(flujo, "whatsapp_client", mensajes)
    return flujo.ActivitiesFlow(), Session(socio={"id": "s1"}, active_flow="activities"), api, mensajes


async def responder(f, sesion, id_):
    if id_ == sesion.confirmation_action and sesion.confirmation_id:
        id_ = sesion.confirmation_id
    return await f.handle(TELEFONO, sesion, IncomingMessage(phone=TELEFONO, type="interactive", interactive_id=id_))


async def elegir_clase(entorno):
    f, sesion, _, _ = entorno
    await f.enter(TELEFONO, sesion)
    await responder(f, sesion, "act_a1")


async def elegir_baja(entorno, identificador="cancel_clase_c1"):
    f, sesion, api, _ = entorno
    api.get_inscripciones_socio.return_value = [inscripcion()]
    await f.enter(TELEFONO, sesion)
    await responder(f, sesion, flujo.ACTION_CANCEL)
    await responder(f, sesion, identificador)


async def test_seleccion_de_instancia_y_post_con_vinculo(entorno):
    f, sesion, api, mensajes = entorno
    await elegir_clase(entorno)
    assert sesion.flow_state.instancia_elegida["id"] == "c1"
    cuerpo = mensajes.send_buttons.call_args.kwargs["body"]
    assert "11/05/2099" in cuerpo and "09:00 - 10:00" in cuerpo
    assert await responder(f, sesion, flujo.CONFIRM_YES) == FlowResult.DONE
    api.post_inscripcion.assert_awaited_once_with("s1", "a1", instancia_id="c1")
    assert api.get_instancias_actividad.await_count == 2  # selección y confirmación


async def test_otro_horario_misma_actividad_no_excluye_actividad(entorno):
    f, sesion, api, mensajes = entorno
    otra = {**CLASE, "id": "c2", "horaInicio": "18:00:00", "horaFin": "19:00:00"}
    api.get_inscripciones_socio.return_value = [inscripcion()]
    api.get_actividades.return_value = [{**ACTIVIDAD, "cupoDisponible": 0}]
    api.get_instancias_actividad.return_value = [CLASE, otra]
    await f.enter(TELEFONO, sesion)
    assert "a1" in sesion.flow_state.available_activities
    await responder(f, sesion, "act_a1")
    assert list(sesion.flow_state.instancias_disponibles) == ["c2"]
    assert sesion.flow_state.instancia_elegida["id"] == "c2"
    assert "18:00 - 19:00" in mensajes.send_buttons.call_args.kwargs["body"]


async def test_fallback_generico_sin_instancias_no_envia_instancia_id(entorno):
    f, sesion, api, _ = entorno
    api.get_instancias_actividad.return_value = []
    await f.enter(TELEFONO, sesion)
    await responder(f, sesion, "act_a1")
    assert sesion.flow_state.step == flujo.ActivitiesStep.AWAITING_CONFIRM
    await responder(f, sesion, flujo.CONFIRM_YES)
    api.post_inscripcion.assert_awaited_once_with("s1", "a1")


async def test_listado_y_baja_de_una_sola_clase(entorno):
    f, sesion, api, mensajes = entorno
    otra = {**CLASE, "id": "c2", "horaInicio": "18:00:00", "horaFin": "19:00:00"}
    api.get_inscripciones_socio.return_value = [inscripcion(), inscripcion("i2", otra)]
    await f.enter(TELEFONO, sesion)
    await responder(f, sesion, flujo.ACTION_CANCEL)
    filas = mensajes.send_list.call_args.kwargs["rows"]
    assert len(filas) == 3
    assert "11/05/2099" in filas[0]["description"]
    assert "09:00 - 10:00" in filas[0]["description"]
    assert "18:00 - 19:00" in filas[1]["description"]
    await responder(f, sesion, filas[0]["id"])
    await responder(f, sesion, flujo.CANCEL_CONFIRM_YES)
    api.get_inscripcion.assert_awaited_once_with("i1")
    api.dar_baja_instancia.assert_awaited_once_with("c1", "s1")
    api.delete_inscripcion.assert_not_awaited()


async def test_generica_con_clases_permite_baja_puntual_sin_cancelar_rango(entorno):
    f, sesion, api, mensajes = entorno
    generica = {**inscripcion(), "instanciaId": None, "fechaFin": None}
    api.get_inscripciones_socio.return_value = [generica]
    api.get_inscripcion.return_value = generica
    await f.enter(TELEFONO, sesion)
    await responder(f, sesion, flujo.ACTION_CANCEL)
    assert len(mensajes.send_list.call_args.kwargs["rows"]) == 3
    await responder(f, sesion, "cancel_clase_c1")
    await responder(f, sesion, flujo.CANCEL_CONFIRM_YES)
    api.dar_baja_instancia.assert_awaited_once_with("c1", "s1")
    api.delete_inscripcion.assert_not_awaited()


async def test_cancelacion_generica_conserva_operacion_anterior(entorno):
    f, sesion, api, _ = entorno
    generica = {**inscripcion(), "instanciaId": None, "fechaFin": None, "clases": []}
    api.get_inscripciones_socio.return_value = [generica]
    api.get_inscripcion.return_value = generica
    await f.enter(TELEFONO, sesion)
    await responder(f, sesion, flujo.ACTION_CANCEL)
    await responder(f, sesion, "cancel_general_i1")
    await responder(f, sesion, flujo.CANCEL_CONFIRM_YES)
    api.delete_inscripcion.assert_awaited_once_with("i1")
    api.dar_baja_instancia.assert_not_awaited()


async def test_bajas_se_muestran_pero_no_se_ofrecen_para_cancelar(entorno):
    f, sesion, api, mensajes = entorno
    baja = inscripcion()
    baja["clases"][0]["estadoClase"] = "Baja"
    api.get_inscripciones_socio.return_value = [baja]
    await f.enter(TELEFONO, sesion)
    assert await responder(f, sesion, flujo.ACTION_CANCEL) == FlowResult.CONTINUE
    await responder(f, sesion, "cancel_clase_c1")
    assert "baja registrada" in mensajes.send_buttons.call_args.kwargs["body"]
    assert flujo.CANCEL_CONFIRM_YES not in dict(mensajes.send_buttons.call_args.kwargs["buttons"])
    await responder(f, sesion, flujo.CANCEL_CONFIRM_YES)
    api.dar_baja_instancia.assert_not_awaited()
    api.delete_inscripcion.assert_not_awaited()


@pytest.mark.parametrize("cambios", [{"cupoDisponible": 0}, {"estado": "Suspendida"}, {"liquidada": True}])
async def test_clases_sin_disponibilidad_no_eluden_cupo_con_generica(entorno, cambios):
    f, sesion, api, _ = entorno
    api.get_instancias_actividad.return_value = [{**CLASE, **cambios}]
    await f.enter(TELEFONO, sesion)
    resultado = await responder(f, sesion, "act_a1")
    # Si todas están suspendidas no hay clases programadas: se conserva el fallback.
    assert resultado == (FlowResult.CONTINUE if cambios.get("estado") == "Suspendida" else FlowResult.DONE)
    api.post_inscripcion.assert_not_awaited()


async def test_clase_llena_antes_de_confirmar_no_hace_post(entorno):
    f, sesion, api, mensajes = entorno
    await elegir_clase(entorno)
    api.get_instancias_actividad.return_value = [{**CLASE, "cupoDisponible": 0}]
    await responder(f, sesion, flujo.CONFIRM_YES)
    api.post_inscripcion.assert_not_awaited()
    assert sesion.completion.body == flujo.SIN_CUPO


@pytest.mark.parametrize(
    "error,esperado",
    [
        (ServicesAPICupoAgotado("SQL interno"), flujo.SIN_CUPO),
        (ServicesAPIConflict("SQL interno"), flujo.NO_DISPONIBLE),
        (ServicesAPINoEncontrado("SQL interno"), flujo.NO_DISPONIBLE),
        (ServicesAPIError("SQL interno"), flujo.GENERIC_ERROR),
    ],
)
async def test_errores_de_alta_no_filtran_detalles(entorno, error, esperado):
    f, sesion, api, mensajes = entorno
    await elegir_clase(entorno)
    api.post_inscripcion.side_effect = error
    await responder(f, sesion, flujo.CONFIRM_YES)
    assert sesion.completion.body == esperado
    mensajes.send_text.assert_not_awaited()


async def test_cambio_de_horario_exige_nueva_confirmacion(entorno):
    f, sesion, api, mensajes = entorno
    await elegir_clase(entorno)
    api.get_instancias_actividad.return_value = [{**CLASE, "horaInicio": "08:00:00"}]
    assert await responder(f, sesion, flujo.CONFIRM_YES) == FlowResult.CONTINUE
    api.post_inscripcion.assert_not_awaited()
    assert "08:00 - 10:00" in mensajes.send_buttons.call_args.kwargs["body"]


@pytest.mark.parametrize("error", [ServicesAPINoEncontrado("interno"), ServicesAPIBadRequest("interno")])
async def test_baja_dejo_de_estar_disponible_no_cancela_fila(entorno, error):
    f, sesion, api, mensajes = entorno
    await elegir_baja(entorno)
    api.get_inscripcion.side_effect = error
    await responder(f, sesion, flujo.CANCEL_CONFIRM_YES)
    api.dar_baja_instancia.assert_not_awaited()
    api.delete_inscripcion.assert_not_awaited()
    assert sesion.completion.body == flujo.NO_DISPONIBLE


async def test_baja_rechazada_por_presencia_no_cancela_fila(entorno):
    f, sesion, api, mensajes = entorno
    await elegir_baja(entorno)
    api.dar_baja_instancia.side_effect = ServicesAPIBadRequest("Ya presente: detalle interno")
    await responder(f, sesion, flujo.CANCEL_CONFIRM_YES)
    api.delete_inscripcion.assert_not_awaited()
    assert sesion.completion.body == flujo.NO_DISPONIBLE


async def test_sesion_perdida_no_usa_confirmacion_vieja(entorno):
    f, _, api, mensajes = entorno
    nueva = Session(socio={"id": "s1"}, active_flow="activities")
    assert await responder(f, nueva, flujo.CONFIRM_YES) == FlowResult.DONE
    api.post_inscripcion.assert_not_awaited()
    api.dar_baja_instancia.assert_not_awaited()
    assert nueva.completion.body == flujo.SESION_VENCIDA
    mensajes.send_text.assert_not_awaited()


async def test_paginacion_y_limites_whatsapp(entorno):
    f, sesion, api, mensajes = entorno
    api.get_instancias_actividad.return_value = [
        {**CLASE, "id": f"c{i}", "fecha": f"2099-06-{i + 1:02d}"} for i in range(25)
    ]
    await f.enter(TELEFONO, sesion)
    await responder(f, sesion, "act_a1")
    await responder(f, sesion, f"{flujo.PAGINA_PREFIX}clases:1")
    for llamada in mensajes.send_list.call_args_list:
        contenido = llamada.kwargs
        assert len(contenido["rows"]) <= 10
        assert len(contenido["section_title"]) <= 24
        assert len(contenido["button_text"]) <= 20
        for fila in contenido["rows"]:
            assert len(fila["title"]) <= 24
            assert len(fila["description"]) <= 72
    assert mensajes.send_list.call_args.kwargs["rows"][0]["id"] == "clase_c7"


async def test_cancelada_o_vencida_solo_lectura(entorno):
    f, sesion, api, mensajes = entorno
    cancelada = {**inscripcion(), "estado": "Cancelada"}
    pasada = inscripcion("i2", {**CLASE, "id": "c2", "fecha": "2020-01-01"})
    api.get_inscripciones_socio.return_value = [cancelada, pasada]
    await f.enter(TELEFONO, sesion)
    assert await responder(f, sesion, flujo.ACTION_CANCEL) == FlowResult.CONTINUE
    for identificador in ("cancel_clase_c1", "cancel_clase_c2"):
        await responder(f, sesion, identificador)
        assert flujo.CANCEL_CONFIRM_YES not in dict(mensajes.send_buttons.call_args.kwargs["buttons"])
        await responder(f, sesion, flujo.CANCEL_CONFIRM_YES)
        await responder(f, sesion, flujo.CANCEL_CONFIRM_NO)
    api.dar_baja_instancia.assert_not_awaited()
    api.delete_inscripcion.assert_not_awaited()
