"""Client for the Club Costa Azul internal services API."""
import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class ServicesAPIError(Exception):
    """Raised when the services API can't be reached or returns an unexpected error."""


class ServicesClient:
    def __init__(self):
        self._client = httpx.AsyncClient(base_url=settings.services_api_base_url, timeout=10.0)

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
            resp = await self._client.get(f"/inscripciones/socio/{socio_id}")
        except httpx.HTTPError as exc:
            logger.error("Network error fetching inscripciones for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc

        if resp.status_code == 404:
            return []
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Services API error fetching inscripciones for %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def post_inscripcion(self, socio_id: str, actividad_id: str) -> dict:
        try:
            resp = await self._client.post(
                "/inscripciones",
                json={"socioId": socio_id, "actividadId": actividad_id},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error creating inscripcion (socio=%s, actividad=%s): %s", socio_id, actividad_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()


services_client = ServicesClient()
