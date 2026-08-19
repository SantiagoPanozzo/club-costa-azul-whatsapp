"""Client for the Club Costa Azul internal services API."""

import logging
from datetime import datetime

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class ServicesAPIError(Exception):
    """Raised when the services API can't be reached or returns an unexpected error."""


class ConflictError(ServicesAPIError):
    """Raised on 409 Conflict, carrying the server's message."""

    def __init__(self, message: str):
        super().__init__(message)
        self.detail = message


class ServicesClient:
    def __init__(self):
        self._client = httpx.AsyncClient(base_url=settings.services_api_base_url, timeout=10.0)
        self._token: str | None = None
        self._token_expiry: datetime | None = None

    async def aclose(self):
        await self._client.aclose()

    async def _ensure_auth_token(self) -> str:
        if not settings.bot_service_ci or not settings.bot_service_password:
            raise ServicesAPIError("Credenciales de servicio no configuradas. Función no disponible.")

        if self._token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._token

        try:
            resp = await self._client.post(
                "/auth/login",
                json={"ci": settings.bot_service_ci, "password": settings.bot_service_password},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error authenticating service account: %s", exc)
            self._token = None
            self._token_expiry = None
            raise ServicesAPIError(f"Error de autenticación del bot: {exc}") from exc

        data = resp.json()
        self._token = data["token"]
        self._token_expiry = datetime.fromisoformat(data["expira"].replace("Z", "+00:00")).replace(tzinfo=None)
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        assert self._token is not None
        return {"Authorization": f"Bearer {self._token}"}

    async def get_socio_by_whatsapp(self, number: str) -> dict | None:
        """Returns the socio dict, or None if no socio is registered with that number."""
        try:
            logger.debug("Fetching from %s/socios/by-whatsapp/%s", settings.services_api_base_url, number)
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
            resp = await self._client.get(f"/socios/{socio_id}/inscripciones")
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
        except httpx.HTTPError as exc:
            logger.error(
                "Error creating inscripcion (socio=%s, actividad=%s): %s",
                socio_id,
                actividad_id,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc
        if resp.status_code == 409:
            detail = resp.json().get("mensaje", "Ya estás inscripto/a o no hay cupo.")
            raise ConflictError(detail)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Error creating inscripcion (socio=%s, actividad=%s): %s",
                socio_id,
                actividad_id,
                exc,
            )
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_instancias_actividad(self, actividad_id: str) -> list[dict]:
        try:
            resp = await self._client.get(f"/actividades/{actividad_id}/instancias")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching instancias for actividad %s: %s", actividad_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_cuotas_socio(self, socio_id: str) -> list[dict]:
        try:
            resp = await self._client.get(f"/socios/{socio_id}/cuotas")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching cuotas for socio %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_comprobantes_cuota(self, cuota_id: str) -> list[dict]:
        try:
            resp = await self._client.get(f"/cuotas/{cuota_id}/comprobantes")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching comprobantes for cuota %s: %s", cuota_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def post_comprobante_cuota(
        self, socio_id: str, cuota_id: str, file_bytes: bytes, mime_type: str, filename: str
    ) -> dict:
        try:
            resp = await self._client.post(
                f"/socios/{socio_id}/cuotas/{cuota_id}/comprobantes",
                files={"archivo": (filename, file_bytes, mime_type)},
            )
        except httpx.HTTPError as exc:
            logger.error("Error uploading comprobante for socio %s, cuota %s: %s", socio_id, cuota_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        if resp.status_code == 409:
            detail = resp.json().get("mensaje", "Ya existe un comprobante para esta cuota.")
            raise ConflictError(detail)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Error uploading comprobante for socio %s, cuota %s: %s", socio_id, cuota_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()


    # --- Authenticated methods (require service account) ---

    async def get_espacios(self) -> list[dict]:
        await self._ensure_auth_token()
        try:
            resp = await self._client.get("/espacios", headers=self._auth_headers())
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching espacios: %s", exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def get_disponibilidad_espacios(self, fecha: str) -> list[dict]:
        await self._ensure_auth_token()
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
        await self._ensure_auth_token()
        try:
            resp = await self._client.get(
                f"/socios/{socio_id}/reservas", headers=self._auth_headers()
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching reservas for socio %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def post_reserva_admin(self, socio_id: str, espacio_id: str, fecha: str) -> dict:
        await self._ensure_auth_token()
        try:
            resp = await self._client.post(
                "/reservas/admin",
                json={"socioId": socio_id, "espacioId": espacio_id, "fecha": fecha},
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            logger.error("Error creating reserva for socio %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        if resp.status_code == 409:
            detail = resp.json().get("mensaje", "No se pudo crear la reserva.")
            raise ConflictError(detail)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Error creating reserva for socio %s: %s", socio_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        return resp.json()

    async def delete_reserva_admin(self, reserva_id: str) -> dict:
        await self._ensure_auth_token()
        try:
            resp = await self._client.delete(
                f"/reservas/{reserva_id}/admin", headers=self._auth_headers()
            )
        except httpx.HTTPError as exc:
            logger.error("Error deleting reserva %s: %s", reserva_id, exc)
            raise ServicesAPIError(str(exc)) from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = "No se pudo cancelar la reserva."
            if resp.status_code == 400:
                detail = resp.json().get("mensaje", detail)
            logger.error("Error deleting reserva %s: %s", reserva_id, exc)
            raise ServicesAPIError(detail) from exc
        return resp.json()


services_client = ServicesClient()
