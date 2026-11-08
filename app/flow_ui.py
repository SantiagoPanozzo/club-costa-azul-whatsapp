"""Bounded message composition and pagination shared by the guided flows."""

from .config import settings
from .state import Session

MENU_ID = "global_menu"
TEXT_BODY_LIMIT = 4096
BUTTON_BODY_LIMIT = 1024


def list_body_limit() -> int:
    # v20's interactive body contract is conservative; opt into a larger limit
    # only after verifying support in the configured Graph API version.
    return settings.whatsapp_list_body_limit


def confirmation_buttons(
    confirm_id: str, back_id: str, label: str = "Confirmar", *, session: Session
) -> list[tuple[str, str]]:
    # A button authorizes only the selection/terms rendered with this nonce.
    return [(session.issue_confirmation(confirm_id), label), (back_id, "Volver"), (MENU_ID, "Menú principal")]


def with_notice(body: str, notice: str = "") -> str:
    return f"{notice}\n\n{body}" if notice else body


def adaptive_summary(heading: str, entries: list[str], prompt: str, *, empty: str, limit: int | None = None) -> str:
    """Entries must be newest-enrollment first, not ordered by class date."""
    limit = list_body_limit() if limit is None else limit
    if not entries:
        return f"{empty}\n\n{prompt}"
    full = f"{heading}\n\n" + "\n".join(entries) + f"\n\n{prompt}"
    if len(full) <= limit:
        return full
    remaining = f"\nTenés otras {len(entries) - 1} inscripciones." if len(entries) > 1 else ""
    latest = f"{heading}\n\n{entries[0]}{remaining}\n\n{prompt}"
    if len(latest) <= limit:
        return latest
    return f"Tenés {len(entries)} inscripciones. Consultá Mis inscripciones para ver el detalle.\n\n{prompt}"


def paginate_rows(
    options: list[dict], page: int, *, page_prefix: str, extra_rows: list[dict] | None = None
) -> tuple[list[dict], int]:
    """Reserve both page controls plus persistent navigation, never hide data."""
    extras = extra_rows or []
    size = 8 - len(extras)
    if size < 1:
        raise ValueError("Too many navigation rows")
    page = max(0, min(page, max(0, (len(options) - 1) // size)))
    rows = [dict(row) for row in options[page * size : (page + 1) * size]]
    rows.extend(dict(row) for row in extras)
    if page:
        rows.append({"id": f"{page_prefix}{page - 1}", "title": "Anteriores"})
    if (page + 1) * size < len(options):
        rows.append({"id": f"{page_prefix}{page + 1}", "title": "Más opciones"})
    return rows, page


def split_text(body: str, limit: int = TEXT_BODY_LIMIT) -> list[str]:
    """Lossless splitting: do not truncate confirmation data or descriptions."""
    if limit < 1:
        raise ValueError("A positive body limit is required")
    if not body:
        return [body]
    chunks = []
    while len(body) > limit:
        boundary = body.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1
        chunks.append(body[:boundary])
        body = body[boundary:]
    if body:
        chunks.append(body)
    return chunks
