"""Real router + storing wrapper + payload formatting, with all I/O mocked."""

from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import conversation, message_store, services_client
from app.state import SessionStore
from app.webhook_parser import IncomingMessage
from app.whatsapp_client import whatsapp_client

PHONE = "synthetic-transcript-no-real-recipient"
ACTIVITY = {
    "id": "a1",
    "nombre": "Yoga",
    "estado": "Activa",
    "cupoDisponible": 5,
    "costo": 100,
    "diasSemana": [1],
    "horaInicio": "09:00:00",
    "horaFin": "10:00:00",
}
CLASS = {
    "id": "c1",
    "actividadId": "a1",
    "fecha": "2099-05-11",
    "horaInicio": "09:00:00",
    "horaFin": "10:00:00",
    "estado": "Programada",
    "liquidada": False,
    "cupoDisponible": 5,
    "costo": 100,
}
SPACE = {"id": "s1", "nombre": "Quincho", "estado": "Disponible", "capacidad": 30, "costo": 100}
EVENT = {
    "id": "e1",
    "nombre": "Encuentro",
    "estado": "Programado",
    "fecha": "2099-05-11",
    "horaInicio": "09:00:00",
    "horaFin": "10:00:00",
    "nombreEspacio": "Quincho",
    "cupoDisponible": 10,
    "costoSocio": 100,
}
QUOTA = {"id": "q1", "periodo": "2026-11", "estado": "Atrasada", "monto": 100, "fechaVencimiento": "2026-11-01"}


class Transcript:
    def __init__(self, monkeypatch):
        self.payloads = []
        self.turns = []
        self.store = SessionStore()
        self.session = self.store.get(PHONE)
        self.session.socio = {"id": "s1", "nombre": "Prueba"}
        future = (date.today() + timedelta(days=2)).isoformat()
        values = {
            "get_socio_by_whatsapp": self.session.socio,
            "get_actividades": [ACTIVITY],
            "get_instancias_actividad": [CLASS],
            "get_inscripciones_socio": [],
            "post_inscripcion": {},
            "get_inscripcion": {},
            "dar_baja_instancia": {},
            "delete_inscripcion": {},
            "get_espacios": [SPACE],
            "get_reservas_socio": [{"id": "r1", "nombreEspacio": "Quincho", "fecha": future, "estado": "Pendiente"}],
            "post_reserva": {},
            "delete_reserva": {},
            "get_eventos": [EVENT],
            "get_inscripciones_evento_socio": [],
            "post_inscripcion_evento": {},
            "delete_inscripcion_evento": {},
            "get_cuotas_socio": [QUOTA],
            "upload_comprobante": {},
            "get_socio_detalle": {"id": "s1", "nombre": "Prueba"},
            "get_noticias": [{"titulo": "Novedad", "fecha": future, "descripcion": "Comunicado de prueba"}],
        }
        self.api = SimpleNamespace(**{k: AsyncMock(return_value=deepcopy(v)) for k, v in values.items()})
        monkeypatch.setattr(services_client, "services_client", self.api)
        monkeypatch.setattr(conversation, "sessions", self.store)
        monkeypatch.setattr(message_store, "store_incoming", AsyncMock(return_value=True))
        self.outgoing = AsyncMock()
        self.runs = AsyncMock()
        monkeypatch.setattr(message_store, "store_outgoing", self.outgoing)
        monkeypatch.setattr(message_store, "save_run", self.runs)
        monkeypatch.setattr(whatsapp_client, "_send", AsyncMock(side_effect=self._capture))
        monkeypatch.setattr(
            whatsapp_client, "download_media", AsyncMock(return_value=(b"synthetic-receipt", "application/pdf"))
        )

    async def _capture(self, payload):
        self.payloads.append(deepcopy(payload))
        return f"synthetic-wamid-{len(self.payloads)}"

    async def send(self, interactive_id=None, *, text=None, raw=False, **kwargs):
        # A logical action simulates tapping that button on the CURRENT card.
        # Use send_raw for forged/replayed wire IDs; never remap a supplied nonce.
        if not raw and self.payloads:
            interactive = self.payloads[-1].get("interactive", {})
            for button in interactive.get("action", {}).get("buttons", []):
                reply_id = button["reply"]["id"]
                if reply_id.split(":", 1)[0] == interactive_id:
                    interactive_id = reply_id
                    break
        before = len(self.payloads)
        incoming = IncomingMessage(
            phone=PHONE,
            type=kwargs.pop("type", "text" if text is not None else "interactive"),
            text=text,
            interactive_id=interactive_id,
            wamid=f"synthetic-in-{len(self.turns)}",
            **kwargs,
        )
        await conversation.handle_message(incoming)
        self.turns.append(self.payloads[before:])
        return self.payloads[-1] if len(self.payloads) > before else None

    async def send_raw(self, interactive_id, **kwargs):
        return await self.send(interactive_id, raw=True, **kwargs)

    @property
    def bodies(self):
        return [p["text"]["body"] if p["type"] == "text" else p["interactive"]["body"]["text"] for p in self.payloads]

    def assert_single_responses(self, total):
        assert len(self.payloads) == total
        assert [len(turn) for turn in self.turns] == [1] * total
