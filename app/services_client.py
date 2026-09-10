"""Client for the Club Costa Azul internal services API."""

import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class ServicesAPIError(Exception):
    """Raised when the services API can't be reached or returns an unexpected error."""


class ServicesAPIConflict(ServicesAPIError):
    """Raised on 409 Conflict — the API rejected the operation with a user-facing message."""


class ServicesClient:
    def __init__(self):
        self._client = httpx.AsyncClient(base_url=settings.services_api_base_url, timeout=10.0)

    def _auth_headers(self) -> dict[str, str]:
        if settings.bot_api_key:
            return {"X-Api-Key": settings.bot_api_key}
        return {}

    async def aclose(self):
        await self._client.aclose()

    async def get_socio_by_whatsapp(self, number: str) -> dict | None:
        """Returns the socio dict, or None if no socio is registered with that number."""
        try:
            print(f"Fetching from {settings.services_api_base_url}/socios/by-whatsapp/{number}")
            resp = await self._client.get(f"/socios/by-whatsapp/{number}")
        except httpx.HTTPError as exc:
            logger.error("Network error fetching socio for %s: %s", number, exc)
            raise ServicesAPIError(str(exc)) from exc

        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Services API error fetching socio for %s: %s", number, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_actividades(self) -> list[dict]:
        try:
            resp = await self._client.get("/actividades")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching actividades: %s", exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_inscripciones_socio(self, socio_id: str) -> list[dict]:
        try:
            resp = await self._client.get(
                f"/socios/{socio_id}/inscripciones",
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            logger.error("Network error fetching inscripciones for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Services API error fetching inscripciones for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_cuotas_socio(self, socio_id: str) -> list[dict]:
        try:
            resp = await self._client.get(
                f"/socios/{socio_id}/cuotas",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching cuotas for socio %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def post_inscripcion(self, socio_id: str, actividad_id: str) -> dict:
        try:
            resp = await self._client.post(
                "/inscripciones",
                json={"socioId": socio_id, "actividadId": actividad_id},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error(
                "Error creating inscripcion (socio=%s, actividad=%s): %s",
                socio_id,
                actividad_id,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def delete_inscripcion(self, inscripcion_id: str) -> dict:
        try:
            resp = await self._client.delete(
                f"/inscripciones/{inscripcion_id}",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error cancelling inscripcion %s: %s", inscripcion_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_socio_detalle(self, socio_id: str) -> dict | None:
        try:
            resp = await self._client.get(f"/socios/{socio_id}", headers=self._auth_headers())
        except httpx.HTTPError as exc:
            logger.error("Error fetching socio detail for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc

        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Services API error fetching socio detail for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_espacios(self) -> list[dict]:
        try:
            resp = await self._client.get("/espacios/", headers=self._auth_headers())
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching espacios: %s", exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_disponibilidad(self, fecha: str) -> list[dict]:
        try:
            resp = await self._client.get(
                "/espacios/disponibilidad", params={"fecha": fecha}, headers=self._auth_headers()
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching disponibilidad for %s: %s", fecha, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_reservas_socio(self, socio_id: str) -> list[dict]:
        try:
            resp = await self._client.get(f"/socios/{socio_id}/reservas", headers=self._auth_headers())
        except httpx.HTTPError as exc:
            logger.error("Error fetching reservas for socio %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc

        if resp.status_code == 404:
            return []
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Services API error fetching reservas for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def post_reserva(
        self,
        socio_id: str,
        espacio_id: str,
        fecha: str,
        hora_inicio: str,
        hora_fin: str,
        cant_personas: int,
        motivo: str,
    ) -> dict:
        try:
            resp = await self._client.post(
                "/reservas",
                json={
                    "socioId": socio_id,
                    "espacioId": espacio_id,
                    "fecha": fecha,
                    "horaInicio": hora_inicio,
                    "horaFin": hora_fin,
                    "cantPersonas": cant_personas,
                    "motivo": motivo,
                },
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            logger.error(
                "Error creating reserva (socio=%s, espacio=%s, fecha=%s): %s",
                socio_id,
                espacio_id,
                fecha,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc

        if resp.status_code == 409:
            body = resp.json()
            raise ServicesAPIConflict(body.get("mensaje", "El espacio ya está reservado para esa fecha."))

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Services API error creating reserva (socio=%s, espacio=%s, fecha=%s): %s",
                socio_id,
                espacio_id,
                fecha,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def delete_reserva(self, reserva_id: str, socio_id: str) -> dict:
        try:
            resp = await self._client.delete(
                f"/reservas/{reserva_id}",
                params={"socioId": socio_id},
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error cancelling reserva %s: %s", reserva_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_noticias(self) -> list[dict]:
        try:
            resp = await self._client.get("/noticias", params={"soloPublicadas": "true"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching noticias: %s", exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_eventos(self) -> list[dict]:
        try:
            resp = await self._client.get("/eventos/")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching eventos: %s", exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_inscripciones_evento_socio(self, socio_id: str) -> list[dict]:
        try:
            resp = await self._client.get(f"/socios/{socio_id}/evento-inscripciones", headers=self._auth_headers())
        except httpx.HTTPError as exc:
            logger.error("Error fetching evento inscriptions for socio %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc

        if resp.status_code == 404:
            return []
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Services API error fetching evento inscriptions for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def post_inscripcion_evento(self, evento_id: str, socio_id: str) -> dict:
        try:
            resp = await self._client.post(
                f"/eventos/{evento_id}/inscripciones",
                json={"socioId": socio_id},
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            logger.error(
                "Error creating evento inscription (socio=%s, evento=%s): %s",
                socio_id,
                evento_id,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc

        if resp.status_code == 409:
            body = resp.json()
            raise ServicesAPIConflict(body.get("mensaje", "Ya estás inscripto/a en este evento."))

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Services API error creating evento inscription (socio=%s, evento=%s): %s",
                socio_id,
                evento_id,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def delete_inscripcion_evento(self, evento_id: str, inscripcion_id: str) -> dict:
        try:
            resp = await self._client.delete(
                f"/eventos/{evento_id}/inscripciones/{inscripcion_id}",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error(
                "Error withdrawing from evento %s inscription %s: %s",
                evento_id,
                inscripcion_id,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()


services_client = ServicesClient()
